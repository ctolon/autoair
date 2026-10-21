"""Append-only audit trail of builder actions (deploy, delete, promote, template save...).

Records are JSON lines written to any fsspec URL (local file by default, object storage
for multi-replica deployments: one object per day per process to avoid write races) or,
with ``database.backend`` sqlite/postgresql, rows of ``<prefix>audit``.
"""

from __future__ import annotations

import datetime
import logging
import os
import socket
import threading
from typing import TYPE_CHECKING, Any

import fsspec
from fastapi import Request
from pydantic import BaseModel

from autoair.auth import forwarded_credentials
from autoair.secret_mask import mask_data

if TYPE_CHECKING:
    from autoair.persistence.protocols import AuditRepository

log = logging.getLogger("autoair.audit")


class AuditRecord(BaseModel):
    timestamp: str
    action: str
    user: str | None
    dag_id: str | None = None
    details: dict[str, Any] = {}


def request_user(request: Request) -> str | None:
    """Best-effort user identity from the (already authenticated) Airflow JWT.

    The token is *not* verified here — authentication happened in the auth dependency;
    this only labels audit records.
    """
    from autoair.iam.resolver import claim_user, jwt_claims

    auth = forwarded_credentials(request)[0].get("Authorization", "")
    return claim_user(jwt_claims(auth))


class FileAuditRepository:
    """JSON lines ``<root>/<day>/<host>-<pid>.jsonl`` on any fsspec URL (one file per day
    and process: replicas never write the same object)."""

    def __init__(self, url: str, storage_options: dict[str, Any] | None = None) -> None:
        self._lock = threading.Lock()
        self._fs, root = fsspec.core.url_to_fs(url, **(storage_options or {}))
        self._root = root.rstrip("/")
        self._suffix = f"{socket.gethostname()}-{os.getpid()}"

    def append(self, record: AuditRecord) -> None:
        day = record.timestamp[:10]
        path = f"{self._root}/{day}/{self._suffix}.jsonl"
        with self._lock:
            self._fs.makedirs(f"{self._root}/{day}", exist_ok=True)
            with self._fs.open(path, "a", encoding="utf-8") as fh:
                fh.write(record.model_dump_json() + "\n")

    def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        dag_id: str | None = None,
        user: str | None = None,
        action: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[AuditRecord]:
        try:
            files = sorted(self._fs.glob(f"{self._root}/*/*.jsonl"), reverse=True)
        except FileNotFoundError:
            return []
        days: dict[str, list[str]] = {}
        for path in files:  # newest day first; every process writes its own file per day
            days.setdefault(str(path).rstrip("/").rsplit("/", 2)[-2], []).append(str(path))
        want = offset + limit
        out: list[AuditRecord] = []
        for day, paths in days.items():
            if since is not None and day < since[:10]:
                break
            if until is not None and day > until[:10]:
                continue
            for path in paths:
                with self._fs.open(path, "r", encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                for line in lines:
                    try:
                        rec = AuditRecord.model_validate_json(line)
                    except ValueError:
                        continue
                    if _matches(rec, dag_id, user, action, since, until):
                        out.append(rec)
            if len(out) >= want:
                break
        out.sort(key=lambda r: r.timestamp, reverse=True)
        return out[offset:want]


def _matches(
    rec: AuditRecord,
    dag_id: str | None,
    user: str | None,
    action: str | None,
    since: str | None,
    until: str | None,
) -> bool:
    return (
        (dag_id is None or rec.dag_id == dag_id)
        and (user is None or rec.user == user)
        and (action is None or rec.action == action)
        and (since is None or rec.timestamp >= since)
        and (until is None or rec.timestamp < until)
    )


class AuditLog:
    """Records builder actions into its sinks (``audit.sinks``): repositories (files, the
    SQL database), a structured log line and/or an OpenTelemetry span. Queries read the
    first repository."""

    def __init__(
        self,
        url: str | None = None,
        storage_options: dict[str, Any] | None = None,
        *,
        repository: AuditRepository | None = None,
        repositories: list[AuditRepository] | None = None,
        log_sink: bool = True,
        otel_sink: bool = False,
    ) -> None:
        repos: list[AuditRepository] = list(repositories or [])
        if repository is not None:
            repos.insert(0, repository)
        elif url and not repos:
            repos.append(FileAuditRepository(url, storage_options))
        self.repositories = repos
        self.repository = repos[0] if repos else None
        self.log_sink = log_sink
        self.otel_sink = otel_sink

    def record(
        self, action: str, *, user: str | None, dag_id: str | None = None, **details: Any
    ) -> AuditRecord:
        rec = AuditRecord(
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            action=action,
            user=user,
            dag_id=dag_id,
            # A git token typed for this request never reaches a sink (autoair.secret_mask).
            details=mask_data(details),
        )
        if self.log_sink:
            # JSON in the message for plain-text handlers; the record as ``extra`` for
            # structured (JSON) log formatters and log shippers.
            log.info("audit %s", rec.model_dump_json(), extra={"audit": rec.model_dump()})
        if self.otel_sink:
            emit_otel(rec)
        for repository in self.repositories:
            try:
                repository.append(rec)
            except Exception:  # auditing must never break the request
                log.exception("failed to write audit record")
        return rec

    def tail(self, limit: int = 100, dag_id: str | None = None) -> list[AuditRecord]:
        if self.repository is None:
            return []
        return self.repository.query(limit=limit, dag_id=dag_id)

    def query(self, **filters: Any) -> list[AuditRecord]:
        """Filtered records, newest first (see ``AuditRepository.query``)."""
        if self.repository is None:
            return []
        return self.repository.query(**filters)


_OTEL_WARNED = False
OTEL_SCOPE = "autoair.audit"  # instrumentation scope / span name prefix of audit records


def emit_otel(rec: AuditRecord) -> None:
    """One span per audit record (``audit <action>``) with ``autoair.audit.*`` attributes; the
    configured tracer provider exports it (autoair.telemetry / OTEL_* variables)."""
    global _OTEL_WARNED
    try:
        from opentelemetry import trace
    except ImportError:
        if not _OTEL_WARNED:
            log.warning("audit sink 'otel' needs opentelemetry (pip install 'autoair[otel]')")
            _OTEL_WARNED = True
        return
    import json

    attributes: dict[str, str] = {
        "autoair.audit.action": rec.action,
        "autoair.audit.timestamp": rec.timestamp,
        "autoair.audit.details": json.dumps(rec.details, default=str, sort_keys=True),
    }
    if rec.user is not None:
        attributes["autoair.audit.user"] = rec.user
    if rec.dag_id is not None:
        attributes["autoair.audit.dag_id"] = rec.dag_id
    try:
        tracer = trace.get_tracer(OTEL_SCOPE)
        with tracer.start_as_current_span(f"audit {rec.action}", attributes=attributes):
            pass
    except Exception:  # auditing must never break the request
        log.exception("failed to emit the audit record to OpenTelemetry")
