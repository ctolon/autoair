"""Specialised writers: environment-aware prompts and validate-and-repair loops."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from autoair.ai.events import StatusEvent
from autoair.ai.llm import LLMError
from autoair.ai.testing import FakeCall, FakeLLM
from autoair.ai.writers import (
    WriteRequest,
    environment_profile,
    import_issues,
    run_writer,
    sql_issues,
)
from autoair.api.app import build_services
from autoair.auth import AuthBackend
from autoair.lsp.shell import shellcheck_path
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


@pytest.fixture(scope="module")
def services(tmp_path_factory: pytest.TempPathFactory) -> Any:
    tmp = tmp_path_factory.mktemp("writers")
    return build_services(Settings(dags_folder=tmp, subfolder=""), AuthBackend())


def run(services: Any, llm: FakeLLM, **kw: Any) -> Any:
    events: list[Any] = []
    request = WriteRequest(research=False, **kw)
    result = asyncio.run(run_writer(request, llm=llm, services=services, emit=events.append))
    return result, events


def code(text: str, **extra: Any) -> dict[str, Any]:
    return {"code": text, "explanation": "e", "requirements": [], "connections": [], **extra}


def test_environment_profile(services: Any) -> None:
    profile = environment_profile(services)
    assert profile.airflow_version
    assert profile.airflow_version.startswith("3")
    assert any(p["name"] == "apache-airflow-providers-standard" for p in profile.providers)
    assert "airflow" in profile.packages
    assert profile.ruff is True
    block = profile.prompt_block()
    assert "Apache Airflow 3" in block
    assert "apache-airflow-providers-standard" in block


def test_task_body_repairs_syntax_and_missing_imports(services: Any) -> None:
    llm = FakeLLM(
        [
            code("return (1"),
            code("import not_a_real_module_xyz\nreturn not_a_real_module_xyz.run()"),
            code(
                "from airflow.sdk import get_current_context\n"
                "ctx = get_current_context()\nreturn str(ctx['ds'])"
            ),
        ]
    )
    result, events = run(services, llm, kind="task_body", description="return the ds")
    assert result.ok
    assert result.rounds == 3
    assert {"syntax", "ruff", "imports"} <= set(result.checks)
    second = llm.calls[1].messages[-1]["content"]
    assert "[syntax" in second
    third = llm.calls[2].messages[-1]["content"]
    assert "not installed" in third
    first_prompt = llm.calls[0].messages[0]["content"]
    assert "Target environment" in first_prompt
    assert "Apache Airflow 3" in first_prompt
    assert [e.stage for e in events if isinstance(e, StatusEvent)][:2] == [
        "environment",
        "write",
    ]


def test_task_body_secret_is_not_ok(services: Any) -> None:
    secret = "sk-ant-" + "Z" * 40
    llm = FakeLLM([code(f"api_key = '{secret}'\nreturn 1")] * 3)
    result, _ = run(services, llm, kind="task_body", description="call the api")
    assert result.ok is False
    assert any(f.category == "secret" for f in result.findings)


@pytest.mark.skipif(shellcheck_path() is None, reason="ShellCheck not installed")
def test_bash_writer_uses_shellcheck(services: Any) -> None:
    llm = FakeLLM(
        [
            code("for f in $(ls *.csv); do\n  rm $f\ndone"),
            code('set -euo pipefail\nfor f in ./*.csv; do\n  rm -- "$f"\ndone'),
        ]
    )
    result, _ = run(services, llm, kind="bash", description="delete csv files")
    assert "shellcheck" in result.checks
    assert result.rounds == 2
    assert result.ok
    assert "SC2" in llm.calls[1].messages[-1]["content"]


def test_sql_writer_blocks_unbounded_delete(services: Any) -> None:
    llm = FakeLLM(
        [
            code("DELETE FROM sales;"),
            code(
                "DELETE FROM sales WHERE ds = '{{ ds }}';\n"
                "INSERT INTO sales SELECT * FROM staging WHERE ds = '{{ ds }}';"
            ),
        ]
    )
    result, _ = run(services, llm, kind="sql", description="reload a day", dialect="postgres")
    assert result.ok
    assert result.rounds == 2
    assert "without WHERE" in llm.calls[1].messages[-1]["content"]
    assert sql_issues("SELECT (1", None)[1][0].message == "unbalanced parentheses"


def test_operator_config_validated_against_catalog(services: Any) -> None:
    def answer(params: dict[str, Any], op: str = BASH) -> dict[str, Any]:
        return {
            "operator": op,
            "task_id": "say hi",
            "params_json": json.dumps(params),
            "explanation": "e",
            "connections": [],
        }

    llm = FakeLLM(
        [
            answer({}, "acme.NotInstalledOperator"),
            answer({"bash_cmd": "echo hi"}),
            answer({"bash_command": "echo hi"}),
        ]
    )
    result, _ = run(services, llm, kind="operator_config", description="run echo hi in bash")
    assert result.ok
    assert result.operator == BASH
    assert result.params == {"bash_command": "echo hi"}
    assert json.loads(result.code)["task_id"] == "say_hi"
    prompt = llm.calls[0].messages[0]["content"]
    assert "Candidate operators (installed)" in prompt
    assert BASH in prompt
    assert "not in the installed catalog" in llm.calls[1].messages[-1]["content"]


def test_custom_operator_structure(services: Any) -> None:
    bad = "from airflow.sdk import BaseOperator\n\nclass Hello(BaseOperator):\n    pass\n"
    good = (
        "from airflow.sdk import BaseOperator\n\n\nclass Hello(BaseOperator):\n"
        '    """Say hello."""\n\n    template_fields = ("name",)\n\n'
        "    def __init__(self, *, name: str, **kwargs):\n"
        "        super().__init__(**kwargs)\n        self.name = name\n\n"
        "    def execute(self, context):\n        return f'hello {self.name}'\n"
    )
    llm = FakeLLM([code(bad, class_name="Hello"), code(good, class_name="Hello")])
    result, _ = run(services, llm, kind="custom_operator", description="hello operator")
    assert result.ok
    assert result.operator == "Hello"
    assert "execute(self, context)" in llm.calls[1].messages[-1]["content"]


def test_inline_function_name_and_python_disabled(services: Any, tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            code("def other():\n    return 1\n", function_name="other"),
            code("def notify(context):\n    return None\n", function_name="notify"),
        ]
    )
    result, _ = run(
        services, llm, kind="inline_function", description="callback", function_name="notify"
    )
    assert result.ok
    assert result.function_name == "notify"
    locked = build_services(
        Settings(dags_folder=tmp_path, subfolder="", allow_python_code=False), AuthBackend()
    )
    with pytest.raises(LLMError, match="disabled"):
        run(locked, FakeLLM(), kind="task_body", description="x")


def test_dag_spec_writer_uses_generator(services: Any) -> None:
    spec = {
        "dag_id": "w",
        "nodes": [
            {
                "kind": "operator",
                "id": "a",
                "task_id": "a",
                "operator": BASH,
                "params": {"bash_command": "echo"},
            }
        ],
    }

    def answer(call: FakeCall) -> dict[str, Any]:
        if "operators" in (call.json_schema or {}).get("properties", {}):
            return {"operators": [BASH], "notes": ""}
        return {"explanation": "made", "spec": spec}

    result, _ = run(services, FakeLLM([answer] * 3), kind="dag_spec", description="x")
    assert result.ok
    assert result.spec is not None
    assert result.spec.dag_id == "w"


def test_import_issues_without_profile() -> None:
    assert import_issues("import os\nimport json") == []
    assert import_issues("from zz_missing_pkg import x")[0].source == "imports"


def test_import_issues_check_airflow_providers() -> None:
    ok = "from airflow.providers.standard.operators.bash import BashOperator"
    assert import_issues(ok) == []
    missing = import_issues("from airflow.providers.zz_not_installed.hooks.x import Hook")
    assert missing[0].message.startswith("provider package")
