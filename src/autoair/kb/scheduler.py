"""Scheduled knowledge-base refreshes of a server process.

A daemon thread per ``Services`` (weak reference: it ends with them) checks every minute
whether a refresh is due (``kb.refresh_interval_hours`` since the last one, or an empty
index with ``kb.refresh_on_start``) and starts one. Every replica runs the check; the
ingestion lease lets only one of them refresh a given index.
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Any

from autoair.kb.config import kb_settings_of
from autoair.kb.state import due

log = logging.getLogger("autoair.kb")

#: Seconds after start-up before the first check (let the server finish starting).
_START_DELAY = 30.0
#: Seconds between checks.
_CHECK_EVERY = 60.0
#: Seconds before an empty index is tried again after a failed first refresh.
_RETRY_EMPTY = 3600.0


def check_once(services: Any) -> bool:
    """Start a refresh when one is due; returns whether one was started."""
    from autoair.kb.ingest import AlreadyRunning
    from autoair.kb.service import kb_enabled, kb_for

    cfg = kb_settings_of(services.settings)
    if not kb_enabled(services):
        return False
    kb = kb_for(services)
    if not kb.any_usable():
        return False
    status = kb.run_status()
    if status.state == "running" and kb.state.holder() is not None:
        return False
    wanted = due(status, cfg.refresh_interval_hours)
    if not wanted and cfg.refresh_on_start:
        never = status.last_refresh is None
        # A failed first refresh is retried hourly instead of after the full interval.
        retry = status.outcome == "failed" and due(status, _RETRY_EMPTY / 3600)
        wanted = (never or retry) and kb.stats().pages == 0
    if not wanted:
        return False
    try:
        kb.start_refresh("refresh")
    except AlreadyRunning:
        return False
    log.info("knowledge-base refresh started (scheduled)")
    return True


# Stop events of the running schedulers. A scheduler whose Services object stays alive (a
# reference cycle, a test client kept by a fixture) keeps checking and logging for the rest of
# the process; stop_all() ends them (test teardown, embedding code that rebuilds Services).
_RUNNING: weakref.WeakSet[threading.Event] = weakref.WeakSet()


def start_scheduler(services: Any) -> threading.Thread | None:
    cfg = kb_settings_of(services.settings)
    if cfg.refresh_interval_hours <= 0 and not cfg.refresh_on_start:
        return None
    ref = weakref.ref(services)
    stop = threading.Event()
    _RUNNING.add(stop)

    def loop() -> None:
        if stop.wait(_START_DELAY):
            return
        while True:
            current = ref()
            if current is None:
                return
            try:
                check_once(current)
            except Exception:
                log.warning("knowledge-base scheduler check failed", exc_info=True)
            del current
            if stop.wait(_CHECK_EVERY):
                return

    thread = threading.Thread(target=loop, name="autoair-kb-scheduler", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]  # keeps the event alive with the thread
    thread.start()
    return thread


def stop_scheduler(thread: threading.Thread | None) -> None:
    """End one scheduler started by ``start_scheduler`` (no-op for ``None``)."""
    stop = getattr(thread, "stop_event", None)
    if isinstance(stop, threading.Event):
        stop.set()


def stop_all() -> None:
    """End every scheduler of this process."""
    for stop in list(_RUNNING):
        stop.set()


__all__ = ["check_once", "start_scheduler", "stop_all", "stop_scheduler"]
