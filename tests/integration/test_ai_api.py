from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autoair.ai import LLMError
from autoair.ai.testing import FakeCall, FakeLLM
from autoair.api.app import build_services
from autoair.api.routers import ai
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


def spec_data() -> dict[str, Any]:
    return {
        "dag_id": "ai_api",
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


def make_client(tmp_path: Path, llm: Any = None) -> TestClient:
    services = build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())
    if llm is not None:
        setattr(services, "llm", llm)  # noqa: B010 - Services may not declare `llm` yet
    api = FastAPI()
    api.state.services = services
    api.include_router(ai.router)
    return TestClient(api)


def test_not_configured(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    status = client.get("/ai/status").json()
    assert status["configured"] is False
    assert status["provider"] is None
    assert "leaks" in status["features"]
    resp = client.post("/ai/generate", json={"prompt": "hi"})
    assert resp.status_code == 503
    assert "AUTOAIR_ANTHROPIC_API_KEY" in resp.json()["detail"]
    assert client.post("/ai/describe", json={"spec": spec_data()}).status_code == 503


def test_generate_and_edit(tmp_path: Path) -> None:
    drafts = [{"explanation": "made", "spec": spec_data()}]
    edited = spec_data()
    edited["nodes"][0]["params"]["bash_command"] = "echo bye"
    drafts.append({"explanation": "edited", "spec": edited})

    def answer(call: FakeCall) -> dict[str, Any]:
        if "operators" in (call.json_schema or {}).get("properties", {}):
            return {"operators": [BASH], "notes": ""}
        return drafts.pop(0)

    llm = FakeLLM([answer] * 4)
    client = make_client(tmp_path, llm)
    status = client.get("/ai/status").json()
    assert (status["configured"], status["provider"], status["model"]) == (
        True,
        "fake",
        "fake-model",
    )
    assert "review" in status["features"]

    first = client.post("/ai/generate", json={"prompt": "say hi"}).json()
    assert first["ok"] is True
    assert first["spec"]["dag_id"] == "ai_api"
    assert first["attempts"] == 1

    second = client.post("/ai/generate", json={"prompt": "say bye", "existing": first["spec"]})
    assert second.status_code == 200
    body = second.json()
    assert body["explanation"] == "edited"
    assert body["spec"]["nodes"][0]["params"]["bash_command"] == "echo bye"


def test_explain_and_describe(tmp_path: Path) -> None:
    llm = FakeLLM(["Add the command.", "Says hi."])
    client = make_client(tmp_path, llm)
    bad = spec_data()
    bad["nodes"][0]["params"] = {}
    explained = client.post("/ai/explain", json={"spec": bad})
    assert explained.status_code == 200
    assert explained.json() == {"explanation": "Add the command."}
    assert "bash_command" in llm.calls[0].messages[0]["content"]  # validated server-side

    described = client.post("/ai/describe", json={"spec": spec_data()})
    assert described.json() == {"doc_md": "Says hi."}


def test_llm_error_is_502(tmp_path: Path) -> None:
    def fail(call: FakeCall) -> str:
        raise LLMError("boom")

    client = make_client(tmp_path, FakeLLM([fail]))
    resp = client.post("/ai/describe", json={"spec": spec_data()})
    assert resp.status_code == 502
    assert "boom" in resp.json()["detail"]


# --------------------------------------------------------------------------- new features


def finding(**kw: Any) -> dict[str, Any]:
    base = {
        "severity": "medium",
        "category": "reliability",
        "title": "t",
        "message": "m",
        "node_id": None,
        "field": None,
        "patch": [],
    }
    return {**base, **kw}


def test_leaks_and_review_work_without_llm(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    leaky = spec_data()
    leaky["nodes"][0]["params"]["bash_command"] = "set -x; curl -u admin:s3cretpw https://x"
    report = client.post("/ai/leaks", json={"spec": leaky}).json()
    rules = {f["rule"] for f in report["findings"]}
    assert {"shell-inline-credentials", "shell-xtrace", "curl-no-timeout"} <= rules
    assert report["ai_used"] is False
    assert "s3cretpw" not in str(report["findings"])
    review = client.post("/ai/review", json={"spec": spec_data()}).json()
    assert review["proposed_ok"] is True
    assert review["proposed_spec"]["default_args"]["retries"] == 2
    # explain needs an LLM
    assert client.post("/ai/explain-dag", json={"spec": spec_data()}).status_code == 503


def test_review_with_ai_model_override_and_apply(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            {
                "summary": "ok",
                "findings": [
                    finding(
                        title="Set a pool",
                        node_id="hello",
                        field="pool",
                        patch=[{"op": "add", "path": "/nodes/@hello/pool", "value_json": '"p"'}],
                    )
                ],
            }
        ]
    )
    client = make_client(tmp_path, llm)
    bad = client.post("/ai/review", json={"spec": spec_data(), "model": "evil-model"})
    assert bad.status_code == 422
    report = client.post("/ai/review", json={"spec": spec_data()}).json()
    assert report["ai_used"] is True
    pool = next(f for f in report["findings"] if f["title"] == "Set a pool")
    applied = client.post(
        "/ai/apply",
        json={"spec": spec_data(), "findings": report["findings"], "only": [pool["id"]]},
    ).json()
    assert applied["applied"] == [pool["id"]]
    assert applied["spec"]["nodes"][0]["pool"] == "p"
    assert applied["spec"]["default_args"].get("retries") is None


def test_explain_code_task_code_and_explain_dag(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            "It echoes.",
            {"code": "return 1", "explanation": "x", "requirements": [], "connections": []},
            {
                "summary": "s",
                "schedule": "manual",
                "steps": [{"node_id": "hello", "title": "t", "explanation": "e"}],
                "data_flow": "-",
                "connections": [],
                "risks": [],
            },
        ]
    )
    client = make_client(tmp_path, llm)
    code = client.post("/ai/explain-code", json={"code": "echo hi", "language": "bash"}).json()
    assert code["explanation"] == "It echoes."
    task = client.post("/ai/task-code", json={"description": "return one"}).json()
    assert task["code"] == "return 1"
    assert task["syntax_ok"] is True
    dag = client.post("/ai/explain-dag", json={"spec": spec_data(), "node_id": "hello"}).json()
    assert dag["steps"][0]["node_id"] == "hello"


