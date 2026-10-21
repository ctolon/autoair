"""Research tools for the agent (and the MCP server).

Air-gapped (always): ``search_docs`` (knowledge base, AutoAir docs, installed provider
docstrings / example DAGs, previously fetched pages), ``read_source``,
``list_installed_packages``, ``get_package_info``.
Online (``ai.research.online``): ``fetch_doc`` (allow-listed sites) and PyPI data in
``get_package_info``.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.ai.config import ai_settings_of
from autoair.ai.rag import Document, Retriever
from autoair.ai.research.local import (
    InstalledIndex,
    allowed_roots,
    installed_packages,
    package_info,
    read_source,
)
from autoair.ai.research.online import OnlineFetcher, ResearchDisabled, docs_url
from autoair.ai.tools import AgentTool

Scope = Literal["installed", "knowledge", "web", "docs"]


class ResearchContext:
    """Per-services research state: installed index, fetcher, web page memory."""

    def __init__(self, services: Any) -> None:
        settings = services.settings
        ai = ai_settings_of(settings)
        self.settings = ai.research
        self.services = services
        custom = [Path(p) for p in getattr(settings, "custom_operator_paths", []) or []]
        self.roots = allowed_roots([*self.settings.extra_source_dirs, *custom])
        self.index = InstalledIndex(
            self.settings.package_patterns, max_modules=self.settings.max_index_modules
        )
        cache_dir = self.settings.cache_dir or Path(settings.state_dir) / "ai-research-cache"
        self.fetcher = OnlineFetcher(self.settings, cache_dir)
        self._web: dict[str, Document] = {}
        self._lock = threading.Lock()

        def installed() -> list[Document]:
            return self.index.documents() if self.settings.index_installed else []

        def knowledge() -> list[Document]:
            from autoair.ai.knowledge import knowledge_for

            r = ai.retrieval
            return knowledge_for(services).documents(
                chunk_size=r.chunk_size, chunk_overlap=r.chunk_overlap
            )

        def web() -> list[Document]:
            with self._lock:
                return list(self._web.values())

        from autoair.ai.rerank import build_reranker

        self.retriever = Retriever(
            [installed, knowledge, web],
            settings=ai.retrieval,
            reranker=build_reranker(
                ai.retrieval, getattr(services, "llm", None), llm_mode=ai.llm_mode
            ),
        )

    def remember_page(self, url: str, title: str, text: str) -> None:
        from autoair.ai.chunking import chunk_markdown

        chunks = chunk_markdown(text)[:40]
        with self._lock:
            # A re-fetch replaces the page: drop chunks of the earlier version first.
            for key in [k for k, d in self._web.items() if d.uri == url]:
                del self._web[key]
            for n, chunk in enumerate(chunks):
                self._web[f"web:{url}#{n}"] = Document(
                    id=f"web:{url}#{n}",
                    kind="web",
                    title=f"{title or url} - {chunk.title}".strip(" -"),
                    text=chunk.text,
                    uri=url,
                    meta={"url": url},
                )
            if len(self._web) > 2000:
                for key in list(self._web)[:500]:
                    del self._web[key]
        self.retriever.invalidate()


def research_for(services: Any) -> ResearchContext:
    """The research state of ``services`` (stored on the services object)."""
    from autoair.ai.corpus import attached

    return attached(services, "_autoair_ai_research", lambda: ResearchContext(services))


# --------------------------------------------------------------------------- tool inputs


class SearchDocsInput(BaseModel):
    query: str = Field(description="What to look up, e.g. 'S3KeySensor deferrable mode'.")
    scopes: list[Scope] | None = Field(
        default=None,
        description="installed (provider docstrings / example DAGs / provider metadata), "
        "knowledge (AutoAir + Airflow best-practice docs), web (pages fetched earlier), docs "
        "(the official Airflow / provider / Astronomer documentation of the knowledge base, "
        "with URL and version for citations).",
    )
    package: str | None = Field(
        default=None, description="Only this distribution, e.g. apache-airflow-providers-amazon."
    )
    component: str | None = Field(
        default=None,
        description="docs scope only: core, task_sdk, providers, helm_chart, airflowctl, "
        "astronomer_airflow, astro_cli, astro, astro_runtime.",
    )
    version: str | None = Field(
        default=None, description="docs scope only: this documentation version (e.g. 3.1.0)."
    )
    limit: int = Field(default=6, ge=1, le=15)


class ReadSourceInput(BaseModel):
    target: str = Field(
        description="Installed module (airflow.providers.http.operators.http), "
        "module.Class, or a file path inside site-packages / the custom operator folders."
    )
    symbol: str | None = Field(
        default=None, description="Class or function (Class.method) to extract."
    )


class PackagesInput(BaseModel):
    pattern: str | None = Field(
        default=None, description="Name filter (substring or fnmatch), e.g. 'amazon'."
    )
    providers_only: bool = False
    limit: int = Field(default=60, ge=1, le=500)


class PackageInput(BaseModel):
    name: str = Field(description="Distribution name, e.g. apache-airflow-providers-google.")
    online: bool = Field(
        default=False, description="Also ask PyPI for the latest release (online mode only)."
    )


class FetchInput(BaseModel):
    url: str = Field(
        description="https URL on an allowed site, or a path under "
        "https://airflow.apache.org/docs/ (e.g. 'apache-airflow-providers-amazon/stable/"
        "operators/s3/s3.html')."
    )
    max_chars: int = Field(default=12_000, ge=500, le=60_000)


SCOPE_KINDS: dict[str, list[str]] = {
    "installed": ["source", "package"],
    "knowledge": ["doc"],
    "web": ["web"],
    # The documentation knowledge base (autoair.kb), searched separately and fused.
    "docs": [],
}


def _caller_llm(services: Any, llm: Any) -> Any:
    """The model research may use on the caller's behalf: theirs, else the central one
    unless ``ai.llm_mode`` is ``per_user`` (then none: extra LLM steps are skipped)."""
    if llm is not None:
        return llm
    if ai_settings_of(services.settings).llm_mode == "per_user":
        return None
    return getattr(services, "llm", None)


async def _kb_hits(services: Any, args: SearchDocsInput, llm: Any) -> list[dict[str, Any]]:
    """Hits of the documentation knowledge base (none when it is off or empty)."""
    from autoair.kb.service import hit_dicts, kb_enabled, kb_for

    if not kb_enabled(services):
        return []
    filters = {"package": args.package, "component": args.component, "version": args.version}
    try:
        result = await kb_for(services).asearch(args.query, k=args.limit, filters=filters, llm=llm)
    except Exception:  # the knowledge base is best effort next to the other scopes
        import logging

        logging.getLogger("autoair.ai.research").warning("docs search failed", exc_info=True)
        return []
    return hit_dicts(result)


def _interleave(
    first: list[dict[str, Any]], second: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Two ranked lists merged by Reciprocal Rank Fusion (rank positions only)."""
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for ranking in (first, second):
        for rank, hit in enumerate(ranking):
            scored.append((-1 / (60 + rank + 1), len(scored), hit))
    return [hit for _, _, hit in sorted(scored)[:limit]]


