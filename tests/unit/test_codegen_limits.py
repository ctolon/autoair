"""Generated code never shadows its own imports, keeps sub-second dates and refuses durations
a ``datetime.timedelta`` cannot hold (with a validation problem instead of broken code)."""

from __future__ import annotations

import ast
import math
from typing import Any

import pytest

from autoair.codegen import CodegenError, generate_source
from autoair.spec import DagSpec
from autoair.validation import validate_spec

BASH = "airflow.providers.standard.operators.bash.BashOperator"
PYOP = "airflow.providers.standard.operators.python.PythonOperator"


def _shadowing_spec(name: str) -> dict[str, Any]:
    return {
        "dag_id": "names",
        "timezone": "Europe/Istanbul",
        "start_date": "2024-01-01T00:00:00",
        "nodes": [
            {
                "id": "p",
                "kind": "python_task",
                "task_id": "p",
                "function_name": name,
                "code": "return 1",
            },
            {
                "id": "b",
                "kind": "operator",
                "task_id": "b",
                "operator": BASH,
                "params": {
                    "bash_command": {
                        "$type": "file",
                        "name": "a.sh",
                        "content": "echo",
                        "run_with": "bash",
                    }
                },
                "retry_delay_seconds": 5,
            },
            {
                "id": "c",
                "kind": "operator",
                "task_id": "c",
                "operator": PYOP,
                "params": {
                    "python_callable": {
                        "$type": "function",
                        "source": "import json\n\n\ndef cb():\n    return json.dumps(1)\n",
                    }
                },
            },
        ],
    }


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in stmt.names}
    return names


@pytest.mark.parametrize(
    "name", ["os", "hashlib", "tempfile", "pendulum", "datetime", "json", "DAG", "task", "dag"]
)
def test_task_function_names_never_shadow_imports(name):
    spec = DagSpec.model_validate(_shadowing_spec(name))
    tree = ast.parse(generate_source(spec))
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assigned = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    imported = _imported_names(tree)
    assert not (functions | assigned) & imported - {"dag"}
    # the task keeps its id: only the Python name changes
    assert "task_id='p'" in ast.unparse(tree)


