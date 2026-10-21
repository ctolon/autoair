"""pgvector store: the knowledge base in PostgreSQL (``autoair[kb-pgvector]``).

* lexical ranking: a ``tsvector`` of AutoAir's identifier-aware tokens (``simple``
  configuration; titles weighted ``A``) with a GIN index, ranked by ``ts_rank_cd``;
* dense ranking: a ``vector(n)`` column (created with the first embeddings, its
  dimension follows the model) with an HNSW cosine index, ranked by ``<=>``;
* the database is AutoAir's own (``database.backend=postgresql``) unless
  ``kb.pgvector.url`` / ``conn_id`` point elsewhere.

psycopg (libpq) has its own network stack: TLS is ``kb.pgvector.ssl_mode`` /
``ssl_root_cert`` (``outbound.*`` does not apply).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from autoair.kb.config import KbPgvectorSettings
from autoair.kb.models import Chunk, PageMeta, PageRecord, StoreStats
from autoair.kb.stores.base import Filters, StoreError, clean_filters, index_terms, normalise

_PAGE_FIELDS = (
    "id", "source", "unit", "component", "package", "version", "url", "title", "updated",
    "content_hash", "etag", "last_modified", "fetched_at", "chunks",
)  # fmt: skip
_CHUNK_FIELDS = (
    "id", "page_id", "n", "source", "component", "package", "version", "url", "title",
    "heading", "text", "hash", "updated",
)  # fmt: skip


def _vec(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in normalise(values)) + "]"


def masked(url: str) -> str:
    return re.sub(r"(?<=://)[^/@]+@", "***@", url)


class PgvectorStore:
    kind = "pgvector"

    def __init__(self, url: str, settings: KbPgvectorSettings, *, engine: Any = None) -> None:
        self.settings = settings
        self.url = url
        self._engine = engine
        schema = settings.schema_name
        p = settings.table_prefix

        def q(name: str) -> str:
            return f'"{schema}"."{name}"' if schema else f'"{name}"'

        self.t_pages, self.t_chunks, self.t_meta = q(p + "pages"), q(p + "chunks"), q(p + "meta")
        self._idx = p

    # -- connection -----------------------------------------------------------------------

    @property
    def engine(self) -> Any:
        if self._engine is None:
            try:
                from sqlalchemy import create_engine
                from sqlalchemy.engine import make_url
            except ImportError as exc:
                raise StoreError("kb.store=pgvector needs autoair[kb-pgvector]") from exc
            url = make_url(self.url)
            if url.drivername in ("postgresql", "postgres"):
                url = url.set(drivername="postgresql+psycopg")
            connect_args: dict[str, Any] = {}
            if self.settings.ssl_mode:
                connect_args["sslmode"] = self.settings.ssl_mode
            if self.settings.ssl_root_cert:
                connect_args["sslrootcert"] = self.settings.ssl_root_cert
            try:
                self._engine = create_engine(
                    url,
                    pool_size=self.settings.pool_size,
                    pool_pre_ping=True,
                    connect_args=connect_args,
                )
            except ImportError as exc:
                raise StoreError("the psycopg driver is missing: autoair[kb-pgvector]") from exc
        return self._engine

    def _run(self, sql: str, params: Mapping[str, Any] | None = None) -> list[Any]:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        try:
            with self.engine.begin() as conn:
                result = conn.execute(text(sql), dict(params or {}))
                return list(result.fetchall()) if result.returns_rows else []
        except SQLAlchemyError as exc:
            raise StoreError(f"pgvector: {type(exc).__name__}: {str(exc).splitlines()[0]}") from exc

    def identity(self) -> str:
        return f"pgvector:{masked(self.url)}:{self.t_chunks}"

    def location(self) -> str:
        return f"{masked(self.url)} ({self.t_chunks})"

    def open(self) -> None:
        if self.settings.create_extension:
            self._run("CREATE EXTENSION IF NOT EXISTS vector")
        if self.settings.schema_name:
            self._run(f'CREATE SCHEMA IF NOT EXISTS "{self.settings.schema_name}"')
        p = self._idx
        self._run(f"CREATE TABLE IF NOT EXISTS {self.t_meta} (key TEXT PRIMARY KEY, value TEXT)")
        self._run(
            f"CREATE TABLE IF NOT EXISTS {self.t_pages} (id TEXT PRIMARY KEY, source TEXT, "
            "unit TEXT, component TEXT, package TEXT, version TEXT, url TEXT, title TEXT, "
            "updated TEXT, content_hash TEXT, etag TEXT, last_modified TEXT, fetched_at TEXT, "
            "chunks INTEGER, body BYTEA)"
        )
        self._run(f"CREATE INDEX IF NOT EXISTS {p}pages_source ON {self.t_pages} (source)")
        self._run(
            f"CREATE TABLE IF NOT EXISTS {self.t_chunks} (id TEXT PRIMARY KEY, page_id TEXT "
            f"REFERENCES {self.t_pages}(id) ON DELETE CASCADE, n INTEGER, source TEXT, "
            "component TEXT, package TEXT, version TEXT, url TEXT, title TEXT, heading TEXT, "
            "text TEXT, hash TEXT, updated TEXT, tsv TSVECTOR, embedding vector)"
        )
        self._run(f"CREATE INDEX IF NOT EXISTS {p}chunks_page ON {self.t_chunks} (page_id)")
        self._run(f"CREATE INDEX IF NOT EXISTS {p}chunks_tsv ON {self.t_chunks} USING GIN (tsv)")
        self._run(
            f"CREATE INDEX IF NOT EXISTS {p}chunks_filter ON {self.t_chunks} "
            "(component, package, version)"
        )

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # -- pages ----------------------------------------------------------------------------

    def pages(self, source: str | None = None) -> dict[str, PageRecord]:
        cols = ", ".join(_PAGE_FIELDS)
        rows = self._run(
            f"SELECT {cols} FROM {self.t_pages}"  # noqa: S608
            + (" WHERE source = :s" if source else ""),
            {"s": source},
        )
        return {
            r[0]: PageRecord.model_validate(dict(zip(_PAGE_FIELDS, r, strict=True))) for r in rows
        }

    def put_page(
        self,
        page: PageMeta,
        body: bytes,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]] | None,
        model: str | None,
    ) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        use_vectors = vectors is not None and model is not None and model == self.vector_model()
        dim = self.get_meta("vector_dim")
        try:
            with self.engine.begin() as conn:
                conn.execute(text(f"DELETE FROM {self.t_pages} WHERE id = :id"), {"id": page.id})  # noqa: S608
                conn.execute(
                    text(
                        f"INSERT INTO {self.t_pages} VALUES (:id, :source, :unit, :component, "  # noqa: S608
                        ":package, :version, :url, :title, :updated, :content_hash, :etag, "
                        ":last_modified, :fetched_at, :chunks, :body)"
                    ),
                    {
                        **page.model_dump(include=set(_PAGE_FIELDS)),
                        "chunks": len(chunks),
                        "body": body,
                    },
                )
                rows = []
                for i, c in enumerate(chunks):
                    vec = None
                    if use_vectors and vectors is not None:
                        if dim is None:
                            dim = str(len(vectors[i]))
                            self._set_dim(conn, int(dim))
                        vec = _vec(vectors[i])
                    rows.append(
                        {
                            **c.model_dump(include=set(_CHUNK_FIELDS)),
                            "terms": index_terms(f"{c.heading}\n{c.text}"),
                            "title_terms": index_terms(f"{c.title} {c.heading}"),
                            "embedding": vec,
                        }
                    )
                if rows:
                    conn.execute(
                        text(
                            f"INSERT INTO {self.t_chunks} (id, page_id, n, source, component, "  # noqa: S608
                            "package, version, url, title, heading, text, hash, updated, tsv, "
                            "embedding) VALUES (:id, :page_id, :n, :source, :component, "
                            ":package, :version, :url, :title, :heading, :text, :hash, :updated, "
                            "setweight(to_tsvector('simple', :title_terms), 'A') || "
                            "to_tsvector('simple', :terms), CAST(:embedding AS vector))"
                        ),
                        rows,
                    )
        except SQLAlchemyError as exc:
            raise StoreError(f"pgvector: {type(exc).__name__}: {str(exc).splitlines()[0]}") from exc

    def _set_dim(self, conn: Any, dim: int) -> None:
        from sqlalchemy import text

        p = self._idx
        conn.execute(
            text(f"ALTER TABLE {self.t_chunks} ALTER COLUMN embedding TYPE vector({int(dim)})")
        )
        conn.execute(
            text(
                f"INSERT INTO {self.t_meta} VALUES ('vector_dim', :v) ON CONFLICT (key) "  # noqa: S608
                "DO UPDATE SET value = EXCLUDED.value"
            ),
            {"v": str(dim)},
        )
        if self.settings.hnsw_index:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {p}chunks_hnsw ON {self.t_chunks} "
                    "USING hnsw (embedding vector_cosine_ops)"
                )
            )

    def flush(self) -> None:
        """Writes are committed immediately; nothing is buffered."""

    def touch_page(self, page: PageMeta) -> None:
        self._run(
            f"UPDATE {self.t_pages} SET etag = :etag, last_modified = :lm, fetched_at = :f "  # noqa: S608
            "WHERE id = :id",
            {"etag": page.etag, "lm": page.last_modified, "f": page.fetched_at, "id": page.id},
        )

    def delete_pages(self, ids: Sequence[str]) -> int:
        count = 0
        for start in range(0, len(ids), 500):
            rows = self._run(
                f"DELETE FROM {self.t_pages} WHERE id = ANY(:ids) RETURNING id",  # noqa: S608
                {"ids": list(ids[start : start + 500])},
            )
            count += len(rows)
        return count

    def clear(self, source: str | None = None) -> None:
        if source:
            self._run(f"DELETE FROM {self.t_pages} WHERE source = :s", {"s": source})  # noqa: S608
            return
        self._run(f"DELETE FROM {self.t_pages}")  # noqa: S608
        self._run(f"DELETE FROM {self.t_meta} WHERE key LIKE 'unit:%'")  # noqa: S608

    def iter_bodies(self, source: str | None = None) -> Iterator[tuple[PageRecord, bytes]]:
        ids = sorted(self.pages(source))
        cols = ", ".join(_PAGE_FIELDS)
        for start in range(0, len(ids), 200):
            rows = self._run(
                f"SELECT {cols}, body FROM {self.t_pages} WHERE id = ANY(:ids) ORDER BY id",  # noqa: S608
                {"ids": ids[start : start + 200]},
            )
            for r in rows:
                record = PageRecord.model_validate(dict(zip(_PAGE_FIELDS, r[:-1], strict=True)))
                yield record, bytes(r[-1] or b"")

    def get_meta(self, key: str) -> str | None:
        rows = self._run(f"SELECT value FROM {self.t_meta} WHERE key = :k", {"k": key})  # noqa: S608
        return str(rows[0][0]) if rows and rows[0][0] is not None else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._run(f"DELETE FROM {self.t_meta} WHERE key = :k", {"k": key})  # noqa: S608
        else:
            self._run(
                f"INSERT INTO {self.t_meta} VALUES (:k, :v) ON CONFLICT (key) DO UPDATE SET "  # noqa: S608
                "value = EXCLUDED.value",
                {"k": key, "v": value},
            )

    # -- vectors --------------------------------------------------------------------------

    def vector_model(self) -> str | None:
        return self.get_meta("vector_model")

    def reset_vectors(self, model: str | None) -> None:
        self._run(f"DROP INDEX IF EXISTS {self._idx}chunks_hnsw")
        self._run(f"UPDATE {self.t_chunks} SET embedding = NULL")  # noqa: S608
        self._run(f"ALTER TABLE {self.t_chunks} ALTER COLUMN embedding TYPE vector")
        self.set_meta("vector_dim", None)
        self.set_meta("vector_model", model)

    def missing_vectors(self, limit: int) -> list[Chunk]:
        cols = ", ".join(_CHUNK_FIELDS)
        rows = self._run(
            f"SELECT {cols} FROM {self.t_chunks} WHERE embedding IS NULL ORDER BY id LIMIT :n",  # noqa: S608
            {"n": limit},
        )
        return [Chunk.model_validate(dict(zip(_CHUNK_FIELDS, r, strict=True))) for r in rows]

    def put_vectors(self, items: Mapping[str, Sequence[float]]) -> None:
        if not items:
            return
        from sqlalchemy import text

        with self.engine.begin() as conn:
            if self.get_meta("vector_dim") is None:
                self._set_dim(conn, len(next(iter(items.values()))))
            conn.execute(
                text(
                    f"UPDATE {self.t_chunks} SET embedding = CAST(:v AS vector) WHERE id = :id"  # noqa: S608
                ),
                [{"id": k, "v": _vec(v)} for k, v in items.items()],
            )

    # -- retrieval ------------------------------------------------------------------------

    def _filters(self, filters: Filters | None) -> tuple[str, dict[str, Any]]:
        parts: list[str] = []
        params: dict[str, Any] = {}
        for key, values in clean_filters(filters).items():
            parts.append(f"{key} = ANY(:f_{key})")
            params[f"f_{key}"] = values
        return (" AND " + " AND ".join(parts)) if parts else "", params

    def _chunks(self, rows: list[Any]) -> list[tuple[Chunk, float]]:
        return [
            (Chunk.model_validate(dict(zip(_CHUNK_FIELDS, r[:-1], strict=True))), float(r[-1]))
            for r in rows
        ]

    def lexical(
        self, tokens: Sequence[str], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]:
        terms = [t for t in tokens if t.isalnum()][:64]
        if not terms:
            return []
        where, params = self._filters(filters)
        cols = ", ".join(_CHUNK_FIELDS)
        rows = self._run(
            f"SELECT {cols}, ts_rank_cd(tsv, q) AS score FROM {self.t_chunks}, "  # noqa: S608
            f"to_tsquery('simple', :q) q WHERE tsv @@ q{where} ORDER BY score DESC LIMIT :k",
            {"q": " | ".join(terms), "k": k, **params},
        )
        return self._chunks(rows)

    def dense(
        self, vector: Sequence[float], k: int, filters: Filters | None
    ) -> list[tuple[Chunk, float]]:
        dim = self.get_meta("vector_dim")
        if dim is None or int(dim) != len(vector):
            return []
        where, params = self._filters(filters)
        cols = ", ".join(_CHUNK_FIELDS)
        rows = self._run(
            f"SELECT {cols}, 1 - (embedding <=> CAST(:v AS vector)) AS score FROM "  # noqa: S608
            f"{self.t_chunks} WHERE embedding IS NOT NULL{where} "
            "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :k",
            {"v": _vec(vector), "k": k, **params},
        )
        return self._chunks(rows)

    def stats(self) -> StoreStats:
        pages = self._run(f"SELECT COUNT(*) FROM {self.t_pages}")[0][0]  # noqa: S608
        chunks, vectors = self._run(
            f"SELECT COUNT(*), COUNT(embedding) FROM {self.t_chunks}"  # noqa: S608
        )[0]
        by_component = self._run(
            f"SELECT component, COUNT(*) FROM {self.t_pages} GROUP BY component"  # noqa: S608
        )
        by_source = self._run(f"SELECT source, COUNT(*) FROM {self.t_pages} GROUP BY source")  # noqa: S608
        versions: dict[str, list[str]] = {}
        for comp, version in self._run(
            f"SELECT DISTINCT component, version FROM {self.t_pages} "  # noqa: S608
            "ORDER BY component, version"
        ):
            versions.setdefault(str(comp), []).append(str(version))
        return StoreStats(
            kind=self.kind,
            location=self.location(),
            pages=int(pages),
            chunks=int(chunks),
            vectors=int(vectors),
            embedding_model=self.vector_model(),
            by_component={str(c): int(n) for c, n in by_component},
            by_source={str(s): int(n) for s, n in by_source},
            versions={c: v[:50] for c, v in versions.items()},
        )


__all__ = ["PgvectorStore", "masked"]
