"""API tests of the Airflow actions of AI troubleshooting: read_only / hitl / apply modes,
the allow-list, IAM, cross-DAG rejection, gateway calls (mocked) and audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autoair.ai.testing import FakeLLM, tool_calls
from autoair.gateway import AirflowGateway
from autoair.iam.model import ALL_PERMISSIONS
from tests.integration.test_ai_assistants_api import (
    FINAL,
    audit,
    events,
    fake_airflow,
    make,
    result_of,
    spec_data,
)

pytestmark = pytest.mark.airflow

ACTIONS = [
    {"action": "retry", "task_id": "hello", "instructions": "Retry hello."},
    {"action": "mark_success", "task_id": "load", "instructions": "Hide the failure."},
    {"action": "clear", "task_id": "x", "dag_id": "payroll", "instructions": "Another DAG."},
    {"action": "fix_connection", "target": "pg", "instructions": "Set connect_timeout."},
]
BODY = {"dag_id": "ai_chat", "run_id": "run1"}
NO_RUN_MANAGE = ALL_PERMISSIONS - {"run:manage"}


def gateway_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], Any]]:
    """Record the run operations (dry runs answer two task instances)."""
    calls: list[tuple[str, tuple[Any, ...], Any]] = []

    async def clear_task_instances(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("clear_task_instances", args, kwargs))
        return {"task_instances": [{"task_id": "hello"}, {"task_id": "load"}]}

    async def set_task_instance_state(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("set_task_instance_state", args, kwargs))
        return {}

    monkeypatch.setattr(AirflowGateway, "clear_task_instances", clear_task_instances)
    monkeypatch.setattr(AirflowGateway, "set_task_instance_state", set_task_instance_state)
    return calls


def analyse(client: Any, **extra: Any) -> dict[str, Any]:
    return result_of(
        events(client.post("/ai/troubleshoot/stream", json={**BODY, "spec": spec_data(), **extra}))
    )


def llm() -> FakeLLM:
    return FakeLLM([tool_calls(("final_answer", {**FINAL, "airflow_actions": ACTIONS}))])


def mode(value: str, **more: Any) -> dict[str, Any]:
    return {"ai": {"troubleshoot": {"airflow_actions": value, **more}}}


def test_read_only_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, _ = make(tmp_path, llm())
    assert client.get("/ai/troubleshoot/options").json()["actions_mode"] == "read_only"
    analysis = analyse(client)
    assert analysis["actions_mode"] == "read_only"
    assert {a["status"] for a in analysis["airflow_actions"]} == {"instruction"}
    retry = analysis["airflow_actions"][0]
    resp = client.post(
        f"/ai/troubleshoot/analyses/{analysis['id']}/actions/{retry['id']}",
        json={"decision": "execute"},
    )
    assert resp.status_code == 409
    assert calls == []


def test_hitl_executes_only_the_approved_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, services = make(tmp_path, llm(), **mode("hitl"))
    options = client.get("/ai/troubleshoot/options").json()
    assert options["actions_mode"] == "hitl"
    assert options["allowed_actions"] == ["clear", "retry", "clear_run"]
    analysis = analyse(client)
    assert analysis["actions_mode"] == "hitl"
    actions = {a["action"]: a for a in analysis["airflow_actions"]}
    assert actions["retry"]["status"] == "proposed"
    assert actions["mark_success"]["blocked"] == "not_allowed"
    assert actions["clear"]["blocked"] == "other_dag"
    assert actions["fix_connection"]["status"] == "instruction"
    assert calls == []  # nothing runs before the user approves

    aid = analysis["id"]
    url = f"/ai/troubleshoot/analyses/{aid}/actions"
    for blocked in (actions["mark_success"], actions["clear"]):
        resp = client.post(f"{url}/{blocked['id']}", json={"decision": "execute"})
        assert resp.status_code == 409, resp.text
    assert client.post(f"{url}/nope", json={"decision": "execute"}).status_code == 404
    fix = actions["fix_connection"]["id"]
    assert client.post(f"{url}/{fix}", json={"decision": "execute"}).status_code == 404
    assert calls == []

    done = client.post(
        f"{url}/{actions['retry']['id']}", json={"decision": "execute", "comment": "go"}
    )
    assert done.status_code == 200, done.text
    retry = next(a for a in done.json()["airflow_actions"] if a["action"] == "retry")
    assert retry["status"] == "succeeded"
    assert retry["results"][0]["by"] == "alice"
    assert retry["results"][0]["automatic"] is False
    assert [(c[0], c[1], c[2].get("dry_run", False)) for c in calls] == [
        ("clear_task_instances", ("ai_chat", "run1", "hello"), True),
        ("clear_task_instances", ("ai_chat", "run1", "hello"), False),
    ]
    assert calls[1][2]["only_failed"] is True
    # idempotent: approving again does not clear again
    again = client.post(f"{url}/{actions['retry']['id']}", json={"decision": "execute"})
    assert again.status_code == 200
    assert len(calls) == 2
    stored = client.get(f"/ai/troubleshoot/analyses/{aid}").json()
    assert next(a for a in stored["airflow_actions"] if a["action"] == "retry")["results"]

    rejected = client.post(f"{url}/{actions['mark_success']['id']}", json={"decision": "reject"})
    assert rejected.json()["airflow_actions"][1]["status"] == "rejected"

    features = [r.feature for r in audit(services)]
    assert {
        "troubleshoot:actions_proposed",
        "troubleshoot:action_approved",
        "troubleshoot:action_executed",
        "troubleshoot:action_rejected",
    } <= set(features)
    executed = next(r for r in audit(services) if r.feature == "troubleshoot:action_executed")
    assert executed.detail["result"] == "succeeded"
    assert executed.detail["airflow_action"]["task_id"] == "hello"
    trail = [r.action for r in services.audit.query(limit=30)]
    assert {"troubleshoot_action_approved", "troubleshoot_action_executed"} <= set(trail)


def test_apply_runs_the_vetted_set_and_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, services = make(tmp_path, llm(), **mode("apply"))
    resp = client.post("/ai/troubleshoot/stream", json={**BODY, "spec": spec_data()})
    evts = events(resp)
    stages = [e["message"] for e in evts if e["type"] == "status" and e["stage"] == "action"]
    assert stages == ["retry hello: running", "retry hello: succeeded - cleared 2 task instance(s)"]
    analysis = result_of(evts)
    actions = {a["action"]: a for a in analysis["airflow_actions"]}
    assert actions["retry"]["status"] == "succeeded"
    assert actions["retry"]["results"][0]["automatic"] is True
    assert actions["mark_success"]["status"] == "blocked"  # not on the allow-list
    assert actions["clear"]["status"] == "blocked"  # another DAG
    assert all(c[1][0] == "ai_chat" for c in calls)
    assert [c[0] for c in calls] == ["clear_task_instances", "clear_task_instances"]
    stored = client.get(f"/ai/troubleshoot/analyses/{analysis['id']}").json()
    assert stored["airflow_actions"][0]["status"] == "succeeded"
    assert any(
        r.feature == "troubleshoot:action_executed" and r.detail["automatic"]
        for r in audit(services)
    )


def test_request_may_only_lower_the_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, _ = make(tmp_path, llm(), **mode("apply"))
    assert analyse(client, airflow_actions="hitl")["actions_mode"] == "hitl"
    assert calls == []
    lowered, _ = make(tmp_path, llm(), **mode("hitl"))
    assert analyse(lowered, airflow_actions="apply")["actions_mode"] == "hitl"
    assert calls == []


def test_allow_list_setting_enables_marking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, _ = make(
        tmp_path, llm(), **mode("apply", allowed_actions=["mark_success"], max_actions=1)
    )
    analysis = analyse(client)
    actions = {a["action"]: a for a in analysis["airflow_actions"]}
    assert actions["retry"]["blocked"] == "not_allowed"
    assert actions["mark_success"]["status"] == "succeeded"
    assert calls == [
        ("set_task_instance_state", ("ai_chat", "run1", "load", "success"), calls[0][2])
    ]


def test_actions_need_run_manage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, services = make(tmp_path, llm(), perms=NO_RUN_MANAGE, **mode("apply"))
    assert client.get("/ai/troubleshoot/options").json()["actions_mode"] == "read_only"
    analysis = analyse(client)
    assert analysis["actions_mode"] == "read_only"
    retry = analysis["airflow_actions"][0]
    resp = client.post(
        f"/ai/troubleshoot/analyses/{analysis['id']}/actions/{retry['id']}",
        json={"decision": "execute"},
    )
    assert resp.status_code == 403
    assert calls == []

    # an analysis made in hitl mode: a user who lost run:manage cannot execute it
    manager, services = make(tmp_path, llm(), **mode("hitl"))
    analysis = analyse(manager)
    retry = analysis["airflow_actions"][0]
    viewer, _ = make(tmp_path, perms=NO_RUN_MANAGE)
    viewer.app.state.services = services
    resp = viewer.post(
        f"/ai/troubleshoot/analyses/{analysis['id']}/actions/{retry['id']}",
        json={"decision": "execute"},
    )
    assert resp.status_code == 403
    assert calls == []


def test_runs_feature_off_makes_actions_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, _ = make(tmp_path, llm(), features={"runs": False}, **mode("apply"))
    analysis = analyse(client)
    assert analysis["actions_mode"] == "read_only"
    retry = analysis["airflow_actions"][0]
    resp = client.post(
        f"/ai/troubleshoot/analyses/{analysis['id']}/actions/{retry['id']}",
        json={"decision": "execute"},
    )
    assert resp.status_code == 404
    assert calls == []


def test_invalid_action_schema_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final answer whose actions break the schema is retried by the agent; actions with
    fields outside the schema never reach the analysis."""
    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    bad = {
        **FINAL,
        "airflow_actions": [
            {"action": "clear", "task_id": "hello", "instructions": "x", "conf": {"rm": "-rf"}}
        ],
    }
    good = {**FINAL, "airflow_actions": []}
    client, _ = make(
        tmp_path,
        FakeLLM([tool_calls(("final_answer", bad)), tool_calls(("final_answer", good))]),
        **mode("apply"),
    )
    analysis = analyse(client)
    assert analysis["airflow_actions"] == []
    assert calls == []


