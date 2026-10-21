"""A small versioned schema migrator (``autoair db upgrade`` / ``autoair db current``).

Why not Alembic: AutoAir owns a handful of tables, often runs *inside* Airflow's process
(whose own Alembic environment and ``alembic_version`` table must not be touched), and must
work with only SQLAlchemy installed. Each migration is a numbered Python function applied in
one transaction; the applied numbers are rows of ``<prefix>schema_version``. Concurrent
upgrades (several replicas starting at once) are serialised by a PostgreSQL advisory lock
(SQLite: the write transaction itself).

Rules for new migrations: append (never edit) an entry to :data:`MIGRATIONS`; use
``checkfirst``/``IF NOT EXISTS`` style operations so a half-applied manual change is
tolerated.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Connection  # top-level export only from SQLAlchemy 2.0

if TYPE_CHECKING:
    from autoair.persistence.db import Database


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[Connection, Database], None]


def _initial(conn: Connection, db: Database) -> None:
    t = db.tables
    db.tables.metadata.create_all(
        conn,
        tables=[
            t.documents,
            t.audit,
            t.ai_audit,
            t.ai_memory,
            t.reviews,
            t.proposals,
            t.login_failures,
            t.replicas,
            t.collab_updates,
        ],
        checkfirst=True,
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "initial schema: documents, audit, AI audit/memory, reviews, proposals, "
        "login failures, replicas, collab updates",
        _initial,
    ),
)
HEAD = MIGRATIONS[-1].version


class MigrationError(RuntimeError):
    pass


def _has_version_table(conn: Connection, db: Database) -> bool:
    return bool(
        inspect(conn).has_table(db.tables.schema_version.name, schema=db.settings.schema_name)
    )


def current(db: Database) -> int | None:
    """The applied schema version (``None``: nothing applied yet)."""
    with db.engine.connect() as conn:
        if not _has_version_table(conn, db):
            return None
        value = conn.execute(select(func.max(db.tables.schema_version.c.version))).scalar()
        return int(value) if value is not None else None


def pending(db: Database) -> list[Migration]:
    applied = current(db) or 0
    return [m for m in MIGRATIONS if m.version > applied]


def upgrade(db: Database, target: int | None = None) -> list[int]:
    """Apply pending migrations up to ``target`` (default: the latest); returns the versions
    applied by this call."""
    goal = HEAD if target is None else target
    if goal > HEAD:
        raise MigrationError(f"unknown schema version {goal} (latest is {HEAD})")
    done: list[int] = []
    with db.engine.begin() as conn:
        db.lock(conn, "schema-migrations")
        if db.dialect == "postgresql" and db.settings.schema_name:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{db.settings.schema_name}"'))
        db.tables.metadata.create_all(conn, tables=[db.tables.schema_version], checkfirst=True)
        version_table = db.tables.schema_version
        applied = conn.execute(select(func.max(version_table.c.version))).scalar() or 0
        for migration in MIGRATIONS:
            if migration.version <= applied or migration.version > goal:
                continue
            migration.apply(conn, db)
            conn.execute(
                version_table.insert().values(
                    version=migration.version,
                    description=migration.description[:200],
                    applied_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
            )
            done.append(migration.version)
    return done


def history(db: Database) -> list[dict[str, object]]:
    """Applied migrations (version, description, applied_at), oldest first."""
    with db.engine.connect() as conn:
        if not _has_version_table(conn, db):
            return []
        rows = conn.execute(
            select(db.tables.schema_version).order_by(db.tables.schema_version.c.version)
        ).mappings()
        return [dict(r) for r in rows]


__all__ = [
    "HEAD",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "current",
    "history",
    "pending",
    "upgrade",
]
