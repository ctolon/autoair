"""Database settings (``AUTOAIR_DATABASE__*``): where AutoAir keeps its own state.

``backend=file`` (the default) stores JSON documents and JSON-lines logs
on fsspec URLs under the state folder. ``sqlite`` and ``postgresql`` keep the same state in a
SQL database through SQLAlchemy (``pip install 'autoair[sqlite]'`` / ``'autoair[postgres]'``),
which makes several AutoAir replicas safe (transactions, row versions, unique constraints).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

DatabaseBackend = Literal["file", "sqlite", "postgresql"]
SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


class DatabaseSettings(BaseModel):
    """AutoAir's own state store (templates, audit, AI memory, reviews, collab snapshots…)."""

    model_config = ConfigDict(extra="forbid")

    backend: DatabaseBackend = Field(
        default="file",
        description="file: JSON files on fsspec URLs (single replica or shared volume); "
        "sqlite: one SQLite file; postgresql: a PostgreSQL server (multi-replica).",
    )
    url: str | None = Field(
        default=None,
        description="SQLAlchemy URL, e.g. postgresql+psycopg://autoair@db:5432/autoair or "
        "sqlite:////var/lib/autoair/autoair.db (default for sqlite: <state>/autoair.db). "
        "Credentials in the URL are masked when the configuration is exposed.",
    )
    password: SecretStr | None = Field(
        default=None,
        description="Database password, merged into url (keeps the secret out of the URL).",
    )
    pool_size: int = Field(default=5, ge=1, le=200, description="Connections kept open.")
    max_overflow: int = Field(
        default=10, ge=0, le=500, description="Extra connections beyond pool_size under load."
    )
    pool_timeout: float = Field(
        default=30, gt=0, description="Seconds to wait for a free connection."
    )
    pool_recycle: int = Field(
        default=1800,
        ge=-1,
        description="Seconds after which a connection is replaced (-1: never); keep it below "
        "the server's / proxy's idle timeout.",
    )
    pool_pre_ping: bool = Field(
        default=True, description="Test a pooled connection before use (survives restarts)."
    )
    connect_args: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra DBAPI connect() arguments (psycopg / sqlite3), e.g. "
        '{"connect_timeout": 10, "application_name": "autoair"}.',
    )
    ssl_mode: SslMode | None = Field(
        default=None,
        description="PostgreSQL sslmode (disable, allow, prefer, require, verify-ca, "
        "verify-full); unset: the driver default.",
    )
    ssl_root_cert: str | None = Field(
        default=None, description="PostgreSQL: CA bundle verifying the server (sslrootcert)."
    )
    echo: bool = Field(default=False, description="Log every SQL statement (debugging).")
    schema_name: str | None = Field(
        default=None,
        description="PostgreSQL schema holding AutoAir's tables (default: the search path).",
    )
    table_prefix: str = Field(
        default="autoair_",
        pattern=r"^[a-z][a-z0-9_]{0,30}$",
        description="Prefix of every AutoAir table (share a database with other apps).",
    )
    migrate_on_start: bool = Field(
        default=True,
        description="Apply schema migrations when the server starts; turn off when an init "
        "job runs `autoair db upgrade` (several replicas starting at once).",
    )
    heartbeat_seconds: float = Field(
        default=30,
        ge=1,
        description="How often a replica records that it is alive (admin panel replicas).",
    )
    sync_seconds: float = Field(
        default=5,
        gt=0,
        description="SQL backends: how often a replica picks up shared changes made on "
        "another replica (the deploy target chosen in the UI).",
    )
    shared_rate_limit: bool = Field(
        default=True,
        description="SQL backends: count failed sign-ins in the database so the limit holds "
        "across replicas (file backend: per replica, in memory).",
    )

    @field_validator("schema_name")
    @classmethod
    def _schema(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
            raise ValueError("schema must be a plain SQL identifier")
        return value

    @property
    def is_sql(self) -> bool:
        return self.backend != "file"


__all__ = ["DatabaseBackend", "DatabaseSettings", "SslMode"]
