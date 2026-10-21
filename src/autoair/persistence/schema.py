"""SQL schema of AutoAir's state (SQLAlchemy Core tables).

Every table name starts with ``database.table_prefix`` and lives in ``database.schema_name``
(PostgreSQL) so AutoAir can share a database with other applications. Timestamps written by
the application are ISO 8601 UTC strings (they sort lexicographically and round-trip the
exact values the file backend stores); ``*_at`` floats are UNIX times used for windows.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)

# Autoincrement ids: BIGINT on PostgreSQL, INTEGER (rowid alias) on SQLite.
_ID = BigInteger().with_variant(Integer(), "sqlite")


@dataclass(frozen=True)
class Tables:
    metadata: MetaData
    schema_version: Table
    documents: Table
    audit: Table
    ai_audit: Table
    ai_memory: Table
    reviews: Table
    proposals: Table
    login_failures: Table
    replicas: Table
    collab_updates: Table

    def all(self) -> list[Table]:
        return [t for t in self.metadata.sorted_tables]


def build_tables(prefix: str = "autoair_", schema: str | None = None) -> Tables:
    md = MetaData(schema=schema)
    p = prefix

    schema_version = Table(
        f"{p}schema_version",
        md,
        Column("version", Integer, primary_key=True, autoincrement=False),
        Column("description", String(200), nullable=False),
        Column("applied_at", String(40), nullable=False),
    )
    # Small JSON / binary documents addressed by (collection, key) with an optimistic
    # version: templates, icon images + assignments, knowledge overrides, per-user LLM
    # settings, the deploy-target choice, collab snapshots, shared secrets, leases.
    documents = Table(
        f"{p}documents",
        md,
        Column("collection", String(64), nullable=False),
        Column("key", String(255), nullable=False),
        Column("data", LargeBinary, nullable=False),
        Column("version", Integer, nullable=False),
        Column("updated_at", String(40), nullable=False),
        PrimaryKeyConstraint("collection", "key", name=f"{p}documents_pk"),
    )
    audit = Table(
        f"{p}audit",
        md,
        Column("id", _ID, primary_key=True, autoincrement=True),
        Column("timestamp", String(40), nullable=False),
        Column("action", String(100), nullable=False),
        Column("user", String(255)),
        Column("dag_id", String(250)),
        Column("details", Text, nullable=False),
        Index(f"{p}audit_ts", "timestamp"),
        Index(f"{p}audit_dag", "dag_id", "timestamp"),
        Index(f"{p}audit_user", "user", "timestamp"),
        Index(f"{p}audit_action", "action", "timestamp"),
    )
    ai_audit = Table(
        f"{p}ai_audit",
        md,
        Column("id", _ID, primary_key=True, autoincrement=True),
        Column("timestamp", String(40), nullable=False),
        Column("user", String(255)),
        Column("feature", String(100), nullable=False),
        Column("provider", String(100)),
        Column("model", String(200)),
        Column("outcome", String(50), nullable=False),
        Column("input_tokens", BigInteger, nullable=False, default=0),
        Column("output_tokens", BigInteger, nullable=False, default=0),
        Column("cost_usd", Float),
        Column("record", Text, nullable=False),
        Index(f"{p}ai_audit_ts", "timestamp"),
        Index(f"{p}ai_audit_user", "user", "timestamp"),
    )
    ai_memory = Table(
        f"{p}ai_memory",
        md,
        Column("id", String(32), primary_key=True),
        Column("scope", String(16), nullable=False),
        Column("owner", String(255), nullable=False, default=""),
        Column("text_hash", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("item", Text, nullable=False),
        # Adding the same text twice to the same scope/owner is idempotent.
        UniqueConstraint("scope", "owner", "text_hash", name=f"{p}ai_memory_text_uq"),
    )
    reviews = Table(
        f"{p}reviews",
        md,
        Column("id", _ID, primary_key=True, autoincrement=True),
        Column("dag_id", String(250), nullable=False),
        Column("etag", String(128), nullable=False),
        Column("user", String(255), nullable=False, default=""),
        Column("verdict", String(32), nullable=False),
        Column("comment_hash", String(64), nullable=False, default=""),
        Column("timestamp", String(40), nullable=False),
        Column("record", Text, nullable=False),
        # A repeated (double-submitted) verdict refreshes its timestamp instead of adding a
        # row: approvals are idempotent.
        UniqueConstraint(
            "dag_id", "etag", "user", "verdict", "comment_hash", name=f"{p}reviews_uq"
        ),
        Index(f"{p}reviews_dag", "dag_id", "timestamp"),
    )
    proposals = Table(
        f"{p}proposals",
        md,
        Column("dag_id", String(250), nullable=False),
        Column("etag", String(128), nullable=False),
        Column("timestamp", String(40), nullable=False),
        Column("author", String(255)),
        Column("data", Text, nullable=False),
        PrimaryKeyConstraint("dag_id", "etag", name=f"{p}proposals_pk"),
        Index(f"{p}proposals_ts", "timestamp"),
    )
    login_failures = Table(
        f"{p}login_failures",
        md,
        Column("id", _ID, primary_key=True, autoincrement=True),
        Column("key", String(255), nullable=False),
        Column("at", Float, nullable=False),
        Index(f"{p}login_failures_key", "key", "at"),
    )
    replicas = Table(
        f"{p}replicas",
        md,
        Column("id", String(128), primary_key=True),
        Column("hostname", String(255), nullable=False),
        Column("pid", Integer, nullable=False),
        Column("version", String(50), nullable=False),
        Column("mode", String(20), nullable=False),
        Column("started_at", String(40), nullable=False),
        Column("last_seen", Float, nullable=False),
    )
    collab_updates = Table(
        f"{p}collab_updates",
        md,
        Column("id", _ID, primary_key=True, autoincrement=True),
        Column("room", String(255), nullable=False),
        Column("origin", String(64), nullable=False),
        Column("kind", String(16), nullable=False),
        Column("payload", LargeBinary, nullable=False),
        Column("created_at", Float, nullable=False),
        Index(f"{p}collab_updates_created", "created_at"),
    )
    return Tables(
        metadata=md,
        schema_version=schema_version,
        documents=documents,
        audit=audit,
        ai_audit=ai_audit,
        ai_memory=ai_memory,
        reviews=reviews,
        proposals=proposals,
        login_failures=login_failures,
        replicas=replicas,
        collab_updates=collab_updates,
    )


__all__ = ["Tables", "build_tables"]
