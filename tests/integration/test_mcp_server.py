from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.optional import importorskip_mcp

importorskip_mcp()

from fastmcp import Client  # noqa: E402

from autoair.ai.testing import FakeCall, FakeLLM  # noqa: E402
from autoair.api.app import Services, build_services  # noqa: E402
from autoair.auth import AuthBackend, bearer_passthrough  # noqa: E402
from autoair.gateway import AirflowGateway  # noqa: E402
from autoair.mcp import RepositoryStore, airflow_credentials, create_mcp_server  # noqa: E402
from autoair.mcp.http import create_mcp_asgi_app  # noqa: E402
from autoair.settings import Settings  # noqa: E402
from autoair.storage import FileSystemRepository  # noqa: E402

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


def spec_data(dag_id: str = "mcp_dag") -> dict[str, Any]:
    return {
        "dag_id": dag_id,
        "nodes": [
            {
                "kind": "operator",
                "id": "hello",
                "task_id": "hello",
                "operator": BASH,
                "params": {"bash_command": "echo hi"},
            }
        ],
    }


class FakeAirflow:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v2")
        if path == "/connections":
            return httpx2.Response(
                200,
                json={
                    "connections": [
                        {"connection_id": "pg", "conn_type": "postgres"},
                        {"connection_id": "s3", "conn_type": "aws"},
                    ]
                },
            )
        if path == "/variables":
            return httpx2.Response(200, json={"variables": [{"key": "env", "value": "secret"}]})
        if path == "/dags/mcp_dag" and request.method == "PATCH":
            return httpx2.Response(200, json={})
        if path == "/dags/mcp_dag/dagRuns" and request.method == "POST":
            return httpx2.Response(200, json={"dag_run_id": "manual__1", "state": "queued"})
        if path.endswith("/details"):
            return httpx2.Response(404, json={"detail": "not found"})
        return httpx2.Response(404, json={"detail": f"unexpected {path}"})


@pytest.fixture
def services(tmp_path: Path) -> Services:
    return build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())


@pytest.fixture
def airflow() -> FakeAirflow:
    return FakeAirflow()


def make_server(services: Services, airflow: FakeAirflow, **kwargs: Any) -> Any:
    def gateway(base_url: str, headers: Mapping[str, str]) -> AirflowGateway:
        return AirflowGateway(
            "http://airflow", headers=dict(headers), transport=httpx2.MockTransport(airflow)
        )

    return create_mcp_server(lambda: services, gateway_factory=gateway, **kwargs)


def run(coro_fn: Any) -> Any:
    return anyio.run(coro_fn)


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    return await client.call_tool(name, args or {}, raise_on_error=False)


def test_tools_listed_with_schemas(services: Services, airflow: FakeAirflow) -> None:
    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert list(tools)[:3] == [
                "list_operators",
                "get_operator_schema",
                "get_dagspec_schema",
            ]
            for name in (
                "validate_dag_spec",
                "generate_code",
                "list_dags",
                "get_dag_spec",
                "save_dag_spec",
                "delete_dag_spec",
                "list_connections",
                "list_variables",
                "get_dag_status",
                "trigger_dag",
                "generate_dag_from_prompt",
                "list_node_kinds",
            ):
                assert name in tools
                assert tools[name].output_schema is not None
            save = tools["save_dag_spec"]
            assert save.annotations is not None
            assert save.annotations.destructive_hint is True
            assert "confirm" in save.input_schema["properties"]
            assert tools["list_operators"].annotations.read_only_hint is True  # type: ignore[union-attr]
            # Context parameters are not exposed as tool arguments.
            assert "ctx" not in tools["list_connections"].input_schema.get("properties", {})

    run(go)


