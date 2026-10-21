"""Elasticsearch store: BM25 (``match`` over the token stream, titles boosted) plus
``dense_vector`` kNN, fused client-side with Reciprocal Rank Fusion.

AutoAir talks to Elasticsearch's REST API with its own HTTP client (httpx2 through
:mod:`autoair.net.tls`, client ``kb``): the CA bundle, client certificates and proxies of
``outbound.*`` apply, and no extra package is needed. The official ``elasticsearch``
client is not used because its transport (``elastic-transport``: urllib3, or httpx - not
httpx2) would bypass those settings. Fusion happens in AutoAir, so the ``rrf`` retriever
(a paid Elasticsearch feature) is not required; Elasticsearch 8.x and 9.x work.

Indices: ``<prefix>-pages`` (metadata + compressed body, not searchable) and
``<prefix>-chunks``; the vector field is added with the first embeddings (its dimension
follows the model).

Writes are buffered: ``put_page`` and ``touch_page`` queue ``_bulk`` actions that are sent
once ``bulk_size`` documents are pending, and the indices are refreshed only before the next
search-based read (``pages``, ``missing_vectors``, retrieval, ...) or on :meth:`flush`, which
the ingestion run calls at its end. A refresh per page made large ingests slow (every refresh
creates a Lucene segment). Stale chunks of a replaced page are deleted by id inside the same
bulk request: chunk ids are ``<page id>#<n>``, and the stored page knows its previous chunk
count (read with the real-time ``_mget``, which sees unrefreshed writes).
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx2

from autoair.kb.config import KbElasticsearchSettings
from autoair.kb.models import Chunk, PageMeta, PageRecord, StoreStats
from autoair.kb.stores.base import Filters, StoreError, clean_filters, index_terms, normalise
from autoair.net import tls

_PAGE_FIELDS = (
    "id", "source", "unit", "component", "package", "version", "url", "title", "updated",
    "content_hash", "etag", "last_modified", "fetched_at", "chunks",
)  # fmt: skip
_CHUNK_FIELDS = (
    "id", "page_id", "n", "source", "component", "package", "version", "url", "title",
    "heading", "text", "hash", "updated",
)  # fmt: skip
_KEYWORD = {"type": "keyword"}
#: Largest result window read when listing pages (Elasticsearch's default max_result_window).
_SCROLL_PAGE = 1000
#: Page ids per ``terms`` query when deleting pages (well below ``index.max_terms_count``).
_DELETE_BATCH = 500

#: One bulk action: the action line plus, for index/update, its source line.
_Action = tuple[dict[str, Any], ...]


class ElasticsearchStore:
    kind = "elasticsearch"

    def __init__(
        self,
        url: str,
        settings: KbElasticsearchSettings,
        *,
        headers: Mapping[str, str] | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.settings = settings
        self.pages_index = f"{settings.index_prefix}-pages"
        self.chunks_index = f"{settings.index_prefix}-chunks"
        self.meta_index = f"{settings.index_prefix}-meta"
        self._headers = dict(headers or {})
        self._transport = transport
        self._client: httpx2.Client | None = None
        self._lock = threading.RLock()
        # Queued writes in call order: ("page", id, actions, chunk count) / ("touch", id, actions).
        self._pending: list[tuple[str, str, list[_Action], int]] = []
        self._pending_docs = 0
        self._pending_pages: set[str] = set()
        # Bulk writes or deletes since the last refresh: searches would not see them yet.
        self._dirty = False

    @property
    def client(self) -> httpx2.Client:
        if self._client is None:
            options = tls.httpx_kwargs("kb", transport=self._transport)
            self._client = httpx2.Client(
                base_url=self.url,
                timeout=self.settings.timeout_seconds,
                headers={"content-type": "application/json", **self._headers},
                transport=self._transport,
                **options,
            )
        return self._client

    def _req(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        ok: Sequence[int] = (),
        ndjson: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            if ndjson is not None:
                resp = self.client.request(
                    method,
                    path,
                    content=ndjson.encode(),
                    headers={"content-type": "application/x-ndjson"},
                    params=params,
                )
            else:
                resp = self.client.request(
                    method, path, json=body if body is not None else None, params=params
                )
        except httpx2.HTTPError as exc:
            raise StoreError(f"elasticsearch unreachable at {self.url}: {exc}") from exc
        if resp.status_code >= 400 and resp.status_code not in ok:
            try:
                reason = resp.json().get("error", {})
                reason = reason.get("reason") if isinstance(reason, dict) else reason
            except ValueError:
                reason = resp.text[:200]
            raise StoreError(f"elasticsearch {method} {path}: HTTP {resp.status_code} {reason}")
        if resp.status_code in ok and resp.status_code >= 400:
            return None
        return resp.json() if resp.content else None

    def identity(self) -> str:
        return f"elasticsearch:{self.url}:{self.settings.index_prefix}"

    def location(self) -> str:
        return f"{self.url} ({self.settings.index_prefix}-*)"

    def open(self) -> None:
        settings = {
            "number_of_shards": self.settings.shards,
            "number_of_replicas": self.settings.replicas,
            "analysis": {"analyzer": {"kb_terms": {"type": "whitespace"}}},
        }
        indices = {
            self.meta_index: {
                "properties": {"key": _KEYWORD, "value": {"type": "keyword", "index": False}}
            },
            self.pages_index: {
                "properties": {
                    **{f: _KEYWORD for f in _PAGE_FIELDS if f not in ("chunks", "title")},
                    "title": {"type": "text"},
                    "chunks": {"type": "integer"},
                    "body": {"type": "binary"},
                }
            },
            self.chunks_index: {
                "properties": {
                    **{f: _KEYWORD for f in _CHUNK_FIELDS if f not in ("n", "text", "title")},
                    "n": {"type": "integer"},
                    "title": {"type": "text"},
                    "text": {"type": "text"},
                    "terms": {"type": "text", "analyzer": "kb_terms"},
                    "title_terms": {"type": "text", "analyzer": "kb_terms"},
                }
            },
        }
        for name, mappings in indices.items():
            if not self._exists(name):
                # 400 resource_already_exists: created meanwhile by another replica.
                self._req(
                    "PUT", f"/{name}", {"settings": settings, "mappings": mappings}, ok=(400,)
                )

    def _exists(self, name: str) -> bool:
        try:
            resp = self.client.head(f"/{name}")
        except httpx2.HTTPError as exc:
            raise StoreError(f"elasticsearch unreachable at {self.url}: {exc}") from exc
        return resp.status_code == 200

    def close(self) -> None:
        if self._client is not None:
            try:
                # Queued writes are sent (not lost); visibility is the next reader's refresh.
                self._send_pending()
            finally:
                self._client.close()
                self._client = None

    def _refresh(self) -> None:
        self._req("POST", f"/{self.pages_index},{self.chunks_index}/_refresh")
        self._dirty = False

    def _bulk(self, actions: Sequence[_Action]) -> None:
        """Send ``actions`` in requests of at most ``bulk_size`` actions, without refresh."""
        size = self.settings.bulk_size
        for start in range(0, len(actions), size):
            batch = actions[start : start + size]
            payload = "".join(json.dumps(line) + "\n" for action in batch for line in action)
            out = self._req("POST", "/_bulk", ndjson=payload, params={"refresh": "false"})
            self._dirty = True
            if out and out.get("errors"):
                errors = [
                    (op, v)
                    for item in out.get("items", [])
                    for op, v in item.items()
                    if v.get("error") and not self._touch_of_deleted_page(op, v)
                ]
                if errors:
                    op, first = errors[0]
                    error = first.get("error")
                    reason = error.get("reason") if isinstance(error, dict) else error
                    raise StoreError(
                        f"elasticsearch bulk write failed for {len(errors)} of {len(batch)} "
                        f"actions (first: {op} {first.get('_index')}/{first.get('_id')}: "
                        f"{reason})"
                    )

    def _touch_of_deleted_page(self, op: str, item: Mapping[str, Any]) -> bool:
        # touch_page of a page deleted meanwhile: nothing to update.
        return (
            op == "update"
            and item.get("_index") == self.pages_index
            and (item.get("status") == httpx2.codes.NOT_FOUND)
        )

    def flush(self) -> None:
        """Send queued writes and refresh, so searches (on every replica) see them."""
        with self._lock:
            self._send_pending()
            if self._dirty:
                self._refresh()

    def _visible(self) -> None:
        """Before a search-based read: queued and unrefreshed writes become visible."""
        if self._pending or self._dirty:
            self.flush()

    def _send_pending(self) -> None:
        with self._lock:
            if not self._pending:
                return
            pending, self._pending = self._pending, []
            self._pending_docs = 0
            self._pending_pages = set()
            page_ids = [pid for kind, pid, _, _ in pending if kind == "page"]
            previous: dict[str, int] = {}
            if page_ids:
                out = self._req(
                    "POST",
                    f"/{self.pages_index}/_mget",
                    {"ids": page_ids},
                    params={"_source": "chunks"},
                )
                for doc in (out or {}).get("docs", []):
                    if doc.get("found"):
                        previous[doc["_id"]] = int((doc.get("_source") or {}).get("chunks") or 0)
            actions: list[_Action] = []
            for kind, pid, queued, count in pending:
                if kind == "page":
                    actions += [
                        ({"delete": {"_index": self.chunks_index, "_id": f"{pid}#{n}"}},)
                        for n in range(count, previous.get(pid, 0))
                    ]
                actions += queued
            self._bulk(actions)

    def _queue(self, kind: str, pid: str, actions: list[_Action], count: int = 0) -> None:
        with self._lock:
            if kind == "page" and pid in self._pending_pages:
                # The stale-chunk range is computed from the stored page: send the queued
                # version first so a second replacement in one batch deletes the right ids.
                self._send_pending()
            self._pending.append((kind, pid, actions, count))
            if kind == "page":
                self._pending_pages.add(pid)
            self._pending_docs += len(actions)
            if self._pending_docs >= self.settings.bulk_size:
                self._send_pending()

    # -- pages ----------------------------------------------------------------------------

    def _scan(self, index: str, query: dict[str, Any], fields: Sequence[str]) -> Iterator[Any]:
        """Every matching document, sorted by id (search_after paging)."""
        after: list[Any] | None = None
        while True:
            body: dict[str, Any] = {
                "size": _SCROLL_PAGE,
                "query": query,
                "sort": [{"id": "asc"}],
                "_source": list(fields),
            }
            if after is not None:
                body["search_after"] = after
            out = self._req("POST", f"/{index}/_search", body)
            hits = out["hits"]["hits"]
            if not hits:
                return
            for hit in hits:
                yield hit["_source"]
            after = hits[-1]["sort"]

    def pages(self, source: str | None = None) -> dict[str, PageRecord]:
        self._visible()
        query = {"term": {"source": source}} if source else {"match_all": {}}
        return {
            d["id"]: PageRecord.model_validate(d)
            for d in self._scan(self.pages_index, query, _PAGE_FIELDS)
        }

    def put_page(
        self,
        page: PageMeta,
        body: bytes,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]] | None,
        model: str | None,
    ) -> None:
        use_vectors = vectors is not None and model is not None and model == self.vector_model()
        if use_vectors and vectors and chunks:
            self._ensure_vector_field(len(vectors[0]))
        actions: list[_Action] = [
            (
                {"index": {"_index": self.pages_index, "_id": page.id}},
                {
                    **page.model_dump(include=set(_PAGE_FIELDS)),
                    "chunks": len(chunks),
                    "body": base64.b64encode(body).decode(),
                },
            )
        ]
        for i, c in enumerate(chunks):
            doc: dict[str, Any] = {
                **c.model_dump(include=set(_CHUNK_FIELDS)),
                "terms": index_terms(f"{c.heading}\n{c.text}"),
                "title_terms": index_terms(f"{c.title} {c.heading}"),
            }
            if use_vectors and vectors is not None:
                doc["embedding"] = normalise(vectors[i])
            actions.append(({"index": {"_index": self.chunks_index, "_id": c.id}}, doc))
        self._queue("page", page.id, actions, len(chunks))

    def _ensure_vector_field(self, dim: int) -> None:
        if self.get_meta("vector_dim") == str(dim):
            return
        self._req(
            "PUT",
            f"/{self.chunks_index}/_mapping",
            {
                "properties": {
                    "embedding": {
                        "type": "dense_vector",
                        "dims": dim,
                        "index": True,
                        "similarity": "cosine",
                    }
                }
            },
        )
        self.set_meta("vector_dim", str(dim))

    def touch_page(self, page: PageMeta) -> None:
        doc = {
            "etag": page.etag,
            "last_modified": page.last_modified,
            "fetched_at": page.fetched_at,
        }
        self._queue(
            "touch",
            page.id,
            [({"update": {"_index": self.pages_index, "_id": page.id}}, {"doc": doc})],
        )

    def delete_pages(self, ids: Sequence[str]) -> int:
        # delete_by_query only sees refreshed documents.
        self._visible()
        count = 0
        for start in range(0, len(ids), _DELETE_BATCH):
            batch = list(ids[start : start + _DELETE_BATCH])
            self._req(
                "POST",
                f"/{self.chunks_index}/_delete_by_query",
                {"query": {"terms": {"page_id": batch}}},
            )
            out = self._req(
                "POST",
                f"/{self.pages_index}/_delete_by_query",
                {"query": {"terms": {"id": batch}}},
            )
            count += int((out or {}).get("deleted", 0))
        self._dirty = True
        return count

    def clear(self, source: str | None = None) -> None:
        self._visible()
        query = {"term": {"source": source}} if source else {"match_all": {}}
        for index in (self.chunks_index, self.pages_index):
            self._req("POST", f"/{index}/_delete_by_query", {"query": query})
        if not source:
            self._req(
                "POST",
                f"/{self.meta_index}/_delete_by_query",
                {"query": {"prefix": {"key": "unit:"}}},
                params={"refresh": "true"},
            )
        self._refresh()

    def iter_bodies(self, source: str | None = None) -> Iterator[tuple[PageRecord, bytes]]:
        self._visible()
        query = {"term": {"source": source}} if source else {"match_all": {}}
        for doc in self._scan(self.pages_index, query, [*_PAGE_FIELDS, "body"]):
            body = base64.b64decode(doc.pop("body", "") or "")
            yield PageRecord.model_validate(doc), body

    def get_meta(self, key: str) -> str | None:
        out = self._req("GET", f"/{self.meta_index}/_doc/{key}", ok=(404,))
        if not out or not out.get("found"):
            return None
        value = out["_source"].get("value")
        return str(value) if value is not None else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._req(
                "DELETE", f"/{self.meta_index}/_doc/{key}", ok=(404,), params={"refresh": "true"}
            )
        else:
            self._req(
                "PUT",
                f"/{self.meta_index}/_doc/{key}",
                {"key": key, "value": value},
                params={"refresh": "true"},
            )

    # -- vectors --------------------------------------------------------------------------

    def vector_model(self) -> str | None:
        return self.get_meta("vector_model")

    def reset_vectors(self, model: str | None) -> None:
        # A dense_vector mapping cannot change: drop the chunks index and re-create it
        # (the pages keep their bodies, the next refresh re-chunks them).
        self.flush()
        self._req("DELETE", f"/{self.chunks_index}", ok=(404,))
        self.set_meta("vector_dim", None)
        self.set_meta("vector_model", model)
        self.open()
        self.set_meta("rechunk", "1")

    def missing_vectors(self, limit: int) -> list[Chunk]:
        self._visible()
        out = self._req(
            "POST",
            f"/{self.chunks_index}/_search",
            {
                "size": limit,
                "query": {"bool": {"must_not": {"exists": {"field": "embedding"}}}},
                "sort": [{"id": "asc"}],
                "_source": list(_CHUNK_FIELDS),
            },
        )
        return [Chunk.model_validate(h["_source"]) for h in out["hits"]["hits"]]

    def put_vectors(self, items: Mapping[str, Sequence[float]]) -> None:
        if not items:
            return
        self._ensure_vector_field(len(next(iter(items.values()))))
        with self._lock:
            self._send_pending()
            self._bulk(
                [
                    (
                        {"update": {"_index": self.chunks_index, "_id": chunk_id}},
                        {"doc": {"embedding": normalise(vec)}},
                    )
                    for chunk_id, vec in items.items()
                ]
            )

    # -- retrieval ------------------------------------------------------------------------

    @staticmethod
    def _filter(filters: Filters | None) -> list[dict[str, Any]]:
        return [{"terms": {k: v}} for k, v in clean_filters(filters).items()]

    def _hits(self, out: Any) -> list[tuple[Chunk, float]]:
        return [
            (
                Chunk.model_validate({k: h["_source"].get(k) for k in _CHUNK_FIELDS}),
                float(h.get("_score") or 0.0),
            )
            for h in out["hits"]["hits"]
        ]

    def lexical(
        self, tokens: Sequence[str], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]:
        terms = " ".join(t for t in tokens if t.isalnum())
        if not terms:
            return []
        self._visible()
        body = {
            "size": k,
            "_source": list(_CHUNK_FIELDS),
            "query": {
                "bool": {
                    "should": [
                        {"match": {"terms": {"query": terms}}},
                        {"match": {"title_terms": {"query": terms, "boost": 3}}},
                    ],
                    "minimum_should_match": 1,
                    "filter": self._filter(filters),
                }
            },
        }
        return self._hits(self._req("POST", f"/{self.chunks_index}/_search", body))

    def dense(
        self, vector: Sequence[float], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]:
        dim = self.get_meta("vector_dim")
        if dim is None or int(dim) != len(vector):
            return []
        self._visible()
        knn: dict[str, Any] = {
            "field": "embedding",
            "query_vector": normalise(vector),
            "k": k,
            "num_candidates": max(k, self.settings.num_candidates),
        }
        flt = self._filter(filters)
        if flt:
            knn["filter"] = flt
        body = {"size": k, "_source": list(_CHUNK_FIELDS), "knn": knn}
        return self._hits(self._req("POST", f"/{self.chunks_index}/_search", body))

    def stats(self) -> StoreStats:
        self._visible()

        def count(index: str, query: dict[str, Any] | None = None) -> int:
            out = self._req("POST", f"/{index}/_count", {"query": query or {"match_all": {}}})
            return int(out.get("count", 0))

        aggs = self._req(
            "POST",
            f"/{self.pages_index}/_search",
            {
                "size": 0,
                "aggs": {
                    "component": {
                        "terms": {"field": "component", "size": 100},
                        "aggs": {"version": {"terms": {"field": "version", "size": 50}}},
                    },
                    "source": {"terms": {"field": "source", "size": 100}},
                },
            },
        )["aggregations"]
        return StoreStats(
            kind=self.kind,
            location=self.location(),
            pages=count(self.pages_index),
            chunks=count(self.chunks_index),
            vectors=count(self.chunks_index, {"exists": {"field": "embedding"}}),
            embedding_model=self.vector_model(),
            by_component={b["key"]: b["doc_count"] for b in aggs["component"]["buckets"]},
            by_source={b["key"]: b["doc_count"] for b in aggs["source"]["buckets"]},
            versions={
                b["key"]: sorted(v["key"] for v in b["version"]["buckets"])
                for b in aggs["component"]["buckets"]
            },
        )


__all__ = ["ElasticsearchStore"]
