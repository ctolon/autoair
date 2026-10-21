"""Tool-call approval: the policy, argument redaction, the approval store (compare-and-set
claim on every state backend, expiry, cancel, prune) and the agent runtime's deferred
tools with the scripted model."""

from __future__ import annotations

import datetime
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from autoair.ai.approvals import (
    ApprovalBusy,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalStore,
    PendingToolCall,
    ToolDecision,
    gated_tools,
    needs_approval,
    new_approval,
    redact_arguments,
    withdraw_gated,
)
from autoair.ai.config import AiSettings, ToolApprovalRule, ToolApprovalSettings
from autoair.ai.tools import AgentTool
from autoair.persistence import DatabaseSettings
from autoair.persistence.db import database_for
from autoair.persistence.documents import FileDocumentStore, StoredDocument
from autoair.persistence.sql import SqlDocumentStore
from tests.db_backends import BACKENDS, database_settings, drop_tables

# --------------------------------------------------------------------------- policy


def test_default_policy_gates_write_tools_only() -> None:
    rule = ToolApprovalRule()
    assert needs_approval("remember", "write", rule)
    assert not needs_approval("search_catalog", "read", rule)


def test_policy_tools_exempt_and_forced_writes() -> None:
    rule = ToolApprovalRule(
        classes=["write"], tools=["fetch_doc", "mcp__jira__*"], exempt=["remember", "fetch_doc"]
    )
    assert not needs_approval("remember", "write", rule)
    # an explicit pattern wins over an exemption
    assert needs_approval("fetch_doc", "read", rule)
    assert needs_approval("mcp__jira__create", "read", rule)
    # hitl troubleshooting: write tools always wait, exemptions do not apply
    assert needs_approval("remember", "write", rule, force_writes=True)
    assert not needs_approval("get_pools", "read", rule, force_writes=True)
    assert needs_approval("get_pools", "read", ToolApprovalRule(classes=["read", "write"]))
    assert not needs_approval("remember", "write", ToolApprovalRule(classes=[]))


def _tool(name: str, kind: str = "read") -> AgentTool:
    return AgentTool(name, name, BaseModel, lambda _a: "x", kind=kind)  # type: ignore[arg-type]


def test_gated_tools_per_agent_and_disabled() -> None:
    tools = [_tool("look"), _tool("store", "write")]
    settings = ToolApprovalSettings(agents={"admin": ToolApprovalRule(classes=["read", "write"])})
    kept, gated, notes = gated_tools(tools, settings, "developer")
    assert [t.name for t in kept] == ["look", "store"]
    assert gated == {"store"}
    assert not notes
    _, gated, _ = gated_tools(tools, settings, "admin")
    assert gated == {"look", "store"}
    # approvals off: the tools that would wait are not offered at all
    kept, gated, notes = gated_tools(tools, ToolApprovalSettings(enabled=False), "developer")
    assert [t.name for t in kept] == ["look"]
    assert not gated
    assert "store" in notes[0]
    assert [t.name for t in withdraw_gated(tools, settings, "developer")] == ["look"]


def test_settings_defaults_are_safe() -> None:
    approvals = AiSettings().approvals
    assert approvals.enabled
    assert approvals.default.classes == ["write"]
    assert approvals.default.exempt == []
    assert approvals.rule("troubleshoot") == approvals.default


# --------------------------------------------------------------------------- redaction


def test_redact_arguments_masks_and_cuts() -> None:
    value, cut = redact_arguments(
        {"text": "hello", "password": "hunter22", "nested": {"api_key": "k" * 40}},
        lambda s: s.replace("hello", "h***"),
        1_000,
    )
    assert value == {"text": "h***", "password": "***", "nested": {"api_key": "***"}}
    assert not cut
    value, cut = redact_arguments({"body": "x" * 500}, str, 100)
    assert cut
    assert len(value["body"]) <= 101
    # anything JSON-like, also not a dict, never raises
    assert redact_arguments("plain", str, 10) == ("plain", False)
    deep: Any = []
    for _ in range(30):
        deep = [deep]
    _, cut = redact_arguments(deep, str, 1_000)
    assert cut


def test_redact_arguments_uses_registered_secrets() -> None:
    from autoair import secret_mask

    with secret_mask.masking("tok-12345678"):
        value, _ = redact_arguments({"note": "use tok-12345678 now"}, str, 1_000)
    assert value == {"note": "use *** now"}


# --------------------------------------------------------------------------- store