def test_tools_that_run_code_are_not_marked_read_only(tmp_path: Path) -> None:
    """MCP clients skip their approval prompt for read-only tools: a tool that runs Python
    on the server (importing a DAG file, a preserve preview) must never claim to be one."""
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", allow_python_code=True), AuthBackend()
    )

    async def go() -> None:
        async with Client(make_server(services, FakeAirflow())) as client:
            tools = {t.name: t for t in await client.list_tools()}
            for name in ("import_python_dag", "preview_dag_source"):
                assert name in tools
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False, name

    run(go)


def test_catalog_validate_generate(services: Services, airflow: FakeAirflow) -> None:
    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            ops = (await call(client, "list_operators", {"query": "bash"})).structured_content
            assert ops is not None
            assert BASH in [o["fqn"] for o in ops["operators"]]

            limited = await call(client, "list_operators", {"limit": 2})
            assert limited.structured_content is not None
            assert limited.structured_content["truncated"] is True

            detail = await call(client, "get_operator_schema", {"fqn": BASH})
            assert detail.structured_content is not None
            assert "bash_command" in detail.structured_content["params_schema"]["properties"]
            missing = await call(client, "get_operator_schema", {"fqn": "no.such.Op"})
            assert missing.is_error

            schema = await call(client, "get_dagspec_schema", {})
            assert schema.structured_content is not None
            assert "dag_id" in json.dumps(schema.structured_content)

            ok = await call(client, "validate_dag_spec", {"spec": spec_data()})
            assert ok.structured_content == {
                "ok": True,
                "problems": [],
                "import_errors": [],
                "note": None,
            }

            # the import check is its own tool (it runs the generated file's code)
            imported = await call(client, "import_check_dag_spec", {"spec": spec_data()})
            assert imported.structured_content is not None
            assert imported.structured_content["ok"] is True, imported.structured_content
            assert imported.structured_content["import_errors"] == []

            bad = spec_data()
            bad["nodes"][0]["params"] = {"bash_command": 1}
            res = (await call(client, "validate_dag_spec", {"spec": bad})).structured_content
            assert res is not None
            assert res["ok"] is False
            assert res["problems"][0]["path"] == "/nodes/0/params/bash_command"

            code = await call(client, "generate_code", {"spec": spec_data()})
            assert code.structured_content is not None
            assert "BashOperator" in code.structured_content["code"]
            invalid = await call(client, "generate_code", {"spec": {"nodes": []}})
            assert invalid.is_error

            kinds = await call(client, "list_node_kinds", {})
            assert kinds.structured_content is not None
            assert "operator" in [k["kind"] for k in kinds.structured_content["node_kinds"]]

    run(go)


