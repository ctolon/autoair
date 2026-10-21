"""Citations: every source a tool returns gets a number the answer cites as ``[n]``.

:class:`SourceCollector` wraps agent tools. After a tool ran it records the sources in the
result (search hits, source files, fetched pages, task logs, audit records…) and attaches
a ``cite`` number to each, so the model can reference them and the UI can render the
``[n]`` markers as links. It also records which tools ran (for the audit trail).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel

from autoair.ai.assistants.threads import Citation, CitationKind
from autoair.ai.tools import AgentTool

_HIT_KINDS: dict[str, CitationKind] = {
    "operator": "source",
    "doc": "doc",
    "knowledge": "doc",
    "installed": "source",
    "source": "source",
    "package": "source",
    "example": "source",
    "spec": "spec",
    "template": "spec",
    "web": "web",
    "kb": "doc",
}


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return value


class SourceCollector:
    """Numbered sources of one assistant turn / analysis (shared by its tools)."""

    def __init__(self, max_sources: int = 200) -> None:
        self.citations: list[Citation] = []
        self._by_ref: dict[str, int] = {}
        self.used_tools: list[str] = []
        self.max_sources = max_sources

    def add(self, kind: CitationKind, title: str, ref: str, snippet: str = "") -> int:
        """Record a source; returns its number (the same ref keeps its number)."""
        ref = (ref or title or "?")[:500]
        if ref in self._by_ref:
            return self._by_ref[ref]
        if len(self.citations) >= self.max_sources:
            return 0
        n = len(self.citations) + 1
        self.citations.append(
            Citation(
                n=n,
                kind=kind,
                title=(title or ref)[:200],
                ref=ref,
                snippet=" ".join(snippet.split())[:240],
            )
        )
        self._by_ref[ref] = n
        return n

    def refs(self) -> list[str]:
        return [f"{c.kind}:{c.ref}" for c in self.citations]

    def cited(self, answer: str) -> list[Citation]:
        """The citations an answer refers to (all of them when it cites none)."""
        used = [c for c in self.citations if f"[{c.n}]" in answer]
        return used or self.citations[:20]

    # ----------------------------------------------------------------- annotation

    def annotate(self, tool: str, args: BaseModel, value: Any) -> Any:
        value = _dump(value)
        if tool in ("search_catalog", "search_docs") and isinstance(value, list):
            out = []
            for hit in value:
                if isinstance(hit, dict):
                    kind = _HIT_KINDS.get(str(hit.get("kind")), "doc")
                    ref = str(hit.get("uri") or hit.get("id") or hit.get("title") or "")
                    n = self.add(
                        kind, str(hit.get("title") or ref), ref, str(hit.get("snippet") or "")
                    )
                    hit = {"cite": n, **hit}
                out.append(hit)
            return out
        if tool == "read_source" and isinstance(value, dict):
            ref = str(value.get("path") or value.get("target") or "")
            line = value.get("start_line")
            title = str(value.get("target") or ref)
            if value.get("symbol"):
                title = f"{title}.{value['symbol']}"
            n = self.add("source", title, f"{ref}:{line}" if line else ref)
            return {"cite": n, **value}
        if tool == "fetch_doc" and isinstance(value, dict):
            n = self.add("web", str(value.get("title") or value.get("url")), str(value.get("url")))
            return {"cite": n, **value}
        if tool == "get_operator" and isinstance(value, dict):
            fqn = str(value.get("fqn") or getattr(args, "fqn", ""))
            n = self.add("source", fqn.rsplit(".", 1)[-1], fqn, str(value.get("summary") or ""))
            return {"cite": n, **value}
        if tool == "get_task_log" and isinstance(value, str):
            ref = "log:{}/{}/{}#{}".format(
                getattr(args, "dag_id", "?"),
                getattr(args, "run_id", "?"),
                getattr(args, "task_id", "?"),
                getattr(args, "try_number", 1),
            )
            n = self.add("log", ref[4:], ref)
            return {"cite": n, "log": value}
        if tool == "get_saved_spec" and isinstance(value, dict):
            dag_id = str(value.get("dag_id") or getattr(args, "dag_id", ""))
            n = self.add("spec", dag_id, f"spec:{dag_id}")
            return {"cite": n, "spec": value}
        return value

    # ----------------------------------------------------------------- wrapping

    def wrap(self, tool: AgentTool) -> AgentTool:
        original: Callable[[Any], Any] = tool.handler
        is_async = inspect.iscoroutinefunction(original)
        name = tool.name

        async def handler(args: Any) -> Any:
            self.used_tools.append(name)
            if is_async:
                value = await original(args)
            else:
                value = await asyncio.to_thread(original, args)
            return self.annotate(name, args, value)

        return dataclasses.replace(tool, handler=handler)

    def wrap_all(self, tools: Iterable[AgentTool]) -> list[AgentTool]:
        return [self.wrap(t) for t in tools]


__all__ = ["SourceCollector"]
