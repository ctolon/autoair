"""API tests of tool-call approval (``ai.approvals``): a chat turn or troubleshooting
analysis pauses when the agent calls a tool that needs approval, the user approves or
denies each call and the run resumes - on any replica, at most once, only by the user who
started it, and audited. Scripted model, fake Airflow and fake MCP servers; no network."""

from __future__ import annotations

import datetime
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ToolReturnPart

from autoair.ai import approvals as approvals_module
from autoair.ai.memory import memory_for
from autoair.ai.testing import FakeCall, FakeLLM, tool_calls
from autoair.iam.model import ALL_PERMISSIONS
from tests.integration.test_ai_assistants_api import (
    MCP_SERVERS,
    _ops_mcp,
    audit,
    events,
    fake_airflow,
    make,
    result_of,
    spec_data,
)
from tests.optional import requires_mcp

pytestmark = pytest.mark.airflow

REMEMBER = ("remember", {"text": "Always set retries on sensors", "scope": "user"})


def chat(client: Any, message: str = "Remember: retries on sensors", **extra: Any) -> Any:
    return client.post(
        "/ai/assistant/chat/stream",
        json={"mode": "developer", "message": message, "spec": spec_data(), **extra},
    )


def resume(client: Any, approval_id: str, *decisions: dict[str, Any]) -> Any:
    return client.post(f"/ai/approvals/{approval_id}/stream", json={"decisions": list(decisions)})


def memories(services: Any) -> list[str]:
    items, _ = memory_for(services).list(visible_to=(None, "alice"), limit=50)
    return [i.text for i in items]


