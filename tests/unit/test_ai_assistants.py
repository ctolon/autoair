"""Unit tests of the assistants: prompts, agent history/final model, threads, compaction,
citations, follow-ups, the filesystem sandbox, project rules and external MCP servers."""

from __future__ import annotations

import asyncio
import itertools
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from autoair.ai import prompts
from autoair.ai.agent import AgentTask, arun_agent
from autoair.ai.assistants.chat import split_follow_ups
from autoair.ai.assistants.compaction import (
    MAX_OLD_TOOL_CHARS,
    compact,
    dump_history,
    history_messages,
    stored_history,
    trim_history,
)
from autoair.ai.assistants.fs_sandbox import FsReadInput, FsSandbox, SandboxError
from autoair.ai.assistants.sources import SourceCollector
from autoair.ai.assistants.threads import (
    ChatMessage,
    ChatThread,
    ThreadNotFound,
    ThreadStore,
)
from autoair.ai.config import AiSettings, McpServerConfig, TroubleshootFilesystem, activate_ai
from autoair.ai.testing import FakeLLM
from autoair.ai.tools import AgentTool
from autoair.persistence.documents import FileDocumentStore
from tests.optional import requires_mcp

# --------------------------------------------------------------------------- prompts


def test_prompt_replace_append_variables_and_rules() -> None:
    names = prompts.names()
    for name in (
        "DEVELOPER_ASSISTANT_SYSTEM",
        "ADMIN_ASSISTANT_SYSTEM",
        "TROUBLESHOOT_SYSTEM",
        "UPGRADE_SYSTEM",
        "DEPLOY_MESSAGE_SYSTEM",
        "COMPACT_SYSTEM",
    ):
        assert name in names
    assert "_ASSISTANT_STYLE" not in names
    base = prompts.get("DEVELOPER_ASSISTANT_SYSTEM")
    assert "your organisation" in base
    assert "${" not in base
    assert "{AUTOAIR_CONTEXT}" not in base
    assert "DagSpec" in base

    class S:
        ai = AiSettings.model_validate(
            {
                "prompts": {
                    "organization": "Acme",
                    "append": {"DEVELOPER_ASSISTANT_SYSTEM": "Answer for ${team_name}."},
                    "replace": {"ADMIN_ASSISTANT_SYSTEM": "Admin of ${organization} ${nope}"},
                    "variables": {"team_name": "data-eng"},
                }
            }
        )

    activate_ai(S())
    with prompts.prompt_context(user="alice"):
        dev = prompts.get("DEVELOPER_ASSISTANT_SYSTEM")
    assert "pairing with\nalice at Acme" in dev
    assert dev.rstrip().endswith("Answer for data-eng.")
    # unknown variables are left as they are
    assert prompts.get("ADMIN_ASSISTANT_SYSTEM") == "Admin of Acme ${nope}"
    assert prompts.source("ADMIN_ASSISTANT_SYSTEM") == "settings"
    assert prompts.overridden("ADMIN_ASSISTANT_SYSTEM")
    # Jinja in built-in prompts is untouched
    assert "{{ ds }}" in prompts.get("WRITER_BASH_SYSTEM")

    seen: list[str | None] = []

    def provider(team: str | None) -> str:
        seen.append(team)
        return "- Owner is always data-eng@"

    prompts.set_rules_provider(provider)
    with prompts.prompt_context(team="core"):
        assert "Owner is always data-eng@" in prompts.get("REVIEW_SYSTEM")
    assert seen[-1] == "core"
    # not an authoring prompt: no rules
    assert "Owner is always" not in prompts.get("EXPLAIN_CODE_SYSTEM")
    prompts.set_rules_provider(lambda team: (_ for _ in ()).throw(RuntimeError("boom")))
    assert prompts.rules_block() == ""  # a failing provider never breaks prompts