def test_save_get_delete_roundtrip(
    tmp_path: Path, services: Services, airflow: FakeAirflow
) -> None:
    store_dir = tmp_path / "store"
    store = RepositoryStore(FileSystemRepository(store_dir))

    async def go() -> None:
        async with Client(make_server(services, airflow, store=store)) as client:
            dry = await call(client, "save_dag_spec", {"spec": spec_data()})
            assert dry.structured_content is not None
            assert dry.structured_content["saved"] is False
            assert dry.structured_content["dry_run"] is True
            assert not store_dir.exists()

            bad = spec_data()
            bad["nodes"][0]["params"] = {}
            rejected = await call(client, "save_dag_spec", {"spec": bad, "confirm": True})
            assert rejected.structured_content is not None
            assert rejected.structured_content["saved"] is False
            assert rejected.structured_content["problems"]

            saved = await call(client, "save_dag_spec", {"spec": spec_data(), "confirm": True})
            assert saved.structured_content is not None
            assert saved.structured_content["saved"] is True
            assert (store_dir / "mcp_dag.py").exists()

            listed = (await call(client, "list_dags", {})).structured_content
            assert listed is not None
            assert [d["dag_id"] for d in listed["dags"]] == ["mcp_dag"]

            got = await call(client, "get_dag_spec", {"dag_id": "mcp_dag"})
            assert got.structured_content is not None
            assert got.structured_content["dag_id"] == "mcp_dag"

            resource = await client.read_resource("autoair://dags/mcp_dag/spec")
            assert '"mcp_dag"' in resource[0].text  # type: ignore[union-attr]

            prompt = await client.get_prompt("explain_dag", {"dag_id": "mcp_dag"})
            assert "BashOperator" in prompt.messages[0].content.text  # type: ignore[union-attr]

            kept = await call(client, "delete_dag_spec", {"dag_id": "mcp_dag"})
            assert kept.structured_content is not None
            assert kept.structured_content["deleted"] is False
            assert (store_dir / "mcp_dag.py").exists()
            etag = kept.structured_content["current_etag"]
            assert etag
            blind = await call(client, "delete_dag_spec", {"dag_id": "mcp_dag", "confirm": True})
            assert blind.structured_content is not None
            assert blind.structured_content["deleted"] is False
            assert blind.structured_content["current_etag"] == etag
            assert etag in blind.structured_content["message"]
            stale = await call(
                client,
                "delete_dag_spec",
                {"dag_id": "mcp_dag", "confirm": True, "base_etag": "0" * 64},
            )
            assert stale.structured_content is not None
            assert stale.structured_content["deleted"] is False
            assert (store_dir / "mcp_dag.py").exists()
            gone = await call(
                client, "delete_dag_spec", {"dag_id": "mcp_dag", "confirm": True, "base_etag": etag}
            )
            assert gone.structured_content is not None
            assert gone.structured_content["deleted"] is True
            assert not (store_dir / "mcp_dag.py").exists()

            missing = await call(client, "get_dag_spec", {"dag_id": "mcp_dag"})
            assert missing.is_error

    run(go)


def test_default_store_uses_services_repository(services: Services, airflow: FakeAirflow) -> None:
    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            await call(client, "save_dag_spec", {"spec": spec_data(), "confirm": True})
        assert services.repository.get("mcp_dag") is not None

    run(go)


def test_airflow_tools(
    services: Services, airflow: FakeAirflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOAIR_AIRFLOW_TOKEN", "tok")

    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            conns = await call(client, "list_connections", {"conn_type": "postgres"})
            assert conns.structured_content is not None
            assert [c["connection_id"] for c in conns.structured_content["connections"]] == ["pg"]
            assert airflow.requests[-1].headers["authorization"] == "Bearer tok"

            variables = await call(client, "list_variables", {})
            assert variables.structured_content is not None
            assert variables.structured_content["variables"] == [
                {"key": "env", "description": None, "team_name": None}
            ]

            status = await call(client, "get_dag_status", {"dag_id": "nope"})
            assert status.structured_content is not None
            assert status.structured_content["exists"] is False

            dry = await call(client, "trigger_dag", {"dag_id": "mcp_dag"})
            assert dry.structured_content is not None
            assert dry.structured_content["triggered"] is False
            sent = len(airflow.requests)
            assert dry.structured_content["message"].startswith("dry run")

            fired = await call(
                client, "trigger_dag", {"dag_id": "mcp_dag", "conf": {"x": 1}, "confirm": True}
            )
            assert fired.structured_content is not None
            assert fired.structured_content["dag_run_id"] == "manual__1"
            assert len(airflow.requests) == sent + 2  # unpause + trigger
            assert json.loads(airflow.requests[-1].content)["conf"] == {"x": 1}

    run(go)


def test_generate_dag_from_prompt(services: Services, airflow: FakeAirflow) -> None:
    def answer(call: FakeCall) -> dict[str, Any]:
        if "operators" in (call.json_schema or {}).get("properties", {}):
            return {"operators": [BASH], "notes": ""}
        return {"explanation": "hi", "spec": spec_data("generated")}

    llm = FakeLLM([answer, answer])

    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            missing = await call(client, "generate_dag_from_prompt", {"prompt": "say hi"})
            assert missing.is_error
        async with Client(make_server(services, airflow, llm=llm)) as client:
            res = await call(client, "generate_dag_from_prompt", {"prompt": "say hi"})
            assert res.structured_content is not None
            assert res.structured_content["ok"] is True
            assert res.structured_content["spec"]["dag_id"] == "generated"

    run(go)


def test_airflow_credentials_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOAIR_AIRFLOW_TOKEN", raising=False)
    assert airflow_credentials(None) == {}
    monkeypatch.setenv("AUTOAIR_AIRFLOW_TOKEN", "Basic abc")
    assert airflow_credentials(None) == {"Authorization": "Basic abc"}


# --------------------------------------------------------------------------- HTTP mount

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client: TestClient, method: str, params: dict[str, Any], id_: int = 1, **headers: str):
    return client.post(
        "/mcp/",
        headers={**MCP_HEADERS, **headers},
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params},
    )


