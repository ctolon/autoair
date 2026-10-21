"""Knowledge-base stores (``kb.store``): local SQLite (default), pgvector, Elasticsearch."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from autoair.kb.config import KbSettings, kb_settings_of
from autoair.kb.stores.base import KbStore, StoreError


def kb_base_dir(settings: Any) -> Path:
    kb = kb_settings_of(settings)
    if kb.cache_dir:
        return Path(kb.cache_dir).expanduser()
    base = getattr(settings, "cache_dir", None) or getattr(settings, "state_dir", None)
    return Path(base or ".autoair") / "kb"


def _pg_url(settings: Any, kb: KbSettings) -> str:
    cfg = kb.pgvector
    if cfg.conn_id:
        from autoair.kb.fetch import FetchError, airflow_connection

        try:
            conn = airflow_connection(cfg.conn_id)
        except FetchError as exc:
            raise StoreError(str(exc)) from exc
        from sqlalchemy.engine import URL

        from autoair.persistence.db import postgres_driver

        return URL.create(
            postgres_driver(),
            username=conn.login or None,
            password=conn.password or None,
            host=conn.host or None,
            port=conn.port or None,
            database=conn.schema or None,
        ).render_as_string(hide_password=False)
    if cfg.url:
        return cfg.url
    db = getattr(settings, "database", None)
    if db is not None and getattr(db, "backend", None) == "postgresql":
        from autoair.persistence.db import resolve_url

        return resolve_url(db)
    raise StoreError(
        "kb.store=pgvector needs kb.pgvector.url, kb.pgvector.conn_id or "
        "database.backend=postgresql"
    )


def _es_target(kb: KbSettings) -> tuple[str, dict[str, str]]:
    cfg = kb.elasticsearch
    headers: dict[str, str] = {}
    url = cfg.url
    if cfg.conn_id:
        from autoair.kb.fetch import FetchError, airflow_connection

        try:
            conn = airflow_connection(cfg.conn_id)
        except FetchError as exc:
            raise StoreError(str(exc)) from exc
        extra: dict[str, Any] = {}
        try:
            extra = dict(conn.extra_dejson or {})
        except (TypeError, ValueError):
            extra = {}
        if extra.get("api_key"):
            headers["authorization"] = f"ApiKey {extra['api_key']}"
        elif conn.login:
            raw = f"{conn.login}:{conn.password or ''}".encode()
            headers["authorization"] = "Basic " + base64.b64encode(raw).decode()
        if not url and conn.host:
            host = str(conn.host)
            scheme = "" if "://" in host else f"{conn.schema or 'https'}://"
            url = f"{scheme}{host}" + (f":{conn.port}" if conn.port else "")
    if not url:
        raise StoreError("kb.store=elasticsearch needs kb.elasticsearch.url or conn_id")
    return url, headers


def build_store(settings: Any) -> KbStore:
    """The configured store (not opened yet)."""
    kb = kb_settings_of(settings)
    if kb.store == "pgvector":
        from autoair.kb.stores.pgvector import PgvectorStore

        return PgvectorStore(_pg_url(settings, kb), kb.pgvector)
    if kb.store == "elasticsearch":
        from autoair.kb.stores.elasticsearch import ElasticsearchStore

        # The credentials live only in the client's default headers (never logged).
        url, headers = _es_target(kb)
        return ElasticsearchStore(url, kb.elasticsearch, headers=headers)
    from autoair.kb.stores.local import LocalStore

    path = (
        Path(kb.local.path).expanduser() if kb.local.path else kb_base_dir(settings) / "kb.sqlite3"
    )
    return LocalStore(path, shared=kb.local.shared)


__all__ = ["KbStore", "StoreError", "build_store", "kb_base_dir"]
