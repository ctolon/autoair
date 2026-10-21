"""Regression tests: ``features.python_code=false`` alone (the policy switch
``allow_python_code`` left at its default) must disable every Python path of the AI writers
and the agent toolbox; the ``dag_spec`` writer must not spend more than ``max_repairs``
repair rounds."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from autoair.ai.testing import FakeCall, FakeLLM
from autoair.ai.toolbox import build_toolbox
from autoair.ai.writers import (
    PythonCodeDisabled,
    WriteRequest,
    environment_profile,
    python_code_enabled,
    run_writer,
)
from autoair.api.app import build_services
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


def _flag_off(tmp_path: Path) -> Any:
    settings = Settings(dags_folder=tmp_path, subfolder="", features={"python_code": False})
    assert settings.allow_python_code is True  # only the feature flag is off
    return build_services(settings, AuthBackend())


def _run(services: Any, llm: FakeLLM, **kw: Any) -> Any:
    request = WriteRequest(research=False, **kw)
    return asyncio.run(run_writer(request, llm=llm, services=services, emit=lambda _e: None))


def test_python_code_enabled_uses_feature_flag(tmp_path: Path) -> None:
    services = _flag_off(tmp_path)
    assert python_code_enabled(services.settings) is False
    assert python_code_enabled(Settings(dags_folder=tmp_path, subfolder="")) is True
    assert python_code_enabled(None) is True
    assert python_code_enabled(None, default=False) is False


@pytest.mark.parametrize("kind", ["task_body", "inline_function", "custom_operator"])
def test_python_writers_refused_with_only_feature_flag(tmp_path: Path, kind: str) -> None:
    services = _flag_off(tmp_path)
    llm = FakeLLM()
    with pytest.raises(PythonCodeDisabled, match="disabled"):
        _run(services, llm, kind=kind, description="x", function_name="f")
    assert llm.calls == []


def test_environment_profile_reports_feature_flag(tmp_path: Path) -> None:
    assert environment_profile(_flag_off(tmp_path), ttl=0).allow_python_code is False


def test_toolbox_hides_python_import_with_only_feature_flag(tmp_path: Path) -> None:
    tools = {t.name for t in build_toolbox(_flag_off(tmp_path))}
    assert "import_python_dag" not in tools
    enabled = build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())
    assert "import_python_dag" in {t.name for t in build_toolbox(enabled)}


def _python_spec() -> dict[str, Any]:
    return {
        "dag_id": "w",
        "nodes": [
            {
                "kind": "python_task",
                "id": "p",
                "task_id": "p",
                "function_name": "p",
                "code": "return 1",
            }
        ],
    }


def test_dag_spec_writer_refuses_python_nodes_with_flag_off(tmp_path: Path) -> None:
    services = _flag_off(tmp_path)

    def answer(call: FakeCall) -> dict[str, Any]:
        if "operators" in (call.json_schema or {}).get("properties", {}):
            return {"operators": [BASH], "notes": ""}
        return {"explanation": "made", "spec": _python_spec()}

    result = _run(services, FakeLLM([answer] * 10), kind="dag_spec", description="x")
    assert not result.ok
    assert any("Python" in i.message for i in result.issues)


def test_dag_spec_writer_honours_max_repairs(tmp_path: Path) -> None:
    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", ai={"writers": {"max_repairs": 1}}),
        AuthBackend(),
    )
    bad = {"dag_id": "w", "nodes": [{"kind": "operator", "id": "a", "task_id": "a"}]}
    spec_calls = 0

    def answer(call: FakeCall) -> dict[str, Any]:
        nonlocal spec_calls
        if "operators" in (call.json_schema or {}).get("properties", {}):
            return {"operators": [BASH], "notes": ""}
        spec_calls += 1
        return {"explanation": "bad", "spec": bad}

    result = _run(services, FakeLLM([answer] * 10), kind="dag_spec", description="x")
    assert not result.ok
    # one draft + max_repairs (1) repair = 2 generation rounds, not 3
    assert spec_calls == 2
    assert result.rounds == 2
