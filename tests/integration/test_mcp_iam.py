"""MCP tools need the same AutoAir permissions as the REST API (security review finding #1:
``/mcp`` used to bypass IAM, so any authenticated user could write DAGs and run code)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.optional import importorskip_mcp

importorskip_mcp()

from autoair.api.app import build_services, create_app  # noqa: E402
from autoair.auth import TokenVerifier, bearer_passthrough  # noqa: E402
from autoair.iam.config import IamSettings  # noqa: E402
from autoair.mcp.iam import TOOL_PERMISSIONS  # noqa: E402
from autoair.mcp.server import create_mcp_server  # noqa: E402
from autoair.settings import Settings  # noqa: E402

AIRFLOW = "http://airflow.test"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}
SPEC = {
    "dag_id": "mcp_iam",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def _jwt(**claims: Any) -> str:
    def enc(o: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS512', 'typ': 'JWT'})}.{enc(claims)}.sig"


def _as(role: str) -> dict[str, str]:
    token = _jwt(sub=role.lower(), role=role, exp=time.time() + 3600)
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


def _client(tmp_path: Path, **iam: Any) -> TestClient:
    settings = Settings(
        dags_folder=tmp_path / "dags",
        subfolder="",
        airflow_url=AIRFLOW,
        ai={"enabled": False},
        import_check_on_deploy=False,
        iam=IamSettings(strategies=["role_mapping"], **iam),
    )
    verifier = TokenVerifier(
        AIRFLOW,
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json={"dags": []})),
    )
    services = build_services(settings, bearer_passthrough(verifier))
    return TestClient(create_app(services), raise_server_exceptions=False)


def _call(client: TestClient, headers: dict[str, str], name: str, args: dict[str, Any]) -> Any:
    resp = client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _tools(client: TestClient, headers: dict[str, str]) -> set[str]:
    resp = client.post(
        "/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    return {t["name"] for t in resp.json()["result"]["tools"]}


def test_users_without_permissions_cannot_write_or_execute(tmp_path: Path) -> None:
    client = _client(tmp_path)
    nobody = _as("Public")  # no AutoAir role
    result = _call(client, nobody, "save_dag_spec", {"spec": SPEC, "confirm": True})
    assert result["isError"]
    assert "spec:write" in result["content"][0]["text"]
    assert not list(tmp_path.rglob("mcp_iam*"))
    assert _call(client, nobody, "delete_dag_spec", {"dag_id": "x", "confirm": True})["isError"]
    assert _tools(client, nobody) == set()

    viewer = _as("Viewer")  # read-only: no code execution through the import check
    denied = _call(client, viewer, "validate_dag_spec", {"spec": SPEC, "import_check": True})
    assert denied["isError"]
    assert "code:execute" in denied["content"][0]["text"]
    assert not _call(client, viewer, "validate_dag_spec", {"spec": SPEC})["isError"]
    assert "save_dag_spec" not in _tools(client, viewer)
    assert "list_operators" in _tools(client, viewer)


def test_admins_can_save_unless_approval_is_required(tmp_path: Path) -> None:
    admin = _as("Admin")
    client = _client(tmp_path)
    assert not _call(client, admin, "save_dag_spec", {"spec": SPEC, "confirm": True})["isError"]
    assert list(tmp_path.rglob("mcp_iam*"))

    strict = _client(tmp_path / "strict", require_approval=True)
    refused = _call(strict, admin, "save_dag_spec", {"spec": SPEC, "confirm": True})
    assert refused["isError"]
    assert "approved" in refused["content"][0]["text"]


def test_every_mcp_tool_is_classified(tmp_path: Path) -> None:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", ai={"enabled": False}),
        bearer_passthrough(None),
    )
    mcp = create_mcp_server(services)
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names - set(TOOL_PERMISSIONS) == set(), "classify new MCP tools in autoair/mcp/iam.py"


def test_feature_flags_disable_mcp_tools(tmp_path: Path) -> None:
    """Review finding 6.2: disabled features must be off for agents too (any transport)."""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    services = build_services(
        Settings(
            dags_folder=tmp_path,
            subfolder="",
            ai={"enabled": False},
            features={"deploy": False, "runs": False, "python_code": False},
        ),
        bearer_passthrough(None),
    )
    mcp = create_mcp_server(services)

    async def run() -> None:
        async with Client(mcp) as client:  # in-memory, like stdio: no HTTP principal
            names = {t.name for t in await client.list_tools()}
            assert {
                "save_dag_spec",
                "delete_dag_spec",
                "trigger_dag",
                "import_python_dag",
            }.isdisjoint(names)
            assert "list_operators" in names
            for tool, args in (
                ("save_dag_spec", {"spec": SPEC, "confirm": True}),
                ("import_python_dag", {"source": "x = 1"}),
            ):
                with pytest.raises(ToolError, match="disabled"):
                    await client.call_tool(tool, args)

    asyncio.run(run())
    assert not list(tmp_path.rglob("mcp_iam*"))


def test_airflow_feature_off_disables_the_mcp_tools_that_call_airflow(tmp_path: Path) -> None:
    """features.airflow off (``autoair local``): no MCP tool reaches an Airflow server."""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    services = build_services(
        Settings(
            dags_folder=tmp_path, subfolder="", ai={"enabled": False}, features={"airflow": False}
        ),
        bearer_passthrough(None),
    )
    mcp = create_mcp_server(services)
    airflow_tools = {
        "list_connections",
        "list_variables",
        "get_dag_status",
        "list_dag_runs",
        "get_task_log",
        "trigger_dag",
    }

    async def run() -> None:
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
            assert airflow_tools.isdisjoint(names)
            assert "list_operators" in names
            with pytest.raises(ToolError, match="feature 'airflow' is off"):
                await client.call_tool("get_dag_status", {"dag_id": "x"})

    asyncio.run(run())
