"""The knowledge-base service of an AutoAir installation (``kb_for(services)``).

Ties together the store, the shared state, the embedder, retrieval and refreshes. It
never imports pydantic-ai: without ``autoair[ai]`` (or without ``ai.embedding``) search is
BM25 only, which needs no model at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from autoair.kb.components import COMPONENTS, component_infos
from autoair.kb.config import KbSettings, KbSourceConfig, kb_settings_of
from autoair.kb.fetch import HttpFetcher
from autoair.kb.models import (
    Chunk,
    KbHit,
    KbRunStatus,
    KbSearchResult,
    KbStatus,
    SourceInfo,
    SourceOverride,
    StoreStats,
)
from autoair.kb.sources import UNUSABLE_REASONS, unusable_code, usable
from autoair.kb.state import KbState, next_refresh
from autoair.kb.stores import build_store, kb_base_dir
from autoair.kb.stores.base import Filters, KbStore, StoreError, clean_filters, query_tokens

log = logging.getLogger("autoair.kb")


def _snippet(text: str, tokens: Sequence[str], size: int) -> str:
    lower = text.lower()
    start = 0
    for token in tokens:
        pos = lower.find(token)
        if pos >= 0:
            start = max(0, pos - size // 4)
            break
    chunk = " ".join(text[start : start + size].split())
    return ("…" if start else "") + chunk + ("…" if start + size < len(text) else "")


class KnowledgeBase:
    def __init__(
        self,
        settings: Any,
        *,
        store: KbStore | None = None,
        state_store: Any = None,
        transport: Any = None,
        resolve_dns: bool = True,
        embedder: Any = ...,
        llm: Any = None,
    ) -> None:
        self.settings = settings
        self.kb: KbSettings = kb_settings_of(settings)
        self.store: KbStore = store or build_store(settings)
        self.base_dir = kb_base_dir(settings)
        if state_store is None:
            from autoair.persistence import document_store

            state_dir = getattr(settings, "state_dir", None) or self.base_dir
            url = self.kb.state_url or str(Path(state_dir) / "kb-state")
            state_store = document_store(settings, "kb_state", file_url=url)
        self.state = KbState(state_store, self.store.identity())
        self._transport = transport
        self._resolve_dns = resolve_dns
        self._embedder = embedder
        self._embed_cache: Any = None
        self._llm = llm
        self._opened = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- infrastructure -------------------------------------------------------------------

    def new_fetcher(self) -> HttpFetcher:
        return HttpFetcher(self.kb.fetch, transport=self._transport, resolve_dns=self._resolve_dns)

    def ensure_open(self) -> None:
        if not self._opened:
            with self._lock:
                if not self._opened:
                    self.store.open()
                    self._opened = True

    @property
    def embedder(self) -> Any:
        if self._embedder is ...:
            self._embedder = None
            if self.kb.search.embeddings:
                try:
                    from autoair.ai.llm import build_embedder

                    self._embedder = build_embedder(self.settings)
                except Exception:  # missing extra, provider setup errors
                    log.warning("knowledge base: embeddings unavailable", exc_info=True)
        return self._embedder

    @property
    def embedding_model(self) -> str | None:
        embedder = self.embedder
        return str(embedder.embedding_model) if embedder is not None else None

    @property
    def embed_batch(self) -> int:
        from autoair.ai.config import ai_settings_of

        return ai_settings_of(self.settings).embedding.batch_size

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors of ``texts`` through the embedding cache (only unknown texts are sent)."""
        from autoair.ai.rag import EmbeddingCache

        embedder = self.embedder
        assert embedder is not None
        model = str(embedder.embedding_model)
        if self._embed_cache is None:
            self._embed_cache = EmbeddingCache(self.base_dir / "embeddings.sqlite3")
        cache: EmbeddingCache = self._embed_cache
        keys = [EmbeddingCache.key(t) for t in texts]
        known = cache.get_many(model, keys)
        missing = [i for i, k in enumerate(keys) if k not in known]
        batch = max(1, self.embed_batch)
        fresh: dict[str, list[float]] = {}
        for start in range(0, len(missing), batch):
            idx = missing[start : start + batch]
            vectors = embedder.embed([texts[i] for i in idx])
            for i, vec in zip(idx, vectors, strict=True):
                fresh[keys[i]] = list(vec)
        if fresh:
            cache.put_many(model, fresh)
            known.update(fresh)
        return [known[k] for k in keys]

    # -- sources --------------------------------------------------------------------------

    def effective_sources(self) -> list[tuple[KbSourceConfig, SourceOverride | None]]:
        try:
            overrides = self.state.overrides()
        except (OSError, ValueError):
            overrides = {}
        out = []
        for source in self.kb.sources:
            over = overrides.get(source.id)
            if over is not None:
                update: dict[str, Any] = {}
                if over.enabled is not None:
                    update["enabled"] = over.enabled
                if over.components is not None:
                    update["components"] = [c for c in over.components if c in COMPONENTS]
                if over.versions is not None:
                    update["versions"] = over.versions
                source = source.model_copy(update=update)
            out.append((source, over))
        return out

    def source_scope(self, source: KbSourceConfig) -> tuple[list[str], list[str]]:
        components: list[str] = list(
            source.components if source.components is not None else self.kb.components
        )
        versions = list(source.versions if source.versions is not None else self.kb.versions)
        return components, versions

    def runnable_sources(
        self, only: Sequence[str] | None = None
    ) -> list[tuple[KbSourceConfig, str | None]]:
        out: list[tuple[KbSourceConfig, str | None]] = []
        for source, _ in self.effective_sources():
            if only and source.id not in only:
                continue
            if not source.enabled:
                if only:
                    out.append((source, "disabled"))
                continue
            out.append((source, usable(source, self.kb)))
        return out

    def any_usable(self) -> bool:
        return any(reason is None for _, reason in self.runnable_sources())

    def source_infos(self, stats: StoreStats | None = None) -> list[SourceInfo]:
        infos = []
        for source, over in self.effective_sources():
            components, versions = self.source_scope(source)
            reason = usable(source, self.kb)
            code = unusable_code(source, self.kb) if source.enabled else "disabled"
            infos.append(
                SourceInfo(
                    id=source.id,
                    kind=source.kind,
                    title=source.title or source.id,
                    enabled=source.enabled,
                    usable=source.enabled and reason is None,
                    reason=reason if source.enabled else UNUSABLE_REASONS["disabled"],
                    reason_code=code,
                    url=source.url,
                    conn_id=source.conn_id,
                    ref=source.ref,
                    layout=source.layout if source.kind in ("git", "path") else None,
                    components=components if source.layout != "files" else [source.component],
                    versions=versions,
                    overridden=over is not None,
                    pages=(stats.by_source.get(source.id, 0) if stats else 0),
                )
            )
        return infos

    def set_source(self, source_id: str, override: SourceOverride | None) -> SourceInfo:
        if source_id not in {s.id for s in self.kb.sources}:
            raise KeyError(source_id)
        if override is not None and override.components is not None:
            unknown = [c for c in override.components if c not in COMPONENTS]
            if unknown:
                raise ValueError(f"unknown components: {', '.join(unknown)}")
        if override is not None and override.versions is not None:
            bad = [v for v in override.versions if not re.fullmatch(r"[\w.+-]{1,40}", v)]
            if bad:
                raise ValueError(f"invalid versions: {', '.join(bad)}")
        self.state.set_override(source_id, override)
        return next(i for i in self.source_infos() if i.id == source_id)

    # -- status ---------------------------------------------------------------------------

    def _exists(self) -> bool:
        check = getattr(self.store, "exists", None)
        return bool(check()) if callable(check) else True

    def stats(self) -> StoreStats:
        if not self._exists():
            return StoreStats(kind=self.store.kind, location=self.store.location())
        try:
            self.ensure_open()
            return self.store.stats()
        except (StoreError, OSError, ValueError) as exc:
            return StoreStats(
                kind=self.store.kind, location=self.store.location(), ok=False, error=str(exc)
            )

    def run_status(self) -> KbRunStatus:
        try:
            return self.state.status()
        except (OSError, ValueError):
            return KbRunStatus()

    def reranker_name(self) -> str | None:
        if not self.kb.search.rerank:
            return None
        from autoair.ai.config import ai_settings_of

        kind = ai_settings_of(self.settings).retrieval.reranker
        return None if kind == "none" else kind

    def status(self, *, enabled: bool = True) -> KbStatus:
        stats = self.stats()
        run = self.run_status()
        hybrid = self.embedder is not None and stats.vectors > 0
        return KbStatus(
            enabled=enabled,
            online=self.kb.online,
            store=stats,
            run=run,
            mode="hybrid" if hybrid else "lexical",
            embedder=self.embedding_model,
            reranker=self.reranker_name(),
            refresh_interval_hours=self.kb.refresh_interval_hours,
            next_refresh=next_refresh(run, self.kb.refresh_interval_hours),
            sources=self.source_infos(stats),
            components=component_infos(),
            providers=self.kb.providers,
        )

    # -- refresh --------------------------------------------------------------------------

    def refresh(self, mode: str = "refresh", sources: Sequence[str] | None = None) -> KbRunStatus:
        """Run a refresh / rebuild now (blocking); raises
        :class:`~autoair.kb.ingest.AlreadyRunning` when one is running somewhere."""
        from autoair.kb.ingest import Ingestor

        self.ensure_open()
        return Ingestor(self).run("rebuild" if mode == "rebuild" else "refresh", sources)

    def start_refresh(
        self, mode: str = "refresh", sources: Sequence[str] | None = None
    ) -> KbRunStatus:
        """Start a refresh in a background thread; returns the status once it started (or
        raises AlreadyRunning)."""
        from autoair.kb.ingest import AlreadyRunning

        holder = self.state.holder()
        if holder is not None:
            raise AlreadyRunning(f"a knowledge-base refresh is already running ({holder})")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise AlreadyRunning("a knowledge-base refresh is already running here")

            def target() -> None:
                try:
                    self.refresh(mode, sources)
                except AlreadyRunning:
                    log.info("knowledge-base refresh skipped: another one is running")
                except Exception:
                    log.exception("knowledge-base refresh failed")

            self._thread = threading.Thread(target=target, name="autoair-kb-refresh", daemon=True)
            self._thread.start()
        return self.run_status()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def cancel(self) -> KbRunStatus:
        def change(s: KbRunStatus) -> None:
            if s.state == "running":
                s.cancel_requested = True

        return self.state.update_status(change)

    # -- search ---------------------------------------------------------------------------

    def _filters(self, filters: Mapping[str, Any] | None) -> dict[str, list[str]]:
        normalised: dict[str, list[str]] = {}
        for key, value in (filters or {}).items():
            if value is None or value == "" or value == []:
                continue
            values = [value] if isinstance(value, str) else list(value)
            normalised[key] = [str(v) for v in values]
        return clean_filters(normalised)

    def _rank(
        self,
        tokens: list[str],
        vector: Sequence[float] | None,
        pool: int,
        filters: Filters,
    ) -> tuple[list[tuple[Chunk, float]], bool]:
        rankings = [self.store.lexical(tokens, pool, filters)]
        dense = False
        if vector is not None:
            try:
                found = self.store.dense(vector, pool, filters)
            except StoreError:
                log.warning("knowledge-base vector search failed; BM25 only", exc_info=True)
                found = []
            if found:
                rankings.append(found)
                dense = True
        rrf = self.kb.search.rrf_k
        fused: dict[str, float] = {}
        chunks: dict[str, Chunk] = {}
        for ranking in rankings:
            for rank, (chunk, _score) in enumerate(ranking):
                fused[chunk.id] = fused.get(chunk.id, 0.0) + 1 / (rrf + rank + 1)
                chunks.setdefault(chunk.id, chunk)
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(chunks[cid], score) for cid, score in ordered], dense

    def _hits(self, ranked: list[tuple[Chunk, float]], tokens: list[str], k: int) -> list[KbHit]:
        cfg = self.kb.search
        per_page: dict[str, int] = {}
        hits: list[KbHit] = []
        urls: set[str] = set()
        for chunk, score in ranked:
            # One hit per section (several chunks of a long section share its URL).
            if per_page.get(chunk.page_id, 0) >= cfg.max_per_page or chunk.url in urls:
                continue
            urls.add(chunk.url)
            per_page[chunk.page_id] = per_page.get(chunk.page_id, 0) + 1
            hits.append(
                KbHit(
                    id=chunk.id,
                    title=chunk.title,
                    heading=chunk.heading,
                    url=chunk.url,
                    snippet=_snippet(chunk.text, tokens, cfg.snippet_chars),
                    text=chunk.text[: cfg.max_text_chars],
                    source=chunk.source,
                    component=chunk.component,
                    package=chunk.package,
                    version=chunk.version,
                    updated=chunk.updated,
                    score=round(score * 1000, 3),
                )
            )
            if len(hits) >= k:
                break
        return hits

    def _reranker(self, llm: Any) -> Any:
        if self.reranker_name() is None:
            return None
        from autoair.ai.config import ai_settings_of
        from autoair.ai.rerank import build_reranker

        ai = ai_settings_of(self.settings)
        return build_reranker(ai.retrieval, llm or self._llm, llm_mode=ai.llm_mode)

    def _tokens(self, query: str) -> list[str]:
        from autoair.ai.config import ai_settings_of

        return query_tokens(query, ai_settings_of(self.settings).retrieval.synonyms)

    async def asearch(
        self,
        query: str,
        *,
        k: int | None = None,
        filters: Mapping[str, Any] | None = None,
        rerank: bool = True,
        llm: Any = None,
    ) -> KbSearchResult:
        cfg = self.kb.search
        k = max(1, min(k or cfg.top_k, cfg.max_results))
        query = query.strip()[:1000]
        if not query or not self._exists():
            return KbSearchResult(query=query, mode="empty")
        await asyncio.to_thread(self.ensure_open)
        tokens = self._tokens(query)
        vector = None
        embedder = self.embedder
        if embedder is not None and self.store.vector_model() == self.embedding_model:
            try:
                aembed = getattr(embedder, "aembed", None)
                raw = (await aembed([query]))[0] if callable(aembed) else None
                if raw is None:
                    raw = (await asyncio.to_thread(embedder.embed, [query]))[0]
                vector = list(raw)
            except Exception:
                log.warning("knowledge-base query embedding failed; BM25 only", exc_info=True)
        pool = max(k * 5, cfg.candidate_pool)
        ranked, dense = await asyncio.to_thread(
            self._rank, tokens, vector, pool, self._filters(filters)
        )
        reranked = False
        reranker = self._reranker(llm) if rerank else None
        if reranker is not None and ranked:
            top = ranked[: cfg.rerank_top_n]
            try:
                scores = await reranker.rerank(
                    query, [f"{c.title} - {c.heading}\n{c.text[:1200]}" for c, _ in top]
                )
                order = sorted(zip(top, scores, strict=False), key=lambda pair: -float(pair[1]))
                ranked = [(c, 1 / (1 + i)) for i, ((c, _), _) in enumerate(order)] + ranked[
                    len(top) :
                ]
                reranked = True
            except Exception:
                log.warning("knowledge-base reranker failed; fused order kept", exc_info=True)
        hits = self._hits(ranked, tokens, k)
        mode = "hybrid" if dense else "lexical"
        return KbSearchResult(query=query, mode=mode, reranked=reranked, hits=hits)

    def search(
        self, query: str, *, k: int | None = None, filters: Mapping[str, Any] | None = None
    ) -> KbSearchResult:
        """Blocking search (CLI); no reranking."""
        cfg = self.kb.search
        k = max(1, min(k or cfg.top_k, cfg.max_results))
        query = query.strip()[:1000]
        if not query or not self._exists():
            return KbSearchResult(query=query, mode="empty")
        self.ensure_open()
        tokens = self._tokens(query)
        vector = None
        if self.embedder is not None and self.store.vector_model() == self.embedding_model:
            try:
                vector = list(self.embedder.embed([query])[0])
            except Exception:
                log.warning("knowledge-base query embedding failed; BM25 only", exc_info=True)
        ranked, dense = self._rank(
            tokens, vector, max(k * 5, cfg.candidate_pool), self._filters(filters)
        )
        return KbSearchResult(
            query=query, mode="hybrid" if dense else "lexical", hits=self._hits(ranked, tokens, k)
        )