def test_prompt_override_file_wins(tmp_path: Path) -> None:
    (tmp_path / "TROUBLESHOOT_SYSTEM.md").write_text("From file for ${organization}")

    class S:
        ai = AiSettings.model_validate(
            {"prompts": {"replace": {"TROUBLESHOOT_SYSTEM": "from settings"}}}
        )

    activate_ai(S())
    prompts.configure(tmp_path)
    try:
        assert prompts.get("TROUBLESHOOT_SYSTEM") == "From file for your organisation"
        assert prompts.source("TROUBLESHOOT_SYSTEM") == "file"
    finally:
        prompts.configure(None)


# --------------------------------------------------------------------------- agent


class _Final(BaseModel):
    answer: str
    follow_ups: list[str] = []
    score: int = 0


def test_agent_history_and_custom_final_model() -> None:
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    llm = FakeLLM([{"answer": "ok", "score": 7}])
    task = AgentTask(
        llm=llm,
        task="second question",
        tools=[],
        history=[
            ModelRequest(parts=[UserPromptPart("first")]),
            ModelResponse(parts=[TextPart("first answer")]),
        ],
        final_model=_Final,
        stream=False,
    )
    result = asyncio.run(arun_agent(task))
    assert result.answer == "ok"
    assert result.extra == {"follow_ups": [], "score": 7}
    sent = llm.calls[0]
    assert [m["role"] for m in sent.messages] == ["user", "assistant", "user"]
    assert sent.json_schema is not None
    assert sent.json_schema["properties"]["score"]["type"] == "integer"


def test_agent_plain_text_answer_uses_final_model() -> None:
    llm = FakeLLM(["Plain answer"])
    task = AgentTask(llm=llm, task="q", tools=[], final_model=_Final, stream=False)
    result = asyncio.run(arun_agent(task))
    assert result.answer == "Plain answer"
    assert result.extra["score"] == 0


def test_agent_tool_schema_override() -> None:
    class Any_(BaseModel):
        model_config = {"extra": "allow"}

    tool = AgentTool("x", "d", Any_, lambda a: a, schema={"type": "object", "title": "T"})
    assert tool.json_schema() == {"type": "object"}


# --------------------------------------------------------------------------- threads


def test_thread_store_crud_ownership_and_pruning(tmp_path: Path) -> None:
    store = ThreadStore(FileDocumentStore(str(tmp_path)), max_threads=2, max_messages=4)
    a = store.save(ChatThread(user="alice", mode="developer", title="one"))
    store.save(ChatThread(user="alice", mode="developer", title="two"))
    store.save(ChatThread(user="alice", mode="admin", title="admin"))
    store.save(ChatThread(user="bob", mode="developer", title="bob's"))
    assert {t.title for t in store.list("alice", "developer")} == {"one", "two"}
    third = store.save(ChatThread(user="alice", mode="developer", title="three"))
    assert len(store.list("alice", "developer")) == 2  # oldest pruned
    assert third.id in {t.id for t in store.list("alice", "developer")}
    with pytest.raises(ThreadNotFound):
        store.get("bob", third.id)
    with pytest.raises(ThreadNotFound):
        store.get("alice", "../etc")
    renamed = store.rename("alice", third.id, "  renamed  ")
    assert renamed.title == "renamed"
    third = store.get("alice", third.id)  # rename saved a newer version
    for i in range(6):
        third.messages.append(
            ChatMessage(role="user" if i % 2 == 0 else "assistant", content=str(i))
        )
    third.summarized = 3
    store.save(third)
    again = store.get("alice", third.id)
    assert [m.content for m in again.messages] == ["2", "3", "4", "5"]
    assert again.summarized == 1
    assert store.delete("alice", third.id)
    with pytest.raises(ThreadNotFound):
        store.get("alice", a.id)


# --------------------------------------------------------------------------- compaction


def _thread(n: int) -> ChatThread:
    t = ChatThread(user="u", mode="developer")
    for i in range(n):
        t.messages.append(
            ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"m{i} " + "x" * 200)
        )
    return t