def paused_turn(client: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    data = result_of(events(chat(client)))
    message = data["message"]
    approval = message["approvals"][-1]
    assert approval["status"] == "pending"
    return data, approval


def test_approve_resumes_the_turn_and_runs_the_tool(tmp_path: Path) -> None:
    seen: list[FakeCall] = []

    def final(call: FakeCall) -> str:
        seen.append(call)
        return "Stored the convention."

    llm = FakeLLM([tool_calls(REMEMBER, text="I will store that."), final, "Next answer."])
    client, services = make(tmp_path, llm)
    data, approval = paused_turn(client)
    message = data["message"]
    assert message["content"] == "I will store that."
    call = approval["calls"][0]
    assert call["tool"] == "remember"
    assert call["kind"] == "write"
    assert call["arguments"]["text"] == "Always set retries on sensors"
    assert memories(services) == []  # nothing ran yet
    assert [a["id"] for a in client.get("/ai/approvals").json()] == [approval["id"]]
    assert client.get(f"/ai/approvals/{approval['id']}").json()["status"] == "pending"
    turn = next(r for r in audit(services) if r.feature == "assistant:developer")
    assert turn.detail["approval_pending"] == approval["id"]

    evts = events(resume(client, approval["id"], {"id": call["id"], "approved": True}))
    resumed = result_of(evts)
    msg = resumed["message"]
    assert msg["id"] == message["id"]  # the same message is completed
    assert msg["content"] == "I will store that.\n\nStored the convention."
    assert msg["approvals"][-1]["status"] == "resolved"
    assert [(t["name"], t["ok"]) for t in msg["tools"]] == [("remember", True)]
    assert memories(services) == ["Always set retries on sensors"]
    assert any(
        isinstance(p, ToolReturnPart) and p.tool_name == "remember" for p in seen[0].tool_results
    )
    assert client.get("/ai/approvals").json() == []
    stored = client.get(f"/ai/approvals/{approval['id']}").json()
    assert stored["status"] == "resolved"
    assert stored["decided_by"] == "alice"
    features = [r.feature for r in audit(services)]
    assert "assistant:tool_approved" in features
    decided = next(r for r in audit(services) if r.feature == "assistant:tool_approved")
    assert decided.detail["tool"] == "remember"
    assert decided.detail["approval_id"] == approval["id"]

    # a second decision is refused: the tool ran once
    assert resume(client, approval["id"], {"id": call["id"], "approved": True}).status_code == 409
    assert memories(services) == ["Always set retries on sensors"]

    # the next turn continues the thread with the complete history (tool call + result)
    thread_id = resumed["thread"]["id"]
    events(chat(client, "Thanks", thread_id=thread_id))
    history = llm.calls[-1].history
    assert any(
        isinstance(p, ToolReturnPart) and p.tool_name == "remember"
        for m in history
        for p in getattr(m, "parts", [])
    )
    thread = client.get(f"/ai/assistant/threads/{thread_id}").json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant", "user", "assistant"]


def test_deny_sends_the_reason_and_skips_the_tool(tmp_path: Path) -> None:
    def final(call: FakeCall) -> str:
        denial = next(p for p in call.tool_results if p.tool_name == "remember")
        assert "denied" in str(denial.content)
        assert "not now" in str(denial.content)
        return "Understood, not stored."

    llm = FakeLLM([tool_calls(REMEMBER), final])
    client, services = make(tmp_path, llm)
    _, approval = paused_turn(client)
    call_id = approval["calls"][0]["id"]
    msg = result_of(
        events(
            resume(client, approval["id"], {"id": call_id, "approved": False, "reason": "not now"})
        )
    )["message"]
    assert msg["content"] == "Understood, not stored."
    assert msg["tools"][0]["ok"] is False
    assert "denied by alice: not now" in msg["tools"][0]["preview"]
    assert memories(services) == []
    denied = next(r for r in audit(services) if r.feature == "assistant:tool_denied")
    assert denied.detail["reason"] == "not now"


def test_decisions_are_validated(tmp_path: Path) -> None:
    client, _ = make(
        tmp_path, FakeLLM([tool_calls(REMEMBER)]), ai={"approvals": {"max_reason_chars": 5}}
    )
    _, approval = paused_turn(client)
    call_id = approval["calls"][0]["id"]
    assert resume(client, approval["id"]).status_code == 422
    assert resume(client, approval["id"], {"id": "nope", "approved": True}).status_code == 422
    too_long = {"id": call_id, "approved": False, "reason": "far too long"}
    assert resume(client, approval["id"], too_long).status_code == 422
    assert resume(client, "doesnotexist", {"id": call_id, "approved": True}).status_code == 404
    # nothing was claimed by the refused requests
    assert client.get(f"/ai/approvals/{approval['id']}").json()["status"] == "pending"


def test_only_the_user_who_started_the_run_can_decide(tmp_path: Path) -> None:
    llm = FakeLLM([tool_calls(REMEMBER), "Stored."])
    client, services = make(tmp_path, llm)
    _, approval = paused_turn(client)
    call_id = approval["calls"][0]["id"]
    mallory, _ = make(tmp_path, FakeLLM(["never"]), user="mallory")
    assert mallory.get("/ai/approvals").json() == []
    assert mallory.get(f"/ai/approvals/{approval['id']}").status_code == 404
    assert resume(mallory, approval["id"], {"id": call_id, "approved": True}).status_code == 404
    assert memories(services) == []
    # the owner still can
    assert result_of(events(resume(client, approval["id"], {"id": call_id, "approved": True})))


def test_admin_assistant_approval_needs_admin_rights(tmp_path: Path) -> None:
    llm = FakeLLM([tool_calls(("remember", {"text": "Rotate keys monthly"}))])
    client, _ = make(tmp_path, llm)
    data = result_of(
        events(
            client.post(
                "/ai/assistant/chat/stream", json={"mode": "admin", "message": "remember this"}
            )
        )
    )
    approval = data["message"]["approvals"][-1]
    viewer, _ = make(tmp_path, FakeLLM(), perms={"builder:use", "ai:use"})
    resp = resume(viewer, approval["id"], {"id": approval["calls"][0]["id"], "approved": True})
    assert resp.status_code == 403


def test_expired_approval_cannot_be_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, services = make(
        tmp_path, FakeLLM([tool_calls(REMEMBER)]), ai={"approvals": {"ttl_seconds": 60}}
    )
    _, approval = paused_turn(client)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)
    monkeypatch.setattr(approvals_module, "now", lambda: later)
    resp = resume(client, approval["id"], {"id": approval["calls"][0]["id"], "approved": True})
    assert resp.status_code == 410
    assert client.get("/ai/approvals").json() == []
    assert client.get(f"/ai/approvals/{approval['id']}").json()["status"] == "expired"
    # the thread shows the current state of its approval snapshot
    thread_id = client.get("/ai/assistant/threads").json()[0]["id"]
    thread = client.get(f"/ai/assistant/threads/{thread_id}").json()
    assert thread["messages"][1]["approvals"][-1]["status"] == "expired"
    assert any(r.feature == "assistant:tool_approval_expired" for r in audit(services))
    assert memories(services) == []