@pytest.mark.airflow
@pytest.mark.parametrize("name", ["os", "hashlib", "json"])
def test_shadowing_names_generate_a_dag_that_parses_and_runs(name, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = generate_source(DagSpec.model_validate(_shadowing_spec(name)))
    namespace: dict[str, Any] = {}
    exec(compile(source, "names.py", "exec"), namespace)
    assert namespace["dag"].get_task("c").python_callable() == "1"


def test_function_name_of_an_import_is_a_warning():
    spec = DagSpec.model_validate(_shadowing_spec("json"))
    problems = [p for p in validate_spec(spec, import_check=False).problems]
    reserved = [p for p in problems if p.code == "function_name_reserved"]
    assert [(p.severity, p.node_id, p.field) for p in reserved] == [
        ("warning", "p", "function_name")
    ]
    ok = DagSpec.model_validate(_shadowing_spec("compute"))
    assert not [
        p
        for p in validate_spec(ok, import_check=False).problems
        if p.code == "function_name_reserved"
    ]


def test_aware_dates_keep_microseconds():
    for tz, expected in (
        (
            "Europe/Istanbul",
            "pendulum.datetime(2024, 1, 1, 13, 20, 30, 123456, tz='Europe/Istanbul')",
        ),
        (None, "datetime.datetime(2024, 1, 1, 10, 20, 30, 123456, tzinfo=datetime.timezone.utc)"),
    ):
        spec = DagSpec.model_validate(
            {"dag_id": "d", "timezone": tz, "start_date": "2024-01-01T10:20:30.123456+00:00"}
        )
        assert expected in generate_source(spec)
    wall = DagSpec.model_validate(
        {"dag_id": "d", "timezone": "UTC", "start_date": "2024-01-01T00:00:00.5"}
    )
    assert "pendulum.datetime(2024, 1, 1, 0, 0, 0, 500000, tz='UTC')" in generate_source(wall)


def _duration_spec(where: str, value: float) -> dict[str, Any]:
    data: dict[str, Any] = {
        "dag_id": "d",
        "nodes": [
            {
                "id": "n",
                "kind": "operator",
                "task_id": "a",
                "operator": BASH,
                "params": {"bash_command": "echo"},
            }
        ],
    }
    node = data["nodes"][0]
    if where == "node":
        node["retry_delay_seconds"] = value
    elif where == "param":
        node["operator"] = "airflow.providers.standard.sensors.external_task.ExternalTaskSensor"
        node["params"] = {"external_dag_id": "up", "execution_delta": value}
    elif where == "tagged":
        node["params"]["env"] = {"x": {"$type": "timedelta", "seconds": value}}
    elif where == "default_args":
        data["default_args"] = {"retry_delay_seconds": value}
    elif where == "dagrun":
        data["dagrun_timeout_seconds"] = value
    return data


@pytest.mark.parametrize("where", ["node", "param", "tagged", "default_args", "dagrun"])
@pytest.mark.parametrize("value", [math.inf, 1e20, -math.inf, math.nan])
def test_durations_a_timedelta_cannot_hold_are_problems_not_code(where, value):
    try:
        spec = DagSpec.model_validate(_duration_spec(where, value))
    except ValueError:
        return  # rejected by the field's bounds (ge=0 / gt=0) already
    problems = [p for p in validate_spec(spec, import_check=False).problems if p.code == "duration"]
    assert len(problems) == 1, problems
    assert problems[0].severity == "error"
    assert problems[0].node_id == (None if where in ("default_args", "dagrun") else "n")
    with pytest.raises(CodegenError, match="duration"):
        generate_source(spec)


def test_the_largest_timedelta_is_still_generated():
    seconds = 999_999_999 * 86_400 + 86_399
    spec = DagSpec.model_validate(_duration_spec("node", seconds))
    assert not [p for p in validate_spec(spec, import_check=False).problems if p.code == "duration"]
    assert f"timedelta(seconds={seconds})" in generate_source(spec)


@pytest.mark.parametrize(
    ("version", "expected"), [((3, 0, 6), "True"), ((3, 1, 8), "True"), ((3, 2, 0), "2.0")]
)
def test_retry_exponential_backoff_follows_the_target_version(version, expected):
    # Airflow < 3.2 types the argument as a bool (base-2 backoff), 3.2+ as the multiplier.
    spec = DagSpec.model_validate(_duration_spec("node", 10))
    spec.nodes[0].retry_exponential_backoff = 2.0
    source = generate_source(spec, airflow_version=version)
    assert f"retry_exponential_backoff={expected}" in source


@pytest.mark.parametrize(
    ("version", "value", "warned"),
    [
        ((3, 1, 8), 1.5, True),  # silently becomes True (base 2)
        ((3, 0, 6), 3.0, True),
        ((3, 1, 8), 1.0, True),
        ((3, 1, 8), 2.0, False),  # what True means anyway
        ((3, 1, 8), 0.0, False),  # off stays off
        ((3, 2, 0), 1.5, False),  # a real multiplier since 3.2
        ((3, 3, 0), 3.0, False),
    ],
)
def test_backoff_multiplier_before_3_2_is_a_warning(version, value, warned):
    spec = DagSpec.model_validate(_duration_spec("node", 10))
    spec.nodes[0].retry_exponential_backoff = value
    report = validate_spec(spec, import_check=False, target_version=version)
    found = [p for p in report.problems if p.field == "retry_exponential_backoff"]
    if not warned:
        assert found == []
        return
    assert len(found) == 1
    problem = found[0]
    assert (problem.severity, problem.code, problem.node_id) == (
        "warning",
        "airflow_version",
        spec.nodes[0].id,
    )
    assert "3.2" in problem.message
    assert report.ok  # a warning: the deploy is not refused
