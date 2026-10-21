"""Deploy to a local folder asks Airflow to reparse the file; a refusal never fails the deploy.

The fake answers like Airflow's ``PUT /parseDagFile/{file_token}``: 201 on success, 403 when
the caller may not edit the DAG (and, from Airflow 3.4, for a file with no registered DAG
without the ``REPARSE_ALL`` view), 404 for a file Airflow does not know.
"""

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


def _client(tmp_path: Path, reparse: httpx2.Response, calls: list[str]) -> TestClient:
    def fake_airflow(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api/v2")
        calls.append(f"{request.method} {path}")
        if path == "/dags/simple/details":
            return httpx2.Response(
                200, json={"dag_id": "simple", "file_token": "tok", "fileloc": "simple.py"}
            )
        if path == "/dags/simple/dagRuns":
            return httpx2.Response(200, json={"dag_runs": []})
        if path == "/parseDagFile/tok" and request.method == "PUT":
            return reparse
        return httpx2.Response(404, json={"detail": f"unexpected {path}"})

    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", import_check_on_deploy=False),
        AuthBackend(),
    )
    app = create_app(services)
    api = next(r.app for r in app.routes if getattr(r, "path", "") == "/api")

    async def gateway():
        async with AirflowGateway(
            "http://airflow", transport=httpx2.MockTransport(fake_airflow)
        ) as gw:
            yield gw

    api.dependency_overrides[get_gateway] = gateway
    return TestClient(app)


def _deploy(client: TestClient, spec: dict) -> dict:
    resp = client.put("/api/specs/simple", json={"spec": spec})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deployed"], body
    return body


def test_reparse_requested(tmp_path, simple_spec_data):
    calls: list[str] = []
    body = _deploy(_client(tmp_path, httpx2.Response(201), calls), simple_spec_data)
    assert body["reparse_requested"] is True
    assert body["message"].endswith("; reparse requested")
    assert "PUT /parseDagFile/tok" in calls


def test_reparse_forbidden_is_explained(tmp_path, simple_spec_data):
    calls: list[str] = []
    refused = httpx2.Response(
        403, json={"detail": "You do not have permission to reparse files with no registered Dag"}
    )
    body = _deploy(_client(tmp_path, refused, calls), simple_spec_data)
    assert body["reparse_requested"] is False
    assert (tmp_path / "simple.py").exists()
    msg = body["message"]
    assert "Airflow refused the reparse request (403" in msg
    assert "REPARSE_ALL" in msg
    assert msg.endswith("Airflow will pick it up on the next bundle refresh")
    assert "could not contact Airflow" not in msg


def test_reparse_unknown_file(tmp_path, simple_spec_data):
    body = _deploy(
        _client(tmp_path, httpx2.Response(404, json={"detail": "File not found"}), []),
        simple_spec_data,
    )
    assert body["reparse_requested"] is False
    assert "does not know the DAG file yet (404)" in body["message"]


def test_reparse_already_queued(tmp_path, simple_spec_data):
    """Airflow answers a second request for a queued file with a unique-constraint 409."""
    queued = httpx2.Response(
        409, json={"detail": {"reason": "Unique constraint violation", "statement": "hidden"}}
    )
    body = _deploy(_client(tmp_path, queued, []), simple_spec_data)
    assert body["reparse_requested"] is True
    assert body["message"].endswith("; reparse already requested")


def test_reparse_server_error(tmp_path, simple_spec_data):
    body = _deploy(
        _client(tmp_path, httpx2.Response(500, json={"detail": "boom"}), []), simple_spec_data
    )
    assert body["reparse_requested"] is False
    assert "the reparse request failed (500: boom)" in body["message"]
