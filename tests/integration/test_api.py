from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.api.deps import get_gateway
from autoair.auth import AuthBackend, TokenVerifier, bearer_passthrough
from autoair.gateway import AirflowGateway
from autoair.settings import Settings

pytestmark = pytest.mark.airflow


def fake_airflow(request: httpx2.Request) -> httpx2.Response:
    path = request.url.path.removeprefix("/api/v2")
    if path == "/connections":
        return httpx2.Response(
            200, json={"connections": [{"connection_id": "pg", "conn_type": "postgres"}]}
        )
    if path.startswith("/dags/") and path.endswith("/details"):
        return httpx2.Response(404, json={"detail": "not found"})
    return httpx2.Response(404, json={"detail": f"unexpected {path}"})


def make_client(tmp_path: Path, auth: AuthBackend | None = None, **settings) -> TestClient:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", **settings), auth or AuthBackend()
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


def test_meta_and_catalog(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/meta").json()["airflow_version"].startswith("3.")
    ops = client.get("/api/catalog/operators").json()
    assert any(o["class_name"] == "BashOperator" for o in ops)
    bash = client.get(
        "/api/catalog/operators/airflow.providers.standard.operators.bash.BashOperator"
    ).json()
    assert "bash_command" in bash["params_schema"]["properties"]


def test_codegen_validate_deploy_roundtrip(tmp_path, simple_spec_data):
    client = make_client(tmp_path, import_check_on_deploy=True)
    code = client.post("/api/codegen", json=simple_spec_data).json()
    assert "BashOperator" in code["code"]
    assert client.post("/api/validate", json=simple_spec_data).json()["ok"]

    res = client.put("/api/specs/simple", json={"spec": simple_spec_data}).json()
    assert res["deployed"], res
    assert (tmp_path / "simple.py").exists()
    assert "next bundle refresh" in res["message"]
    assert client.get("/api/specs").json()[0]["dag_id"] == "simple"
    assert client.get("/api/specs/simple").json()["spec"]["nodes"][1]["params"] == {
        "bash_command": "echo hi"
    }
    response = client.delete("/api/specs/simple", headers={"If-Match": res["etag"]})
    assert response.json()["deleted"] is True
    assert response.json()["status"] == "deleted"


def test_deploy_refuses_invalid_spec(tmp_path, simple_spec_data):
    simple_spec_data["edges"].append({"source": "b", "target": "a"})
    res = make_client(tmp_path).put("/api/specs/simple", json={"spec": simple_spec_data}).json()
    assert res["deployed"] is False
    assert not [p for p in tmp_path.iterdir() if p.name != ".autoair"]


def test_policy_enforced_on_deploy(tmp_path, simple_spec_data):
    client = make_client(tmp_path, allow_privileged_operators=False)
    res = client.put("/api/specs/simple", json={"spec": simple_spec_data}).json()
    assert res["deployed"] is False
    assert any(p["code"] == "policy" for p in res["report"]["problems"])


def test_airflow_proxy(tmp_path):
    assert make_client(tmp_path).get("/api/airflow/connections").json()[0]["connection_id"] == "pg"


def test_bearer_auth_required(tmp_path):
    client = make_client(tmp_path, auth=bearer_passthrough())
    assert client.get("/api/meta").status_code == 401
    assert client.get("/api/meta", headers={"Authorization": "Bearer x"}).status_code == 200
    client.cookies.set("_token", "x")
    assert client.get("/api/meta").status_code == 200


def test_standalone_token_verified_against_airflow(tmp_path):
    calls = []

    def airflow(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.headers.get("authorization"))
        ok = request.headers.get("authorization") == "Bearer good"
        return httpx2.Response(200 if ok else 401, json={"dags": []})

    verifier = TokenVerifier("http://airflow", transport=httpx2.MockTransport(airflow))
    client = make_client(tmp_path, auth=bearer_passthrough(verifier))
    assert client.get("/api/specs", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.get("/api/specs", headers={"Authorization": "Bearer good"}).status_code == 200
    assert client.get("/api/specs", headers={"Authorization": "Bearer good"}).status_code == 200
    assert calls == ["Bearer bad", "Bearer good"]  # second success served from cache


def test_openapi_is_exported(tmp_path):
    client = make_client(tmp_path)
    schema = client.get("/api/openapi.json").json()
    assert "DagSpec" in json.dumps(schema)