def _http_app(services: Services, airflow: FakeAirflow, auth: AuthBackend) -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.mount(
        "/mcp", create_mcp_asgi_app(services, server=make_server(services, airflow), auth=auth)
    )
    return TestClient(app)


def _initialize(client: TestClient, **headers: str) -> httpx2.Response:
    return _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
        **headers,
    )


def test_http_app_serves_tools(services: Services, airflow: FakeAirflow) -> None:
    client = _http_app(services, airflow, AuthBackend())
    init = _initialize(client)
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "autoair"
    listed = _rpc(client, "tools/list", {}, 2, **{"mcp-protocol-version": "2025-06-18"})
    assert listed.status_code == 200, listed.text
    names = [t["name"] for t in listed.json()["result"]["tools"]]
    assert "validate_dag_spec" in names

    called = _rpc(
        client,
        "tools/call",
        {"name": "list_connections", "arguments": {}},
        3,
        **{"mcp-protocol-version": "2025-06-18", "Authorization": "Bearer from-request"},
    )
    assert called.status_code == 200, called.text
    assert called.json()["result"]["structuredContent"]["connections"]
    assert airflow.requests[-1].headers["authorization"] == "Bearer from-request"


def test_http_app_applies_auth(services: Services, airflow: FakeAirflow) -> None:
    client = _http_app(services, airflow, bearer_passthrough())
    assert _initialize(client).status_code == 401
    assert _initialize(client, Authorization="Bearer x").status_code == 200


# --------------------------------------------------------------------------- FastMCP features


class HistoryAirflow(FakeAirflow):
    """Adds run history, task instances and logs."""

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api/v2")
        if path == "/dags/mcp_dag/dagRuns" and request.method == "GET":
            runs = [
                {
                    "dag_run_id": f"r{i}",
                    "state": "success",
                    "start_date": f"2026-01-0{i + 1}T00:00:00Z",
                    "end_date": f"2026-01-0{i + 1}T00:05:00Z",
                    "conf": {"secret": "x"},
                }
                for i in range(4)
            ]
            self.requests.append(request)
            return httpx2.Response(200, json={"dag_runs": runs})
        if path.endswith("/taskInstances"):
            self.requests.append(request)
            return httpx2.Response(
                200,
                json={
                    "task_instances": [
                        {"task_id": "hello", "state": "success", "try_number": 1, "duration": 200}
                    ]
                },
            )
        if "/logs/" in path:
            self.requests.append(request)
            return httpx2.Response(200, json={"content": [{"event": "x" * 3000 + "BOOM"}]})
        return super().__call__(request)