def test_fix_reads_task_log_from_airflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair.gateway import AirflowGateway, TaskInstanceInfo

    async def tis(self: Any, dag_id: str, run_id: str) -> list[TaskInstanceInfo]:
        return [TaskInstanceInfo(task_id="hello", try_number=2, state="failed")]

    async def log(self: Any, dag_id: str, run_id: str, task_id: str, try_number: int) -> str:
        assert try_number == 2
        return "ERROR command not found: ech"

    monkeypatch.setattr(AirflowGateway, "task_instances", tis)
    monkeypatch.setattr(AirflowGateway, "task_log", log)
    llm = FakeLLM(
        [
            {
                "summary": "Typo in the command",
                "findings": [
                    finding(
                        title="Fix typo",
                        node_id="hello",
                        field="bash_command",
                        patch=[
                            {
                                "op": "replace",
                                "path": "/nodes/@hello/params/bash_command",
                                "value_json": '"echo hi"',
                            }
                        ],
                    )
                ],
            }
        ]
    )
    client = make_client(tmp_path, llm)
    broken = spec_data()
    broken["nodes"][0]["params"]["bash_command"] = "ech hi"
    report = client.post(
        "/ai/fix", json={"spec": broken, "run_id": "r1", "task_id": "hello"}
    ).json()
    assert report["summary"] == "Typo in the command"
    assert report["proposed_spec"]["nodes"][0]["params"]["bash_command"] == "echo hi"
    assert "command not found" in llm.calls[0].messages[0]["content"]


def test_optimize_without_airflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair.gateway import AirflowError, AirflowGateway

    async def runs(self: Any, dag_id: str, limit: int = 25) -> Any:
        raise AirflowError(502, "cannot reach Airflow")

    monkeypatch.setattr(AirflowGateway, "dag_runs", runs)
    client = make_client(tmp_path)
    report = client.post("/ai/optimize", json={"spec": spec_data()}).json()
    assert report["notes"][0].startswith("Run history unavailable")
    assert report["stats"] is None
    assert any(f["rule"] == "no-execution-timeout" for f in report["findings"])


def test_search_and_ask(tmp_path: Path) -> None:
    from autoair.ai.testing import tool_calls

    llm = FakeLLM(
        [
            tool_calls(("search_catalog", {"query": "bash"})),
            tool_calls(("final_answer", {"answer": "Use BashOperator."})),
        ]
    )
    client = make_client(tmp_path, llm)
    hits = client.get("/ai/search", params={"q": "bash command", "kinds": "operator"}).json()
    assert hits[0]["kind"] == "operator"
    result = client.post(
        "/ai/ask", json={"question": "How do I run a script?", "spec": spec_data()}
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["answer"] == "Use BashOperator."
    assert body["steps"][0]["tool"] == "search_catalog"
    assert body["steps"][0]["ok"] is True
