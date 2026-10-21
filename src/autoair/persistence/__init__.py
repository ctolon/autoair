"""AutoAir's persistence layer: pluggable state backends (``database.backend``).

* ``file`` (default): JSON documents and JSON-lines logs on fsspec URLs;
* ``sqlite`` / ``postgresql``: SQLAlchemy 2 Core (``autoair[sqlite]`` / ``autoair[postgres]``).

The domain modules ask the factories below for their repositories; nothing outside
:mod:`autoair.persistence.sql` / :mod:`autoair.persistence.db` imports SQLAlchemy, so the
file backend works without it.
"""

from __future__ import annotations

from typing import Any

from autoair.persistence.config import DatabaseSettings
from autoair.persistence.documents import (
    ANY,
    DocumentConflict,
    DocumentStore,
    FileDocumentStore,
    StoredDocument,
)


def is_sql(settings: Any) -> bool:
    db = getattr(settings, "database", None)
    return isinstance(db, DatabaseSettings) and db.is_sql


def document_store(
    settings: Any,
    collection: str,
    *,
    file_url: str,
    suffix: str = ".json",
    storage_options: dict[str, Any] | None = None,
) -> DocumentStore:
    """The documents of ``collection``: rows of the SQL database when one is configured,
    else files ``<file_url>/<key><suffix>``."""
    if is_sql(settings):
        from autoair.persistence.db import database_for
        from autoair.persistence.sql import SqlDocumentStore

        db = database_for(settings)
        assert db is not None
        return SqlDocumentStore(db, collection)
    return FileDocumentStore(file_url, suffix=suffix, storage_options=storage_options)


__all__ = [
    "ANY",
    "DatabaseSettings",
    "DocumentConflict",
    "DocumentStore",
    "FileDocumentStore",
    "StoredDocument",
    "document_store",
    "is_sql",
]