def test_new_tools_annotations_resources_and_prompts(
    services: Services, airflow: FakeAirflow
) -> None:
    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            for name in (
                "search_knowledge",
                "apply_spec_patch",
                "review_dag_spec",
                "scan_dag_leaks",
                "optimize_dag_parameters",
                "list_dag_runs",
                "get_task_log",
                "fix_dag_spec",
                "import_python_dag",
                "list_memories",
                "remember",
                "forget",
            ):
                assert name in tools, name
                assert tools[name].output_schema is not None
                assert tools[name].description
            assert tools["get_task_log"].annotations.open_world_hint is True  # type: ignore[union-attr]
            assert tools["trigger_dag"].annotations.idempotent_hint is False  # type: ignore[union-attr]
            assert tools["validate_dag_spec"].annotations.idempotent_hint is True  # type: ignore[union-attr]
            assert tools["forget"].annotations.destructive_hint is True  # type: ignore[union-attr]
            limit = tools["list_operators"].input_schema["properties"]["limit"]
            assert limit["maximum"] == 200

            uris = {str(r.uri) for r in await client.list_resources()}
            assert {"autoair://schema/dagspec", "autoair://operators", "autoair://docs"} <= uris
            templates = {t.uri_template for t in await client.list_resource_templates()}
            assert "autoair://docs/{name}" in templates
            guide = await client.read_resource("autoair://docs/dagspec-authoring")
            assert "Node kinds" in guide[0].text  # type: ignore[union-attr]
            index = await client.read_resource("autoair://operators")
            assert BASH in index[0].text  # type: ignore[union-attr]

            prompts = {p.name for p in await client.list_prompts()}
            assert {
                "build_dag_from_description",
                "review_dag",
                "fix_import_error",
                "optimize_parameters",
                "scan_for_leaks",
                "explain_dag",
                "convert_existing_dag",
            } <= prompts
            fix = await client.get_prompt("fix_import_error", {"dag_id": "d", "error": "Boom"})
            assert "Boom" in fix.messages[0].content.text  # type: ignore[union-attr]

    run(go)


def test_search_patch_review_leaks_and_paging(services: Services, airflow: FakeAirflow) -> None:
    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            found = await call(client, "search_knowledge", {"query": "run a bash command"})
            assert found.structured_content is not None
            assert found.structured_content["retrieval"] == "bm25"
            assert "BashOperator" in found.structured_content["hits"][0]["title"]

            page = await call(client, "list_operators", {"limit": 2, "offset": 1})
            assert page.structured_content is not None
            assert page.structured_content["next_offset"] == 3
            assert len(page.structured_content["operators"]) == 2

            patched = await call(
                client,
                "apply_spec_patch",
                {
                    "spec": spec_data(),
                    "patch": [{"op": "add", "path": "/nodes/@hello/retries", "value": 4}],
                },
            )
            assert patched.structured_content is not None
            assert patched.structured_content["ok"] is True
            assert patched.structured_content["spec"]["nodes"][0]["retries"] == 4
            broken = await call(
                client,
                "apply_spec_patch",
                {"spec": spec_data(), "patch": [{"op": "remove", "path": "/nodes/@x/id"}]},
            )
            assert broken.structured_content is not None
            assert "no node" in broken.structured_content["error"]

            review = await call(client, "review_dag_spec", {"spec": spec_data()})
            assert review.structured_content is not None
            assert review.structured_content["kind"] == "review"
            assert review.structured_content["proposed_ok"] is True

            leaky = spec_data()
            leaky["nodes"][0]["params"]["bash_command"] = "echo $DB_PASSWORD"
            leaks = await call(client, "scan_dag_leaks", {"spec": leaky})
            assert leaks.structured_content is not None
            assert leaks.structured_content["findings"][0]["rule"] == "shell-echo-secret"

            no_llm = await call(client, "review_dag_spec", {"spec": spec_data(), "use_ai": True})
            assert no_llm.is_error
            assert "no LLM configured" in no_llm.content[0].text  # type: ignore[union-attr]
            assert (await call(client, "fix_dag_spec", {"spec": spec_data()})).is_error

    run(go)


