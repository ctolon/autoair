"""SQLAlchemy engine for the ``sqlite`` / ``postgresql`` backends (``autoair[sqlite]``,
``autoair[postgres]``).

* SQLite: WAL journal, ``busy_timeout`` and ``BEGIN IMMEDIATE`` write transactions, so
  several processes on one host serialise their read-modify-write cycles;
* PostgreSQL (psycopg 3; psycopg2 on SQLAlchemy 1.4): pooled connections,
  ``sslmode``/``sslrootcert``, transaction-level advisory locks for leader-only work (schema
  migrations, AI audit retention, memory).

Works on SQLAlchemy 1.4 (the version Airflow 3.0 pins) and 2.x: engines are ``future=True``
and only APIs both lines have are used (see ``persistence/sql.py::_returning``).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from autoair.config_reference import mask_url_credentials
from autoair.persistence.config import DatabaseSettings

log = logging.getLogger("autoair.persistence")


class DatabaseError(RuntimeError):
    """The database is unusable (missing driver, schema not migrated, bad URL…)."""


def sqlalchemy_available() -> bool:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        return False
    return True


def sqlalchemy_major() -> int:
    """Major version of the installed SQLAlchemy (``0`` when it is missing)."""
    try:
        import sqlalchemy
    except ImportError:
        return 0
    return int(sqlalchemy.__version__.split(".", 1)[0])


def postgres_driver() -> str:
    """SQLAlchemy driver name for PostgreSQL: psycopg 3 on SQLAlchemy 2; psycopg2 on 1.4,
    which has no psycopg 3 dialect (Airflow 3.0 pins ``sqlalchemy<2``; the Airflow postgres
    provider installs psycopg2)."""
    return "postgresql+psycopg" if sqlalchemy_major() >= 2 else "postgresql+psycopg2"


def with_postgres_driver(url: Any) -> Any:
    """``url`` (a SQLAlchemy ``URL``) with the psycopg driver the installed SQLAlchemy has:
    a bare ``postgresql://`` gets the default driver, and ``postgresql+psycopg://`` (the
    documented form) falls back to psycopg2 on SQLAlchemy 1.4. Other drivers are kept."""
    name = url.drivername
    if name in ("postgresql", "postgres") or (
        name == "postgresql+psycopg" and sqlalchemy_major() < 2
    ):
        return url.set(drivername=postgres_driver())
    return url


def driver_hint(dialect: str, extra: str = "postgres") -> str:
    """Install hint for a missing database driver (``extra``: the autoair extra to name)."""
    if dialect == "postgresql" and sqlalchemy_major() < 2:
        return (
            "SQLAlchemy 1.4 (Airflow 3.0) drives PostgreSQL through psycopg2: "
            "pip install psycopg2-binary (or apache-airflow-providers-postgres)"
        )
    return f"pip install 'autoair[{extra}]'"


def resolve_url(settings: DatabaseSettings, default_sqlite_path: Path | None = None) -> str:
    """The effective SQLAlchemy URL (``postgresql://`` uses psycopg 3, or psycopg2 on
    SQLAlchemy 1.4; the password setting is merged in)."""
    from sqlalchemy.engine import make_url

    if settings.backend == "file":
        raise DatabaseError("database.backend is 'file': there is no SQL database")
    raw = settings.url
    if not raw:
        if settings.backend != "sqlite":
            raise DatabaseError("set database.url (AUTOAIR_DATABASE__URL) for postgresql")
        if default_sqlite_path is None:
            raise DatabaseError("set database.url (AUTOAIR_DATABASE__URL) for sqlite")
        raw = f"sqlite:///{default_sqlite_path}"
    url = make_url(raw)
    if url.get_backend_name() != settings.backend:
        raise DatabaseError(
            f"database.url is a {url.get_backend_name()} URL but database.backend is "
            f"{settings.backend!r}"
        )
    if settings.backend == "postgresql":
        url = with_postgres_driver(url)
    if settings.password is not None:
        url = url.set(password=settings.password.get_secret_value())
    return url.render_as_string(hide_password=False)


def masked_url(url: str) -> str:
    return mask_url_credentials(url)


def _lock_key(name: str) -> int:
    """A stable signed 64-bit key for ``pg_advisory_*lock``."""
    digest = hashlib.sha256(name.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


class Database:
    """Engine, tables and helpers of one configured SQL database."""

    def __init__(
        self, settings: DatabaseSettings, *, default_sqlite_path: Path | None = None
    ) -> None:
        if not sqlalchemy_available():
            extra = "postgres" if settings.backend == "postgresql" else "sqlite"
            raise DatabaseError(
                f"database.backend={settings.backend} needs SQLAlchemy: "
                f"pip install 'autoair[{extra}]'"
            )
        from sqlalchemy import create_engine, event
        from sqlalchemy.engine import make_url

        from autoair.persistence.schema import build_tables

        self.settings = settings
        self.url = resolve_url(settings, default_sqlite_path)
        parsed = make_url(self.url)
        self.dialect = parsed.get_backend_name()
        connect_args: dict[str, Any] = dict(settings.connect_args)
        kwargs: dict[str, Any] = {"echo": settings.echo, "future": True}
        if self.dialect == "sqlite":
            database = parsed.database or ""
            memory = database in ("", ":memory:") or "mode=memory" in self.url
            if not memory:
                Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
                from sqlalchemy.pool import QueuePool

                # SQLAlchemy 2 pools file databases by default; 1.4 (Airflow 3.0) uses
                # NullPool, which rejects the pool_* arguments.
                kwargs.update(
                    poolclass=QueuePool,
                    pool_size=settings.pool_size,
                    max_overflow=settings.max_overflow,
                    pool_timeout=settings.pool_timeout,
                    pool_recycle=settings.pool_recycle,
                    pool_pre_ping=settings.pool_pre_ping,
                )
            else:
                from sqlalchemy.pool import StaticPool

                kwargs["poolclass"] = StaticPool
            connect_args.setdefault("check_same_thread", False)
            connect_args.setdefault("timeout", settings.pool_timeout)
        else:
            if settings.ssl_mode:
                connect_args.setdefault("sslmode", settings.ssl_mode)
            if settings.ssl_root_cert:
                connect_args.setdefault("sslrootcert", settings.ssl_root_cert)
            kwargs.update(
                pool_size=settings.pool_size,
                max_overflow=settings.max_overflow,
                pool_timeout=settings.pool_timeout,
                pool_recycle=settings.pool_recycle,
                pool_pre_ping=settings.pool_pre_ping,
            )
        try:
            self.engine = create_engine(self.url, connect_args=connect_args, **kwargs)
        except ImportError as exc:  # driver missing (psycopg)
            raise DatabaseError(
                f"cannot load the {self.dialect} driver ({exc}): {driver_hint(self.dialect)}"
            ) from exc
        if self.dialect == "sqlite":
            busy_ms = int(settings.pool_timeout * 1000)

            @event.listens_for(self.engine, "connect")
            def _sqlite_connect(dbapi_conn: Any, _record: Any) -> None:
                dbapi_conn.isolation_level = None  # SQLAlchemy emits BEGIN itself
                cur = dbapi_conn.cursor()
                cur.execute(f"PRAGMA busy_timeout = {busy_ms}")
                with contextlib.suppress(Exception):
                    cur.execute("PRAGMA journal_mode = WAL")
                cur.execute("PRAGMA synchronous = NORMAL")
                cur.close()

            @event.listens_for(self.engine, "begin")
            def _sqlite_begin(conn: Any) -> None:
                # Take the write lock up front: read-modify-write transactions never fail
                # with "database is locked" half-way (and never lose an update).
                conn.exec_driver_sql("BEGIN IMMEDIATE")

        self.tables = build_tables(settings.table_prefix, settings.schema_name)
        self.prefix = settings.table_prefix
        self._secret_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    @property
    def masked_url(self) -> str:
        return masked_url(self.url)

    def begin(self) -> contextlib.AbstractContextManager[Any]:
        """A connection in a transaction (committed on success, rolled back on error)."""
        return self.engine.begin()

    def connect(self) -> contextlib.AbstractContextManager[Any]:
        return self.engine.connect()

    def lock(self, conn: Any, name: str) -> None:
        """Serialise ``name`` across replicas until the transaction ends (PostgreSQL
        advisory lock; SQLite's ``BEGIN IMMEDIATE`` already holds the database lock)."""
        if self.dialect == "postgresql":
            from sqlalchemy import text

            conn.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(self.prefix + name)}
            )

    def try_lock(self, conn: Any, name: str) -> bool:
        """Like :meth:`lock` without waiting: ``False`` when another replica holds it."""
        if self.dialect == "postgresql":
            from sqlalchemy import text

            got = conn.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _lock_key(self.prefix + name)}
            ).scalar()
            return bool(got)
        return True

    def shared_secret(self, name: str, factory: Callable[[], bytes] | None = None) -> bytes:
        """A secret shared by every replica: created once (first writer wins) in the
        ``secrets`` collection, then read by everyone."""
        from autoair.persistence.sql import SqlDocumentStore

        store = SqlDocumentStore(self, "secrets")
        with self._secret_lock:
            found = store.get(name)
            if found is not None:
                return found.data
            value = factory() if factory is not None else secrets.token_bytes(32)
            return store.put_if_absent(name, value).data

    def ping(self) -> bool:
        from sqlalchemy import text

        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            log.warning("database ping failed", exc_info=True)
            return False
        return True

    def pool_status(self) -> str:
        pool = self.engine.pool
        status = getattr(pool, "status", None)
        return str(status()) if callable(status) else type(pool).__name__

    def dispose(self) -> None:
        self.engine.dispose()


