"""Run operations: request bodies per Airflow version (strict bodies reject unknown fields)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest

from autoair.api.routers.airflow import BackfillRequest, ClearRequest, capabilities_for
from autoair.gateway import AirflowError, AirflowGateway


class Recorder:
    def __init__(self, response: Any = None, status: int = 200) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] = []
        self.response = response if response is not None else {}
        self.status = status

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.raw_path.decode().split("?", 1)[0]  # keeps %2F visible
        self.calls.append((request.method, path, dict(request.url.params), body))
        return httpx2.Response(self.status, json=self.response)

    def gateway(self) -> AirflowGateway:
        return AirflowGateway("http://airflow", transport=httpx2.MockTransport(self))


def test_clear_sends_only_set_fields():
    rec = Recorder()
    asyncio.run(rec.gateway().clear_run("etl", "r/1", only_failed=True, dry_run=True))
    method, path, _, body = rec.calls[0]
    assert (method, path) == ("POST", "/api/v2/dags/etl/dagRuns/r%2F1/clear")
    assert body == {"dry_run": True, "only_failed": True}


def test_clear_only_new_and_latest_version():
    rec = Recorder()
    asyncio.run(rec.gateway().clear_run("etl", "r1", only_new=True, run_on_latest_version=False))
    assert rec.calls[0][3] == {
        "dry_run": False,
        "only_failed": False,
        "only_new": True,
        "run_on_latest_version": False,
    }
    with pytest.raises(AirflowError, match="mutually exclusive"):
        asyncio.run(rec.gateway().clear_run("etl", "r1", only_new=True, only_failed=True))


def test_keep_task_state_uses_clear_task_instances():
    rec = Recorder()
    asyncio.run(
        rec.gateway().clear_run("etl", "r1", only_failed=True, keep_task_state=True, note="why")
    )
    _, path, _, body = rec.calls[0]
    assert path == "/api/v2/dags/etl/clearTaskInstances"
    assert body == {
        "dry_run": False,
        "only_failed": True,
        "note": "why",
        "dag_run_id": "r1",
        "keep_task_state": True,
        "reset_dag_runs": True,
    }
    with pytest.raises(AirflowError, match="only_new"):
        asyncio.run(rec.gateway().clear_run("etl", "r1", keep_task_state=True, only_new=True))


def test_clear_request_validation():
    with pytest.raises(ValueError, match="mutually exclusive"):
        ClearRequest(only_new=True, only_failed=True)
    with pytest.raises(ValueError, match="cannot be combined"):
        ClearRequest(only_new=True, keep_task_state=True)
    assert ClearRequest().run_on_latest_version is None


def test_backfill_bodies():
    rec = Recorder()
    gw = rec.gateway()
    asyncio.run(gw.backfill("etl", "2026-01-01", "2026-01-02", dry_run=True, drain_dag=True))
    _, path, _, body = rec.calls[0]
    assert path == "/api/v2/backfills/dry_run"
    assert "drain_dag" not in body  # never sent to the dry run
    assert "run_on_latest_version" not in body
    assert body["reprocess_behavior"] == "none"
    asyncio.run(
        gw.backfill(
            "etl",
            "2026-01-01",
            "2026-01-02",
            reprocess_behavior="failed",
            run_on_latest_version=True,
            drain_dag=True,
            dag_run_conf={"a": 1},
        )
    )
    _, path, _, body = rec.calls[1]
    assert path == "/api/v2/backfills"
    assert body["reprocess_behavior"] == "failed"
    assert body["run_on_latest_version"] is True
    assert body["drain_dag"] is True
    assert body["dag_run_conf"] == {"a": 1}
    with pytest.raises(ValueError, match="reprocess_behavior"):
        BackfillRequest(from_date="a", to_date="b", reprocess_behavior="all")  # type: ignore[arg-type]


def test_scheduling_state_patch():
    rec = Recorder()
    asyncio.run(rec.gateway().set_scheduling_state("etl", "draining"))
    method, path, params, body = rec.calls[0]
    assert (method, path) == ("PATCH", "/api/v2/dags/etl")
    assert params == {"update_mask": "scheduling_state"}
    assert body == {"scheduling_state": "draining"}


def test_asset_event_and_materialize():
    rec = Recorder({"id": 3})
    gw = rec.gateway()
    asyncio.run(gw.create_asset_event(7, {"rows": 3}))
    asyncio.run(gw.create_asset_event(7))
    asyncio.run(gw.materialize_asset(7))
    assert rec.calls[0][1:] == ("/api/v2/assets/events", {}, {"asset_id": 7, "extra": {"rows": 3}})
    assert rec.calls[1][3] == {"asset_id": 7, "extra": {}}
    assert rec.calls[2][:2] == ("POST", "/api/v2/assets/7/materialize")


def test_dag_status_reports_scheduling_state():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/details"):
            return httpx2.Response(200, json={"is_paused": False, "scheduling_state": "draining"})
        return httpx2.Response(200, json={"dag_runs": []})

    gw = AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler))
    status = asyncio.run(gw.dag_status("etl"))
    assert status.scheduling_state == "draining"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (None, set()),
        ("garbage", set()),
        ("3.0.6", {"materialize"}),
        (
            "3.1.0",
            {"materialize", "clear_run_on_latest_version", "backfill_run_on_latest_version"},
        ),
        (
            "3.3.2",
            {
                "materialize",
                "clear_run_on_latest_version",
                "backfill_run_on_latest_version",
                "clear_only_new",
            },
        ),
        (
            "3.4.0b1",
            {
                "materialize",
                "clear_run_on_latest_version",
                "backfill_run_on_latest_version",
                "clear_only_new",
                "keep_task_state",
                "scheduling_state",
                "retry_try_is_next",
            },
        ),
    ],
)
def test_capabilities_by_version(version, expected):
    caps = capabilities_for(version, "server").model_dump()
    on = {k for k, v in caps.items() if v is True}
    assert on == expected
    assert caps["source"] == ("server" if expected else "unknown")