def research_tools(
    services: Any, *, online: bool | None = None, llm: Any = None
) -> list[AgentTool]:
    """Research tools; ``llm`` is the caller's model (query rewriting / LLM reranking)."""
    ctx = research_for(services)
    online_allowed = ctx.settings.online if online is None else online
    caller_llm = _caller_llm(services, llm)

    async def search_docs(args: SearchDocsInput) -> Any:
        scopes = list(args.scopes or SCOPE_KINDS)
        kinds = sorted({k for s in scopes for k in SCOPE_KINDS[s]})
        filters = {"package": args.package.lower()} if args.package else None
        variants: Sequence[str] = ()
        ai = ai_settings_of(services.settings)
        if ai.retrieval.llm_query_rewrite and caller_llm is not None:
            from autoair.ai.query import rewrite_queries

            variants = (await rewrite_queries(caller_llm, args.query))[1:]
        reranker = None
        if (
            ai.retrieval.reranker == "llm"
            and caller_llm is not None
            and caller_llm is not getattr(services, "llm", None)
        ):
            from autoair.ai.rerank import LLMReranker

            reranker = LLMReranker(caller_llm)
        found: list[dict[str, Any]] = []
        if kinds:
            hits = await ctx.retriever.asearch(
                args.query, args.limit, kinds, filters, variants=variants, reranker=reranker
            )
            found = [h.model_dump(exclude={"score"}) for h in hits]
        if "docs" not in scopes:
            return found
        docs = await _kb_hits(services, args, caller_llm)
        return _interleave(found, docs, args.limit)

    def read_source_tool(args: ReadSourceInput) -> Any:
        return read_source(
            args.target,
            symbol=args.symbol,
            roots=ctx.roots,
            max_chars=ctx.settings.max_source_chars,
        ).model_dump()

    def packages(args: PackagesInput) -> Any:
        return [
            p.model_dump(exclude_none=True)
            for p in installed_packages(
                args.pattern, providers_only=args.providers_only, limit=args.limit
            )
        ]

    async def package(args: PackageInput) -> Any:
        local = package_info(args.name)
        out: dict[str, Any] = {"installed": local.model_dump(exclude_none=True) if local else None}
        if args.online:
            if not online_allowed:
                out["pypi"] = "online research is disabled (air-gapped)"
            else:
                try:
                    out["pypi"] = await ctx.fetcher.pypi(args.name)
                except (LookupError, ValueError, ResearchDisabled) as exc:
                    out["pypi"] = f"unavailable: {exc}"
        if local is None and "pypi" not in out:
            out["note"] = "not installed in this environment"
        return out

    tools = [
        AgentTool(
            "search_docs",
            "Search documentation available offline: the AutoAir / Airflow knowledge base, "
            "docstrings and example DAGs of the INSTALLED Airflow providers (exact for this "
            "environment's versions), the indexed official Airflow / provider / Astronomer "
            "documentation (scope docs: results carry url, version and source - cite them) "
            "and pages fetched earlier. Use before writing code that uses a provider API.",
            SearchDocsInput,
            search_docs,
        ),
        AgentTool(
            "read_source",
            "Read the source of an installed module / class / function (read-only, "
            "site-packages and custom operator folders only). Use it to confirm parameter "
            "names and behaviour.",
            ReadSourceInput,
            read_source_tool,
        ),
        AgentTool(
            "list_installed_packages",
            "Installed Python distributions (name, version), optionally only Airflow "
            "providers. Code you write may only import what is installed.",
            PackagesInput,
            packages,
        ),
        AgentTool(
            "get_package_info",
            "Version, summary and provider metadata (operators, hooks, sensors, connection "
            "types) of an installed distribution; with online=true also PyPI's latest "
            "release.",
            PackageInput,
            package,
        ),
    ]
    if online_allowed:

        async def fetch_doc(args: FetchInput) -> Any:
            result = await ctx.fetcher.fetch(docs_url(args.url))
            if result.status < 400:
                ctx.remember_page(result.final_url, result.title, result.text)
            return {
                "url": result.final_url,
                "status": result.status,
                "title": result.title,
                "text": result.text[: args.max_chars],
                "truncated": result.truncated or len(result.text) > args.max_chars,
                "cached": result.cached,
            }

        tools.append(
            AgentTool(
                "fetch_doc",
                "Fetch a documentation page from an allowed site (airflow.apache.org, "
                "pypi.org, raw.githubusercontent.com by default). Prefer search_docs first; "
                "fetched pages become searchable.",
                FetchInput,
                fetch_doc,
            )
        )
    return tools


__all__ = ["ResearchContext", "research_for", "research_tools"]
