"""A small in-memory Elasticsearch for the knowledge-base store tests (no network).

It implements only the REST calls :class:`autoair.kb.stores.elasticsearch.ElasticsearchStore`
makes, and models Elasticsearch's near-real-time visibility: ``_search``, ``_count`` and
``_delete_by_query`` see the documents as of the last ``_refresh``, while ``_mget`` and
``GET _doc`` are real time. Every request is recorded so tests can count bulk requests and
refreshes.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import httpx2


def _get(doc: dict[str, Any], field: str) -> Any:
    return doc.get(field)


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    ((kind, body),) = query.items()
    if kind == "match_all":
        return True
    if kind == "term":
        ((field, value),) = body.items()
        return bool(_get(doc, field) == value)
    if kind == "terms":
        ((field, values),) = body.items()
        return _get(doc, field) in values
    if kind == "prefix":
        ((field, value),) = body.items()
        return str(_get(doc, field) or "").startswith(value)
    if kind == "exists":
        return _get(doc, body["field"]) is not None
    if kind == "bool":
        if any(_matches(doc, q) for q in _as_list(body.get("must_not"))):
            return False
        if not all(_matches(doc, q) for q in _as_list(body.get("filter"))):
            return False
        should = _as_list(body.get("should"))
        return not should or any(_matches(doc, q) for q in should)
    if kind == "match":
        ((field, spec),) = body.items()
        wanted = set(str(spec["query"]).split())
        return bool(wanted & set(str(_get(doc, field) or "").split()))
    raise AssertionError(f"fake elasticsearch: unsupported query {kind}")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class FakeElasticsearch:
    def __init__(self) -> None:
        self.live: dict[str, dict[str, dict[str, Any]]] = {}
        self.visible: dict[str, dict[str, dict[str, Any]]] = {}
        self.requests: list[tuple[str, str]] = []

    # -- helpers for tests ----------------------------------------------------------------

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, p in self.requests if m == method and p.endswith(suffix))

    def reset_requests(self) -> None:
        self.requests.clear()

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    # -- request handling -----------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        method = request.method
        self.requests.append((method, path))
        raw = request.content.decode() if request.content else ""
        parts = [p for p in path.split("/") if p]
        if path == "/_bulk":
            return self._bulk(raw)
        body = json.loads(raw) if raw else {}
        if len(parts) == 1:
            name = parts[0]
            if method == "HEAD":
                return httpx2.Response(200 if name in self.live else 404)
            if method == "PUT":
                if name in self.live:
                    return self._error(400, "resource_already_exists_exception")
                self.live[name], self.visible[name] = {}, {}
                return httpx2.Response(200, json={"acknowledged": True})
            if method == "DELETE":
                if name not in self.live:
                    return self._error(404, "index_not_found_exception")
                del self.live[name], self.visible[name]
                return httpx2.Response(200, json={"acknowledged": True})
        names, op = parts[0].split(","), parts[1]
        for name in names:
            if name not in self.live:
                return self._error(404, "index_not_found_exception")
        index = names[0]
        if op == "_refresh":
            for name in names:
                self.visible[name] = copy.deepcopy(self.live[name])
            return httpx2.Response(200, json={})
        if op == "_mapping":
            return httpx2.Response(200, json={"acknowledged": True})
        if op == "_doc":
            doc_id = parts[2]
            if method == "GET":
                doc = self.live[index].get(doc_id)
                if doc is None:
                    return httpx2.Response(404, json={"found": False})
                return httpx2.Response(200, json={"found": True, "_source": doc})
            if method == "PUT":
                self.live[index][doc_id] = body
            elif self.live[index].pop(doc_id, None) is None:
                return httpx2.Response(404, json={"result": "not_found"})
            if request.url.params.get("refresh") == "true":
                self.visible[index] = copy.deepcopy(self.live[index])
            return httpx2.Response(200, json={"result": "ok"})
        if op == "_mget":
            fields = request.url.params.get("_source")
            docs = []
            for doc_id in body["ids"]:
                doc = self.live[index].get(doc_id)
                if doc is None:
                    docs.append({"_id": doc_id, "found": False})
                else:
                    src = {fields: doc.get(fields)} if fields else doc
                    docs.append({"_id": doc_id, "found": True, "_source": src})
            return httpx2.Response(200, json={"docs": docs})
        if op == "_delete_by_query":
            ids = [i for i, d in self.visible[index].items() if _matches(d, body["query"])]
            deleted = 0
            for doc_id in ids:
                if self.live[index].pop(doc_id, None) is not None:
                    deleted += 1
            if request.url.params.get("refresh") == "true":
                self.visible[index] = copy.deepcopy(self.live[index])
            return httpx2.Response(200, json={"deleted": deleted})
        if op == "_count":
            n = sum(1 for d in self.visible[index].values() if _matches(d, body["query"]))
            return httpx2.Response(200, json={"count": n})
        if op == "_search":
            return self._search(index, body)
        raise AssertionError(f"fake elasticsearch: unsupported {method} {path}")

    @staticmethod
    def _error(status: int, kind: str) -> httpx2.Response:
        return httpx2.Response(status, json={"error": {"type": kind, "reason": kind}})

    def _search(self, index: str, body: dict[str, Any]) -> httpx2.Response:
        docs = list(self.visible[index].items())
        if "aggs" in body:
            aggs: dict[str, Any] = {}
            for name, spec in body["aggs"].items():
                field = spec["terms"]["field"]
                counts = Counter(d.get(field) for _, d in docs)
                buckets = []
                for key, n in counts.items():
                    bucket: dict[str, Any] = {"key": key, "doc_count": n}
                    for sub, sub_spec in spec.get("aggs", {}).items():
                        sub_field = sub_spec["terms"]["field"]
                        sub_counts = Counter(
                            d.get(sub_field) for _, d in docs if d.get(field) == key
                        )
                        bucket[sub] = {
                            "buckets": [{"key": k, "doc_count": c} for k, c in sub_counts.items()]
                        }
                    buckets.append(bucket)
                aggs[name] = {"buckets": buckets}
            return httpx2.Response(200, json={"hits": {"hits": []}, "aggregations": aggs})
        if "knn" in body:
            knn = body["knn"]
            flt = {"bool": {"filter": knn.get("filter", [])}}
            scored = sorted(
                (
                    (
                        sum(
                            a * b for a, b in zip(d["embedding"], knn["query_vector"], strict=True)
                        ),
                        i,
                        d,
                    )
                    for i, d in docs
                    if d.get("embedding") and _matches(d, flt)
                ),
                key=lambda x: -x[0],
            )
            out = [{"_id": i, "_score": score, "_source": d} for score, i, d in scored[: knn["k"]]]
            return httpx2.Response(200, json={"hits": {"hits": out}})
        hits = [(i, d) for i, d in docs if _matches(d, body.get("query", {"match_all": {}}))]
        hits.sort(key=lambda x: str(x[1].get("id")))
        after = body.get("search_after")
        if after is not None:
            hits = [h for h in hits if str(h[1].get("id")) > after[0]]
        fields = body.get("_source")
        out = [
            {
                "_id": i,
                "_score": 1.0,
                "_source": {f: d.get(f) for f in fields} if fields else d,
                "sort": [d.get("id")],
            }
            for i, d in hits[: body.get("size", 10)]
        ]
        return httpx2.Response(200, json={"hits": {"hits": out}})

    def _bulk(self, raw: str) -> httpx2.Response:
        lines = [json.loads(line) for line in raw.splitlines() if line]
        items: list[dict[str, Any]] = []
        errors = False
        i = 0
        while i < len(lines):
            ((op, meta),) = lines[i].items()
            index, doc_id = meta["_index"], meta["_id"]
            self.live.setdefault(index, {})
            self.visible.setdefault(index, {})
            result: dict[str, Any] = {"_index": index, "_id": doc_id, "status": 200}
            if op == "delete":
                i += 1
                if self.live[index].pop(doc_id, None) is None:
                    result.update(status=404, result="not_found")
            else:
                source = lines[i + 1]
                i += 2
                if op == "index":
                    self.live[index][doc_id] = source
                elif doc_id in self.live[index]:
                    self.live[index][doc_id].update(source["doc"])
                else:
                    errors = True
                    result.update(
                        status=404,
                        error={"type": "document_missing_exception", "reason": "missing"},
                    )
            items.append({op: result})
        return httpx2.Response(200, json={"errors": errors, "items": items})


__all__ = ["FakeElasticsearch"]
