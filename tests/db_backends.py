"""State backends exercised by the persistence tests: file and SQLite always, PostgreSQL
when ``AUTOAIR_TEST_POSTGRES_URL`` is set (e.g. ``postgresql://postgres:x@localhost:55432/
postgres``). Every PostgreSQL test gets its own table prefix and drops its tables after."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

PG_URL = os.environ.get("AUTOAIR_TEST_POSTGRES_URL")

BACKENDS = [
    pytest.param("file", id="file"),
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "postgresql",
        id="postgresql",
        marks=[
            pytest.mark.postgres,
            pytest.mark.skipif(not PG_URL, reason="AUTOAIR_TEST_POSTGRES_URL is not set"),
        ],
    ),
]
SQL_BACKENDS = BACKENDS[1:]


def database_settings(backend: str, tmp_path: Path, prefix: str | None = None) -> dict[str, Any]:
    if backend == "file":
        return {"backend": "file"}
    if backend == "sqlite":
        return {"backend": "sqlite", "url": f"sqlite:///{tmp_path / 'state.db'}"}
    return {
        "backend": "postgresql",
        "url": PG_URL,
        "table_prefix": prefix or f"t{uuid.uuid4().hex[:10]}_",
        "pool_size": 2,
    }


def drop_tables(settings: Any) -> None:
    """Drop the tables of a PostgreSQL test prefix and forget cached engines."""
    from autoair.persistence.db import database_for, reset_databases

    if settings.database.backend == "postgresql":
        db = database_for(settings)
        if db is not None:
            with db.begin() as conn:
                db.tables.metadata.drop_all(conn)
    reset_databases()


def cleanup(settings: Any) -> Iterator[None]:
    try:
        yield
    finally:
        drop_tables(settings)
