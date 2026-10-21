"""The knowledge-base scheduler thread can be stopped."""

from __future__ import annotations

from autoair.kb import scheduler
from autoair.settings import Settings


class _Services:
    # start_scheduler keeps a weak reference: a plain class instance supports one.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings


def _services(**kb: object) -> _Services:
    return _Services(Settings(kb=kb))


def test_no_thread_without_refreshes() -> None:
    assert (
        scheduler.start_scheduler(_services(refresh_interval_hours=0, refresh_on_start=False))
        is None
    )


def test_stop_scheduler_ends_the_thread() -> None:
    services = _services(refresh_on_start=True)
    thread = scheduler.start_scheduler(services)
    assert thread is not None
    assert thread.is_alive()
    scheduler.stop_scheduler(thread)
    thread.join(timeout=5)
    assert not thread.is_alive()
    scheduler.stop_scheduler(None)  # no-op


def test_stop_all_ends_every_thread() -> None:
    keep = [_services(refresh_on_start=True) for _ in range(3)]
    threads = [scheduler.start_scheduler(s) for s in keep]
    scheduler.stop_all()
    for thread in threads:
        assert thread is not None
        thread.join(timeout=5)
        assert not thread.is_alive()
