"""Local knowledge-base store: one SQLite file, no dependencies.

* chunks are indexed with **FTS5** (``bm25()`` ranking; the indexed text is AutoAir's
  identifier-aware token stream, so ``S3KeySensor`` matches ``s3 key sensor``; titles
  weigh three times the body). Python builds without FTS5 fall back to the in-memory
  :class:`autoair.ai.rag.BM25Index` over the same rows;
* vectors are float32 blobs searched by brute force (numpy when installed); for large
  embedded indexes prefer pgvector or Elasticsearch;
* WAL mode: other processes on the host read while one writes.
"""

from __future__ import annotations

import array
import json
import math
import socket
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from autoair.kb.models import Chunk, PageMeta, PageRecord, StoreStats
from autoair.kb.stores.base import Filters, clean_filters, index_terms, normalise

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY, source TEXT, unit TEXT, component TEXT, package TEXT, version TEXT,
    url TEXT, title TEXT, updated TEXT, content_hash TEXT, etag TEXT, last_modified TEXT,
    fetched_at TEXT, chunks INTEGER, body BLOB
);
CREATE INDEX IF NOT EXISTS pages_source ON pages(source);
CREATE TABLE IF NOT EXISTS chunks (
    rid INTEGER PRIMARY KEY, id TEXT UNIQUE, page_id TEXT, n INTEGER, source TEXT,
    component TEXT, package TEXT, version TEXT, url TEXT, title TEXT, heading TEXT,
    text TEXT, hash TEXT, updated TEXT, terms TEXT, title_terms TEXT
);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_id);
CREATE TABLE IF NOT EXISTS vectors (rid INTEGER PRIMARY KEY, vec BLOB);
"""
_CHUNK_COLS = (
    "c.id, c.page_id, c.n, c.source, c.component, c.package, c.version, c.url, c.title, "
    "c.heading, c.text, c.hash, c.updated"
)
_PAGE_COLS = (
    "id, source, unit, component, package, version, url, title, updated, content_hash, etag, "
    "last_modified, fetched_at, chunks"
)


def _fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        conn.close()
    except sqlite3.OperationalError:
        return False
    return True


def _chunk(row: Sequence[Any]) -> Chunk:
    keys = ("id", "page_id", "n", "source", "component", "package", "version", "url", "title",
            "heading", "text", "hash", "updated")  # fmt: skip
    return Chunk.model_validate(dict(zip(keys, row, strict=False)))


def _page(row: Sequence[Any]) -> PageRecord:
    keys = [k.strip() for k in _PAGE_COLS.split(",")]
    return PageRecord.model_validate(dict(zip(keys, row, strict=True)))


def _where(filters: Filters | None, alias: str = "c") -> tuple[str, list[Any]]:
    parts: list[str] = []
    args: list[Any] = []
    for key, values in clean_filters(filters).items():
        parts.append(f"{alias}.{key} IN ({','.join('?' * len(values))})")
        args.extend(values)
    return (" AND ".join(parts), args)


class LocalStore:
    kind = "local"

    def __init__(self, path: Path, *, shared: bool = False) -> None:
        self.path = path
        self.shared = shared
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self.fts = _fts5_available()
        self._vec_cache: tuple[str, list[int], Any] | None = None
        self._bm25_cache: tuple[str, Any, list[int]] | None = None

    def identity(self) -> str:
        where = str(self.path.resolve())
        return f"local:{where}" if self.shared else f"local:{socket.gethostname()}:{where}"

    def location(self) -> str:
        return str(self.path)

    def exists(self) -> bool:
        """The index file exists (read-only callers do not create it)."""
        return self._db is not None or self.path.exists()

    def open(self) -> None:
        with self._lock:
            if self._db is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(_SCHEMA)
            if self.fts:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                    "terms, title_terms, content='', contentless_delete=1)"
                    if sqlite3.sqlite_version_info >= (3, 43, 0)
                    else "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                    "terms, title_terms)"
                )
            db.commit()
            self._db = db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.open()
        assert self._db is not None
        return self._db

    def _bump(self) -> None:
        self.db.execute(
            "INSERT INTO meta VALUES ('generation', '1') ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(value AS INTEGER) + 1"
        )

    def _generation(self) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        return str(row[0]) if row else "0"

    # -- pages ----------------------------------------------------------------------------

    def pages(self, source: str | None = None) -> dict[str, PageRecord]:
        with self._lock:
            sql = f"SELECT {_PAGE_COLS} FROM pages"  # noqa: S608
            rows = (
                self.db.execute(sql + " WHERE source = ?", (source,)).fetchall()
                if source
                else self.db.execute(sql).fetchall()
            )
        return {r[0]: _page(r) for r in rows}

    def _delete_chunks(self, page_ids: Sequence[str]) -> None:
        db = self.db
        for start in range(0, len(page_ids), 500):
            batch = list(page_ids[start : start + 500])
            marks = ",".join("?" * len(batch))
            rids = [
                r[0]
                for r in db.execute(
                    f"SELECT rid FROM chunks WHERE page_id IN ({marks})",  # noqa: S608
                    batch,
                )
            ]
            for r_start in range(0, len(rids), 500):
                rb = rids[r_start : r_start + 500]
                rmarks = ",".join("?" * len(rb))
                if self.fts:
                    db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({rmarks})", rb)  # noqa: S608
                db.execute(f"DELETE FROM vectors WHERE rid IN ({rmarks})", rb)  # noqa: S608
                db.execute(f"DELETE FROM chunks WHERE rid IN ({rmarks})", rb)  # noqa: S608

    def put_page(
        self,
        page: PageMeta,
        body: bytes,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]] | None,
        model: str | None,
    ) -> None:
        with self._lock:
            db = self.db
            try:
                self._delete_chunks([page.id])
                db.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        page.id,
                        page.source,
                        page.unit,
                        page.component,
                        page.package,
                        page.version,
                        page.url,
                        page.title,
                        page.updated,
                        page.content_hash,
                        page.etag,
                        page.last_modified,
                        page.fetched_at,
                        len(chunks),
                        body,
                    ),
                )
                use_vectors = vectors is not None and model and model == self.vector_model()
                for i, c in enumerate(chunks):
                    terms = index_terms(f"{c.heading}\n{c.text}")
                    title_terms = index_terms(f"{c.title} {c.heading}")
                    cur = db.execute(
                        "INSERT INTO chunks (id, page_id, n, source, component, package, "
                        "version, url, title, heading, text, hash, updated, terms, title_terms) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            c.id,
                            c.page_id,
                            c.n,
                            c.source,
                            c.component,
                            c.package,
                            c.version,
                            c.url,
                            c.title,
                            c.heading,
                            c.text,
                            c.hash,
                            c.updated,
                            terms,
                            title_terms,
                        ),
                    )
                    rid = cur.lastrowid
                    if self.fts:
                        db.execute(
                            "INSERT INTO chunks_fts (rowid, terms, title_terms) VALUES (?,?,?)",
                            (rid, terms, title_terms),
                        )
                    if use_vectors and vectors is not None:
                        db.execute(
                            "INSERT INTO vectors VALUES (?, ?)",
                            (rid, array.array("f", normalise(vectors[i])).tobytes()),
                        )
                self._bump()
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def flush(self) -> None:
        """Writes are committed immediately; nothing is buffered."""

    def touch_page(self, page: PageMeta) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE pages SET etag=?, last_modified=?, fetched_at=? WHERE id=?",
                (page.etag, page.last_modified, page.fetched_at, page.id),
            )
            self.db.commit()

    def delete_pages(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        with self._lock:
            db = self.db
            self._delete_chunks(list(ids))
            count = 0
            for start in range(0, len(ids), 500):
                batch = list(ids[start : start + 500])
                marks = ",".join("?" * len(batch))
                count += db.execute(
                    f"DELETE FROM pages WHERE id IN ({marks})",  # noqa: S608
                    batch,
                ).rowcount
            self._bump()
            db.commit()
            return count

    def clear(self, source: str | None = None) -> None:
        with self._lock:
            if source:
                ids = list(self.pages(source))
                self.delete_pages(ids)
                return
            db = self.db
            db.execute("DELETE FROM pages")
            db.execute("DELETE FROM chunks")
            db.execute("DELETE FROM vectors")
            if self.fts:
                db.execute("DELETE FROM chunks_fts")
            db.execute("DELETE FROM meta WHERE key LIKE 'unit:%'")
            self._bump()
            db.commit()

    def iter_bodies(self, source: str | None = None) -> Iterator[tuple[PageRecord, bytes]]:
        ids = sorted(self.pages(source))
        for start in range(0, len(ids), 200):
            batch = ids[start : start + 200]
            marks = ",".join("?" * len(batch))
            with self._lock:
                rows = self.db.execute(
                    f"SELECT {_PAGE_COLS}, body FROM pages WHERE id IN ({marks})",  # noqa: S608
                    batch,
                ).fetchall()
            for row in rows:
                yield _page(row[:-1]), bytes(row[-1] or b"")

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self.db.commit()

    # -- vectors --------------------------------------------------------------------------

    def vector_model(self) -> str | None:
        return self.get_meta("vector_model")

    def reset_vectors(self, model: str | None) -> None:
        with self._lock:
            self.db.execute("DELETE FROM vectors")
            self._bump()
            self.db.commit()
            self.set_meta("vector_model", model)

    def missing_vectors(self, limit: int) -> list[Chunk]:
        with self._lock:
            rows = self.db.execute(
                f"SELECT {_CHUNK_COLS} FROM chunks c LEFT JOIN vectors v ON v.rid = c.rid "  # noqa: S608
                "WHERE v.rid IS NULL ORDER BY c.rid LIMIT ?",
                (limit,),
            ).fetchall()
        return [_chunk(r) for r in rows]

    def put_vectors(self, items: Mapping[str, Sequence[float]]) -> None:
        if not items:
            return
        with self._lock:
            db = self.db
            for chunk_id, vec in items.items():
                row = db.execute("SELECT rid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                if row is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                        (row[0], array.array("f", normalise(vec)).tobytes()),
                    )
            self._bump()
            db.commit()

    # -- retrieval ------------------------------------------------------------------------

    def _chunks_by_rid(self, rids: Sequence[int]) -> dict[int, Chunk]:
        if not rids:
            return {}
        marks = ",".join("?" * len(rids))
        rows = self.db.execute(
            f"SELECT c.rid, {_CHUNK_COLS} FROM chunks c WHERE c.rid IN ({marks})",  # noqa: S608
            list(rids),
        ).fetchall()
        return {r[0]: _chunk(r[1:]) for r in rows}

    def lexical(
        self, tokens: Sequence[str], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]:
        terms = [t for t in tokens if t.isalnum()][:64]
        if not terms:
            return []
        with self._lock:
            if not self.fts:
                return self._lexical_python(terms, k, filters)
            where, args = _where(filters)
            match = " OR ".join(f'"{t}"' for t in terms)
            sql = (
                "SELECT c.rid, bm25(chunks_fts, 1.0, 3.0) AS score FROM chunks_fts "  # noqa: S608
                "JOIN chunks c ON c.rid = chunks_fts.rowid WHERE chunks_fts MATCH ?"
                + (f" AND {where}" if where else "")
                + " ORDER BY score LIMIT ?"
            )
            rows = self.db.execute(sql, [match, *args, k]).fetchall()
            found = self._chunks_by_rid([r[0] for r in rows])
        # bm25() is "lower is better" (negative): report positive scores.
        return [(found[r[0]], -float(r[1])) for r in rows if r[0] in found]

    def _lexical_python(
        self, terms: Sequence[str], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]:
        from autoair.ai.rag import BM25Index, Document

        generation = self._generation()
        cache = self._bm25_cache
        if cache is None or cache[0] != generation:
            rows = self.db.execute("SELECT rid, title_terms, terms FROM chunks").fetchall()
            docs = [
                Document(id=str(r[0]), kind="doc", title=r[1] or "", text=r[2] or "") for r in rows
            ]
            cache = (generation, BM25Index(docs), [int(r[0]) for r in rows])
            self._bm25_cache = cache
        index, rids = cache[1], cache[2]
        ranked = index.search(" ".join(terms), max(k * 5, k))
        found = self._chunks_by_rid([rids[i] for i, _ in ranked])
        allowed = clean_filters(filters)
        out: list[tuple[Chunk, float]] = []
        for i, score in ranked:
            chunk = found.get(rids[i])
            if chunk is None:
                continue
            if any(getattr(chunk, key) not in vals for key, vals in allowed.items()):
                continue
            out.append((chunk, score))
        return out[:k]

    def _vectors(self) -> tuple[list[int], Any]:
        generation = self._generation()
        cache = self._vec_cache
        if cache is not None and cache[0] == generation:
            return cache[1], cache[2]
        rows = self.db.execute("SELECT rid, vec FROM vectors ORDER BY rid").fetchall()
        rids = [int(r[0]) for r in rows]
        matrix: Any
        try:
            import numpy as np  # type: ignore[import-not-found,unused-ignore]

            matrix = (
                np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows else None
            )
        except ImportError:
            matrix = []
            for r in rows:
                vec = array.array("f")
                vec.frombytes(r[1])
                matrix.append(vec)
        self._vec_cache = (generation, rids, matrix)
        return rids, matrix

    def dense(
        self, vector: Sequence[float], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]:
        with self._lock:
            rids, matrix = self._vectors()
            if not rids or matrix is None:
                return []
            q = normalise(vector)
            allowed_rids: set[int] | None = None
            where, args = _where(filters)
            if where:
                allowed_rids = {
                    int(r[0])
                    for r in self.db.execute(
                        f"SELECT c.rid FROM chunks c WHERE {where}",  # noqa: S608
                        args,
                    )
                }
            if isinstance(matrix, list):
                import operator

                if len(matrix[0]) != len(q):
                    return []

                scores = [math.fsum(map(operator.mul, row, q)) for row in matrix]
            else:
                import numpy as np  # type: ignore[import-not-found,unused-ignore]

                if matrix.shape[1] != len(q):
                    return []
                scores = list(matrix @ np.asarray(q, dtype=np.float32))
            order = sorted(range(len(rids)), key=lambda i: -scores[i])
            picked: list[tuple[int, float]] = []
            for i in order:
                if allowed_rids is not None and rids[i] not in allowed_rids:
                    continue
                picked.append((rids[i], float(scores[i])))
                if len(picked) >= k:
                    break
            found = self._chunks_by_rid([r for r, _ in picked])
        return [(found[r], s) for r, s in picked if r in found]

    def stats(self) -> StoreStats:
        with self._lock:
            db = self.db
            pages = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            vectors = db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            by_component = dict(
                db.execute("SELECT component, COUNT(*) FROM pages GROUP BY component").fetchall()
            )
            by_source = dict(
                db.execute("SELECT source, COUNT(*) FROM pages GROUP BY source").fetchall()
            )
            versions: dict[str, list[str]] = {}
            for comp, version in db.execute(
                "SELECT DISTINCT component, version FROM pages ORDER BY component, version"
            ).fetchall():
                versions.setdefault(comp, []).append(version)
        for comp in list(versions):
            versions[comp] = versions[comp][:50]
        return StoreStats(
            kind=self.kind,
            location=self.location(),
            pages=int(pages),
            chunks=int(chunks),
            vectors=int(vectors),
            embedding_model=self.vector_model(),
            by_component={str(k): int(v) for k, v in by_component.items()},
            by_source={str(k): int(v) for k, v in by_source.items()},
            versions=json.loads(json.dumps(versions)),
        )


__all__ = ["LocalStore"]
