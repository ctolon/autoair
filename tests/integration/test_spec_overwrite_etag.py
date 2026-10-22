"""Overwriting an existing DAG spec needs the etag it was loaded with (optimistic locking):
REST answers 428 without ``base_etag`` / ``If-Match``, MCP refuses and returns the current
etag so the agent can retry; ``storage.require_base_etag=false`` relaxes it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.api.deps import get_gateway
from autoair.auth import AuthBackend
from autoair.gateway import AirflowGateway
from autoair.settings import Settings
from autoair.storage.base import spec_etag

pytestmark = pytest.mark.airflow

SPEC: dict[str, Any] = {
    "dag_id": "etag_dag",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def _client(tmp_path: Path, **storage: Any) -> TestClient:
    extra: dict[str, Any] = {"storage": storage} if storage else {}
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        import_check_on_deploy=False,
        ai={"enabled": False},
        iam={"anonymous_roles": ["Admin"]},
        **extra,
    )
    app = create_app(build_services(settings, AuthBackend()))
    api = next(r.app for r in app.routes if getattr(r, "path", "") == "/api")

    async def gateway():  # type: ignore[no-untyped-def]
        async with AirflowGateway(
            "http://airflow",
            transport=httpx2.MockTransport(lambda r: httpx2.Response(404, json={})),
        ) as gw:
            yield gw

    api.dependency_overrides[get_gateway] = gateway
    return TestClient(app)


def test_overwrite_without_etag_is_428(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.put("/api/specs/etag_dag", json={"spec": SPEC})
    assert first.status_code == 200, first.text  # a new DAG needs no etag
    assert first.json()["deployed"]
    etag = first.json()["etag"]

    blind = client.put("/api/specs/etag_dag", json={"spec": {**SPEC, "description": "v2"}})
    assert blind.status_code == 428, blind.text
    assert etag in blind.json()["detail"]
    assert blind.headers["etag"] == f'"{etag}"'
    assert '"description"' not in (tmp_path / "etag_dag.autoair.json").read_text()

    with_body = client.put(
        "/api/specs/etag_dag", json={"spec": {**SPEC, "description": "v2"}, "base_etag": etag}
    )
    assert with_body.status_code == 200, with_body.text
    assert with_body.json()["deployed"]
    with_header = client.put(
        "/api/specs/etag_dag",
        json={"spec": {**SPEC, "description": "v3"}},
        headers={"If-Match": f'"{with_body.json()["etag"]}"'},
    )
    assert with_header.status_code == 200, with_header.text
    assert with_header.json()["deployed"]


def test_setting_relaxes_the_requirement(tmp_path: Path) -> None:
    client = _client(tmp_path, require_base_etag=False)
    assert client.put("/api/specs/etag_dag", json={"spec": SPEC}).json()["deployed"]
    again = client.put("/api/specs/etag_dag", json={"spec": {**SPEC, "description": "v2"}})
    assert again.status_code == 200, again.text
    assert again.json()["deployed"]


def test_mcp_save_needs_the_current_etag(tmp_path: Path) -> None:
    from tests.optional import importorskip_mcp

    importorskip_mcp()
    from fastmcp import Client

    from autoair.mcp import create_mcp_server

    services = build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())
    server = create_mcp_server(lambda: services)

    async def go() -> None:
        async with Client(server) as client:

            async def save(args: dict[str, Any]) -> dict[str, Any]:
                res = await client.call_tool("save_dag_spec", args, raise_on_error=False)
                assert res.structured_content is not None, res
                return res.structured_content

            created = await save({"spec": SPEC, "confirm": True})
            assert created["saved"] is True, created
            current = spec_etag(services.repository.target.get("etag_dag").spec)
            assert created["etag"] == current

            changed = {**SPEC, "description": "v2"}
            dry = await save({"spec": changed})
            assert dry["current_etag"] == current

            blind = await save({"spec": changed, "confirm": True})
            assert blind["saved"] is False
            assert blind["current_etag"] == current
            assert current in blind["message"]

            stale = await save({"spec": changed, "confirm": True, "base_etag": "0" * 64})
            assert stale["saved"] is False
            assert stale["current_etag"] == current

            ok = await save({"spec": changed, "confirm": True, "base_etag": current})
            assert ok["saved"] is True, ok
            assert services.repository.get("etag_dag").description == "v2"

    anyio.run(go)