def format_hits(hits: Sequence[KbHit], max_chars: int) -> str:
    """Passages for a prompt, each with its citation line (bounded size)."""
    parts: list[str] = []
    used = 0
    for n, hit in enumerate(hits, start=1):
        where = " ".join(x for x in (hit.component, hit.package, hit.version) if x)
        head = f"[docs {n}] {hit.title}" + (f" - {hit.heading}" if hit.heading else "")
        block = f"{head} ({where})\nURL: {hit.url}\n{hit.text}"
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)]
            if len(block) < 200:
                break
        parts.append(block)
        used += len(block)
    return "\n\n---\n\n".join(parts)


def kb_enabled(services: Any) -> bool:
    settings = getattr(services, "settings", None)
    try:
        return bool(settings is not None and settings.feature("kb"))
    except (ValueError, AttributeError):
        return False


def kb_for(services: Any) -> KnowledgeBase:
    """The knowledge base of a ``Services`` object (stored on it)."""
    from autoair.ai.corpus import attached

    return attached(
        services,
        "_autoair_kb",
        lambda: KnowledgeBase(services.settings, llm=getattr(services, "llm", None)),
    )


async def kb_context(services: Any, query: str, *, k: int = 3, max_chars: int | None = None) -> str:
    """Documentation passages for a prompt ("" when the knowledge base is off, empty or
    failing). Callers wrap the text as untrusted data."""
    if not query.strip() or not kb_enabled(services):
        return ""
    try:
        kb = kb_for(services)
        result = await kb.asearch(query, k=k, rerank=False)
    except Exception:
        log.debug("knowledge-base context unavailable", exc_info=True)
        return ""
    return format_hits(result.hits, max_chars or kb.kb.search.context_max_chars)


def hit_dicts(result: KbSearchResult) -> list[dict[str, Any]]:
    """Hits as agent-tool results (``kind`` and ``uri`` for the citation collector)."""
    out = []
    for hit in result.hits:
        data = hit.model_dump(exclude={"score", "id"}, exclude_none=True)
        last = hit.heading.rsplit(" > ", 1)[-1] if hit.heading else ""
        label = hit.title + (f" - {last}" if last and last != hit.title else "")
        data["title"] = f"{label} ({hit.package} {hit.version})"
        out.append({"kind": "kb", "uri": hit.url, **data})
    return out


__all__ = [
    "KnowledgeBase",
    "format_hits",
    "hit_dicts",
    "kb_context",
    "kb_enabled",
    "kb_for",
]