def test_history_alternates_and_includes_summary() -> None:
    t = _thread(5)
    t.messages.insert(2, ChatMessage(role="user", content="double user"))
    t.summary = "earlier stuff"
    out = history_messages(t, upto=len(t.messages) - 1)
    roles = [m.kind for m in out]
    assert roles[0] == "request"
    assert "earlier stuff" in str(out[0].parts[0].content)  # type: ignore[union-attr]
    assert all(a != b for a, b in itertools.pairwise(roles))
    assert roles[-1] == "response"


def test_stored_history_round_trip_and_api_view() -> None:
    llm = FakeLLM(["first answer"])
    result = asyncio.run(arun_agent(AgentTask(llm=llm, task="first", tools=[], stream=False)))
    t = ChatThread(user="u", mode="developer")
    t.messages.append(ChatMessage(role="user", content="first"))
    answer = ChatMessage(
        role="assistant", content="first answer", history=dump_history(result.messages)
    )
    t.messages.append(answer)
    assert stored_history(answer)  # the pydantic-ai messages come back
    out = history_messages(t)
    assert [m.kind for m in out] == ["request", "response"]
    # Stored with the thread, never returned by the API.
    assert answer.model_dump(mode="json")["history"] is None
    stored = answer.model_dump(mode="json", context={"store": True})
    assert stored["history"]
    assert ChatMessage.model_validate(stored).history == answer.history
    broken = ChatMessage(role="assistant", content="x", history=[{"kind": "nope"}])
    assert stored_history(broken) == []


def test_trim_history_shortens_old_tool_results_only() -> None:
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    big = "x" * (MAX_OLD_TOOL_CHARS + 500)
    messages = [
        ModelRequest(parts=[UserPromptPart("old question")]),
        ModelResponse(parts=[ToolCallPart("t", {}, tool_call_id="1")]),
        ModelRequest(parts=[ToolReturnPart("t", big, tool_call_id="1")]),
        ModelRequest(parts=[UserPromptPart("new question")]),
        ModelResponse(parts=[ToolCallPart("t", {}, tool_call_id="2")]),
        ModelRequest(parts=[ToolReturnPart("t", big, tool_call_id="2")]),
    ]
    out = trim_history(messages)
    old = out[2].parts[0]
    new = out[5].parts[0]
    assert len(str(old.content)) < len(big)  # type: ignore[union-attr]
    assert new.content == big  # type: ignore[union-attr]


def test_compact_folds_old_messages() -> None:
    t = _thread(10)
    llm = FakeLLM(["- summary of goals"])
    done = asyncio.run(compact(t, llm, upto=9, trigger_chars=500, keep_messages=4))
    assert done
    assert t.summary == "- summary of goals"
    assert t.summarized == 5
    assert "COMPACT" not in llm.calls[0].system
    assert "summary" in llm.calls[0].system.lower()
    # below the trigger: nothing happens
    assert not asyncio.run(compact(_thread(2), llm, upto=1, trigger_chars=10_000, keep_messages=2))


def test_split_follow_ups() -> None:
    text = (
        "Use BashOperator [1].\n\n<follow_ups>\n- How do I retry?\n2) And timeouts?\n</follow_ups>"
    )
    answer, follow = split_follow_ups(text)
    assert answer == "Use BashOperator [1]."
    assert follow == ["How do I retry?", "And timeouts?"]
    assert split_follow_ups("no block") == ("no block", [])
    # an unterminated block (stopped mid-stream) is still removed
    assert split_follow_ups("A\n<follow_ups>\n- q")[0] == "A"


# --------------------------------------------------------------------------- citations


class _NoArgs(BaseModel):
    pass


def test_source_collector_annotates_and_dedupes() -> None:
    src = SourceCollector()
    hits = [
        {"id": "doc:a", "kind": "doc", "title": "Retries", "uri": "kb://retries", "snippet": "s"},
        {"id": "pkg", "kind": "source", "title": "S3Hook", "snippet": ""},
    ]
    out = src.annotate("search_docs", _NoArgs(), hits)
    assert [h["cite"] for h in out] == [1, 2]
    assert src.annotate("search_docs", _NoArgs(), hits[:1])[0]["cite"] == 1

    class LogArgs(BaseModel):
        dag_id: str = "d"
        run_id: str = "r"
        task_id: str = "t"
        try_number: int = 2

    log = src.annotate("get_task_log", LogArgs(), "boom")
    assert log == {"cite": 3, "log": "boom"}
    assert src.citations[2].ref == "log:d/r/t#2"
    assert [c.n for c in src.cited("see [3] and [1]")] == [1, 3]

    async def handler(args: Any) -> Any:
        return hits

    tool = src.wrap(AgentTool("search_catalog", "d", _NoArgs, handler))
    assert asyncio.run(tool.handler(_NoArgs()))[0]["cite"] == 1
    assert src.used_tools == ["search_catalog"]