@pytest.fixture(params=BACKENDS)
def documents(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    settings = SimpleNamespace(
        database=DatabaseSettings(**database_settings(request.param, tmp_path)),
        state_dir=tmp_path / "state",
    )
    db = database_for(settings)
    yield (FileDocumentStore(str(tmp_path / "docs")) if db is None else SqlDocumentStore(db, "ap"))
    drop_tables(settings)


CALL = PendingToolCall(id="t1", tool="remember", arguments={"text": "x"})


def _pending(settings: ToolApprovalSettings, user: str = "alice") -> Any:
    return new_approval(user=user, agent="developer", calls=[CALL], settings=settings)


def test_claim_finish_and_second_decision(documents: Any) -> None:
    settings = ToolApprovalSettings()
    store = ApprovalStore(documents, settings)
    approval = store.create(_pending(settings))
    assert [a.id for a in store.pending("alice")] == [approval.id]
    with pytest.raises(ValueError, match="exactly once"):
        store.claim("alice", approval.id, [])
    with pytest.raises(ValueError, match="exactly once"):
        store.claim("alice", approval.id, [ToolDecision(id="other", approved=True)])
    claimed = store.claim("alice", approval.id, [ToolDecision(id="t1", approved=True)], by="r1")
    assert claimed.status == "resuming"
    assert claimed.claimed_by == "r1"
    assert claimed.decided_by == "alice"
    with pytest.raises(ApprovalBusy):
        store.claim("alice", approval.id, [ToolDecision(id="t1", approved=False)])
    done = store.finish("alice", approval.id, "resolved")
    assert done.status == "resolved"
    assert done.messages == []
    assert done.context == {}
    with pytest.raises(ApprovalBusy) as busy:
        store.claim("alice", approval.id, [ToolDecision(id="t1", approved=True)])
    assert busy.value.state == "resolved"
    assert store.pending("alice") == []


def test_only_the_owner_finds_it(documents: Any) -> None:
    settings = ToolApprovalSettings()
    store = ApprovalStore(documents, settings)
    approval = store.create(_pending(settings))
    with pytest.raises(ApprovalNotFound):
        store.get("mallory", approval.id)
    with pytest.raises(ApprovalNotFound):
        store.claim("mallory", approval.id, [ToolDecision(id="t1", approved=True)])
    with pytest.raises(ApprovalNotFound):
        store.get("alice", "../etc")
    assert store.pending("mallory") == []
    assert store.get("alice", approval.id).status == "pending"


def test_expiry_and_stale_claims(documents: Any) -> None:
    settings = ToolApprovalSettings(ttl_seconds=60, claim_timeout_seconds=30)
    store = ApprovalStore(documents, settings)
    approval = store.create(_pending(settings))
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=61)
    with pytest.raises(ApprovalExpired):
        store.claim("alice", approval.id, [ToolDecision(id="t1", approved=True)], at=later)
    assert store.get("alice", approval.id).status == "expired"
    # a claim whose replica died is shown as failed and never taken over
    other = store.create(_pending(settings))
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=40)
    store.claim("alice", other.id, [ToolDecision(id="t1", approved=True)], at=old)
    assert store.get("alice", other.id).status == "failed"
    with pytest.raises(ApprovalBusy):
        store.claim("alice", other.id, [ToolDecision(id="t1", approved=True)])


def test_cancel_release_and_prune(documents: Any) -> None:
    settings = ToolApprovalSettings(max_pending_per_user=2)
    store = ApprovalStore(documents, settings)
    first = store.create(_pending(settings))
    claimed = store.create(_pending(settings))
    store.claim("alice", claimed.id, [ToolDecision(id="t1", approved=True)], by="me")
    with pytest.raises(ApprovalBusy):
        store.cancel("alice", claimed.id)
    store.release("alice", claimed.id, "someone-else")
    assert store.get("alice", claimed.id).status == "resuming"
    store.release("alice", claimed.id, "me")
    released = store.get("alice", claimed.id)
    assert released.status == "pending"
    assert released.decisions == []
    # a third pending approval cancels the oldest beyond max_pending_per_user
    store.create(_pending(settings))
    assert store.get("alice", first.id).status == "cancelled"
    assert store.cancel("alice", "doesnotexist") is None