def test_a_new_message_cancels_the_pending_approval(tmp_path: Path) -> None:
    llm = FakeLLM([tool_calls(REMEMBER), "Something else."])
    client, services = make(tmp_path, llm)
    data, approval = paused_turn(client)
    thread_id = data["thread"]["id"]
    nxt = result_of(events(chat(client, "Forget it, explain sensors", thread_id=thread_id)))
    assert nxt["message"]["content"] == "Something else."
    # the paused turn is sent as text (its stored messages end with an unanswered call)
    assert "tool approval" in str(llm.calls[-1].history)
    thread = client.get(f"/ai/assistant/threads/{thread_id}").json()
    assert thread["messages"][1]["approvals"][-1]["status"] == "cancelled"
    resp = resume(client, approval["id"], {"id": approval["calls"][0]["id"], "approved": True})
    assert resp.status_code == 409
    assert memories(services) == []


def test_approvals_off_withdraws_write_tools(tmp_path: Path) -> None:
    llm = FakeLLM(["No tools needed."])
    client, _ = make(tmp_path, llm, ai={"approvals": {"enabled": False}})
    evts = events(chat(client))
    assert "remember" not in llm.calls[0].tools
    assert any(e["type"] == "status" and e["stage"] == "approval" for e in evts)
    # exempting a tool lets it run without a decision
    llm = FakeLLM([tool_calls(REMEMBER), "Stored."])
    (tmp_path / "b").mkdir()
    client, services = make(
        tmp_path / "b", llm, ai={"approvals": {"agents": {"developer": {"exempt": ["remember"]}}}}
    )
    msg = result_of(events(chat(client)))["message"]
    assert msg["approvals"] == []
    assert msg["content"] == "Stored."
    assert memories(services) == ["Always set retries on sensors"]


def test_tightened_policy_gates_read_tools(tmp_path: Path) -> None:
    llm = FakeLLM([tool_calls(("get_current_spec", {})), "Read it."])
    client, _ = make(tmp_path, llm, ai={"approvals": {"default": {"tools": ["get_current_spec"]}}})
    _, approval = paused_turn(client)
    assert approval["calls"][0]["tool"] == "get_current_spec"
    assert approval["calls"][0]["kind"] == "read"


def test_failed_resume_marks_the_approval_failed(tmp_path: Path) -> None:
    llm = FakeLLM([tool_calls(REMEMBER)])  # no scripted answer left for the resumed run
    client, _ = make(tmp_path, llm)
    data, approval = paused_turn(client)
    evts = events(
        resume(client, approval["id"], {"id": approval["calls"][0]["id"], "approved": True})
    )
    assert any(e["type"] == "error" for e in evts)
    assert client.get(f"/ai/approvals/{approval['id']}").json()["status"] == "failed"
    thread = client.get(f"/ai/assistant/threads/{data['thread']['id']}").json()
    assert thread["messages"][1]["approvals"][-1]["status"] == "failed"


def test_two_replicas_resume_once(tmp_path: Path) -> None:
    db = {"backend": "sqlite", "url": f"sqlite:///{tmp_path / 'state.db'}"}
    started = threading.Event()
    release = threading.Event()

    def slow(call: FakeCall) -> str:
        started.set()
        release.wait(10)
        return "Stored once."

    first, services = make(tmp_path, FakeLLM([tool_calls(REMEMBER), slow]), database=db)
    _, approval = paused_turn(first)
    second, _ = make(tmp_path, FakeLLM(["replica two must not run"]), database=db)
    decision = {"id": approval["calls"][0]["id"], "approved": True}
    out: dict[str, Any] = {}

    def run_first() -> None:
        out["first"] = resume(first, approval["id"], decision)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(10)
    # replica two gets the same decision while replica one is running the tool
    assert resume(second, approval["id"], decision).status_code == 409
    release.set()
    worker.join(20)
    assert result_of(events(out["first"]))["message"]["approvals"][-1]["status"] == "resolved"
    assert memories(services) == ["Always set retries on sensors"]


@requires_mcp
def test_unannotated_mcp_tool_needs_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ops_mcp(monkeypatch)
    llm = FakeLLM(
        [
            tool_calls(
                ("mcp__ops__db_health", {"host": "db"}), ("mcp__ops__restart_db", {"host": "db"})
            ),
            "Restarted.",
        ]
    )
    client, _ = make(tmp_path, llm, ai={"mcp_client": MCP_SERVERS})
    data, approval = paused_turn(client)
    # the read-only tool ran, the unannotated one waits
    assert [t["name"] for t in data["message"]["tools"]] == ["mcp__ops__db_health"]
    assert [c["tool"] for c in approval["calls"]] == ["mcp__ops__restart_db"]
    msg = result_of(
        events(resume(client, approval["id"], {"id": approval["calls"][0]["id"], "approved": True}))
    )["message"]
    restart = next(t for t in msg["tools"] if t["name"] == "mcp__ops__restart_db")
    assert restart["ok"]
    assert "restarted" in restart["preview"]


