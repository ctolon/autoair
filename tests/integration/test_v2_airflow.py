"""Spec v2 features, templates, YAML and Python import against a real Airflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoair.catalog import OperatorCatalog
from autoair.codegen import render
from autoair.codegen.yaml_dag import export_yaml_dag, import_yaml_dag, validate_yaml_text
from autoair.importer.python_dag import import_python_source
from autoair.spec import DagSpec
from autoair.templates import TemplateInput, TemplateStore
from autoair.validation import validate_spec
from autoair.validation.dagbag_check import check_source

pytestmark = pytest.mark.airflow
BASH = "airflow.providers.standard.operators.bash.BashOperator"


@pytest.fixture(scope="module")
def catalog() -> OperatorCatalog:
    return OperatorCatalog()


def test_builtin_templates_are_valid_and_import(catalog):
    store = TemplateStore("memory://autoair-tests/templates")
    for summary in store.list():
        tpl = store.get(summary.id)
        assert tpl is not None
        if tpl.spec is None:
            assert tpl.fragment is not None
            continue
        if not tpl.available:  # needs a newer Airflow than installed
            continue
        report = validate_spec(tpl.spec, catalog=catalog, import_check=True)
        assert report.ok, (summary.id, report.problems)


def test_user_templates_roundtrip():
    store = TemplateStore("memory://autoair-tests/user")
    spec = DagSpec.model_validate({"dag_id": "mine"})
    store.save(TemplateInput(id="mine", title="Mine", kind="dag", spec=spec))
    assert store.get("mine").spec.dag_id == "mine"
    assert any(t.id == "mine" and not t.builtin for t in store.list())
    with pytest.raises(Exception, match="built-in"):
        store.save(TemplateInput(id="etl-basic", title="x", kind="dag", spec=spec))
    assert store.delete("mine")


def test_yaml_roundtrip_and_schema():
    spec = DagSpec.model_validate(
        {
            "dag_id": "y",
            "schedule": {"type": "cron", "expression": "0 6 * * *", "timezone": "Europe/Istanbul"},
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "operator": BASH,
                    "params": {"bash_command": "echo {{ ds }}"},
                    "retries": 2,
                },
                {
                    "kind": "operator",
                    "id": "b",
                    "task_id": "b",
                    "operator": BASH,
                    "params": {
                        "bash_command": "echo",
                        "env": {"X": {"$type": "xcom", "node": "a"}},
                    },
                },
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
    )
    export = export_yaml_dag(spec)
    assert export.lossless
    assert validate_yaml_text(export.yaml) == []
    back, issues = import_yaml_dag(export.yaml)
    assert issues == []
    by_task = {n.task_id: n for n in back.task_nodes()}
    assert by_task["a"].params["bash_command"] == "echo {{ ds }}"
    assert by_task["b"].params["env"]["X"]["$type"] == "xcom"
    assert back.schedule.timezone == "Europe/Istanbul"


def test_yaml_reports_unsupported():
    spec = DagSpec.model_validate(
        {
            "dag_id": "y",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "p",
                    "task_id": "p",
                    "function_name": "p",
                    "code": "pass",
                }
            ],
        }
    )
    assert not export_yaml_dag(spec).lossless


def test_python_import_roundtrip(tmp_path: Path):
    source = """
import datetime
from airflow.sdk import DAG, task, TaskGroup, Param
from airflow.providers.standard.operators.bash import BashOperator

with DAG("legacy", schedule="0 5 * * *", start_date=datetime.datetime(2026, 1, 1),
         params={"n": Param(2, type="integer")}) as dag:
    with TaskGroup("g"):
        a = BashOperator(task_id="a", bash_command="echo {{ params.n }}", retries=3)

        @task
        def b(x: int = 1):
            return x * 2

        bb = b()
    a >> bb
