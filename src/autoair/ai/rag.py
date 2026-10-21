"""Hybrid retrieval over the operator catalog, AutoAir docs / knowledge base, saved DAG
specs, templates and (research) installed package sources.

* **BM25** keyword ranking (pure Python, always on) with identifier-aware tokenisation
  (``S3ToRedshiftOperator`` -> ``s3 to redshift operator``) and query expansion
  (:mod:`autoair.ai.query`).
* Optional **dense embeddings** from the configured provider (OpenAI-compatible, Gemini,
  Ollama), cached on disk in SQLite keyed by model + text hash and kept in memory by hash,
  so re-indexing only embeds new or changed chunks (**incremental indexing**).
* **Reciprocal Rank Fusion** of the lexical and dense rankings (and of several query
  variants), then an optional **reranker** (:mod:`autoair.ai.rerank`).
* **Metadata filters** (``kind`` and any ``meta`` key) and a TTL **query cache**.
* Async API (:meth:`Retriever.asearch`): index work runs in a worker thread and query
  embeddings use the provider's async client - the event loop never blocks.

Failures of a source, the embedder or the reranker degrade silently to what still works.
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from autoair.ai.chunking import chunk_text
from autoair.ai.config import RetrievalSettings
from autoair.ai.query import expand_query

log = logging.getLogger("autoair.ai.rag")

DocKind = Literal["operator", "doc", "spec", "template", "memory", "source", "package", "web"]
KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
Filters = Mapping[str, Any]

_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "i", "in",
        "into", "is", "it", "of", "on", "or", "that", "the", "this", "to", "use", "using",
        "what", "when", "which", "with", "you", "your", "do", "does", "can", "should", "my",
        "me", "we",
    }
)  # fmt: skip
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens; splits camelCase / snake_case / dotted names, light stemming."""
    out: list[str] = []
    for raw in _WORD.findall(_CAMEL.sub(" ", text).lower()):
        if raw in _STOP or len(raw) < 2:
            continue
        if len(raw) > 4 and raw.endswith("s") and not raw.endswith("ss"):
            raw = raw[:-1]
        out.append(raw)
    return out


class Document(BaseModel):
    id: str
    kind: DocKind
    title: str
    text: str
    uri: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Hit(BaseModel):
    id: str
    kind: DocKind
    title: str
    snippet: str
    uri: str | None = None
    score: float
    meta: dict[str, Any] = Field(default_factory=dict)


def matches(doc: Document, kinds: set[str] | None, filters: Filters | None) -> bool:
    """``kind`` in ``kinds`` and every filter matches ``doc.meta`` (a list value = any of)."""
    if kinds is not None and doc.kind not in kinds:
        return False
    for key, wanted in (filters or {}).items():
        value = doc.kind if key == "kind" else doc.meta.get(key)
        if isinstance(wanted, (list, tuple, set, frozenset)):
            if value not in wanted:
                return False
        elif value != wanted:
            return False
    return True


class BM25Index:
    def __init__(
        self, docs: Sequence[Document], *, k1: float = 1.4, b: float = 0.7, title_boost: int = 3
    ) -> None:
        self.docs = list(docs)
        self.k1, self.b = k1, b
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._lengths: list[int] = []
        for i, doc in enumerate(self.docs):
            tokens = tokenize(doc.title) * title_boost + tokenize(doc.text)
            self._lengths.append(len(tokens))
            for term, tf in Counter(tokens).items():
                self._postings[term].append((i, tf))
        n = len(self.docs)
        self._avg = (sum(self._lengths) / n) if n else 0.0
        self._idf = {
            t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self._postings.items()
        }

    def search(
        self,
        query: str,
        k: int = 10,
        kinds: Iterable[str] | None = None,
        filters: Filters | None = None,
        *,
        extra_terms: Iterable[str] = (),
        extra_weight: float = 0.3,
    ) -> list[tuple[int, float]]:
        """BM25 ranking; ``extra_terms`` (query expansion) count ``extra_weight`` times."""
        allowed = set(kinds) if kinds else None
        scores: dict[int, float] = defaultdict(float)
        weights = dict.fromkeys(tokenize(query), 1.0)
        for term in extra_terms:
            for token in tokenize(term):
                weights.setdefault(token, extra_weight)
        for term, weight in weights.items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            idf *= weight
            for i, tf in self._postings[term]:
                if (allowed is not None or filters) and not matches(self.docs[i], allowed, filters):
                    continue
                norm = self.k1 * (1 - self.b + self.b * self._lengths[i] / (self._avg or 1))
                scores[i] += idf * tf * (self.k1 + 1) / (tf + norm)
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]


