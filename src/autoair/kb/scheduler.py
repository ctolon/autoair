"""Scheduled knowledge-base refreshes of a server process.

A daemon thread per ``Services`` (weak reference: it ends with them) checks every minute
whether a refresh is due (``kb.refresh_interval_hours`` since the last one, or an empty
index with ``kb.refresh_on_start``) and starts one. Every replica runs the check; the
ingestion lease lets only one of them refresh a given index.
"""

from __future__ import annotations

import logging
import threading
import time
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


def start_scheduler(services: Any) -> threading.Thread | None:
    cfg = kb_settings_of(services.settings)
    if cfg.refresh_interval_hours <= 0 and not cfg.refresh_on_start:
        return None
    ref = weakref.ref(services)

    def loop() -> None:
        time.sleep(_START_DELAY)
        while True:
            current = ref()
            if current is None:
                return
            try:
                check_once(current)
            except Exception:
                log.warning("knowledge-base scheduler check failed", exc_info=True)
            del current
            time.sleep(_CHECK_EVERY)

    thread = threading.Thread(target=loop, name="autoair-kb-scheduler", daemon=True)
    thread.start()
    return thread


__all__ = ["check_once", "start_scheduler"]
