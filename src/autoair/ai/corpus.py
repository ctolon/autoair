"""Retrieval corpus of an AutoAir installation (operators, docs, saved DAGs, templates)
and the per-services :class:`~autoair.ai.rag.Retriever` singleton."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

from autoair.ai.config import ai_settings_of
from autoair.ai.rag import CorpusSource, Document, Retriever

log = logging.getLogger("autoair.ai.rag")

T = TypeVar("T")

MAX_SPECS = 300
MAX_TEMPLATES = 300


def operator_documents(catalog: Any) -> list[Document]:
    docs: list[Document] = []
    for s in catalog.summaries():
        if not s.builder_supported:
            continue
        detail = catalog.get(s.fqn)
        params = ""
        doc = ""
        if detail is not None:
            props = detail.params_schema.get("properties") or {}
            params = ", ".join(sorted(props)[:40])
            doc = (detail.docstring or "")[:700]
        text = "\n".join(
            x
            for x in (
                s.summary,
                f"Provider: {s.provider}" + (f" ({s.integration})" if s.integration else ""),
                f"Category: {s.category}" + (" (deprecated)" if s.deprecated else ""),
                f"Parameters: {params}" if params else "",
                doc,
            )
            if x
        )
        docs.append(
            Document(
                id=f"operator:{s.fqn}",
                kind="operator",
                title=f"{s.class_name} ({s.fqn})",
                text=text,
                uri=f"autoair://operators/{s.fqn}/schema",
                meta={"fqn": s.fqn, "provider": s.provider, "deprecated": s.deprecated},
            )
        )
    return docs


def spec_documents(store: Any, limit: int = MAX_SPECS) -> list[Document]:
    docs: list[Document] = []
    for item in list(store.list())[:limit]:
        try:
            spec = store.get(item.dag_id)
        except Exception:
            log.debug("skipping unreadable spec %s", item.dag_id, exc_info=True)
            continue
        if spec is None:
            continue
        operators = sorted(
            {getattr(n, "operator", "").rsplit(".", 1)[-1] for n in spec.task_nodes()} - {""}
        )
        tasks = [n.task_id for n in spec.task_nodes()]
        text = "\n".join(
            x
            for x in (
                spec.description or "",
                f"Tags: {', '.join(spec.tags)}" if spec.tags else "",
                f"Schedule: {spec.schedule.type}",
                f"Operators: {', '.join(operators)}" if operators else "",
                f"Tasks: {', '.join(tasks[:60])}",
                (spec.doc_md or "")[:800],
            )
            if x
        )
        docs.append(
            Document(
                id=f"spec:{spec.dag_id}",
                kind="spec",
                title=spec.display_name or spec.dag_id,
                text=text,
                uri=f"autoair://dags/{spec.dag_id}/spec",
                meta={"dag_id": spec.dag_id},
            )
        )
    return docs


def template_documents(templates: Any, limit: int = MAX_TEMPLATES) -> list[Document]:
    docs: list[Document] = []
    for t in list(templates.list())[:limit]:
        data = t.model_dump() if hasattr(t, "model_dump") else dict(t)
        tid = str(data.get("id"))
        text = "\n".join(
            str(x)
            for x in (
                data.get("description") or "",
                f"Kind: {data.get('kind', '')}",
                f"Tags: {', '.join(data.get('tags') or [])}",
            )
            if x
        )
        docs.append(
            Document(
                id=f"template:{tid}",
                kind="template",
                title=str(data.get("name") or tid),
                text=text,
                uri=f"autoair://templates/{tid}",
                meta={"template_id": tid},
            )
        )
    return docs


def sources_for(services: Any) -> list[CorpusSource]:
    from autoair.ai.knowledge import knowledge_for
    from autoair.mcp.store import store_for

    retrieval = ai_settings_of(getattr(services, "settings", None)).retrieval

    def operators() -> list[Document]:
        return operator_documents(services.catalog)

    def knowledge() -> list[Document]:
        return knowledge_for(services).documents(
            chunk_size=retrieval.chunk_size, chunk_overlap=retrieval.chunk_overlap
        )

    def specs() -> list[Document]:
        return spec_documents(store_for(services), retrieval.max_specs)

    def templates() -> list[Document]:
        store = getattr(services, "templates", None)
        return template_documents(store, retrieval.max_templates) if store is not None else []

    return [operators, knowledge, specs, templates]


_LOCK = threading.Lock()
#: Fallback for services objects that do not accept attributes (keyed by ``id``; the
#: object itself is kept to detect ``id`` reuse, so this map is kept small).
_FALLBACK: dict[tuple[str, int], tuple[Any, Any]] = {}
_FALLBACK_MAX = 16


def attached(services: Any, attr: str, factory: Callable[[], T]) -> T:
    """A per-``services`` singleton stored ON the services object (so it lives and dies
    with it, instead of a module-level map keeping every ``Services`` alive)."""
    with _LOCK:
        value = getattr(services, attr, None)
        if value is not None:
            return cast("T", value)
        cached = _FALLBACK.get((attr, id(services)))
        if cached is not None and cached[0] is services:
            return cast("T", cached[1])
        created = factory()
        try:
            setattr(services, attr, created)
        except (AttributeError, TypeError):
            while len(_FALLBACK) >= _FALLBACK_MAX:
                _FALLBACK.pop(next(iter(_FALLBACK)))
            _FALLBACK[(attr, id(services))] = (services, created)
        return created


def _build_retriever(services: Any) -> Retriever:
    from autoair.ai.llm import build_embedder
    from autoair.ai.rerank import build_reranker

    settings = getattr(services, "settings", None)
    ai = ai_settings_of(settings)
    embedder = None
    try:
        embedder = build_embedder(settings)
    except Exception:
        log.warning("embeddings disabled: provider setup failed", exc_info=True)
    cache_path = None
    if settings is not None:
        base = getattr(settings, "cache_dir", None) or getattr(settings, "state_dir", None)
        if base is not None:
            cache_path = base / "ai-embeddings.sqlite3"
    return Retriever(
        sources_for(services),
        embedder=embedder,
        cache_path=cache_path,
        settings=ai.retrieval,
        reranker=build_reranker(ai.retrieval, getattr(services, "llm", None), llm_mode=ai.llm_mode),
        embed_batch=ai.embedding.batch_size,
        embed_chars=ai.embedding.max_chars,
        cache_max_entries=ai.retrieval.embedding_cache_max_entries,
    )


def retriever_for(services: Any) -> Retriever:
    """The (cached) retriever of a ``Services`` object."""
    return attached(services, "_autoair_ai_retriever", lambda: _build_retriever(services))