def test_hitl_action_claimed_elsewhere_is_not_run_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another replica holds the claim (status running): this one answers 409
    action_running without calling Airflow; results carry a message code."""
    from autoair.ai.assistants.troubleshoot import analysis_store_for

    fake_airflow(monkeypatch)
    calls = gateway_calls(monkeypatch)
    client, services = make(tmp_path, llm(), **mode("hitl"))
    analysis = analyse(client)
    retry = next(a for a in analysis["airflow_actions"] if a["action"] == "retry")
    store = analysis_store_for(services)
    store.claim_action("alice", analysis["id"], retry["id"], "replica-b", timeout=300)
    url = f"/ai/troubleshoot/analyses/{analysis['id']}/actions/{retry['id']}"
    resp = client.post(url, json={"decision": "execute"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "action_running"
    assert calls == []

    # The claim is released (as if it expired): this replica executes it once.
    def release(a: Any) -> None:
        next(x for x in a.airflow_actions if x.id == retry["id"]).status = "proposed"

    store.mutate("alice", analysis["id"], release)
    done = client.post(url, json={"decision": "execute"}).json()
    action = next(a for a in done["airflow_actions"] if a["id"] == retry["id"])
    assert action["status"] == "succeeded"
    assert action["claimed_by"] == "alice"
    assert action["results"][-1]["code"] == "cleared"
    assert action["results"][-1]["params"] == {"count": 2}
    assert len(calls) == 2  # dry run + clear
