"""Admin panel back end: users seen, AI usage, pending proposals, cache and database status.

The routes live in :mod:`autoair.api.routers.admin`; this module holds the logic so it is
shared by the API and tests, and works on both state backends (files / SQL database).
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import hashlib
import io
import json
import logging
import threading
import time
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from autoair.audit import AuditRecord
from autoair.persistence import document_store
from autoair.persistence.documents import DocumentStore

log = logging.getLogger("autoair.admin")

USER_SEEN_INTERVAL = 300.0  # seconds between two "seen" writes for one user and process
USAGE_SCAN_LIMIT = 20000  # AI audit records aggregated on the file backend


class SeenUser(BaseModel):
    user: str
    roles: list[str] = Field(default_factory=list, description="AutoAir roles when last seen.")
    permissions: int = Field(default=0, description="Number of AutoAir permissions held.")
    auth: str | None = None
    last_seen: str | None = None
    actions: int = Field(default=0, description="Audit records (recent window).")
    last_action: str | None = None
    last_action_at: str | None = None


class UsageRow(BaseModel):
    user: str | None
    provider: str | None = None
    model: str | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    errors: int = 0


# --------------------------------------------------------------------------- users seen


def users_store(settings: Any) -> DocumentStore:
    return document_store(settings, "users_seen", file_url=str(settings.state_dir / "users"))


def _user_key(user: str) -> str:
    return hashlib.sha256(user.encode()).hexdigest()[:40]


class SeenUsers:
    """Remembers who used AutoAir and with which roles (throttled per user and process)."""

    def __init__(self, store: DocumentStore, interval: float = USER_SEEN_INTERVAL) -> None:
        self.store = store
        self.interval = interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def due(self, user: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._last.get(user, -1e18) < self.interval:
                return False
            self._last[user] = now
            if len(self._last) > 10_000:
                self._last.clear()
            return True

    def record(self, user: str, *, roles: Iterable[str], permissions: int, auth: str) -> None:
        payload = {
            "user": user,
            "roles": sorted(set(roles)),
            "permissions": permissions,
            "auth": auth,
            "last_seen": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.store.put(_user_key(user), json.dumps(payload).encode())

    def all(self) -> list[SeenUser]:
        out: list[SeenUser] = []
        for key in self.store.keys():  # noqa: SIM118 - a DocumentStore, not a dict
            found = self.store.get(key)
            if found is None:
                continue
            try:
                out.append(SeenUser.model_validate_json(found.data))
            except ValueError:
                continue
        return out


def seen_users_for(services: Any) -> SeenUsers:
    existing = getattr(services, "_autoair_seen_users", None)
    if isinstance(existing, SeenUsers):
        return existing
    created = SeenUsers(users_store(services.settings))
    with contextlib.suppress(AttributeError, TypeError):
        services._autoair_seen_users = created
    return created


def users_overview(seen: list[SeenUser], audit: list[AuditRecord]) -> list[SeenUser]:
    """Users seen by the API merged with the users found in ``audit`` (newest first)."""
    by_user: dict[str, SeenUser] = {u.user: u.model_copy() for u in seen}
    for rec in audit:
        if not rec.user:
            continue
        item = by_user.setdefault(rec.user, SeenUser(user=rec.user))
        item.actions += 1
        if item.last_action_at is None or rec.timestamp > item.last_action_at:
            item.last_action_at = rec.timestamp
            item.last_action = rec.action
    return sorted(
        by_user.values(),
        key=lambda u: max(u.last_seen or "", u.last_action_at or ""),
        reverse=True,
    )


# --------------------------------------------------------------------------- audit export

CSV_FIELDS = ("timestamp", "action", "user", "dag_id", "details")


def audit_csv(records: Iterable[AuditRecord]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_FIELDS)
    for rec in records:
        writer.writerow(
            [
                rec.timestamp,
                _cell(rec.action),
                _cell(rec.user or ""),
                _cell(rec.dag_id or ""),
                _cell(json.dumps(rec.details, sort_keys=True, default=str)),
            ]
        )
    return buf.getvalue()


def _cell(text: str) -> str:
    """Neutralise spreadsheet formulas (CSV injection): a leading = + - @ becomes text."""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# --------------------------------------------------------------------------- AI usage


def aggregate_usage(records: Iterable[Any]) -> list[UsageRow]:
    """Requests, tokens and cost per (user, provider, model) of AI audit records."""
    rows: dict[tuple[str | None, str | None, str | None], UsageRow] = {}
    for rec in records:
        key = (rec.user, rec.provider, rec.model)
        row = rows.setdefault(key, UsageRow(user=rec.user, provider=rec.provider, model=rec.model))
        usage = rec.usage
        row.requests += 1
        row.input_tokens += usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        row.output_tokens += usage.output_tokens
        if usage.cost_usd is not None:
            row.cost_usd = round((row.cost_usd or 0.0) + usage.cost_usd, 8)
        if rec.outcome != "ok":
            row.errors += 1
    return sorted(rows.values(), key=lambda r: (-(r.cost_usd or 0), -r.requests))


def usage_rows(raw: Iterable[dict[str, Any]]) -> list[UsageRow]:
    out = [
        UsageRow(
            user=r.get("user"),
            provider=r.get("provider"),
            model=r.get("model"),
            requests=int(r.get("requests") or 0),
            input_tokens=int(r.get("input_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            cost_usd=float(r["cost_usd"]) if r.get("cost_usd") is not None else None,
            errors=int(r.get("errors") or 0),
        )
        for r in raw
    ]
    return sorted(out, key=lambda r: (-(r.cost_usd or 0), -r.requests))


# --------------------------------------------------------------------------- proposals


def all_pending_proposals(store: Any) -> list[Any]:
    """Pending proposals of every DAG (SQL store: one query; file store: a scan)."""
    if store is None:
        return []
    listing = getattr(store, "all_pending", None)
    if callable(listing):
        return list(listing())
    fs = getattr(store, "fs", None)
    root = getattr(store, "root", None)
    if fs is None or root is None:
        return []
    try:
        dirs = fs.ls(root, detail=False)
    except (FileNotFoundError, OSError):
        return []
    out: list[Any] = []
    for path in dirs:
        dag_id = str(path).rstrip("/").rsplit("/", 1)[-1]
        try:
            out.extend(store.pending(dag_id))
        except Exception:
            log.debug("skipping proposals of %s", dag_id, exc_info=True)
    return sorted(out, key=lambda p: p.timestamp, reverse=True)


__all__ = [
    "SeenUser",
    "SeenUsers",
    "UsageRow",
    "aggregate_usage",
    "all_pending_proposals",
    "audit_csv",
    "seen_users_for",
    "usage_rows",
    "users_overview",
]
