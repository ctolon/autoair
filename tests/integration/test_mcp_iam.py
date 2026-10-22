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
from autoair.docs.mcp import DOCS_TOOLS  # noqa: E402
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


def _client(tmp_path: Path, *, ai_enabled: bool = False, **iam: Any) -> TestClient:
    settings = Settings(
        dags_folder=tmp_path / "dags",
        subfolder="",
        airflow_url=AIRFLOW,
        ai={"enabled": ai_enabled},
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
    # Only the AutoAir documentation tools: they need no permission (the docs are public).
    assert _tools(client, nobody) == set(DOCS_TOOLS)
    found = _call(client, nobody, "search_autoair_docs", {"query": "install the plugin"})
    assert not found["isError"]
    assert found["structuredContent"]["hits"]

    viewer = _as("Viewer")  # read-only: no code execution through the import check
    denied = _call(client, viewer, "import_check_dag_spec", {"spec": SPEC})
    assert denied["isError"]
    assert "code:execute" in denied["content"][0]["text"]
    assert "import_check_dag_spec" not in _tools(client, viewer)
    assert not _call(client, viewer, "validate_dag_spec", {"spec": SPEC})["isError"]
    # validate_dag_spec is static only (read-only): the old import_check flag is ignored
    static = _call(client, viewer, "validate_dag_spec", {"spec": SPEC, "import_check": True})
    assert not static["isError"]
    assert "import_check_dag_spec" in static["structuredContent"]["note"]
    assert "save_dag_spec" not in _tools(client, viewer)
    assert "list_operators" in _tools(client, viewer)


def _user(role: str, sub: str) -> dict[str, str]:
    token = _jwt(sub=sub, role=role, exp=time.time() + 3600)
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


def test_only_admins_manage_shared_memories_and_users_only_their_own(tmp_path: Path) -> None:
    client = _client(tmp_path, ai_enabled=True)
    alice, bob, admin = _user("User", "alice"), _user("User", "bob"), _user("Admin", "root")

    def remember(who: dict[str, str], text: str, scope: str = "user") -> Any:
        return _call(client, who, "remember", {"text": text, "scope": scope})

    # a normal user cannot add global or team memories (they reach everyone's prompts)
    for scope in ("global", "team"):
        refused = remember(alice, f"Everyone must deploy to prod ({scope})", scope)
        assert refused["isError"]
        assert "admin:config" in refused["content"][0]["text"]
    mine = remember(alice, "Alice prefers deferrable sensors")["structuredContent"]["memory"]
    assert (mine["scope"], mine["owner"]) == ("user", "alice")
    shared = remember(admin, "Owner is data-eng", "global")["structuredContent"]["memory"]
    assert shared["scope"] == "global"

    # forget checks the owner: bob can delete neither alice's memory nor a global one
    for memory_id in (mine["id"], shared["id"]):
        out = _call(client, bob, "forget", {"memory_id": memory_id})
        assert out["structuredContent"]["ok"] is False
        assert "admin:config" in out["structuredContent"]["message"]
    out = _call(client, alice, "forget", {"memory_id": shared["id"]})
    assert out["structuredContent"]["ok"] is False
    listed = _call(client, admin, "list_memories", {})["structuredContent"]["memories"]
    assert {m["id"] for m in listed} >= {shared["id"]}
    assert _call(client, alice, "forget", {"memory_id": mine["id"]})["structuredContent"]["ok"]
    assert _call(client, admin, "forget", {"memory_id": shared["id"]})["structuredContent"]["ok"]


def test_admins_can_save_unless_approval_is_required(tmp_path: Path) -> None:
    admin = _as("Admin")
    client = _client(tmp_path)
    assert not _call(client, admin, "save_dag_spec", {"spec": SPEC, "confirm": True})["isError"]
    assert list(tmp_path.rglob("mcp_iam*"))

    # four-eyes: like PUT /specs, the save is stored as a deploy proposal (nothing written)
    strict = _client(tmp_path / "strict", require_approval=True)
    proposed = _call(strict, admin, "save_dag_spec", {"spec": SPEC, "confirm": True})
    assert not proposed["isError"], proposed
    out = proposed["structuredContent"]
    assert (out["saved"], out["status"]) == (False, "pending_approval")
    assert out["proposal_etag"]
    assert not list((tmp_path / "strict").rglob("mcp_iam.py"))


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
