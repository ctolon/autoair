from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from autoair.ai import (
    LLM,
    LLMError,
    ModelConfig,
    build_llm,
    check_spec,
    describe_dag,
    explain_problems,
    generate_spec,
    rank_operators,
)
from autoair.ai.config import AiSettings
from autoair.ai.testing import FakeCall, FakeLLM
from autoair.catalog import OperatorCatalog
from autoair.registry import default_registry
from autoair.spec import DagSpec
from autoair.validation import Problem

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"


@pytest.fixture(scope="module")
def catalog() -> OperatorCatalog:
    return OperatorCatalog()


def good_spec(dag_id: str = "ai_dag") -> dict[str, Any]:
    return {
        "dag_id": dag_id,
        "schedule": {"type": "cron", "expression": "0 6 * * *"},
        "nodes": [
            {"kind": "operator", "id": "start", "operator": EMPTY},
            {
                "kind": "operator",
                "id": "hello",
                "task_id": "hello",
                "operator": BASH,
                "params": {"bash_command": "echo hello"},
            },
        ],
        "edges": [{"source": "start", "target": "hello"}],
    }


def router(*spec_answers: dict[str, Any], picks: tuple[str, ...] = (BASH, EMPTY)):
    """Fake LLM answering the operator selection and the spec drafts."""
    drafts = list(spec_answers)

    def answer(call: FakeCall) -> dict[str, Any]:
        props = (call.json_schema or {}).get("properties", {})
        if "operators" in props:
            enum = props["operators"]["items"]["enum"]
            return {"operators": [p for p in picks if p in enum], "notes": ""}
        return drafts.pop(0)

    return FakeLLM([answer] * (len(spec_answers) + 1))


def test_happy_path(catalog: OperatorCatalog) -> None:
    llm = router({"explanation": "Daily hello", "spec": good_spec()})
    result = generate_spec(
        "Every day at 6 run a bash command that says hello", catalog=catalog, llm=llm
    )
    assert result.ok, result.problems
    assert result.attempts == 1
    assert result.spec is not None
    assert result.spec.dag_id == "ai_dag"
    # normalisation filled the missing task_id
    assert result.spec.node_map()["start"].task_id == "start"  # type: ignore[union-attr]
    assert result.explanation == "Daily hello"
    assert BASH in result.selected_operators
    spec_call = llm.calls[-1]
    assert spec_call.kind == "json"
    assert "spec" in spec_call.json_schema["properties"]  # type: ignore[index]
    assert "DagSpec JSON schema" in spec_call.system
    assert "bash_command" in spec_call.messages[0]["content"]  # operator param schema sent


def test_repair_loop_fixes_invalid_answer(catalog: OperatorCatalog) -> None:
    bad = good_spec()
    bad["nodes"][1]["params"] = {"bash_command": 42, "not_a_param": 1}
    bad["edges"].append({"source": "hello", "target": "ghost"})
    llm = router({"explanation": "v1", "spec": bad}, {"explanation": "v2", "spec": good_spec()})
    result = generate_spec("say hello", catalog=catalog, llm=llm, max_repairs=2)
    assert result.ok
    assert result.attempts == 2
    assert result.explanation == "v2"
    feedback = llm.calls[-1].messages[-1]["content"]
    assert "/nodes/1/params/bash_command" in feedback
    assert "not_a_param" in feedback
    assert llm.calls[-1].messages[-2]["role"] == "assistant"


def test_repair_gives_up(catalog: OperatorCatalog) -> None:
    bad = good_spec()
    bad["nodes"][1]["params"] = {}
    llm = router({"explanation": "", "spec": bad}, {"explanation": "", "spec": copy.deepcopy(bad)})
    result = generate_spec("say hello", catalog=catalog, llm=llm, max_repairs=1)
    assert not result.ok
    assert result.attempts == 2
    assert result.spec is not None
    assert any(p.code == "param" for p in result.problems)


def test_schema_errors_have_paths(catalog: OperatorCatalog) -> None:
    data = good_spec()
    data["schedule"] = {"type": "cron", "expression": "bogus"}
    data["nodes"][1]["retries"] = -1
    check = check_spec(data, catalog=catalog, registry=default_registry())
    assert not check.ok
    assert check.spec is None
    paths = {p.path for p in check.problems}
    assert "/schedule/expression" in paths
    assert "/nodes/1/retries" in paths
    assert {p.node_id for p in check.problems if p.path == "/nodes/1/retries"} == {"hello"}


