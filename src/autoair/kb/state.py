"""Shared state of the knowledge base in AutoAir's persistence layer (multi-replica).

Collection ``kb_state`` (files under ``kb.state_url`` / ``<state>/kb-state``, or rows of
the SQL database):

* ``lock-<store>`` - the ingestion **lease**: owner and expiry, taken with an atomic
  read-modify-write (:meth:`DocumentStore.update`) so exactly one replica ingests into a
  given index; renewed while the run lasts, free again after ``kb.lock_ttl_seconds`` when
  its owner died;
* ``status-<store>`` - :class:`~autoair.kb.models.KbRunStatus` (progress, errors, last
  refresh), which every replica reads for the admin panel;
* ``sources`` - admin-panel overrides of the configured sources (enabled, components,
  versions); never credentials.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import uuid
from collections.abc import Callable
from typing import Any

from autoair.kb.models import KbRunStatus, SourceOverride, digest
from autoair.persistence.documents import DocumentStore


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


class _Held(Exception):
    pass


def replica_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class KbState:
    def __init__(self, store: DocumentStore, identity: str) -> None:
        self.docs = store
        self.key = digest(identity, size=16)

    # -- lease ----------------------------------------------------------------------------

    def acquire(self, owner: str, ttl: float) -> bool:
        def fn(raw: bytes | None) -> bytes:
            cur = json.loads(raw) if raw else None
            now = datetime.datetime.now(datetime.timezone.utc).timestamp()
            if cur and cur.get("owner") != owner and float(cur.get("expires", 0)) > now:
                raise _Held
            return json.dumps({"owner": owner, "expires": now + ttl, "since": now_iso()}).encode()

        try:
            self.docs.update(f"lock-{self.key}", fn)
        except _Held:
            return False
        return True

    def release(self, owner: str) -> None:
        def fn(raw: bytes | None) -> bytes | None:
            cur = json.loads(raw) if raw else None
            if cur and cur.get("owner") != owner:
                return raw
            return None

        self.docs.update(f"lock-{self.key}", fn)

    def holder(self) -> str | None:
        found = self.docs.get(f"lock-{self.key}")
        if found is None:
            return None
        data = json.loads(found.data)
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        return str(data.get("owner")) if float(data.get("expires", 0)) > now else None

    @staticmethod
    def new_owner() -> str:
        return f"{replica_id()}:{uuid.uuid4().hex[:8]}"

    # -- status ---------------------------------------------------------------------------

    def status(self) -> KbRunStatus:
        found = self.docs.get(f"status-{self.key}")
        if found is None:
            return KbRunStatus()
        try:
            return KbRunStatus.model_validate_json(found.data)
        except ValueError:
            return KbRunStatus()

    def update_status(self, change: Callable[[KbRunStatus], None]) -> KbRunStatus:
        out: list[KbRunStatus] = []

        def fn(raw: bytes | None) -> bytes:
            try:
                status = KbRunStatus.model_validate_json(raw) if raw else KbRunStatus()
            except ValueError:
                status = KbRunStatus()
            change(status)
            out[:] = [status]
            return status.model_dump_json().encode()

        self.docs.update(f"status-{self.key}", fn)
        return out[0]

    # -- admin overrides ------------------------------------------------------------------

    def overrides(self) -> dict[str, SourceOverride]:
        found = self.docs.get("sources")
        if found is None:
            return {}
        try:
            data = json.loads(found.data)
        except ValueError:
            return {}
        out: dict[str, SourceOverride] = {}
        for key, value in (data if isinstance(data, dict) else {}).items():
            try:
                out[str(key)] = SourceOverride.model_validate(value)
            except ValueError:
                continue
        return out

    def set_override(self, source: str, override: SourceOverride | None) -> None:
        def fn(raw: bytes | None) -> bytes:
            try:
                data: dict[str, Any] = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            if override is None:
                data.pop(source, None)
            else:
                data[source] = override.model_dump(exclude_none=True)
            return json.dumps(data, indent=2, sort_keys=True).encode()

        self.docs.update("sources", fn)


def due(status: KbRunStatus, interval_hours: float, *, now: float | None = None) -> bool:
    if interval_hours <= 0:
        return False
    current = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    return current - _ts(status.last_refresh) >= interval_hours * 3600


def next_refresh(status: KbRunStatus, interval_hours: float) -> str | None:
    if interval_hours <= 0:
        return None
    last = _ts(status.last_refresh)
    if not last:
        return None
    when = last + interval_hours * 3600
    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


__all__ = ["KbState", "due", "next_refresh", "now_iso", "replica_id"]
