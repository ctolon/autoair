"""Agent loop and AI feature functions with scripted (fake) models."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from autoair.ai.agent import AgentResult, AgentTask, arun_agent
from autoair.ai.assist import (
    body_syntax_error,
    compact_spec,
    explain_dag,
    fix_dag,
    optimize_dag,
    review_dag,
    scan_dag_leaks,
    write_task_code,
)
from autoair.ai.guardrails import Budget
from autoair.ai.llm import LLMError
from autoair.ai.testing import FakeLLM, tool_calls
from autoair.ai.toolbox import build_toolbox
from autoair.ai.tools import AgentTool
from autoair.api.app import build_services
from autoair.auth import AuthBackend
from autoair.catalog import OperatorCatalog
from autoair.registry import default_registry
from autoair.settings import Settings
from autoair.spec import DagSpec

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


@pytest.fixture(scope="module")
def catalog() -> OperatorCatalog:
    return OperatorCatalog()


def spec_data(command: str = "echo hi") -> dict[str, Any]:
    return {
        "dag_id": "agent_dag",
        "nodes": [
            {
                "kind": "operator",
                "id": "hello",
                "task_id": "hello",
                "operator": BASH,
                "params": {"bash_command": command},
            }
        ],
    }


# --------------------------------------------------------------------------- agent loop


def run_agent(
    llm: Any,
    task: str,
    tools: list[AgentTool],
    *,
    max_steps: int = 8,
    token_budget: int = 250_000,
    **kw: Any,
) -> AgentResult:
    return asyncio.run(
        arun_agent(
            AgentTask(
                llm=llm,
                task=task,
                tools=tools,
                budget=Budget(max_steps=max_steps, max_tokens=token_budget),
                stream=False,
                **kw,
            )
        )
    )


class EchoInput(BaseModel):
    text: str


def echo_tool(calls: list[str]) -> AgentTool:
    def handler(args: EchoInput) -> dict[str, str]:
        calls.append(args.text)
        if args.text == "fail":
            raise RuntimeError("tool exploded")
        return {"echo": args.text * 3}

    return AgentTool("echo", "Echo text.", EchoInput, handler)


def test_agent_uses_tools_and_returns_validated_answer(catalog: OperatorCatalog) -> None:
    executed: list[str] = []
    llm = FakeLLM(
        [
            tool_calls(("echo", {"text": "a"}), ("echo", {"wrong": 1})),
            tool_calls(("echo", {"text": "a"}), ("nope", {}), ("echo", {"text": "fail"})),
            {
                "answer": "Done.",
                "findings": [
                    {
                        "severity": "low",
                        "category": "reliability",
                        "title": "Add retries",
                        "message": "m",
                        "node_id": "hello",
                        "field": "retries",
                        "patch": [
                            {"op": "add", "path": "/nodes/@hello/retries", "value_json": "2"}
                        ],
                    }
                ],
                "spec": spec_data(),
            },
        ]
    )
    result = run_agent(
        llm,
        "Do it",
        [echo_tool(executed)],
        context="ctx",
        catalog=catalog,
        registry=default_registry(),
    )
    assert result.answer == "Done."
    assert result.stopped == "answer"
    assert executed == ["a", "fail"]  # second "a" served from cache, invalid args never run
    # Unknown tools are answered by pydantic-ai (retry prompt) and never reach a handler.
    assert sorted((s.tool, s.ok) for s in result.steps) == [
        ("echo", False),
        ("echo", False),
        ("echo", True),
        ("echo", True),
    ]
    assert result.proposed_ok is True
    assert result.findings[0].patch[0].value == 2
    assert result.findings[0].node_id == "hello"
    # Tool results were fed back to the model (failures flagged).
    results = llm.calls[1].tool_results
    assert results[0].content == '{"echo":"aaa"}'
    assert getattr(results[1], "outcome", None) == "failed"
    assert "invalid arguments" in str(results[1].content)
    assert llm.calls[0].messages[0]["content"].startswith("ctx")
    assert llm.calls[0].tools == ["echo"]
    assert [t.name for t in llm.calls[0].info.output_tools] == ["final_answer"]
    assert result.messages  # the run's pydantic-ai messages (thread history)


def test_agent_forces_final_answer_at_step_limit() -> None:
    llm = FakeLLM([tool_calls(("echo", {"text": "1"})), {"answer": "partial"}])
    result = run_agent(llm, "loop forever", [echo_tool([])], max_steps=2)
    assert result.answer == "partial"
    assert result.stopped == "answer"
    last = llm.calls[-1]
    assert last.tools == []  # only the final answer is left
    assert not last.info.allow_text_output or last.info.output_tools
    assert "Budget reached" in last.messages[-1]["content"]


def test_agent_token_budget_and_text_answer() -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.usage import RequestUsage

    heavy = ModelResponse(
        parts=[ToolCallPart("echo", {"text": "x"}, tool_call_id="t0")],
        usage=RequestUsage(input_tokens=5_000),
    )
    llm = FakeLLM([heavy, "plain answer"])
    result = run_agent(llm, "q", [echo_tool([])], token_budget=1_000)
    assert result.answer == "plain answer"
    assert result.stopped == "budget"
    assert result.usage.input_tokens >= 5_000
    assert llm.calls[-1].tools == []


def test_agent_step_limit_without_answer_stops() -> None:
    llm = FakeLLM([tool_calls(("echo", {"text": str(i)})) for i in range(10)])
    result = run_agent(llm, "q", [echo_tool([])], max_steps=1)
    assert result.stopped == "max_steps"
    assert "stopped before giving an answer" in result.answer


def test_agent_refusal_raises() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart

    llm = FakeLLM([ModelResponse(parts=[TextPart("")], finish_reason="content_filter")])
    with pytest.raises(LLMError):
        run_agent(llm, "q", [])


def test_toolbox_tools(tmp_path: Path) -> None:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", allow_python_code=False), AuthBackend()
    )
    tools = {t.name: t for t in build_toolbox(services)}
    assert "import_python_dag" not in tools
    assert "get_task_log" not in tools  # no gateway factory
    hits = asyncio.run(
        tools["search_catalog"].handler(tools["search_catalog"].input_model(query="bash"))
    )
    assert any("BashOperator" in h["title"] for h in hits)
    op = tools["get_operator"].handler(tools["get_operator"].input_model(fqn=BASH))
    assert "bash_command" in op["required"]
    valid = tools["validate_spec"].handler(tools["validate_spec"].input_model(spec=spec_data()))
    assert valid["ok"] is True
    lint = tools["lint_spec"].handler(tools["lint_spec"].input_model(spec=spec_data()))
    assert any(f["rule"] == "no-retries" for f in lint)
    code = tools["render_code"].handler(tools["render_code"].input_model(spec=spec_data()))
    assert "BashOperator" in code
    for tool in tools.values():
        schema = tool.json_schema()
        assert schema["type"] == "object"
        json.dumps(schema)


# --------------------------------------------------------------------------- features


def ai_findings(*items: dict[str, Any], summary: str = "AI summary") -> dict[str, Any]:
    return {"summary": summary, "findings": list(items)}


def finding(**kw: Any) -> dict[str, Any]:
    base = {
        "severity": "medium",
        "category": "reliability",
        "title": "Something",
        "message": "Because.",
        "node_id": None,
        "field": None,
        "patch": [],
    }
    return {**base, **kw}


def test_review_merges_rules_and_ai(catalog: OperatorCatalog) -> None:
    spec = DagSpec.model_validate(spec_data())
    llm = FakeLLM(
        [
            ai_findings(
                finding(
                    title="Use a pool",
                    node_id="hello",
                    field="pool",
                    patch=[{"op": "add", "path": "/nodes/@hello/pool", "value_json": '"db"'}],
                ),
                finding(title="Ghost", node_id="not-a-node"),
            )
        ]
    )
    report = review_dag(
        spec,
        catalog=catalog,
        registry=default_registry(),
        llm=llm,
        conventions=["[global] Owner is data-eng"],
        knowledge="KB",
    )
    call = llm.calls[0]
    assert call.kind == "json"
    assert "findings" in call.json_schema["properties"]  # type: ignore[index]
    user = call.messages[0]["content"]
    assert "Already reported" in user
    assert "Owner is data-eng" in user
    assert "Operators used:" in user
    assert '"position"' not in user
    assert report.ai_used
    assert report.summary == "AI summary"
    titles = {f.title: f for f in report.findings}
    assert titles["Use a pool"].source == "ai"
    assert titles["Ghost"].node_id is None  # unknown node ids are dropped
    assert any(f.rule == "no-retries" for f in report.findings)
    assert report.proposed_spec is not None
    hello = report.proposed_spec.node_map()["hello"]
    assert hello.pool == "db"  # type: ignore[union-attr]
    assert report.proposed_spec.default_args.retries == 2


def test_rules_only_reports_without_llm(catalog: OperatorCatalog) -> None:
    spec = DagSpec.model_validate(spec_data("echo $API_TOKEN"))
    leaks = scan_dag_leaks(spec, catalog=catalog, registry=default_registry())
    assert not leaks.ai_used
    assert leaks.findings[0].rule == "shell-echo-secret"
    assert "finding" in leaks.summary
    opt = optimize_dag(spec, catalog=catalog, registry=default_registry())
    assert opt.notes
    assert "No run history" in opt.notes[0]
    assert any(f.rule == "no-execution-timeout" for f in opt.findings)


def test_leak_scan_masks_secrets_before_calling_the_model(catalog: OperatorCatalog) -> None:
    secret = "ghp_" + "b" * 36
    spec = DagSpec.model_validate(spec_data(f"git clone https://{secret}@github.com/x"))
    llm = FakeLLM([ai_findings()])
    report = scan_dag_leaks(spec, catalog=catalog, registry=default_registry(), llm=llm)
    assert secret not in llm.calls[0].messages[0]["content"]
    assert "***REDACTED***" in llm.calls[0].messages[0]["content"]
    assert report.ai_used


def test_fix_applies_patches_and_repairs(catalog: OperatorCatalog) -> None:
    broken = spec_data()
    broken["nodes"][0]["params"] = {"bash_command": 5, "bogus": 1}
    llm = FakeLLM(
        [
            ai_findings(
                finding(
                    title="bash_command must be a string",
                    node_id="hello",
                    field="bash_command",
                    severity="high",
                    category="correctness",
                    patch=[
                        {
                            "op": "replace",
                            "path": "/nodes/@hello/params/bash_command",
                            "value_json": '"echo 5"',
                        }
                    ],
                ),
                summary="Wrong type",
            ),
            ai_findings(
                finding(
                    title="Remove unknown param",
                    node_id="hello",
                    field="bogus",
                    patch=[
                        {"op": "remove", "path": "/nodes/@hello/params/bogus", "value_json": ""}
                    ],
                )
            ),
        ]
    )
    report = fix_dag(
        broken,
        llm=llm,
        catalog=catalog,
        registry=default_registry(),
        task_log="Traceback ... TypeError",
        task_id="hello",
    )
    assert report.proposed_ok is True
    assert report.summary == "Wrong type"
    assert len(llm.calls) == 2
    first = llm.calls[0].messages[0]["content"]
    assert "/nodes/0/params/bash_command" in first
    assert "Failed task log (task hello)" in first
    assert "still has problems" in llm.calls[1].messages[-1]["content"]
    assert {f.title for f in report.findings} == {
        "bash_command must be a string",
        "Remove unknown param",
    }


def test_fix_with_nothing_to_fix(catalog: OperatorCatalog) -> None:
    llm = FakeLLM()
    report = fix_dag(spec_data(), llm=llm, catalog=catalog, registry=default_registry())
    assert report.proposed_ok is True
    assert llm.calls == []


def test_explain_dag_structured(catalog: OperatorCatalog) -> None:
    llm = FakeLLM(
        [
            {
                "summary": "Says hi.",
                "schedule": "manual",
                "steps": [
                    {"node_id": "hello", "title": "Greet", "explanation": "echo"},
                    {"node_id": "ghost", "title": "?", "explanation": "?"},
                ],
                "data_flow": "none",
                "connections": [],
                "risks": ["no retries"],
            }
        ]
    )
    out = explain_dag(
        DagSpec.model_validate(spec_data()), llm=llm, catalog=catalog, question="why?"
    )
    assert out.steps[0].node_id == "hello"
    assert out.steps[1].node_id is None
    assert out.model == "fake-model"
    assert "why?" in llm.calls[0].messages[0]["content"]


def test_write_task_code_repairs_syntax_and_scans() -> None:
    bad = {"code": "return (", "explanation": "", "requirements": [], "connections": []}
    good = {
        "code": "import requests\nr = requests.get(url)\nreturn r.status_code",
        "explanation": "Calls the API.",
        "requirements": ["requests"],
        "connections": [],
    }
    llm = FakeLLM([bad, good])
    spec = DagSpec.model_validate(
        {
            "dag_id": "d",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "call",
                    "task_id": "call",
                    "function_name": "call",
                    "code": "pass",
                    "arguments": {"url": "https://x"},
                }
            ],
        }
    )
    result = write_task_code("Call the API", llm=llm, spec=spec, node_id="call")
    assert result.syntax_ok
    assert "syntax error" in llm.calls[1].messages[0]["content"]
    assert "call(url)" in llm.calls[0].messages[0]["content"]
    assert [f.rule for f in result.findings] == ["http-no-timeout"]
    assert body_syntax_error("return x", ["x"]) is None
    assert body_syntax_error("return (") is not None


def test_structured_output_repair_on_validation_error() -> None:
    llm = FakeLLM([{"summary": "x", "findings": [{"title": None}]}, ai_findings(finding())])
    report = review_dag(
        DagSpec.model_validate(spec_data()), catalog=None, registry=default_registry(), llm=llm
    )
    assert len(llm.calls) == 2  # pydantic-ai output retry
    assert "valid string" in llm.calls[1].messages[-1]["content"]
    assert report.ai_used
    assert compact_spec(spec_data()).startswith('{"dag_id"')