class _BothReadFirst:
    """Every thread's first ``get`` waits until all have read: both claimers see the same
    pending version, so the race always happens."""

    def __init__(self, inner: Any, parties: int) -> None:
        self.inner = inner
        self.barrier = threading.Barrier(parties, timeout=10)
        self.seen: set[int] = set()

    def get(self, key: str) -> StoredDocument | None:
        doc = self.inner.get(key)
        me = threading.get_ident()
        if me not in self.seen:
            self.seen.add(me)
            self.barrier.wait()
        return doc

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_two_replicas_one_claim(documents: Any) -> None:
    settings = ToolApprovalSettings()
    approval = ApprovalStore(documents, settings).create(_pending(settings))
    racing = _BothReadFirst(documents, 2)
    stores = [ApprovalStore(racing, settings), ApprovalStore(racing, settings)]  # two replicas
    won: list[str] = []
    busy: list[str] = []

    def decide(i: int) -> None:
        try:
            stores[i].claim(
                "alice", approval.id, [ToolDecision(id="t1", approved=i == 0)], by=f"r{i}"
            )
            won.append(f"r{i}")
        except ApprovalBusy:
            busy.append(f"r{i}")

    threads = [threading.Thread(target=decide, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert len(won) == 1
    assert len(busy) == 1
    stored = ApprovalStore(documents, settings).get("alice", approval.id)
    assert stored.status == "resuming"
    assert stored.claimed_by == won[0]
    assert stored.decisions[0].approved is (won[0] == "r0")


# --------------------------------------------------------------------------- agent runtime


class _Input(BaseModel):
    text: str = ""
    password: str = ""


def test_agent_pauses_and_resumes_with_decisions() -> None:
    import asyncio

    from autoair.ai.agent import AgentTask, ResumeState, arun_agent
    from autoair.ai.testing import FakeLLM, tool_calls

    ran: list[str] = []

    def store(args: _Input) -> str:
        ran.append(args.text)
        return "stored"

    tools = [
        AgentTool("look", "Look around.", _Input, lambda _a: "seen"),
        AgentTool("store", "Store a note.", _Input, store, kind="write"),
    ]
    llm = FakeLLM(
        [
            tool_calls(
                ("look", {}),
                ("store", {"text": "a", "password": "hunter22"}),
                ("store", {"text": "b"}),
                text="Storing.",
            ),
            lambda call: "Done: " + " | ".join(str(p.content) for p in call.tool_results),
        ]
    )
    task = AgentTask(llm=llm, task="go", tools=tools, approval_required=frozenset({"store"}))
    paused = asyncio.run(arun_agent(task))
    assert paused.stopped == "approval"
    assert paused.answer == "Storing."
    assert [c.tool for c in paused.pending] == ["store", "store"]
    assert paused.pending[0].arguments == {"text": "a", "password": "***"}
    assert [s.tool for s in paused.steps] == ["look"]
    assert ran == []
    resumed = asyncio.run(
        arun_agent(
            AgentTask(
                llm=llm,
                task="",
                tools=tools,
                approval_required=frozenset({"store"}),
                history=paused.messages,
                resume=ResumeState(
                    calls=paused.pending,
                    decisions=[
                        ToolDecision(id=paused.pending[0].id, approved=True),
                        ToolDecision(id=paused.pending[1].id, approved=False, reason="not b"),
                    ],
                    decided_by="alice",
                ),
                step_offset=len(paused.steps),
            )
        )
    )
    assert resumed.stopped == "answer"
    assert ran == ["a"]  # the denied call never ran
    assert "Their reason: not b" in resumed.answer
    steps = [(s.index, s.tool, s.ok, s.output_preview) for s in resumed.steps]
    assert steps[0] == (1, "store", False, "denied by alice: not b")
    assert steps[1][:3] == (2, "store", True)


def test_agent_without_gated_tools_never_defers() -> None:
    import asyncio

    from autoair.ai.agent import AgentTask, arun_agent
    from autoair.ai.testing import FakeLLM, tool_calls

    llm = FakeLLM([tool_calls(("store", {"text": "a"})), "ok"])
    ran: list[str] = []
    tools = [AgentTool("store", "Store.", _Input, lambda a: ran.append(a.text), kind="write")]
    result = asyncio.run(arun_agent(AgentTask(llm=llm, task="go", tools=tools)))
    assert result.stopped == "answer"
    assert ran == ["a"]
    assert not result.pending


def test_denial_reasons_are_scrubbed_before_the_model_sees_them() -> None:
    from pydantic_ai.tools import ToolDenied

    from autoair.ai.agent import ResumeState, clean_reason
    from autoair.ai.approvals import PendingToolCall
    from autoair.ai.config import GuardrailSettings
    from autoair.ai.guardrails import GuardrailPipeline
    from autoair.secret_mask import masking

    reason = "no: password=hunter2-typed-in and the key is registered-secret-0001"
    call = PendingToolCall(id="c1", tool="store", arguments={})
    state = ResumeState(
        calls=[call], decisions=[ToolDecision(id="c1", approved=False, reason=reason)]
    )
    with masking("registered-secret-0001"):
        denial = state.results(GuardrailPipeline(GuardrailSettings())).approvals["c1"]
        cleaned = clean_reason(reason)
    assert isinstance(denial, ToolDenied)
    assert "hunter2-typed-in" not in denial.message
    assert "registered-secret-0001" not in denial.message
    assert "Their reason: no:" in denial.message
    assert "hunter2-typed-in" not in cleaned
    assert "registered-secret-0001" not in cleaned