def test_airflow_history_tools(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_AIRFLOW_TOKEN", "tok")
    history = HistoryAirflow()

    async def go() -> None:
        async with Client(make_server(services, history)) as client:
            runs = await call(client, "list_dag_runs", {"dag_id": "mcp_dag", "limit": 4})
            assert runs.structured_content is not None
            assert len(runs.structured_content["runs"]) == 4
            assert runs.structured_content["runs"][0]["conf"] is None  # conf never leaked

            log = await call(
                client,
                "get_task_log",
                {"dag_id": "mcp_dag", "run_id": "r0", "task_id": "hello", "tail_chars": 500},
            )
            assert log.structured_content is not None
            assert log.structured_content["text"].endswith("BOOM")
            assert log.structured_content["truncated"] is True
            assert "/logs/1" in str(history.requests[-1].url)  # latest try looked up

            opt = await call(client, "optimize_dag_parameters", {"spec": spec_data()})
            assert opt.structured_content is not None
            rules = {f["rule"] for f in opt.structured_content["findings"]}
            assert "tune-timeout-missing" in rules

    run(go)


def test_memory_tools(
    services: Services, airflow: FakeAirflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOAIR_MCP_USER", "alice")
    monkeypatch.delenv("AUTOAIR_AIRFLOW_TOKEN", raising=False)

    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            added = await call(client, "remember", {"text": "Prefer deferrable sensors"})
            assert added.structured_content is not None
            memory = added.structured_content["memory"]
            assert (memory["scope"], memory["owner"]) == ("user", "alice")
            await call(client, "remember", {"text": "Owner is data-eng", "scope": "global"})
            secret = await call(
                client, "remember", {"text": "token ghp_" + "c" * 36, "scope": "global"}
            )
            assert secret.structured_content is not None
            assert secret.structured_content["ok"] is False

            listed = await call(client, "list_memories", {})
            assert listed.structured_content is not None
            assert listed.structured_content["total"] == 2
            text = (await client.read_resource("autoair://memory"))[0].text  # type: ignore[union-attr]
            assert "[user:alice] Prefer deferrable sensors" in text

            gone = await call(client, "forget", {"memory_id": memory["id"]})
            assert gone.structured_content is not None
            assert gone.structured_content["ok"] is True

        monkeypatch.setenv("AUTOAIR_MCP_USER", "bob")
        async with Client(make_server(services, airflow)) as client:
            listed = await call(client, "list_memories", {})
            assert listed.structured_content is not None
            assert [m["text"] for m in listed.structured_content["memories"]] == [
                "Owner is data-eng"
            ]

    run(go)


def test_read_only_policy_blocks_writes(tmp_path: Path, airflow: FakeAirflow) -> None:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", ai={"mcp": {"allow_writes": False}}),
        AuthBackend(),
    )

    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            for name, args in (
                ("save_dag_spec", {"spec": spec_data(), "confirm": True}),
                ("delete_dag_spec", {"dag_id": "mcp_dag", "confirm": True}),
                ("trigger_dag", {"dag_id": "mcp_dag", "confirm": True}),
            ):
                res = await call(client, name, args)
                assert res.is_error, name
                assert "read-only" in res.content[0].text  # type: ignore[union-attr]
        assert airflow.requests == []
        assert not (tmp_path / "mcp_dag.py").exists()

    run(go)


def test_fix_dag_spec_with_llm(services: Services, airflow: FakeAirflow) -> None:
    llm = FakeLLM(
        [
            {
                "summary": "wrong type",
                "findings": [
                    {
                        "severity": "high",
                        "category": "correctness",
                        "title": "Use a string",
                        "message": "m",
                        "node_id": "hello",
                        "field": "bash_command",
                        "patch": [
                            {
                                "op": "replace",
                                "path": "/nodes/@hello/params/bash_command",
                                "value_json": '"echo ok"',
                            }
                        ],
                    }
                ],
            }
        ]
    )
    bad = spec_data()
    bad["nodes"][0]["params"]["bash_command"] = 3

    async def go() -> None:
        async with Client(make_server(services, airflow, llm=llm)) as client:
            res = await call(client, "fix_dag_spec", {"spec": bad})
            assert res.structured_content is not None, res.content
            assert res.structured_content["proposed_ok"] is True
            assert res.structured_content["proposed_spec"]["nodes"][0]["params"] == {
                "bash_command": "echo ok"
            }

    run(go)


def test_import_python_dag_respects_policy(tmp_path: Path, airflow: FakeAirflow) -> None:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", allow_python_code=False), AuthBackend()
    )

    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            res = await call(client, "import_python_dag", {"source": "x = 1"})
            assert res.is_error
            assert "disabled" in res.content[0].text  # type: ignore[union-attr]

    run(go)