# --------------------------------------------------------------------------- troubleshoot


@requires_mcp
def test_troubleshoot_hitl_pauses_for_write_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import FastMCP

    from autoair.ai.assistants import mcp_client

    fake_airflow(monkeypatch)
    ops = FastMCP("ops")
    restarted: list[str] = []

    @ops.tool
    def restart_db(host: str) -> str:
        """Restart a database host."""
        restarted.append(host)
        return f"{host} restarted"

    monkeypatch.setattr(mcp_client, "build_transport", lambda cfg: ops)
    llm = FakeLLM(
        [
            tool_calls(("get_task_instances", {}), ("mcp__ops__restart_db", {"host": "db"})),
            tool_calls(("final_answer", {"answer": "Restarted the database."})),
        ]
    )
    client, services = make(
        tmp_path,
        llm,
        perms=ALL_PERMISSIONS,
        ai={
            "troubleshoot": {"airflow_actions": "hitl"},
            "mcp_client": {"servers": [{"name": "ops", "url": "https://mcp.example/ops"}]},
            # hitl: write tools wait for approval even when exempted
            "approvals": {"agents": {"troubleshoot": {"exempt": ["mcp__ops__*"]}}},
        },
    )
    body = {"dag_id": "ai_chat", "run_id": "run1", "mcp": [{"server": "ops"}], "spec": spec_data()}
    paused = result_of(events(client.post("/ai/troubleshoot/stream", json=body)))
    assert paused["status"] == "awaiting_approval"
    approval = paused["approvals"][-1]
    assert [c["tool"] for c in approval["calls"]] == ["mcp__ops__restart_db"]
    assert restarted == []
    stored = client.get(f"/ai/troubleshoot/analyses/{paused['id']}").json()
    assert stored["status"] == "awaiting_approval"

    done = result_of(
        events(resume(client, approval["id"], {"id": approval["calls"][0]["id"], "approved": True}))
    )
    assert done["id"] == paused["id"]
    assert done["summary"] == "Restarted the database."
    assert done["status"] == "no_change"
    assert [s["tool"] for s in done["steps"]] == ["get_task_instances", "mcp__ops__restart_db"]
    assert done["approvals"][-1]["status"] == "resolved"
    assert restarted == ["db"]
    assert any(r.feature == "troubleshoot:tool_approved" for r in audit(services))


@requires_mcp
def test_troubleshoot_exempt_write_tool_runs_outside_hitl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import FastMCP

    from autoair.ai.assistants import mcp_client

    fake_airflow(monkeypatch)
    ops = FastMCP("ops")

    @ops.tool
    def restart_db(host: str) -> str:
        """Restart a database host."""
        return f"{host} restarted"

    monkeypatch.setattr(mcp_client, "build_transport", lambda cfg: ops)
    llm = FakeLLM(
        [
            tool_calls(("mcp__ops__restart_db", {"host": "db"})),
            tool_calls(("final_answer", {"answer": "Done."})),
        ]
    )
    client, _ = make(
        tmp_path,
        llm,
        ai={
            "mcp_client": {"servers": [{"name": "ops", "url": "https://mcp.example/ops"}]},
            "approvals": {"agents": {"troubleshoot": {"exempt": ["mcp__ops__*"]}}},
        },
    )
    body = {"dag_id": "ai_chat", "run_id": "run1", "mcp": [{"server": "ops"}], "spec": spec_data()}
    analysis = result_of(events(client.post("/ai/troubleshoot/stream", json=body)))
    assert analysis["status"] == "no_change"
    assert analysis["approvals"] == []


def test_denial_reasons_are_scrubbed_in_the_audit_and_the_model_context(tmp_path: Path) -> None:
    def final(call: FakeCall) -> str:
        denial = next(p for p in call.tool_results if p.tool_name == "remember")
        assert "hunter2-in-a-reason" not in str(denial.content)
        return "Understood."

    llm = FakeLLM([tool_calls(REMEMBER), final])
    client, services = make(tmp_path, llm)
    _, approval = paused_turn(client)
    call_id = approval["calls"][0]["id"]
    reason = "no, password=hunter2-in-a-reason"
    msg = result_of(
        events(resume(client, approval["id"], {"id": call_id, "approved": False, "reason": reason}))
    )["message"]
    assert "hunter2-in-a-reason" not in msg["tools"][0]["preview"]
    denied = next(r for r in audit(services) if r.feature == "assistant:tool_denied")
    assert "hunter2-in-a-reason" not in denied.detail["reason"]
    assert denied.detail["reason"].startswith("no, password=")