"""
    result = import_python_source(source, "legacy.py")
    assert result.import_errors == []
    dag = result.dags[0]
    assert dag.spec is not None, dag.error
    regenerated = render(dag.spec)
    check = check_source(regenerated, "legacy2.py")
    assert check.ok, check.import_errors
    assert sorted(check.task_ids["legacy"]) == ["g.a", "g.b"]


def _requires_newer(spec: DagSpec) -> bool:
    from autoair._compat import airflow_version
    from autoair.codegen.features import required_features

    installed = airflow_version() or (0, 0, 0)
    return any(f.min_version > installed for f in required_features(spec))


def test_v2_golden_runs_every_feature(catalog):
    path = Path(__file__).parent.parent / "golden" / "specs" / "v2_features.autoair.json"
    spec = DagSpec.model_validate(json.loads(path.read_text()))
    if _requires_newer(spec):
        report = validate_spec(spec, catalog=catalog)
        assert any(p.code == "airflow_version" for p in report.problems)
        pytest.skip("golden spec uses features newer than the installed Airflow")
    report = validate_spec(spec, catalog=catalog, import_check=True)
    assert report.ok, report.problems


def _branch_external_spec(**kwargs: object) -> DagSpec:
    import sys

    return DagSpec.model_validate(
        {
            "dag_id": "branch_ext",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "pick",
                    "task_id": "pick",
                    "function_name": "pick",
                    "decorator": "branch_external_python",
                    "decorator_kwargs": {"python": sys.executable, **kwargs},
                    "code": 'return "left"',
                },
                *(
                    {
                        "kind": "operator",
                        "id": t,
                        "task_id": t,
                        "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                    }
                    for t in ("left", "right")
                ),
            ],
            "edges": [{"source": "pick", "target": "left"}, {"source": "pick", "target": "right"}],
        }
    )


def test_branch_external_python_generates_imports_and_roundtrips(catalog):
    spec = _branch_external_spec()
    source = render(spec)
    assert "@task.branch_external_python(" in source
    assert "python=" in source
    report = validate_spec(spec, catalog=catalog, import_check=True)
    assert report.ok, report.problems
    check = check_source(source, "branch_ext.py")
    assert check.ok, check.import_errors
    assert sorted(check.task_ids["branch_ext"]) == ["left", "pick", "right"]

    back = import_python_source(source, "branch_ext.py").dags[0].spec
    assert back is not None
    pick = next(n for n in back.task_nodes() if n.task_id == "pick")
    assert pick.decorator == "branch_external_python"


def test_branch_external_python_validation():
    spec = _branch_external_spec()
    no_python = spec.model_copy(deep=True)
    no_python.nodes[0].decorator_kwargs.pop("python")
    codes = {(p.code, p.severity) for p in validate_spec(no_python).problems}
    assert ("decorator", "error") in codes  # needs the interpreter path
    unknown = spec.model_copy(deep=True)
    unknown.nodes[0].code = 'return "nowhere"'
    assert any(p.code == "branch" for p in validate_spec(unknown).problems)


@pytest.mark.parametrize(
    ("schedule", "expected_hour_utc"),
    [
        ({"type": "preset", "preset": "@daily"}, 21),  # Istanbul midnight = 21:00 UTC
        ({"type": "cron", "expression": "0 6 * * *"}, 3),  # 06:00 Istanbul
        ({"type": "cron", "expression": "0 6 * * *", "timezone": "UTC"}, 6),  # own tz wins
    ],
)
def test_dag_timezone(schedule, expected_hour_utc):
    import datetime

    spec = DagSpec.model_validate(
        {
            "dag_id": "tz_dag",
            "timezone": "Europe/Istanbul",
            "schedule": schedule,
            "start_date": "2026-03-01T00:00:00",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                }
            ],
        }
    )
    source = render(spec)
    assert 'pendulum.datetime(2026, 3, 1, tz="Europe/Istanbul")' in source
    check = check_source(source, "tz_dag.py")
    assert check.ok, check.import_errors
    from autoair.loader import build_dag

    dag = build_dag(spec)
    assert dag.timezone.name == "Europe/Istanbul"
    assert dag.start_date == datetime.datetime(2026, 2, 28, 21, tzinfo=datetime.timezone.utc)
    from autoair.api.routers.airflow import _next_runs

    preview = _next_runs(spec.model_copy(update={"catchup": True}), 2)
    assert preview.error is None, preview.error
    first = datetime.datetime.fromisoformat(preview.runs[0])
    assert first.astimezone(datetime.timezone.utc).hour == expected_hour_utc

    back = import_python_source(source, "tz_dag.py").dags[0].spec
    assert back is not None
    assert back.timezone == "Europe/Istanbul"
    assert back.start_date.replace(tzinfo=None) == datetime.datetime(2026, 3, 1)


def test_invalid_dag_timezone():
    with pytest.raises(ValueError, match="unknown timezone"):
        DagSpec.model_validate({"dag_id": "x", "timezone": "Mars/Olympus"})
