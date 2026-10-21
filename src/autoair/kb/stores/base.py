"""The pluggable knowledge-base store interface.

A store keeps three things: the **pages** (metadata, content hash, ETag, the parsed
sections as a compressed body: lets a bundle be exported and the index be re-chunked
without fetching again), their **chunks** with a keyword (BM25-like) index, and optional
**vectors** of one embedding model. Retrieval asks the store for two rankings (lexical,
dense) and fuses them itself (Reciprocal Rank Fusion), so every store behaves the same.

Implementations: :mod:`.local` (SQLite + FTS5), :mod:`.pgvector`, :mod:`.elasticsearch`.
The contract tests in ``tests/unit/test_kb_store_contract.py`` run against all of them.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol, runtime_checkable

from autoair.ai.query import expand_query
from autoair.ai.rag import tokenize
from autoair.kb.models import Chunk, PageMeta, PageRecord, StoreStats

#: component / package / version / source -> allowed values.
Filters = Mapping[str, Sequence[str]]
FILTER_KEYS = ("component", "package", "version", "source")


class StoreError(Exception):
    """The store is unreachable or misconfigured (message without credentials)."""


def query_tokens(query: str, synonyms: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """Identifier-aware tokens of a query plus the Airflow synonym expansion."""
    vocab = {k: tuple(v) for k, v in (synonyms or {}).items()}
    return list(dict.fromkeys(tokenize(expand_query(query, vocab))))


def index_terms(text: str) -> str:
    return " ".join(tokenize(text))


def normalise(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def clean_filters(filters: Filters | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, values in (filters or {}).items():
        if key in FILTER_KEYS and values:
            out[key] = [str(v) for v in values][:50]
    return out


@runtime_checkable
class KbStore(Protocol):
    kind: str

    def identity(self) -> str:
        """Stable id of the index (lock and status are kept per index)."""
        ...

    def location(self) -> str: ...

    def open(self) -> None:
        """Create the schema if needed."""
        ...

    def close(self) -> None: ...

    # -- pages ----------------------------------------------------------------------------

    def pages(self, source: str | None = None) -> dict[str, PageRecord]: ...

    def put_page(
        self,
        page: PageMeta,
        body: bytes,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]] | None,
        model: str | None,
    ) -> None:
        """Replace a page and its chunks atomically (vectors aligned with ``chunks``)."""
        ...

    def touch_page(self, page: PageMeta) -> None:
        """Update fetch metadata (ETag, Last-Modified, fetched_at) of an unchanged page."""
        ...

    def delete_pages(self, ids: Sequence[str]) -> int: ...

    def flush(self) -> None:
        """Make buffered writes durable and visible to searches on every replica.

        Stores may batch ``put_page`` / ``touch_page`` / ``put_vectors``; the ingestion run
        calls this once at its end. Reads of the same store instance see its own writes
        without it. A no-op for stores that write through."""
        ...

    def clear(self, source: str | None = None) -> None: ...

    def iter_bodies(self, source: str | None = None) -> Iterator[tuple[PageRecord, bytes]]: ...

    def get_meta(self, key: str) -> str | None: ...

    def set_meta(self, key: str, value: str | None) -> None: ...

    # -- vectors --------------------------------------------------------------------------

    def vector_model(self) -> str | None: ...

    def reset_vectors(self, model: str | None) -> None:
        """Drop every vector and record ``model`` as the store's embedding model."""
        ...

    def missing_vectors(self, limit: int) -> list[Chunk]: ...

    def put_vectors(self, items: Mapping[str, Sequence[float]]) -> None: ...

    # -- retrieval ------------------------------------------------------------------------

    def lexical(
        self, tokens: Sequence[str], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]: ...

    def dense(
        self, vector: Sequence[float], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]: ...

    def stats(self) -> StoreStats: ...


__all__ = [
    "FILTER_KEYS",
    "Filters",
    "KbStore",
    "StoreError",
    "clean_filters",
    "index_terms",
    "normalise",
    "query_tokens",
]
