"""Typed connection form metadata (hook-meta passthrough) and secrets-backend awareness."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.api.deps import get_gateway
from autoair.auth import AuthBackend
from autoair.gateway import AirflowGateway
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

SQL_OP = "airflow.providers.common.sql.operators.sql.SQLExecuteQueryOperator"


def _client(tmp_path: Path, handler, **settings) -> TestClient:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", **settings), AuthBackend()
    )
    app = create_app(services)
    api = next(r.app for r in app.routes if getattr(r, "path", "") == "/api")

    async def gateway():
        async with AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler)) as gw:
            yield gw

    api.dependency_overrides[get_gateway] = gateway
    return TestClient(app)


def test_hook_meta_passthrough(tmp_path):
    def airflow(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/ui/connections/hook_meta"
        return httpx2.Response(
            200,
            json=[
                {
                    "connection_type": "postgres",
                    "hook_name": "Postgres",
                    "standard_fields": {"url_schema": {"hidden": False, "title": "Database"}},
                    "extra_fields": None,
                }
            ],
        )

    body = _client(tmp_path, airflow).get("/api/airflow/connections/hook-meta").json()
    assert body == [
        {
            "connection_type": "postgres",
            "hook_name": "Postgres",
            "hook_class_name": None,
            "default_conn_name": None,
            "standard_fields": {
                "description": None,
                "url_schema": {"hidden": False, "title": "Database", "placeholder": None},
                "host": None,
                "port": None,
                "login": None,
                "password": None,
            },
            "extra_fields": {},
        }
    ]


def test_hook_meta_absent_in_airflow_gives_empty_list(tmp_path):
    client = _client(tmp_path, lambda r: httpx2.Response(404, json={"detail": "Not Found"}))
    response = client.get("/api/airflow/connections/hook-meta")
    assert response.status_code == 200
    assert response.json() == []


def test_hook_meta_forbidden_by_airflow_is_forwarded(tmp_path):
    client = _client(tmp_path, lambda r: httpx2.Response(403, json={"detail": "Forbidden"}))
    assert client.get("/api/airflow/connections/hook-meta").status_code == 403


def test_meta_reports_secrets_backend(tmp_path):
    def nothing(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={})

    assert _client(tmp_path, nothing).get("/api/meta").json()["secrets_backend"] is False
    meta = _client(tmp_path, nothing, airflow_secrets_backend=True).get("/api/meta").json()
    assert meta["secrets_backend"] is True


def test_ids_unknown_to_the_metastore_are_not_validation_errors(tmp_path):
    """Connections/variables from a secrets backend are invisible to the REST API: the
    validator never checks their existence."""
    spec = {
        "dag_id": "vaulted",
        "nodes": [
            {
                "kind": "operator",
                "id": "q",
                "task_id": "q",
                "operator": SQL_OP,
                "params": {
                    "conn_id": "only_in_vault",
                    "sql": "select '{{ var.value.only_in_vault }}'",
                },
            }
        ],
    }
    client = _client(tmp_path, lambda r: httpx2.Response(404, json={"detail": "nope"}))
    report = client.post("/api/validate", json=spec).json()
    assert report["ok"], report
    assert not [p for p in report["problems"] if "only_in_vault" in p["message"]]


def test_meta_reports_airflow_public_url(tmp_path):
    def nothing(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={})

    meta = _client(tmp_path, nothing, airflow_url="http://airflow:8080/").get("/api/meta").json()
    assert meta["airflow_public_url"] == "http://airflow:8080"
    meta = (
        _client(
            tmp_path,
            nothing,
            airflow_url="http://airflow:8080",
            sso={"airflow_public_url": "https://airflow.example.com/"},
        )
        .get("/api/meta")
        .json()
    )
    assert meta["airflow_public_url"] == "https://airflow.example.com"


def test_capabilities_and_run_ops_routes(tmp_path):
    seen: list[tuple[str, str, bytes]] = []

    def airflow(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.url.path == "/api/v2/version":
            return httpx2.Response(200, json={"version": "3.4.0"})
        if request.url.path.endswith("/details"):
            return httpx2.Response(200, json={"scheduling_state": "draining"})
        if request.url.path.endswith("/dagRuns"):
            return httpx2.Response(200, json={"dag_runs": []})
        return httpx2.Response(200, json={})

    client = _client(tmp_path, airflow)
    caps = client.get("/api/airflow/capabilities").json()
    assert caps["keep_task_state"] is True
    assert caps["scheduling_state"] is True
    status = client.put("/api/airflow/dags/etl/scheduling-state", json={"state": "draining"})
    assert status.json()["scheduling_state"] == "draining"
    assert (
        client.put("/api/airflow/dags/etl/scheduling-state", json={"state": "x"}).status_code == 422
    )
    bad = client.post(
        "/api/airflow/dags/etl/runs/r1/clear", json={"only_new": True, "only_failed": True}
    )
    assert bad.status_code == 422
    ok = client.post("/api/airflow/dags/etl/runs/r1/clear", json={"keep_task_state": True})
    assert ok.status_code == 200
    assert seen[-1][1] == "/api/v2/dags/etl/clearTaskInstances"
    assert client.post("/api/airflow/assets/7/events", json={"extra": {"a": 1}}).status_code == 200
    assert seen[-1][1] == "/api/v2/assets/events"
    assert client.post("/api/airflow/assets/7/materialize").status_code == 200
    assert seen[-1][1] == "/api/v2/assets/7/materialize"


def test_capabilities_fall_back_to_the_installed_airflow(tmp_path):
    client = _client(tmp_path, lambda r: httpx2.Response(503, json={"detail": "down"}))
    caps = client.get("/api/airflow/capabilities").json()
    assert caps["source"] in ("installed", "unknown")