# --------------------------------------------------------------------------- embeddings


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embeds texts (:class:`autoair.ai.models.Embedder`; sync ``embed`` for index
    builds in worker threads, async ``aembed`` for queries)."""

    @property
    def embedding_model(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingCache:
    """SQLite cache: (model, sha256(text)) -> float32 vector, capped at ``max_entries``
    rows (the oldest writes are pruned first)."""

    def __init__(self, path: Path, max_entries: int = 100_000) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS emb (model TEXT, hash TEXT, vec BLOB, "
            "PRIMARY KEY (model, hash))"
        )
        self._db.commit()

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def get_many(self, model: str, hashes: Sequence[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        with self._lock:
            for start in range(0, len(hashes), 500):
                chunk = list(hashes[start : start + 500])
                marks = ",".join("?" * len(chunk))
                rows = self._db.execute(
                    f"SELECT hash, vec FROM emb WHERE model = ? AND hash IN ({marks})",  # noqa: S608
                    [model, *chunk],
                ).fetchall()
                for h, blob in rows:
                    vec = array.array("f")
                    vec.frombytes(blob)
                    out[h] = list(vec)
        return out

    def put_many(self, model: str, items: dict[str, list[float]]) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO emb VALUES (?, ?, ?)",
                [(model, h, array.array("f", v).tobytes()) for h, v in items.items()],
            )
            self._prune()
            self._db.commit()

    def _prune(self) -> None:
        if self.max_entries <= 0:
            return
        (count,) = self._db.execute("SELECT COUNT(*) FROM emb").fetchone()
        if count <= self.max_entries:
            return
        # ``INSERT OR REPLACE`` assigns a fresh rowid, so rowid order is write order.
        self._db.execute(
            "DELETE FROM emb WHERE rowid IN (SELECT rowid FROM emb ORDER BY rowid LIMIT ?)",
            (count - self.max_entries,),
        )

    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM emb").fetchone()[0])


def _normalise(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


class VectorIndex:
    """Dense vectors of the corpus. Vectors are kept per text hash, so :meth:`update`
    after a corpus change embeds only new/changed chunks (incremental indexing)."""

    def __init__(
        self,
        docs: Sequence[Document],
        embedder: EmbeddingProvider,
        cache: EmbeddingCache | None,
        batch: int = 64,
        max_chars: int = 1500,
    ) -> None:
        self.embedder = embedder
        self.cache = cache
        self.batch = batch
        self.max_chars = max_chars
        self._known: dict[str, list[float]] = {}
        self.vectors: list[list[float]] = []
        self.embedded = 0
        self.update(docs)

    def text_of(self, doc: Document) -> str:
        return f"{doc.title}\n{doc.text[: self.max_chars]}"

    def update(self, docs: Sequence[Document]) -> None:
        self.vectors = self.compute(docs)

    def compute(self, docs: Sequence[Document]) -> list[list[float]]:
        """Vectors of ``docs`` (embedding only unknown chunks, in batches). Vectors no
        longer in the corpus are dropped from memory (the SQLite cache keeps them)."""
        texts = [self.text_of(d) for d in docs]
        hashes = [EmbeddingCache.key(t) for t in texts]
        model = self.embedder.embedding_model
        missing_hashes = [h for h in dict.fromkeys(hashes) if h not in self._known]
        if missing_hashes and self.cache is not None:
            self._known.update(self.cache.get_many(model, missing_hashes))
        by_hash = dict(zip(hashes, texts, strict=True))
        missing = [h for h in dict.fromkeys(hashes) if h not in self._known]
        fresh: dict[str, list[float]] = {}
        for start in range(0, len(missing), self.batch):
            chunk = missing[start : start + self.batch]
            vectors = self.embedder.embed([by_hash[h] for h in chunk])
            for h, vec in zip(chunk, vectors, strict=True):
                fresh[h] = _normalise(vec)
        if fresh:
            self.embedded += len(fresh)
            if self.cache is not None:
                self.cache.put_many(model, fresh)
            self._known.update(fresh)
        vectors = [_normalise(self._known[h]) for h in hashes]
        current = set(hashes)
        self._known = {h: v for h, v in self._known.items() if h in current}
        return vectors

    def search_vector(
        self, q: Sequence[float], k: int, allowed: Sequence[bool]
    ) -> list[tuple[int, float]]:
        return _search_vectors(self.vectors, q, k, allowed)

    def search(self, query: str, k: int, allowed: Sequence[bool]) -> list[tuple[int, float]]:
        return self.search_vector(_normalise(self.embedder.embed([query])[0]), k, allowed)


def _search_vectors(
    vectors: Sequence[Sequence[float]], q: Sequence[float], k: int, allowed: Sequence[bool]
) -> list[tuple[int, float]]:
    scored = [(i, _dot(q, v)) for i, v in enumerate(vectors) if allowed[i]]
    scored.sort(key=lambda kv: -kv[1])
    return scored[:k]


# --------------------------------------------------------------------------- retriever

#: Index (re)builds run here, not on the default executor: a long first embedding of
#: the corpus must not starve ``asyncio.to_thread`` work elsewhere in the app.
_INDEX_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="autoair-rag-index")


@dataclass(frozen=True)
class _Snapshot:
    """One consistent view of the index: documents, BM25 and their vectors (aligned)."""

    bm25: BM25Index
    vectors: list[list[float]] | None
    fingerprint: str


CorpusSource = Callable[[], list[Document]]


def _snippet(text: str, query: str, size: int = 320) -> str:
    terms = set(tokenize(query))
    lower = text.lower()
    best = 0
    for term in terms:
        pos = lower.find(term)
        if pos >= 0:
            best = max(0, pos - size // 4)
            break
    chunk = text[best : best + size].strip()
    return ("…" if best else "") + chunk + ("…" if best + size < len(text) else "")


def _fingerprint(docs: Sequence[Document]) -> str:
    digest = hashlib.sha256()
    for d in docs:
        digest.update(f"{d.id}:{d.title}\0".encode())
        digest.update(d.text.encode())
        digest.update(b"\1")
    return digest.hexdigest()


class Retriever:
    """Hybrid search over a corpus assembled from ``sources`` (re-read after
    ``ttl_seconds``; the index is rebuilt only when a source changed).

    The index is an immutable :class:`_Snapshot` swapped atomically after a rebuild, so
    a search always sees documents and vectors from the same build. Only one thread
    rebuilds at a time, and while it embeds, other searches keep using the previous
    snapshot instead of queueing on a lock."""

    def __init__(
        self,
        sources: Sequence[CorpusSource],
        *,
        embedder: EmbeddingProvider | None = None,
        cache_path: Path | None = None,
        ttl_seconds: float | None = None,
        settings: RetrievalSettings | None = None,
        reranker: Any = None,
        embed_batch: int = 64,
        embed_chars: int = 1500,
        cache_max_entries: int = 100_000,
    ) -> None:
        self.settings = settings or RetrievalSettings()
        self._sources = list(sources)
        self._embedder = embedder
        self._cache = (
            EmbeddingCache(cache_path, cache_max_entries) if (embedder and cache_path) else None
        )
        self._ttl = self.settings.index_ttl_seconds if ttl_seconds is None else ttl_seconds
        #: Serialises rebuilds (held while reading sources and embedding).
        self._build_lock = threading.Lock()
        #: Guards the snapshot pointer and the small query / query-vector caches.
        self._lock = threading.Lock()
        self._built_at = 0.0
        self._fingerprints: list[str] = []
        self._snapshot: _Snapshot | None = None
        self._vectors: VectorIndex | None = None
        self._embed_batch = embed_batch
        self._embed_chars = embed_chars
        self.reranker = reranker
        self._queries: OrderedDict[str, tuple[float, list[Hit]]] = OrderedDict()
        self._qvec: OrderedDict[str, list[float]] = OrderedDict()
        self.rebuilds = 0

    @property
    def hybrid(self) -> bool:
        return self._embedder is not None

    @property
    def fingerprint(self) -> str:
        snap = self._snapshot
        return snap.fingerprint if snap is not None else ""

    def _read_sources(self) -> list[list[Document]]:
        out: list[list[Document]] = []
        for source in self._sources:
            try:
                out.append(list(source()))
            except Exception:
                log.warning("RAG source failed", exc_info=True)
                out.append([])
        return out

    def _fresh(self) -> bool:
        return self._snapshot is not None and time.monotonic() - self._built_at < self._ttl

    def _ensure_snapshot(self) -> _Snapshot:
        snap = self._snapshot
        if snap is not None and self._fresh():
            return snap
        # Someone else is rebuilding: serve the previous snapshot rather than wait.
        if not self._build_lock.acquire(blocking=snap is None):
            assert snap is not None
            return snap
        try:
            if self._fresh():
                assert self._snapshot is not None
                return self._snapshot
            per_source = self._read_sources()
            prints = [_fingerprint(docs) for docs in per_source]
            current = self._snapshot
            if current is None or prints != self._fingerprints:
                docs = [d for group in per_source for d in group]
                s = self.settings
                bm25 = BM25Index(docs, k1=s.bm25_k1, b=s.bm25_b, title_boost=s.title_boost)
                vectors: list[list[float]] | None = None
                if self._embedder is not None and docs:
                    try:
                        if self._vectors is None:
                            self._vectors = VectorIndex(
                                [],
                                self._embedder,
                                self._cache,
                                batch=self._embed_batch,
                                max_chars=self._embed_chars,
                            )
                        vectors = self._vectors.compute(docs)
                        self._vectors.vectors = vectors
                    except Exception:
                        log.warning("embedding index unavailable; using BM25 only", exc_info=True)
                        self._vectors = None
                current = _Snapshot(
                    bm25, vectors, hashlib.sha256("".join(prints).encode()).hexdigest()
                )
                with self._lock:
                    self._snapshot = current
                    self._fingerprints = prints
                    self._queries.clear()
                self.rebuilds += 1
            self._built_at = time.monotonic()
            return current
        finally:
            self._build_lock.release()

    def _ensure(self) -> BM25Index:
        return self._ensure_snapshot().bm25

    async def _aensure(self) -> _Snapshot:
        snap = self._snapshot
        if snap is not None and self._fresh():
            return snap
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_INDEX_EXECUTOR, self._ensure_snapshot)

    def invalidate(self) -> None:
        self._built_at = 0.0

    # -- ranking --------------------------------------------------------------------------

    def _lexical(
        self,
        index: BM25Index,
        query: str,
        pool: int,
        kinds: set[str] | None,
        filters: Filters | None,
    ) -> list[tuple[int, float]]:
        extra: list[str] = []
        if self.settings.query_expansion:
            vocab = {k: tuple(v) for k, v in self.settings.synonyms.items()}
            expanded = expand_query(query, vocab)
            extra = expanded[len(query) :].split()
        return index.search(query, pool, kinds, filters, extra_terms=extra)

    def _fuse(
        self, rankings: Sequence[Sequence[tuple[int, float]]], k: int
    ) -> list[tuple[int, float]]:
        fused: dict[int, float] = defaultdict(float)
        for ranking in rankings:
            for rank, (i, _) in enumerate(ranking):
                fused[i] += 1 / (self.settings.rrf_k + rank + 1)
        return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:k]

    def _allowed(
        self, index: BM25Index, kinds: set[str] | None, filters: Filters | None
    ) -> list[bool]:
        return [matches(d, kinds, filters) for d in index.docs]

    def _hits(self, index: BM25Index, ranked: Sequence[tuple[int, float]], query: str) -> list[Hit]:
        hits: list[Hit] = []
        for i, score in ranked:
            doc = index.docs[i]
            hits.append(
                Hit(
                    id=doc.id,
                    kind=doc.kind,
                    title=doc.title,
                    snippet=_snippet(doc.text, query),
                    uri=doc.uri,
                    score=round(score * 1000, 3),
                    meta=doc.meta,
                )
            )
        return hits

    def _cache_key(self, snap: _Snapshot, *parts: Any) -> str:
        return json.dumps([snap.fingerprint, *parts], sort_keys=True, default=str)

    def _cached(self, key: str) -> list[Hit] | None:
        with self._lock:
            entry = self._queries.get(key)
            if entry is None:
                return None
            if time.monotonic() - entry[0] > self.settings.query_cache_ttl_seconds:
                self._queries.pop(key, None)
                return None
            self._queries.move_to_end(key)
        return [h.model_copy() for h in entry[1]]

    def _store(self, snap: _Snapshot, key: str, hits: list[Hit]) -> None:
        if self.settings.query_cache_size <= 0:
            return
        with self._lock:
            if self._snapshot is not snap:  # the index changed meanwhile: don't cache
                return
            self._queries[key] = (time.monotonic(), hits)
            while len(self._queries) > self.settings.query_cache_size:
                self._queries.popitem(last=False)

    def search(
        self,
        query: str,
        k: int = 8,
        kinds: Iterable[str] | None = None,
        filters: Filters | None = None,
    ) -> list[Hit]:
        """Synchronous hybrid search (no reranking; use :meth:`asearch` for that)."""
        if not query.strip():
            return []
        snap = self._ensure_snapshot()
        index = snap.bm25
        kind_set = set(kinds) if kinds else None
        key = self._cache_key(snap, "sync", query, k, sorted(kind_set or []), filters)
        cached = self._cached(key)
        if cached is not None:
            return cached
        pool = max(k * 5, self.settings.candidate_pool)
        rankings = [self._lexical(index, query, pool, kind_set, filters)]
        if snap.vectors is not None:
            try:
                q = self._query_vector_sync(query)
                allowed = self._allowed(index, kind_set, filters)
                rankings.append(_search_vectors(snap.vectors, q, pool, allowed))
            except Exception:
                log.warning("embedding query failed; BM25 only", exc_info=True)
        hits = self._hits(index, self._fuse(rankings, k), query)
        self._store(snap, key, hits)
        return hits

    def _query_vector_sync(self, query: str) -> list[float]:
        with self._lock:
            cached = self._qvec.get(query)
        if cached is not None:
            return cached
        assert self._embedder is not None
        vec = _normalise(self._embedder.embed([query])[0])
        self._remember_qvec(query, vec)
        return vec

    def _remember_qvec(self, query: str, vec: list[float]) -> None:
        with self._lock:
            self._qvec[query] = vec
            while len(self._qvec) > 256:
                self._qvec.popitem(last=False)

    async def _query_vector(self, query: str) -> list[float]:
        with self._lock:
            cached = self._qvec.get(query)
        if cached is not None:
            return cached
        embedder: Any = self._embedder
        aembed = getattr(embedder, "aembed", None)
        if callable(aembed):
            raw = (await aembed([query]))[0]
        else:
            raw = (await asyncio.to_thread(embedder.embed, [query]))[0]
        vec = _normalise(raw)
        self._remember_qvec(query, vec)
        return vec

    async def asearch(
        self,
        query: str,
        k: int | None = None,
        kinds: Iterable[str] | None = None,
        filters: Filters | None = None,
        *,
        variants: Sequence[str] = (),
        rerank: bool = True,
        reranker: Any = None,
    ) -> list[Hit]:
        """Async hybrid search: BM25 + dense for the query and its ``variants`` (LLM
        rewrites), fused with RRF, then reranked when a reranker is configured
        (``reranker`` overrides the retriever's own, e.g. one on the caller's model)."""
        k = k or self.settings.top_k
        if not query.strip():
            return []
        # Documents and vectors are read from ONE snapshot: a concurrent rebuild can
        # neither misalign them nor evict what this search is using.
        snap = await self._aensure()
        index = snap.bm25
        kind_set = set(kinds) if kinds else None
        active = reranker if reranker is not None else self.reranker
        use_rerank = bool(rerank and active is not None)
        key = self._cache_key(
            snap,
            "async",
            query,
            list(variants),
            k,
            sorted(kind_set or []),
            filters,
            getattr(active, "name", None) if use_rerank else None,
            id(active) if use_rerank and reranker is not None else None,
        )
        cached = self._cached(key)
        if cached is not None:
            return cached
        pool = max(k * 5, self.settings.candidate_pool)
        queries = list(dict.fromkeys([query, *variants]))
        rankings: list[list[tuple[int, float]]] = []
        for q in queries:
            rankings.append(
                await asyncio.to_thread(self._lexical, index, q, pool, kind_set, filters)
            )
        if snap.vectors is not None:
            allowed = self._allowed(index, kind_set, filters)
            for q in queries:
                try:
                    qv = await self._query_vector(q)
                    rankings.append(_search_vectors(snap.vectors, qv, pool, allowed))
                except Exception:
                    log.warning("embedding query failed; BM25 only", exc_info=True)
                    break
        fused = self._fuse(rankings, max(k, self.settings.rerank_top_n if use_rerank else k))
        if use_rerank and fused:
            fused = await self._rerank(active, index, query, fused)
        hits = self._hits(index, fused[:k], query)
        self._store(snap, key, hits)
        return hits

    async def _rerank(
        self, reranker: Any, index: BM25Index, query: str, fused: list[tuple[int, float]]
    ) -> list[tuple[int, float]]:
        top = fused[: self.settings.rerank_top_n]
        texts = [f"{index.docs[i].title}\n{index.docs[i].text[:1200]}" for i, _ in top]
        try:
            scores = await reranker.rerank(query, texts)
        except Exception:
            log.warning("reranker failed; keeping the fused order", exc_info=True)
            return fused
        reranked = sorted(
            ((i, float(s)) for (i, _), s in zip(top, scores, strict=False)),
            key=lambda kv: -kv[1],
        )
        # Rerank scores are on another scale: express them as fused-like positive scores.
        out = [(i, 1 / (1 + rank)) for rank, (i, _) in enumerate(reranked)]
        return out + fused[len(top) :]

    def document(self, doc_id: str) -> Document | None:
        index = self._ensure()
        return next((d for d in index.docs if d.id == doc_id), None)

    def documents(self) -> list[Document]:
        return list(self._ensure().docs)

    def context(
        self,
        query: str,
        *,
        k: int = 4,
        kinds: Iterable[str] | None = None,
        max_chars: int = 4000,
        filters: Filters | None = None,
    ) -> str:
        """Retrieved passages formatted for a prompt (bounded size)."""
        return self.format_context(self.search(query, k, kinds, filters), max_chars)

    async def acontext(
        self,
        query: str,
        *,
        k: int = 4,
        kinds: Iterable[str] | None = None,
        max_chars: int = 4000,
        filters: Filters | None = None,
    ) -> str:
        return self.format_context(await self.asearch(query, k, kinds, filters), max_chars)

    def format_context(self, hits: Sequence[Hit], max_chars: int) -> str:
        index = self._ensure()
        by_id = {d.id: d for d in index.docs}
        parts: list[str] = []
        used = 0
        for hit in hits:
            doc = by_id.get(hit.id)
            if doc is None:
                continue
            body = doc.text[: max(400, (max_chars - used) // 2)]
            block = f"[{doc.kind}] {doc.title}\n{body}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)


# --------------------------------------------------------------------------- corpus builders


def markdown_documents(
    path: Path | None = None,
    *,
    uri_prefix: str = "autoair://docs/",
    text: str | None = None,
    doc_id: str | None = None,
    chunk_size: int = 1200,
    chunk_overlap: int = 150,
    meta: Mapping[str, Any] | None = None,
) -> list[Document]:
    """Split Markdown into one document per ``##`` section (long sections are chunked)."""
    if text is None:
        if path is None:
            raise ValueError("markdown_documents needs a path or text")
        text = path.read_text(encoding="utf-8")
    doc_id = doc_id or (path.stem if path is not None else "doc")
    title = doc_id.replace("-", " ").title()
    first = text.splitlines()[0] if text else ""
    if first.startswith("# "):
        title = first[2:].strip()
    sections = re.split(r"(?m)^## ", text)
    docs: list[Document] = []
    for n, section in enumerate(sections):
        lines = section.strip().splitlines()
        if not lines:
            continue
        heading = title if n == 0 else lines[0].strip()
        body = "\n".join(lines if n == 0 else lines[1:]).strip()
        if not body:
            continue
        for m, piece in enumerate(chunk_text(body, chunk_size, chunk_overlap)):
            docs.append(
                Document(
                    id=f"doc:{doc_id}#{n}" + (f".{m}" if m else ""),
                    kind="doc",
                    title=heading if n == 0 else f"{title} - {heading}",
                    text=piece,
                    uri=f"{uri_prefix}{doc_id}",
                    meta={"doc_id": doc_id, **(meta or {})},
                )
            )
    return docs


def knowledge_files() -> list[Path]:
    """Shipped knowledge plus the repository ``docs/`` folder when running from source."""
    files = sorted(KNOWLEDGE_DIR.glob("*.md"))
    repo_docs = Path(__file__).resolve().parents[3] / "docs"
    if repo_docs.is_dir():
        files += [p for p in sorted(repo_docs.glob("*.md")) if p.stem.lower() != "roadmap"]
    return files


def doc_documents() -> list[Document]:
    docs: list[Document] = []
    for path in knowledge_files():
        try:
            docs.extend(markdown_documents(path))
        except OSError:
            continue
    return docs
