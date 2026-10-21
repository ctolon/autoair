"""Latest log try per Airflow version: 3.4 gives up_for_retry instances the upcoming try."""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from autoair.api.routers.airflow import capabilities_for
from autoair.gateway import AirflowGateway, TaskInstanceInfo, log_try_number, parse_version


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [("3.4.0b2", (3, 4, 0)), ("3.3.2", (3, 3, 2)), ("3", (3, 0, 0)), ("", None), (None, None)],
)
def test_parse_version(raw, parsed):
    assert parse_version(raw) == parsed


def _ti(state: str | None, try_number: int) -> TaskInstanceInfo:
    return TaskInstanceInfo(task_id="t", state=state, try_number=try_number)


@pytest.mark.parametrize(
    ("state", "try_number", "server", "expected"),
    [
        # 3.4: up_for_retry carries the upcoming try; the failed one is try_number - 1.
        ("up_for_retry", 2, (3, 4, 0), 1),
        ("up_for_retry", 3, (3, 5, 1), 2),
        # Before 3.4 (and when the version is unknown) try_number is the failed try.
        ("up_for_retry", 1, (3, 3, 2), 1),
        ("up_for_retry", 2, None, 2),
        # Other states are read the same way on every version.
        ("failed", 2, (3, 4, 0), 2),
        ("running", 1, (3, 4, 0), 1),
        (None, 0, (3, 4, 0), 1),
    ],
)
def test_log_try_number(state, try_number, server, expected):
    assert log_try_number(_ti(state, try_number), server) == expected


def test_latest_log_try_reads_the_server_version_once():
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        return httpx2.Response(200, json={"version": "3.4.0b2"})

    async def run() -> list[int]:
        async with AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler)) as gw:
            return [
                await gw.latest_log_try(_ti("up_for_retry", 2)),
                await gw.latest_log_try(_ti("up_for_retry", 3)),
                await gw.latest_log_try(_ti("success", 1)),
            ]

    assert asyncio.run(run()) == [1, 2, 1]
    assert calls == ["/api/v2/version"]


def test_latest_log_try_without_a_readable_version():
    async def run() -> int:
        async with AirflowGateway(
            "http://airflow",
            transport=httpx2.MockTransport(lambda r: httpx2.Response(403, json={"detail": "no"})),
        ) as gw:
            return await gw.latest_log_try(_ti("up_for_retry", 2))

    assert asyncio.run(run()) == 2


def test_capability_flag():
    assert capabilities_for("3.4.0", "server").retry_try_is_next is True
    assert capabilities_for("3.3.2", "server").retry_try_is_next is False
    assert capabilities_for(None, "unknown").retry_try_is_next is False