def test_app_builds_without_fastmcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import sys

    from autoair.api.app import create_app

    for name in [m for m in sys.modules if m == "fastmcp" or m.startswith("fastmcp.")]:
        monkeypatch.delitem(sys.modules, name)
    for name in ("autoair.mcp.http", "autoair.mcp.server"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "fastmcp", None)  # type: ignore[arg-type]
    services = build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())
    with caplog.at_level("INFO", logger="autoair.api.app"):
        app = create_app(services)
    assert "MCP endpoint disabled" in caplog.text
    client = TestClient(app)
    assert client.get("/api/ai/status").status_code == 200
    assert client.post("/api/ai/leaks", json={"spec": spec_data()}).status_code == 200
    assert client.post("/mcp/", json={}).status_code in (404, 405)


RAW_CODE = """from airflow.sdk import DAG

with DAG(dag_id="mcp_raw", schedule=None):
    pass
"""


def test_raw_code_tools(services: Services, airflow: FakeAirflow) -> None:
    async def go() -> None:
        async with Client(make_server(services, airflow)) as client:
            dry = await call(client, "save_raw_dag_code", {"dag_id": "mcp_raw", "code": RAW_CODE})
            assert dry.structured_content is not None
            assert dry.structured_content["dry_run"] is True
            saved = await call(
                client,
                "save_raw_dag_code",
                {"dag_id": "mcp_raw", "code": RAW_CODE, "confirm": True},
            )
            assert saved.structured_content is not None, saved
            assert saved.structured_content["saved"] is True, saved.structured_content
            got = (await call(client, "get_dag_code", {"dag_id": "mcp_raw"})).structured_content
            assert got == {"dag_id": "mcp_raw", "document_type": "raw", "code": RAW_CODE}
            raws = (await call(client, "list_dags", {"document_type": "raw"})).structured_content
            assert raws is not None
            assert [d["dag_id"] for d in raws["dags"]] == ["mcp_raw"]

            await call(client, "save_dag_spec", {"spec": spec_data(), "confirm": True})
            visual = (await call(client, "get_dag_code", {"dag_id": "mcp_dag"})).structured_content
            assert visual is not None
            assert visual["document_type"] == "spec"
            assert "BashOperator" in visual["code"]
            refused = await call(
                client,
                "save_raw_dag_code",
                {"dag_id": "mcp_dag", "code": RAW_CODE, "confirm": True},
            )
            assert refused.is_error
            broken = await call(
                client,
                "save_raw_dag_code",
                {"dag_id": "mcp_raw", "code": "def x(:\n", "confirm": True},
            )
            assert broken.structured_content is not None
            assert broken.structured_content["saved"] is False
        assert (services.settings.dags_folder / "mcp_raw.py").read_text().endswith(RAW_CODE)

    run(go)


def test_raw_code_tools_follow_python_code(tmp_path: Path, airflow: FakeAirflow) -> None:
    svc = build_services(
        Settings(dags_folder=tmp_path, subfolder="", features={"python_code": False}), AuthBackend()
    )

    async def go() -> None:
        async with Client(make_server(svc, airflow)) as client:
            names = {t.name for t in await client.list_tools()}
            assert "save_raw_dag_code" not in names
            assert "get_dag_code" in names
            raw = {"spec": {"dag_id": "x", "raw_code": RAW_CODE}, "confirm": True}
            assert (await call(client, "save_dag_spec", raw)).is_error

    run(go)