# --------------------------------------------------------------------------- per-process cache

_LOCK = threading.Lock()
_DATABASES: dict[tuple[str, str, str | None], Database] = {}


def database_for(settings: Any) -> Database | None:
    """The process-wide :class:`Database` of ``settings`` (``None`` for the file backend),
    migrated on first use when ``database.migrate_on_start`` is on."""
    db_settings: DatabaseSettings | None = getattr(settings, "database", None)
    if db_settings is None or not db_settings.is_sql:
        return None
    default_path = Path(settings.state_dir) / "autoair.db"
    url = resolve_url(db_settings, default_path)
    key = (url, db_settings.table_prefix, db_settings.schema_name)
    with _LOCK:
        db = _DATABASES.get(key)
        if db is not None:
            return db
        db = Database(db_settings, default_sqlite_path=default_path)
        from autoair.persistence import migrations

        if db_settings.migrate_on_start:
            applied = migrations.upgrade(db)
            if applied:
                log.info("database %s: applied migrations %s", db.masked_url, applied)
        else:
            version = migrations.current(db)
            if version != migrations.HEAD:
                raise DatabaseError(
                    f"database schema is at version {version}, AutoAir needs "
                    f"{migrations.HEAD}: run `autoair db upgrade`"
                )
        _DATABASES[key] = db
        return db


def reset_databases() -> None:
    """Dispose of every cached engine (tests)."""
    with _LOCK:
        for db in _DATABASES.values():
            with contextlib.suppress(Exception):
                db.dispose()
        _DATABASES.clear()


@contextlib.contextmanager
def transaction(db: Database) -> Iterator[Any]:
    with db.begin() as conn:
        yield conn


__all__ = [
    "Database",
    "DatabaseError",
    "database_for",
    "driver_hint",
    "masked_url",
    "postgres_driver",
    "reset_databases",
    "resolve_url",
    "sqlalchemy_available",
    "sqlalchemy_major",
    "transaction",
    "with_postgres_driver",
]