def test_edit_mode_keeps_positions(catalog: OperatorCatalog) -> None:
    existing_data = good_spec()
    existing_data["nodes"][0]["task_id"] = "start"
    existing_data["nodes"][1]["position"] = {"x": 120, "y": 40}
    existing = DagSpec.model_validate(existing_data)

    edited = good_spec()
    edited["nodes"].append(
        {
            "kind": "operator",
            "id": "bye",
            "operator": BASH,
            "params": {"bash_command": "echo bye"},
        }
    )
    edited["edges"].append({"source": "hello", "target": "bye"})
    llm = router({"explanation": "added bye", "spec": edited})
    result = generate_spec("add a goodbye task", catalog=catalog, llm=llm, existing=existing)
    assert result.ok, result.problems
    assert result.spec is not None
    nodes = result.spec.node_map()
    assert set(nodes) == {"start", "hello", "bye"}
    assert nodes["hello"].position.x == 120
    request = llm.calls[-1].messages[0]["content"]
    assert "Current DAG spec to modify" in request
    assert "add a goodbye task" in request


def test_bare_spec_answer_is_accepted(catalog: OperatorCatalog) -> None:
    llm = router(good_spec())
    assert generate_spec("hello", catalog=catalog, llm=llm).ok


def test_llm_failure_raises(catalog: OperatorCatalog) -> None:
    llm = FakeLLM([{"operators": [], "notes": ""}])
    with pytest.raises(LLMError):
        generate_spec("hello", catalog=catalog, llm=llm)


def test_rank_operators_prefers_matches(catalog: OperatorCatalog) -> None:
    ranked = rank_operators("copy a file", catalog.summaries(), limit=5)
    fqns = [s.fqn for s in ranked]
    assert len(fqns) == 5
    assert EMPTY in fqns
    assert BASH in fqns
    assert any("file" in f.lower() for f in fqns)


def test_explain_and_describe() -> None:
    data = good_spec()
    data["nodes"][0]["task_id"] = "start"
    spec = DagSpec.model_validate(data)
    problem = Problem(severity="error", code="x", message="broken", node_id="hello")
    llm = FakeLLM(["Fix the command.", "Says hello daily."])
    assert explain_problems(spec, [], llm) == "No problems found."
    assert explain_problems(spec, [problem], llm) == "Fix the command."
    assert "broken" in llm.calls[0].messages[0]["content"]
    assert describe_dag(spec, llm) == "Says hello daily."
    assert llm.calls[-1].kind == "text"


# --------------------------------------------------------------------------- Anthropic


def _anthropic(handler: Any, **kwargs: Any) -> LLM:
    config = ModelConfig(provider="anthropic", api_key=SecretStr("sk-test"), **kwargs)
    return LLM(config, transport=httpx2.MockTransport(handler))


def _message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_anthropic_request_shape() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(request.content), key=request.headers.get("x-api-key"))
        return httpx2.Response(200, json=_message("hello"))

    client = _anthropic(handler, effort="high")
    assert client.complete_text("sys", [{"role": "user", "content": "hi"}], 1000) == "hello"
    assert seen["model"] == "claude-sonnet-5-5"
    assert seen["max_tokens"] == 1000
    assert seen["output_config"]["effort"] == "high"
    assert seen["system"][0]["text"] == "sys"
    assert seen["key"] == "sk-test"


def test_anthropic_refusal_and_truncation() -> None:
    with pytest.raises(LLMError, match="declined"):
        _anthropic(lambda r: httpx2.Response(200, json=_message("", "refusal"))).complete_text(
            "s", "q"
        )
    with pytest.raises(LLMError, match="truncated"):
        _anthropic(lambda r: httpx2.Response(200, json=_message("{", "max_tokens"))).complete_text(
            "s", "q"
        )


def _ai(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(ai=AiSettings(**fields))


def test_build_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert build_llm(SimpleNamespace()) is None

    llm = build_llm(_ai(anthropic_api_key=SecretStr("sk-test"), model="claude-opus-5-5"))
    assert isinstance(llm, LLM)
    assert llm.provider == "anthropic"
    assert llm.model == "claude-opus-5-5"

    conn_llm = build_llm(_ai(llm_conn_id="claude"))
    assert isinstance(conn_llm, LLM)
    assert conn_llm.conn_id == "claude"
    assert conn_llm.provider == "anthropic"


def test_airflow_connection_llm_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = json.dumps({"model": "anthropic:claude-haiku-5-5"})
    monkeypatch.setenv(
        "AIRFLOW_CONN_MY_CLAUDE",
        json.dumps({"conn_type": "pydanticai", "password": "sk-x", "extra": extra}),
    )
    llm = LLM(conn_id="my_claude")
    resolved = llm.resolve()
    assert resolved is not None
    assert resolved.model == "claude-haiku-5-5"
    assert resolved.api_key is not None
    assert resolved.api_key.get_secret_value() == "sk-x"
    assert llm.model == "claude-haiku-5-5"
