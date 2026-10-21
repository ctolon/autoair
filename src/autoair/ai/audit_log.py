"""Audit log of AI calls: who asked what feature, with which model, how many tokens it
cost, which tools ran and what the guardrails did.

JSON lines on any fsspec URL (default ``<state>/ai-audit``), one file per day and
process (no write races between replicas), or rows of ``<prefix>ai_audit`` when
``database.backend`` is sqlite/postgresql. Prompts are not stored unless
``ai.audit.include_prompts`` is on (then redacted).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import re
import socket
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any

import fsspec
from pydantic import BaseModel, Field

from autoair.ai.config import AiAuditSettings
from autoair.ai.rules import redact
from autoair.ai.usage import Usage

if TYPE_CHECKING:
    from autoair.persistence.protocols import AiAuditRepository

log = logging.getLogger("autoair.ai.audit")

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AiAuditRecord(BaseModel):
    timestamp: str
    user: str | None = None
    role: str | None = None
    feature: str
    provider: str | None = None
    model: str | None = None
    llm_source: str | None = Field(
        default=None, description="central / user / user-connection / service."
    )
    usage: Usage = Field(default_factory=Usage)
    tools: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    outcome: str = "ok"
    error: str | None = None
    prompt: str | None = None
    sources: list[str] = Field(
        default_factory=list, description="Sources the answer used (docs, files, audit ids)."
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Feature specifics, e.g. thread / analysis id, dag/run ids, proposal "
        "decisions (never secrets).",
    )


class FileAiAuditRepository:
    """JSON lines ``<root>/<day>/<host>-<pid>.jsonl`` (one file per day and process)."""

    def __init__(self, url: str) -> None:
        self._fs: Any
        self._fs, root = fsspec.core.url_to_fs(url)
        self._root = str(root).rstrip("/")
        self._suffix = f"{socket.gethostname()}-{os.getpid()}"
        self._lock = threading.Lock()

    def _day_dirs(self) -> list[str]:
        try:
            entries = self._fs.ls(self._root, detail=False)
        except (FileNotFoundError, OSError):
            return []
        return sorted(str(p).rstrip("/") for p in entries if _DAY.match(str(p).rstrip("/")[-10:]))

    def prune(self, *, retention_days: int, max_total_mb: int, today: str) -> list[str]:
        removed: list[str] = []
        days = self._day_dirs()
        if retention_days > 0:
            cutoff = (
                datetime.date.fromisoformat(today) - datetime.timedelta(days=retention_days)
            ).isoformat()
            for day in list(days):
                if day[-10:] < cutoff:
                    removed.append(day)
                    days.remove(day)
        limit = max_total_mb * 1024 * 1024
        if limit > 0 and len(days) > 1:
            sizes = {}
            for day in days:
                try:
                    sizes[day] = int(self._fs.du(day, total=True) or 0)
                except Exception:
                    sizes[day] = 0
            total = sum(sizes.values())
            for day in days[:-1]:  # never the current day
                if total <= limit:
                    break
                removed.append(day)
                total -= sizes[day]
        for day in removed:
            try:
                self._fs.rm(day, recursive=True)
            except Exception:
                log.warning("cannot prune AI audit folder %s", day, exc_info=True)
        return removed

    def append(self, record: AiAuditRecord, line: str) -> None:
        day = record.timestamp[:10]
        with self._lock:
            self._fs.makedirs(f"{self._root}/{day}", exist_ok=True)
            with self._fs.open(
                f"{self._root}/{day}/{self._suffix}.jsonl", "a", encoding="utf-8"
            ) as fh:
                fh.write(line + "\n")

    def recent(
        self, limit: int = 100, user: str | None = None, since: str | None = None
    ) -> list[AiAuditRecord]:
        out: list[AiAuditRecord] = []
        try:
            days = sorted(
                (p for p in self._fs.ls(self._root, detail=False) if p.rstrip("/")[-10:][:2]),
                reverse=True,
            )
        except (FileNotFoundError, OSError):
            return []
        for day in days[: None if since else 14]:
            if since is not None and str(day).rstrip("/")[-10:] < since[:10]:
                break
            lines: list[str] = []
            try:
                for path in self._fs.ls(day, detail=False):
                    if str(path).endswith(".jsonl"):
                        with self._fs.open(path, "r", encoding="utf-8") as fh:
                            lines.extend(fh.read().splitlines())
            except (FileNotFoundError, OSError):
                continue
            for line in lines:
                try:
                    rec = AiAuditRecord.model_validate(json.loads(line))
                except ValueError:
                    continue
                if (user is None or rec.user == user) and (since is None or rec.timestamp >= since):
                    out.append(rec)
            if len(out) >= limit * 2:
                break
        out.sort(key=lambda r: r.timestamp, reverse=True)
        return out[:limit]


class AiAuditLog:
    def __init__(
        self,
        settings: AiAuditSettings,
        default_url: str,
        *,
        repository: AiAuditRepository | None = None,
    ) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self.repository: AiAuditRepository | None = None
        if settings.enabled:
            if repository is not None:
                self.repository = repository
            else:
                try:
                    self.repository = FileAiAuditRepository(settings.url or default_url)
                except Exception:
                    log.warning("AI audit log disabled: cannot open its location", exc_info=True)
        self._pruned_day: str | None = None
        self._pending_lock = threading.Lock()
        self._pending: set[Future[None]] = set()
        self._executor: ThreadPoolExecutor | None = None

    def prune(self, today: str | None = None) -> list[str]:
        """Apply ``retention_days`` / ``max_total_mb``: delete whole days, oldest first
        (SQL: only the replica holding the retention lock prunes). Returns what was removed."""
        if self.repository is None:
            return []
        day = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        return self.repository.prune(
            retention_days=self.settings.retention_days,
            max_total_mb=self.settings.max_total_mb,
            today=day,
        )

    def submit(self, record: AiAuditRecord) -> None:
        """Write ``record`` in the background (one dedicated writer thread, in order), so
        callers on the event loop never block on storage I/O."""
        with self._pending_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(1, thread_name_prefix="ai-audit")
            future = self._executor.submit(self.record, record)
            self._pending.add(future)
        future.add_done_callback(self._done)

    def _done(self, future: Future[None]) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    def flush(self, timeout: float = 10.0) -> None:
        """Wait for the background writes queued so far."""
        with self._pending_lock:
            pending = list(self._pending)
        if pending:
            wait(pending, timeout=timeout)

    def record(self, record: AiAuditRecord) -> None:
        if not self.settings.include_prompts:
            record.prompt = None
        elif record.prompt:
            record.prompt = redact(record.prompt)[:4000]
        line = record.model_dump_json(exclude_none=True)
        log.info("ai-audit %s", line)
        if self.repository is None:
            return
        day = record.timestamp[:10]
        with self._lock:
            if self._pruned_day != day:
                self._pruned_day = day  # once per day and process
                try:
                    self.prune(day)
                except Exception:
                    log.warning("cannot prune the AI audit log", exc_info=True)
            try:
                self.repository.append(record, line)
            except Exception:
                log.warning("cannot write the AI audit log", exc_info=True)

    def recent(
        self, limit: int = 100, user: str | None = None, since: str | None = None
    ) -> list[AiAuditRecord]:
        """Newest records first."""
        if self.repository is None:
            return []
        self.flush()
        return self.repository.recent(limit, user, since)


_STATE_LOCK = threading.Lock()
_AUDIT_ATTR = "_autoair_ai_audit_log"


def audit_for(services: Any) -> AiAuditLog:
    """The AI audit log of a ``Services`` object (kept on it): SQL rows when a database is
    configured (``<prefix>ai_audit``), else files under ``<state>/ai-audit``."""
    from autoair.ai.config import ai_settings_of

    with _STATE_LOCK:
        cached = getattr(services, _AUDIT_ATTR, None)
        if isinstance(cached, AiAuditLog):
            return cached
        repository = None
        db = getattr(services, "database", None)
        if db is not None:
            from autoair.persistence.sql import SqlAiAuditRepository

            repository = SqlAiAuditRepository(db)
        audit = AiAuditLog(
            ai_settings_of(services.settings).audit,
            str(services.settings.state_dir / "ai-audit"),
            repository=repository,
        )
        with contextlib.suppress(AttributeError, TypeError):
            setattr(services, _AUDIT_ATTR, audit)
        return audit


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


__all__ = [
    "AiAuditLog",
    "AiAuditRecord",
    "FileAiAuditRepository",
    "audit_for",
    "now_iso",
]
