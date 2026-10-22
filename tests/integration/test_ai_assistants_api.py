"""API tests of the chat assistants, troubleshooting, upgrade assistant, deploy messages,
project rules and the external MCP server admin (fake LLM, fake Airflow; no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from autoair.ai import prompts
from autoair.ai.events import parse_sse_frames
from autoair.ai.testing import FakeCall, FakeLLM, tool_calls
from autoair.api.app import build_services
from autoair.api.routers import admin_ai, ai
from autoair.auth import AuthBackend
from autoair.gateway import (
    AirflowGateway,
    ConnectionMetadata,
    DagRunInfo,
    DagVersionInfo,
    ImportErrorInfo,
    TaskInstanceInfo,
)
from autoair.iam.model import ALL_PERMISSIONS, Principal
from autoair.settings import Settings
from tests.optional import requires_mcp

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"
VIEWER = {"builder:use", "spec:read", "airflow:read", "ai:use"}


def spec_data(command: str = "echo hi") -> dict[str, Any]:
    return {
        "dag_id": "ai_chat",
        "nodes": [
            {
                "kind": "operator",
                "id": "hello",
                "task_id": "hello",
                "operator": BASH,
                "params": {"bash_command": command},
            },
            {
                "kind": "operator",
                "id": "load",
                "task_id": "load",
                "operator": BASH,
                "params": {"bash_command": "echo load"},
            },
        ],
        "edges": [{"source": "hello", "target": "load"}],
    }


def make(
    tmp_path: Path,
    llm: Any = None,
    *,
    perms: set[str] | frozenset[str] = ALL_PERMISSIONS,
    user: str = "alice",
    **settings: Any,
) -> tuple[TestClient, Any]:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", **settings), AuthBackend()
    )
    if llm is not None:
        setattr(services, "llm", llm)  # noqa: B010
    api = FastAPI()
    api.state.services = services

    async def principal(request: Request) -> None:
        request.state.autoair_principal = Principal(user=user, permissions=set(perms))

    deps = [Depends(principal)]
    ai.include_ai_routers(api.router, services, deps)
    api.include_router(admin_ai.router, dependencies=deps)
    return TestClient(api), services


def events(resp: Any) -> list[dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    return parse_sse_frames(resp.text)


def result_of(evts: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [e for e in evts if e["type"] == "error"]
    assert not errors, errors
    found = [e for e in evts if e["type"] == "result"]
    assert found, evts
    data: dict[str, Any] = found[0]["data"]
    return data


def save(services: Any, spec: Any) -> None:
    from autoair.codegen import render
    from autoair.mcp.store import store_for

    store_for(services).save(spec, render(spec, services.registry))


def audit(services: Any) -> list[Any]:
    return ai.audit_for(services).recent(50)


# --------------------------------------------------------------------------- developer chat


def test_developer_chat_turn_proposal_threads_and_audit(tmp_path: Path) -> None:
    patch = {
        "title": "Add retries",
        "summary": "Retry the flaky task.",
        "findings": [
            {
                "title": "Retry hello",
                "severity": "medium",
                "category": "reliability",
                "node_id": "hello",
                "field": "retries",
                "patch": [{"op": "add", "path": "/nodes/@hello/retries", "value_json": "3"}],
            }
        ],
    }
    llm = FakeLLM(
        [
            tool_calls(("get_current_spec", {}), ("search_catalog", {"query": "bash"})),
            tool_calls(("propose_spec_change", patch)),
            (
                "Add retries to `hello` [1].\n\n<follow_ups>\n- Why three retries?\n"
                "- Add a timeout too?\n</follow_ups>"
            ),
            "Regenerated answer.",
        ]
    )
    client, services = make(tmp_path, llm)
    resp = client.post(
        "/ai/assistant/chat/stream",
        json={"mode": "developer", "message": "Make hello more robust", "spec": spec_data()},
    )
    evts = events(resp)
    kinds = [e["type"] for e in evts]
    assert "tool_call" in kinds
    assert "token" in kinds
    assert kinds[-1] == "done"
    assert any(e["type"] == "status" and e["stage"] == "thread" for e in evts)
    data = result_of(evts)
    msg = data["message"]
    assert msg["content"] == "Add retries to `hello` [1]."
    assert msg["follow_ups"] == ["Why three retries?", "Add a timeout too?"]
    assert msg["citations"][0]["n"] == 1
    assert msg["citations"][0]["kind"] == "spec"
    assert {t["name"] for t in msg["tools"]} >= {"get_current_spec", "propose_spec_change"}
    proposal = msg["proposals"][0]
    assert proposal["kind"] == "spec_patch"
    assert proposal["proposed_ok"] is True
    hello = next(n for n in proposal["proposed_spec"]["nodes"] if n["id"] == "hello")
    assert hello["retries"] == 3
    assert msg["usage"] is not None  # the fake model reports no tokens
    thread_id = data["thread"]["id"]
    assert data["thread"]["title"] == "Make hello more robust"
    # the system prompt is the developer assistant's, with the history-free first turn
    first = llm.calls[0]
    assert "developer assistant" in first.system
    assert "untrusted" in first.system.lower()

    threads = client.get("/ai/assistant/threads", params={"mode": "developer"}).json()
    assert [t["id"] for t in threads] == [thread_id]
    assert client.get("/ai/assistant/threads", params={"mode": "admin"}).json() == []
    full = client.get(f"/ai/assistant/threads/{thread_id}").json()
    assert [m["role"] for m in full["messages"]] == ["user", "assistant"]

    renamed = client.patch(f"/ai/assistant/threads/{thread_id}", json={"title": "Robust hello"})
    assert renamed.json()["title"] == "Robust hello"

    decided = client.post(
        f"/ai/assistant/threads/{thread_id}/proposals/{proposal['id']}/decision",
        json={"decision": "approve"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"

    # regenerate replaces the answer and sends the earlier turns as history
    again = result_of(
        events(
            client.post(
                "/ai/assistant/chat/stream",
                json={"mode": "developer", "thread_id": thread_id, "regenerate": True},
            )
        )
    )
    assert again["message"]["content"] == "Regenerated answer."
    assert again["user_message"]["content"] == "Make hello more robust"
    full = client.get(f"/ai/assistant/threads/{thread_id}").json()
    assert [m["role"] for m in full["messages"]] == ["user", "assistant"]

    records = audit(services)
    turns = [r for r in records if r.feature == "assistant:developer"]
    assert len(turns) == 2
    assert turns[-1].detail["thread_id"] == thread_id
    assert "propose_spec_change" in turns[-1].tools
    assert any(s.startswith("spec:") for s in turns[-1].sources)
    assert any(r.feature == "assistant:proposal_approve" for r in records)

    response = client.delete(f"/ai/assistant/threads/{thread_id}")
    assert response.status_code == 200
    assert client.get(f"/ai/assistant/threads/{thread_id}").status_code == 404


def test_chat_validation_and_other_users_threads(tmp_path: Path) -> None:
    client, services = make(tmp_path, FakeLLM(["hi"]))
    assert client.post("/ai/assistant/chat/stream", json={"message": "  "}).status_code == 422
    assert client.post("/ai/assistant/chat/stream", json={"regenerate": True}).status_code == 422
    assert (
        client.post(
            "/ai/assistant/chat/stream", json={"thread_id": "abc123", "message": "x"}
        ).status_code
        == 404
    )
    data = result_of(events(client.post("/ai/assistant/chat/stream", json={"message": "hey"})))
    other, _ = make(tmp_path, FakeLLM(), user="mallory")
    other.app.state.services = services
    assert other.get(f"/ai/assistant/threads/{data['thread']['id']}").status_code == 404
    # a viewer without spec:write cannot approve a spec change (tested on a fake proposal)
    viewer, _ = make(tmp_path, perms=VIEWER)
    viewer.app.state.services = services
    assert (
        viewer.post("/ai/assistant/chat/stream", json={"mode": "admin", "message": "x"}).status_code
        == 403
    )


def test_chat_compaction_of_long_threads(tmp_path: Path) -> None:
    from autoair.ai.assistants.threads import ChatMessage, ChatThread, thread_store_for

    llm = FakeLLM(["- user wants retries on hello", "Done."])
    client, services = make(
        tmp_path,
        llm,
        ai={"assistants": {"compaction_trigger_chars": 2_000, "compaction_keep_messages": 2}},
    )
    thread = ChatThread(user="alice", mode="developer")
    for i in range(8):
        thread.messages.append(
            ChatMessage(
                role="user" if i % 2 == 0 else "assistant", content=f"turn {i} " + "x" * 600
            )
        )
    thread_store_for(services).save(thread)
    evts = events(
        client.post(
            "/ai/assistant/chat/stream", json={"thread_id": thread.id, "message": "and now?"}
        )
    )
    assert any(e["type"] == "status" and e["stage"] == "compact" for e in evts)
    result_of(evts)
    stored = thread_store_for(services).get("alice", thread.id)
    assert stored.summary == "- user wants retries on hello"
    assert stored.summarized == 6  # messages before the last two of the history
    sent = llm.calls[1]
    assert "Summary of the earlier conversation" in sent.messages[0]["content"]


# --------------------------------------------------------------------------- admin chat


def test_admin_assistant_tools_and_permissions(tmp_path: Path) -> None:
    from autoair.admin import seen_users_for

    llm = FakeLLM(
        [
            tool_calls(
                ("explain_user_access", {"user": "bob", "action": "deploy"}),
                ("search_audit", {"days": 7}),
                ("get_feature_flags", {}),
            ),
            tool_calls(
                (
                    "propose_config_snippet",
                    {
                        "title": "Give bob Deployer",
                        "format": "yaml",
                        "content": "iam:\n  role_mapping:\n    Deployer: [bob]\n"
                        "password: hunter2hunter2",
                    },
                )
            ),
            "Bob lacks `deploy` [1][2][3][4].",
        ]
    )
    client, services = make(tmp_path, llm)
    seen_users_for(services).record("bob", roles=["Viewer"], permissions=4, auth="test")
    services.audit.record("permission_denied", user="bob", permission="deploy", path="/x")
    services.audit.record("deploy", user="carol", dag_id="sales")
    data = result_of(
        events(
            client.post(
                "/ai/assistant/chat/stream",
                json={"mode": "admin", "message": "Why can't bob deploy?"},
            )
        )
    )
    msg = data["message"]
    names = set(llm.calls[0].tools)
    assert {"explain_user_access", "get_configuration", "search_audit"} <= names
    assert "propose_spec_change" not in names
    assert "get_current_spec" not in names
    access = next(t for t in msg["tools"] if t["name"] == "explain_user_access")
    assert access["ok"]
    assert '"missing":["spec:write","deploy"]' in access["preview"]
    snippet = msg["proposals"][0]
    assert snippet["kind"] == "config_snippet"
    assert "Deployer" in snippet["snippet"]
    assert "hunter2hunter2" not in snippet["snippet"]
    assert any(c["kind"] == "audit" for c in msg["citations"])
    assert "ADMIN" not in llm.calls[0].system or "administration assistant" in llm.calls[0].system

    # admin:panel alone is enough; ai:use alone is not
    panel, _ = make(tmp_path, perms={"ai:use", "admin:panel"})
    panel.app.state.services = services
    info = panel.get("/ai/assistant/info").json()
    assert info["admin"]["allowed"] is True
    viewer, _ = make(tmp_path, perms=VIEWER)
    viewer.app.state.services = services
    assert viewer.get("/ai/assistant/info").json()["admin"]["allowed"] is False


# --------------------------------------------------------------------------- troubleshoot


def fake_airflow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"logs": []}

    async def dag_run(self: Any, dag_id: str, run_id: str) -> DagRunInfo | None:
        if run_id != "run1":
            return None
        return DagRunInfo(dag_run_id=run_id, state="failed", bundle_version="abc", conf={"k": 1})

    async def task_instances(self: Any, dag_id: str, run_id: str) -> list[TaskInstanceInfo]:
        return [
            TaskInstanceInfo(
                task_id="hello", state="failed", try_number=2, operator="BashOperator"
            ),
            TaskInstanceInfo(task_id="load", state="upstream_failed", try_number=0),
        ]

    async def task_log(
        self: Any, dag_id: str, run_id: str, task_id: str, try_number: int, map_index: int = -1
    ) -> str:
        seen["logs"].append((task_id, try_number))
        return (
            f"[try {try_number}] starting\n"
            "psycopg2.OperationalError: connection to server at db:5432 refused\n"
            "using password=sup3rs3cretvalue\n"
        )

    async def dag_versions(self: Any, dag_id: str) -> list[DagVersionInfo]:
        return [DagVersionInfo(version_number=4, bundle_name="dags", bundle_version="abc")]

    async def import_errors(self: Any, f: str | None = None) -> list[ImportErrorInfo]:
        return [ImportErrorInfo(filename="other.py", stack_trace="SyntaxError")]

    async def version(self: Any) -> dict[str, Any]:
        return {"version": "3.3.2"}

    async def providers(self: Any) -> list[dict[str, Any]]:
        return [{"package_name": "apache-airflow-providers-postgres", "version": "6.0.0"}]

    async def xcom_keys(self: Any, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"key": "return_value", "map_index": -1}]

    async def connection_metadata(self: Any, conn_id: str) -> ConnectionMetadata | None:
        return ConnectionMetadata(
            connection_id=conn_id, conn_type="postgres", host="db", extra_keys=["sslmode"]
        )

    for name, fn in {
        "dag_run": dag_run,
        "task_instances": task_instances,
        "task_log": task_log,
        "dag_versions": dag_versions,
        "import_errors": import_errors,
        "version": version,
        "providers": providers,
        "xcom_keys": xcom_keys,
        "connection_metadata": connection_metadata,
    }.items():
        monkeypatch.setattr(AirflowGateway, name, fn)
    return seen


FINAL = {
    "answer": "The database refused connections; retries were too few.",
    "hypotheses": [
        {
            "title": "Postgres unreachable",
            "explanation": "The connection was refused.",
            "confidence": 0.9,
            "category": "infrastructure",
            "evidence": [{"source": "log:hello#2", "line": 2, "quote": "connection ... refused"}],
        },
        {"title": "Wrong host", "confidence": 0.2, "category": "configuration"},
    ],
    "remediation": [{"title": "Check the database", "kind": "infrastructure"}],
    "findings": [
        {
            "title": "Retry hello",
            "severity": "medium",
            "category": "reliability",
            "node_id": "hello",
            "field": "retries",
            "patch": [{"op": "add", "path": "/nodes/@hello/retries", "value_json": "4"}],
        }
    ],
    "config_changes": [
        {"target": "connection pg extra", "format": "json", "snippet": '{"connect_timeout": 10}'}
    ],
    "airflow_actions": [{"action": "clear", "target": "hello", "instructions": "Clear hello."}],
}


def test_troubleshoot_analysis_approve_and_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = fake_airflow(monkeypatch)
    llm = FakeLLM(
        [
            tool_calls(
                ("get_connection_metadata", {"conn_id": "pg"}),
                (
                    "get_task_log",
                    {"task_id": "hello", "try_number": 1, "mode": "grep", "pattern": "refused"},
                ),
            ),
            tool_calls(("final_answer", FINAL)),
        ]
    )
    client, services = make(
        tmp_path, llm, ai={"troubleshoot": {"resources": {"xcom_values": False}}}
    )
    options = client.get("/ai/troubleshoot/options").json()
    assert options["enabled"]
    assert options["allowed"]["xcom_values"] is False
    assert options["allowed"]["filesystem"] is False  # no roots configured

    resp = client.post(
        "/ai/troubleshoot/stream",
        json={
            "dag_id": "ai_chat",
            "run_id": "run1",
            "spec": spec_data(),
            "scope": {"xcom_values": True, "airflow_config": True, "pools": False},
        },
    )
    evts = events(resp)
    assert any(e["type"] == "status" and e["stage"] == "collect" for e in evts)
    analysis = result_of(evts)
    assert analysis["status"] == "proposed"
    assert analysis["focus_tasks"] == ["hello"]
    assert analysis["scope"]["xcom_values"] is False  # the server does not allow it
    assert analysis["scope"]["airflow_config"] is False
    assert analysis["dag_version"] == 4
    assert analysis["bundle_version"] == "abc"
    assert analysis["hypotheses"][0]["confidence"] == 0.9
    assert analysis["hypotheses"][0]["evidence"][0]["line"] == 2
    assert analysis["config_changes"][0]["format"] == "json"
    assert analysis["airflow_actions"][0]["action"] == "clear"
    hello = next(n for n in analysis["proposed_spec"]["nodes"] if n["id"] == "hello")
    assert hello["retries"] == 4
    assert "retries=4" in analysis["code_after"]
    assert "retries=4" not in analysis["code_before"]
    assert {"get_connection_metadata", "get_task_log"} <= set(analysis["tools_used"])
    assert any(s["kind"] == "log" for s in analysis["sources"])
    assert ("hello", 1) in seen["logs"]
    assert ("hello", 2) in seen["logs"]

    # the evidence pack: numbered log lines, scrubbed secrets, upstream, versions
    first = llm.calls[0]
    pack = first.messages[0]["content"]
    assert "    2: psycopg2.OperationalError" in pack
    assert "sup3rs3cretvalue" not in pack
    assert "apache-airflow-providers-postgres==6.0.0" in pack
    assert "<untrusted-data" in pack
    tool_names = set(first.tools)
    assert "get_connection_metadata" in tool_names
    assert "get_pools" not in tool_names  # the user narrowed the scope
    assert "get_airflow_config" not in tool_names  # not allowed by the server
    assert "fs_read" not in tool_names
    assert "incident analyst" in first.system

    aid = analysis["id"]
    listed = client.get("/ai/troubleshoot/analyses", params={"dag_id": "ai_chat"}).json()
    assert [a["id"] for a in listed] == [aid]
    assert client.post(f"/ai/troubleshoot/analyses/{aid}/applied", json={}).status_code == 409

    # a viewer may not approve a change
    viewer, _ = make(tmp_path, perms=VIEWER)
    viewer.app.state.services = services
    viewer_denied = viewer.post(
        f"/ai/troubleshoot/analyses/{aid}/decision", json={"decision": "approve"}
    )
    assert viewer_denied.status_code in (403, 404)

    decided = client.post(
        f"/ai/troubleshoot/analyses/{aid}/decision",
        json={"decision": "approve", "comment": "go"},
    ).json()
    assert decided["analysis"]["status"] == "approved"
    assert decided["applied"]["ok"] is True
    assert decided["applied"]["applied"]
    assert "retries=4" in decided["code_after"]
    done = client.post(
        f"/ai/troubleshoot/analyses/{aid}/applied",
        json={"deployed": True, "detail": {"version": 5}},
    ).json()
    assert done["status"] == "applied"
    assert [d["decision"] for d in done["decisions"]] == ["approve", "applied"]

    features = [r.feature for r in audit(services)]
    assert {"troubleshoot", "troubleshoot:approve", "troubleshoot:applied"} <= set(features)
    record = next(r for r in audit(services) if r.feature == "troubleshoot")
    assert record.detail["analysis_id"] == aid
    assert record.detail["run_id"] == "run1"
    assert "get_task_log" in record.tools
    trail = [r.action for r in services.audit.query(limit=20)]
    assert "troubleshoot_approve" in trail
    assert "troubleshoot_applied" in trail


def test_troubleshoot_permissions_and_missing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_airflow(monkeypatch)
    no_airflow, _ = make(tmp_path, FakeLLM(), perms={"ai:use"})
    body = {"dag_id": "ai_chat", "run_id": "run1"}
    assert no_airflow.post("/ai/troubleshoot/stream", json=body).status_code == 403
    client, services = make(tmp_path, FakeLLM(["x"]))
    evts = events(client.post("/ai/troubleshoot/stream", json={**body, "run_id": "nope"}))
    error = next(e for e in evts if e["type"] == "error")
    assert error["status"] == 404
    off, _ = make(tmp_path, FakeLLM(), ai={"troubleshoot": {"enabled": False}})
    assert off.post("/ai/troubleshoot/stream", json=body).status_code == 404
    # reject: nothing changes, and it is audited
    llm = FakeLLM([tool_calls(("final_answer", FINAL))])
    client, services = make(tmp_path, llm)
    analysis = result_of(
        events(client.post("/ai/troubleshoot/stream", json={**body, "spec": spec_data()}))
    )
    rejected = client.post(
        f"/ai/troubleshoot/analyses/{analysis['id']}/decision", json={"decision": "reject"}
    ).json()
    assert rejected["analysis"]["status"] == "rejected"
    assert rejected["applied"] is None
    assert any(r.feature == "troubleshoot:reject" for r in audit(services))


@requires_mcp
def test_troubleshoot_filesystem_and_mcp_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import FastMCP

    from autoair.ai.assistants import mcp_client

    fake_airflow(monkeypatch)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "scheduler.log").write_text("ok\nERROR scheduler heartbeat lost\n")
    metrics = FastMCP("metrics")

    @metrics.tool(annotations={"readOnlyHint": True})
    def db_health(host: str) -> str:
        """Health of a database host."""
        return f"{host} is down since 10:02"

    monkeypatch.setattr(mcp_client, "build_transport", lambda cfg: metrics)
    llm = FakeLLM(
        [
            tool_calls(
                ("fs_read", {"path": "scheduler.log", "mode": "grep", "pattern": "ERROR"}),
                ("mcp__metrics__db_health", {"host": "db"}),
            ),
            tool_calls(("final_answer", {"answer": "DB down."})),
        ]
    )
    client, _services = make(
        tmp_path,
        llm,
        ai={
            "troubleshoot": {"filesystem": {"roots": [str(logs)]}},
            "mcp_client": {"servers": [{"name": "metrics", "url": "https://mcp.example/mcp"}]},
        },
    )
    options = client.get("/ai/troubleshoot/options").json()
    assert options["filesystem_roots"] == [str(logs.resolve())]
    assert [s["name"] for s in options["mcp_servers"]] == ["metrics"]
    tools = client.get("/ai/mcp/servers/metrics/tools").json()
    assert [t["name"] for t in tools] == ["db_health"]
    analysis = result_of(
        events(
            client.post(
                "/ai/troubleshoot/stream",
                json={
                    "dag_id": "ai_chat",
                    "run_id": "run1",
                    "scope": {"filesystem": True},
                    "mcp": [{"server": "metrics"}],
                },
            )
        )
    )
    steps = {s["tool"]: s for s in analysis["steps"]}
    assert steps["fs_read"]["ok"]
    assert "heartbeat lost" in steps["fs_read"]["output_preview"]
    assert steps["mcp__metrics__db_health"]["ok"]
    assert "down since" in steps["mcp__metrics__db_health"]["output_preview"]
    assert analysis["status"] == "no_change"
    assert any(s["kind"] == "file" for s in analysis["sources"])


# --------------------------------------------------------------------------- upgrade / message


def test_upgrade_assistant_rules_and_ai(tmp_path: Path) -> None:
    from autoair.spec import DagSpec

    client, services = make(tmp_path)
    old = spec_data("echo {{ execution_date }}")
    report = client.post("/ai/upgrade", json={"spec": old, "use_ai": False}).json()
    dag = report["dags"][0]
    assert any(f["rule"] == "airflow3-template" for f in dag["findings"])
    assert dag["code_before"]
    assert dag["ai_used"] is False

    def answer(call: FakeCall) -> dict[str, Any]:
        assert "ruff AIR3 diagnostics" in call.messages[0]["content"]
        return {
            "summary": "Use logical_date.",
            "findings": [
                {
                    "title": "Replace execution_date",
                    "severity": "high",
                    "category": "airflow3",
                    "node_id": "hello",
                    "field": "bash_command",
                    "patch": [
                        {
                            "op": "replace",
                            "path": "/nodes/@hello/params/bash_command",
                            "value_json": json.dumps("echo {{ logical_date }}"),
                        }
                    ],
                }
            ],
        }

    client, services = make(tmp_path, FakeLLM([answer]))
    dag = client.post("/ai/upgrade", json={"spec": old}).json()["dags"][0]
    assert dag["ai_used"]
    assert dag["summary"] == "Use logical_date."
    assert "logical_date" in dag["code_after"]
    assert dag["proposed_ok"] is True
    save(services, DagSpec.model_validate(old))
    save(services, DagSpec.model_validate({**spec_data(), "dag_id": "other"}))
    resp = client.post("/ai/upgrade", json={"all": True})
    assert resp.status_code == 200, resp.text
    every = resp.json()
    assert sorted(d["dag_id"] for d in every["dags"]) == ["ai_chat", "other"]
    assert any("single DAGs" in n for n in every["notes"])
    assert client.post("/ai/upgrade", json={}).status_code == 422


def test_deploy_message_with_and_without_model(tmp_path: Path) -> None:
    from autoair.spec import DagSpec

    client, services = make(tmp_path)
    new = client.post("/ai/deploy-message", json={"spec": spec_data()}).json()
    assert new["title"] == "Add DAG ai_chat"
    assert new["new_dag"]
    assert not new["ai_used"]
    save(services, DagSpec.model_validate(spec_data()))
    changed = spec_data()
    changed["nodes"][1]["retries"] = 2
    plain = client.post("/ai/deploy-message", json={"spec": changed}).json()
    assert "`load`" in plain["description"]

    def answer(call: FakeCall) -> dict[str, Any]:
        assert "node load retries" in call.messages[0]["content"]
        return {"title": "Retry load twice", "description": "## Summary\n- Retry `load`."}

    client, services = make(tmp_path, FakeLLM([answer]))
    save(services, DagSpec.model_validate(spec_data()))
    msg = client.post("/ai/deploy-message", json={"spec": changed}).json()
    assert msg["title"] == "Retry load twice"
    assert msg["ai_used"]
    assert any(r.feature == "deploy_message" for r in audit(services))


# --------------------------------------------------------------------------- rules


def test_project_rules_crud_and_injection(tmp_path: Path) -> None:
    client, services = make(tmp_path, team="core")
    assert (
        client.put("/ai/rules/global", json={"content": "Use owner data-eng@."}).status_code == 200
    )
    assert (
        client.put("/ai/rules/team:core", json={"content": "Tag DAGs with core."}).status_code
        == 200
    )
    assert client.put("/ai/rules/bad scope", json={"content": "x"}).status_code == 422
    eff = client.get("/ai/rules/effective").json()
    assert "data-eng@" in eff["text"]
    assert "Tag DAGs with core" in eff["text"]
    assert "data-eng@" in prompts.get("WRITER_TASK_BODY_SYSTEM")
    assert len(client.get("/ai/rules").json()) == 2
    viewer, _ = make(tmp_path, perms=VIEWER)
    viewer.app.state.services = services
    assert viewer.put("/ai/rules/global", json={"content": "x"}).status_code == 403
    response = client.delete("/ai/rules/team:core")
    assert response.status_code == 200
    assert "Tag DAGs" not in client.get("/ai/rules/effective").json()["text"]
    response = client.delete("/ai/rules/team:core")
    assert response.status_code == 404
    assert "ai_rules_put" in [r.action for r in services.audit.query(limit=10)]


# --------------------------------------------------------------------------- MCP admin


@requires_mcp
def test_admin_mcp_servers_crud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp import FastMCP

    from autoair.ai.assistants import mcp_client

    server = FastMCP("x")

    @server.tool
    def ping() -> str:
        """Ping."""
        return "pong"

    captured: list[Any] = []

    def transport(cfg: Any) -> Any:
        captured.append(cfg)
        return server

    monkeypatch.setattr(mcp_client, "build_transport", transport)
    client, services = make(
        tmp_path,
        ai={"mcp_client": {"servers": [{"name": "configured", "url": "https://c.example/mcp"}]}},
    )
    body = {
        "url": "https://mcp.example/mcp",
        "headers": {"Authorization": "Bearer t0ps3cr3t"},
        "tools": ["ping"],
    }
    put = client.put("/admin/mcp-servers/grafana", json=body)
    assert put.status_code == 200, put.text
    assert put.json()["headers"] == ["Authorization"]
    assert put.json()["source"] == "database"
    raw = (tmp_path / ".autoair").rglob("grafana.json")
    stored = "".join(p.read_text() for p in raw)
    assert stored
    assert "t0ps3cr3t" not in stored
    listed = client.get("/admin/mcp-servers").json()
    assert [v["name"] for v in listed] == ["configured", "grafana"]
    assert "t0ps3cr3t" not in json.dumps(listed)
    # keep the stored header when '***' is sent back
    client.put("/admin/mcp-servers/grafana", json={**body, "headers": {"Authorization": "***"}})
    assert client.post("/admin/mcp-servers/grafana/test").json() == [
        {"name": "ping", "description": "Ping.", "allowed": True}
    ]
    assert captured[-1].headers["Authorization"].get_secret_value() == "Bearer t0ps3cr3t"
    assert client.put("/admin/mcp-servers/configured", json=body).status_code == 409
    assert client.put("/admin/mcp-servers/Bad", json=body).status_code == 422
    stdio = client.put("/admin/mcp-servers/evil", json={**body, "transport": "stdio", "url": "x"})
    assert stdio.status_code == 422
    panel, _ = make(tmp_path, perms={"admin:panel"})
    panel.app.state.services = services
    assert panel.get("/admin/mcp-servers").status_code == 200
    assert panel.put("/admin/mcp-servers/x", json=body).status_code == 403
    response = client.delete("/admin/mcp-servers/grafana")
    assert response.status_code == 200
    response = client.delete("/admin/mcp-servers/grafana")
    assert response.status_code == 404
    actions = [r.action for r in services.audit.query(limit=20)]
    assert "ai_mcp_server_put" in actions
    assert "ai_mcp_server_delete" in actions


def test_airflow_feature_off_hides_airflow_tools_and_troubleshooting(tmp_path: Path) -> None:
    """features.airflow off: no Airflow tools for the developer assistant, no troubleshooting."""
    llm = FakeLLM(["Nothing to do."])
    client, _ = make(tmp_path, llm, features={"airflow": False})
    resp = client.post(
        "/ai/assistant/chat/stream",
        json={"mode": "developer", "message": "How did it run?", "spec": spec_data()},
    )
    assert events(resp)[-1]["type"] == "done"
    names = set(llm.calls[0].tools)
    assert "get_current_spec" in names
    assert {"list_dag_runs", "get_task_log", "list_task_instances"}.isdisjoint(names)

    assert client.get("/ai/troubleshoot/options").json()["enabled"] is False
    resp = client.post(
        "/ai/troubleshoot/stream", json={"dag_id": "ai_chat", "run_id": "run1", "spec": spec_data()}
    )
    assert resp.status_code == 404
    assert "airflow feature is disabled" in resp.json()["detail"]

    llm_on = FakeLLM(["ok"])
    on, _ = make(tmp_path, llm_on)
    events(
        on.post(
            "/ai/assistant/chat/stream",
            json={"mode": "developer", "message": "How did it run?", "spec": spec_data()},
        )
    )
    assert "list_dag_runs" in set(llm_on.calls[0].tools)


def _ops_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp import FastMCP

    from autoair.ai.assistants import mcp_client

    ops = FastMCP("ops")

    @ops.tool(annotations={"readOnlyHint": True})
    def db_health(host: str) -> str:
        """Health of a database host."""
        return f"{host} is fine"

    @ops.tool
    def restart_db(host: str) -> str:
        """Restart a database host."""
        return f"{host} restarted"

    monkeypatch.setattr(mcp_client, "build_transport", lambda cfg: ops)


MCP_SERVERS = {
    "servers": [
        {"name": "ops", "url": "https://mcp.example/ops", "agents": ["developer", "admin"]},
        {"name": "metrics", "url": "https://mcp.example/metrics"},  # troubleshoot only
    ]
}


def _turn_tools(client: TestClient, llm: Any, mode: str, **extra: Any) -> tuple[set[str], Any]:
    before = len(llm.calls)
    evts = events(
        client.post(
            "/ai/assistant/chat/stream",
            json={"mode": mode, "message": "Is the db ok?", "spec": spec_data(), **extra},
        )
    )
    names = set(llm.calls[before].tools)
    return names, evts


@requires_mcp
def test_chat_assistants_attach_scoped_external_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ops_mcp(monkeypatch)
    llm = FakeLLM(
        [
            tool_calls(("mcp__ops__db_health", {"host": "db"})),
            "The db is fine.",
            "admin answer",
            "no mcp",
        ]
    )
    client, _ = make(tmp_path, llm, ai={"mcp_client": MCP_SERVERS})
    info = client.get("/ai/assistant/info").json()
    assert [s["name"] for s in info["developer"]["mcp_servers"]] == ["ops"]
    assert [s["name"] for s in info["admin"]["mcp_servers"]] == ["ops"]

    # developer: every tool of the servers scoped to it (not the troubleshoot-only one)
    names, evts = _turn_tools(client, llm, "developer")
    assert {"mcp__ops__db_health", "mcp__ops__restart_db"} <= names
    assert not any(n.startswith("mcp__metrics__") for n in names)
    msg = result_of(evts)["message"]
    step = next(t for t in msg["tools"] if t["name"] == "mcp__ops__db_health")
    assert step["ok"]
    assert "db is fine" in step["preview"]

    # admin: read-only tools only, with a note about the others
    names, evts = _turn_tools(client, llm, "admin")
    assert "mcp__ops__db_health" in names
    assert "mcp__ops__restart_db" not in names
    assert any(
        e["type"] == "status" and e["stage"] == "mcp" and "not marked read-only" in e["message"]
        for e in evts
    )

    # an explicit empty selection attaches nothing
    names, _ = _turn_tools(client, llm, "developer", mcp=[])
    assert not any(n.startswith("mcp__") for n in names)


@requires_mcp
def test_chat_mcp_tools_respect_iam_and_the_profile_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ops_mcp(monkeypatch)
    # viewers: the role allow-list (ai.guardrails.tool_allowlist) has no mcp__* tools
    llm = FakeLLM(["viewer answer"])
    viewer, _ = make(tmp_path, llm, perms=VIEWER, ai={"mcp_client": MCP_SERVERS})
    names, _ = _turn_tools(viewer, llm, "developer")
    assert not any(n.startswith("mcp__") for n in names)
    assert viewer.get("/ai/mcp/servers/ops/tools", params={"agent": "admin"}).status_code == 403
    assert viewer.get("/ai/assistant/info").json()["admin"]["mcp_servers"] == []

    llm = FakeLLM(["off"])
    off, _ = make(
        tmp_path,
        llm,
        ai={"mcp_client": MCP_SERVERS, "assistants": {"developer": {"allow_external_mcp": False}}},
    )
    names, _ = _turn_tools(off, llm, "developer")
    assert not any(n.startswith("mcp__") for n in names)
    refused = off.post(
        "/ai/assistant/chat/stream",
        json={"mode": "developer", "message": "x", "mcp": [{"server": "ops"}]},
    )
    assert refused.status_code == 403