# --------------------------------------------------------------------------- filesystem


def _sandbox(root: Path, **kw: Any) -> FsSandbox:
    fs = TroubleshootFilesystem(roots=[root], **kw)
    return FsSandbox(fs, [root], scrub=lambda t: t.replace("hunter2", "***"))


def test_fs_sandbox_reads_inside_roots_only(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    (root / "dag_id=x" / "run_id=r").mkdir(parents=True)
    log = root / "dag_id=x" / "run_id=r" / "attempt=1.log"
    log.write_text("\n".join(f"line {i}" for i in range(1, 501)) + "\npassword=hunter2\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    (root / ".env").write_text("A=1")
    (root / "link").symlink_to(outside)
    (root / "dirlink").symlink_to(tmp_path)
    sb = _sandbox(root)

    listing = sb.list(str(root))
    names = {Path(str(e["path"])).name for e in listing["entries"]}  # type: ignore[index]
    assert "dag_id=x" in names
    assert ".env" not in names
    assert "link" not in names

    tail = sb.read(FsReadInput(path="dag_id=x/run_id=r/attempt=1.log", line_count=3))
    assert "***" in str(tail["text"])
    assert "hunter2" not in str(tail["text"])
    assert tail["total_lines"] == 501
    grep = sb.read(FsReadInput(path=str(log), mode="grep", pattern=r"line 42\b"))
    assert "    42: line 42" in str(grep["text"])
    window = sb.read(FsReadInput(path=str(log), mode="lines", start_line=10, line_count=2))
    assert str(window["text"]).splitlines()[0].strip().startswith("10:")

    for bad in ("../secret.txt", str(outside), "link", "dirlink/secret.txt", ".env", "nope.log"):
        with pytest.raises(SandboxError):
            sb.read(FsReadInput(path=bad))
    with pytest.raises(SandboxError):
        sb.read(FsReadInput(path=str(root)))  # a directory


def test_fs_sandbox_limits(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "big.log").write_text("x" * 5000)
    (root / "app.txt").write_text("hello")
    sb = _sandbox(root, max_file_bytes=1024, allow_globs=["*.log"])
    with pytest.raises(SandboxError, match="larger"):
        sb.read(FsReadInput(path="big.log"))
    with pytest.raises(SandboxError, match="allowed file name"):
        sb.read(FsReadInput(path="app.txt"))
    assert not FsSandbox(TroubleshootFilesystem(), []).enabled
    with pytest.raises(SandboxError):
        FsSandbox(TroubleshootFilesystem(), []).read(FsReadInput(path="/etc/passwd"))


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_fs_sandbox_refuses_fifo(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    os.mkfifo(root / "pipe.log")
    with pytest.raises(SandboxError):
        _sandbox(root).read(FsReadInput(path="pipe.log"))


# --------------------------------------------------------------------------- MCP servers


def test_mcp_server_config_validation() -> None:
    with pytest.raises(ValueError, match="needs a command"):
        McpServerConfig(name="x", transport="stdio")
    with pytest.raises(ValueError, match="http"):
        McpServerConfig(name="x", transport="http", url="ftp://x")
    with pytest.raises(ValueError, match="pattern"):
        McpServerConfig(name="Bad Name", url="https://x")
    ok = McpServerConfig(
        name="grafana", url="https://mcp.example/mcp", headers={"A": "s3cr3t-v4lue"}
    )
    assert "s3cr3t" not in repr(ok)


@requires_mcp
def test_mcp_tools_in_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fastmcp import FastMCP

    from autoair.ai.assistants import mcp_client
    from autoair.ai.assistants.mcp_client import (
        McpSelection,
        agent_tool_name,
        attach_mcp_tools,
        list_server_tools,
    )

    server = FastMCP("metrics")

    @server.tool
    def cpu(host: str) -> str:
        """CPU usage of a host."""
        return f"{host}: 97%"

    @server.tool
    def restart(host: str) -> str:
        """Restart a host (dangerous)."""
        return "restarted"

    monkeypatch.setattr(mcp_client, "build_transport", lambda cfg: server)
    from autoair.settings import Settings

    class Services:
        settings = Settings(
            dags_folder=tmp_path,
            subfolder="",
            ai={
                "mcp_client": {
                    "servers": [
                        {"name": "metrics", "url": "https://mcp.example/mcp", "tools": ["cpu"]}
                    ]
                }
            },
        )

    services = Services()
    cfg = services.settings.ai.mcp_client.servers[0]
    listed = asyncio.run(list_server_tools(services, cfg))
    assert {t.name: t.allowed for t in listed} == {"cpu": True, "restart": False}

    async def run() -> tuple[list[str], str, list[str]]:
        async with attach_mcp_tools(
            services,
            [McpSelection(server="metrics"), McpSelection(server="missing")],
            "troubleshoot",
        ) as (tools, notes):
            out = await tools[0].handler(tools[0].input_model.model_validate({"host": "db1"}))
            return [t.name for t in tools], out, notes

    names, out, notes = asyncio.run(run())
    assert names == [agent_tool_name("metrics", "cpu")] == ["mcp__metrics__cpu"]
    assert out == "db1: 97%"
    assert "missing" in notes[0]


# --------------------------------------------------------------------------- gateway


def test_gateway_connection_metadata_never_returns_secrets() -> None:
    import httpx2

    from autoair.gateway import AirflowGateway

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/connections/pg"):
            return httpx2.Response(
                200,
                json={
                    "connection_id": "pg",
                    "conn_type": "postgres",
                    "host": "db",
                    "login": "admin",
                    "password": "***",
                    "schema": "sales",
                    "port": 5432,
                    "extra": '{"sslmode": "require", "api_token": "abc123"}',
                },
            )
        if request.url.path.endswith("/pools"):
            return httpx2.Response(
                200, json={"pools": [{"name": "default_pool", "slots": 8, "queued_slots": 3}]}
            )
        if request.url.path.endswith("/xcomEntries"):
            return httpx2.Response(200, json={"xcom_entries": [{"key": "rows", "map_index": -1}]})
        return httpx2.Response(404, json={"detail": "nope"})

    async def run() -> tuple[Any, Any, Any, Any]:
        async with AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler)) as gw:
            return (
                await gw.connection_metadata("pg"),
                await gw.connection_metadata("missing"),
                await gw.pool_states(),
                await gw.xcom_keys("d", "r", "t"),
            )

    meta, missing, pools, keys = asyncio.run(run())
    dumped = meta.model_dump(by_alias=True)
    assert dumped["extra_keys"] == ["api_token", "sslmode"]
    assert dumped["schema"] == "sales"
    assert dumped["port"] == 5432
    text = str(dumped)
    assert "abc123" not in text
    assert "admin" not in text
    assert "require" not in text
    assert missing is None
    assert pools[0].queued_slots == 3
    assert keys == [{"key": "rows", "map_index": -1}]


def test_scrub_secrets_in_logs_and_snippets() -> None:
    from autoair.ai.rules import scrub_secrets

    assert scrub_secrets("using password=sup3rs3cret now") == "using password=*** now"
    assert scrub_secrets("Authorization: Bearer abcdef123") == "Authorization: Bearer ***"
    assert scrub_secrets("db_token: 'abcd1234'") == "db_token: '***'"
    assert scrub_secrets("password: <set-me>") == "password: <set-me>"
    assert (
        scrub_secrets("conn_id=postgres_default retries=3") == "conn_id=postgres_default retries=3"
    )
