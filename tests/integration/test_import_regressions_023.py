"""Python import -> spec -> generated code keeps what the DAG file does (0.2.3 review).

Each test runs the regenerated DAG's task functions, so a silently changed argument, a lost
import or a shadowed name shows up as a wrong result, not only as a different spec.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from autoair.codegen import generate_source
from autoair.importer.python_dag import import_python_source

pytestmark = pytest.mark.airflow


def _import(source: str) -> Any:
    result = import_python_source(source)
    assert not result.import_errors, result.import_errors
    (dag,) = result.dags
    return dag


def _run(source: str, tmp_path: Path, task_id: str) -> Any:
    """Load ``source`` with Airflow's DagBag and call the task's function as Airflow would."""
    from autoair._compat import dagbag_class

    path = tmp_path / "regenerated.py"
    path.write_text(source)
    cls = dagbag_class()
    kwargs: dict[str, Any] = {"dag_folder": str(path)}
    if "include_examples" in inspect.signature(cls.__init__).parameters:
        kwargs["include_examples"] = False
    bag = cls(**kwargs)
    assert not bag.import_errors, bag.import_errors
    (dag,) = bag.dags.values()
    task = dag.get_task(task_id)
    return task.python_callable(*task.op_args, **task.op_kwargs)


def _regenerated(source: str) -> tuple[Any, str]:
    imported = _import(source)
    return imported, generate_source(imported.spec)


TASKFLOW = """
from airflow.sdk import dag, task

@dag(schedule=None)
def calc():
    @task
    def multiply(x=2):
        return x * 2

    @task
    def needs(x):
        return x + 1

    multiply(7)
    needs(41)

calc()
"""


def test_positional_taskflow_arguments_are_kept(tmp_path: Path) -> None:
    """B01: ``multiply(7)`` came back as ``multiply(x=2)`` (14 -> 4), and ``needs(41)`` as
    ``needs()``, which Airflow rejects."""
    imported, source = _regenerated(TASKFLOW)
    nodes = {n.task_id: n for n in imported.spec.nodes}
    assert nodes["multiply"].arguments == {"x": 7}
    assert nodes["needs"].arguments == {"x": 41}
    assert _run(source, tmp_path, "multiply") == 14
    assert _run(source, tmp_path, "needs") == 42


KEYWORD_ONLY = """
from airflow.sdk import dag, task

@dag(schedule=None)
def kw():
    @task
    def transform(*, factor=2):
        return factor * 3

    @task
    def scaled(value, /, *, by=10):
        return value * by

    transform()
    scaled(4)

kw()
"""


def test_keyword_only_parameters_and_defaults_are_kept(tmp_path: Path) -> None:
    """B02: ``def transform(*, factor=2)`` was generated as ``def transform():`` and the
    task failed with NameError."""
    imported, source = _regenerated(KEYWORD_ONLY)
    assert not imported.warnings, imported.warnings
    assert _run(source, tmp_path, "transform") == 6
    assert _run(source, tmp_path, "scaled") == 40


def test_varargs_are_reported_not_dropped_silently() -> None:
    imported = _import(
        """
from airflow.sdk import dag, task

@dag(schedule=None)
def star():
    @task
    def total(*values, **options):
        return sum(values)

    total(1, 2, 3)

star()
"""
    )
    joined = " ".join(imported.warnings)
    assert "*values" in joined
    assert "**options" in joined
    assert "positional argument" in joined


PYTHON_OPERATOR = "airflow.providers.standard.operators.python.PythonOperator"


def _inline(task_id: str, source: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "kind": "operator",
        "task_id": task_id,
        "operator": PYTHON_OPERATOR,
        "params": {"python_callable": {"$type": "function", "source": source}},
    }


def test_inline_functions_with_the_same_alias_keep_their_own_objects(tmp_path: Path) -> None:
    """B06: ``floor as calc`` and ``ceil as calc`` were both hoisted to module level, so both
    tasks used the last one ([1, 1] instead of [1, 2])."""
    from autoair.spec import DagSpec

    spec = DagSpec.model_validate(
        {
            "dag_id": "aliases",
            "nodes": [
                _inline(
                    "down", "from math import floor as calc\n\ndef down():\n    return calc(1.2)\n"
                ),
                _inline("up", "from math import ceil as calc\n\ndef up():\n    return calc(1.2)\n"),
                _inline(
                    "again",
                    "from math import floor as calc\n\ndef again():\n    return calc(2.7)\n",
                ),
            ],
        }
    )
    source = generate_source(spec)
    assert [_run(source, tmp_path, t) for t in ("down", "up", "again")] == [1, 2, 2]


CRON_WINDOW = """
import datetime
from airflow.sdk import DAG
from airflow.timetables.trigger import CronTriggerTimetable
from airflow.providers.standard.operators.empty import EmptyOperator

with DAG(
    "windowed",
    schedule=CronTriggerTimetable(
        "0 3 * * *", timezone="UTC", interval=datetime.timedelta(hours=2), run_immediately=True
    ),
    start_date=datetime.datetime(2026, 1, 1),
):
    EmptyOperator(task_id="a")
"""


def test_cron_trigger_window_and_run_immediately_are_kept(tmp_path: Path) -> None:
    """B08: ``interval=timedelta(hours=2), run_immediately=True`` came back as a 0 s window
    and run_immediately=False, without a warning."""
    imported, source = _regenerated(CRON_WINDOW)
    schedule = imported.spec.schedule
    assert schedule.type == "cron"
    assert schedule.interval_seconds == 7200
    assert schedule.run_immediately is True
    path = tmp_path / "windowed.py"
    path.write_text(source)
    cls = __import__("autoair._compat", fromlist=["dagbag_class"]).dagbag_class()
    kwargs: dict[str, Any] = {"dag_folder": str(path)}
    if "include_examples" in inspect.signature(cls.__init__).parameters:
        kwargs["include_examples"] = False
    (dag,) = cls(**kwargs).dags.values()
    tt = dag.timetable
    window = getattr(tt, "_interval", None) or getattr(tt, "interval", None)
    assert window.total_seconds() == 7200
    assert bool(getattr(tt, "run_immediately", None) or getattr(tt, "_run_immediately", None))


MODULE_IMPORTS = """
import math
from airflow.sdk import dag, task

LIMIT = 3

@dag(schedule=None)
def uses_math():
    @task
    def ceil():
        \"\"\"Rounds up.\"\"\"
        return math.ceil(1.2)

    @task
    def capped():
        return min(LIMIT, 10)

    ceil()
    capped()

uses_math()
"""


def test_taskflow_bodies_keep_the_module_imports_they_use(tmp_path: Path) -> None:
    """B09: ``math.ceil`` in a TaskFlow body lost the file's ``import math`` (NameError),
    and validation of the imported spec failed with F821 on a valid DAG."""
    from autoair.validation import validate_spec

    imported, source = _regenerated(MODULE_IMPORTS)
    assert _run(source, tmp_path, "ceil") == 2
    nodes = {n.task_id: n for n in imported.spec.nodes}
    assert nodes["ceil"].code.startswith('"""Rounds up."""\nimport math\n')
    assert any("LIMIT" in w for w in imported.warnings)  # a module constant is reported
    report = validate_spec(imported.spec, import_check=False)
    assert not [p for p in report.problems if p.node_id == nodes["ceil"].id and "F821" in p.code]
    # Importing the generated file again gives the same task body (no duplicated import).
    again = _import(source)
    assert {n.task_id: n.code for n in again.spec.nodes}["ceil"] == nodes["ceil"].code


@pytest.mark.parametrize("cron_intervals", ["true", "false"])
def test_trigger_semantics_do_not_depend_on_airflow_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cron_intervals: str
) -> None:
    """B04: ``data_interval=False`` generated a bare cron string / timedelta, so Airflow's
    create_cron_data_intervals / create_delta_data_intervals chose the semantics."""
    from autoair.spec import DagSpec

    monkeypatch.setenv("AIRFLOW__SCHEDULER__CREATE_CRON_DATA_INTERVALS", cron_intervals)
    monkeypatch.setenv("AIRFLOW__SCHEDULER__CREATE_DELTA_DATA_INTERVALS", cron_intervals)
    empty = "airflow.providers.standard.operators.empty.EmptyOperator"
    for schedule, expected in (
        ({"type": "cron", "expression": "0 3 * * *"}, "CronTriggerTimetable"),
        ({"type": "interval", "seconds": 3600}, "DeltaTriggerTimetable"),
        (
            {"type": "cron", "expression": "0 3 * * *", "data_interval": True},
            "CronDataIntervalTimetable",
        ),
    ):
        spec = DagSpec.model_validate(
            {
                "dag_id": "sem",
                "schedule": schedule,
                "nodes": [{"kind": "operator", "id": "a", "task_id": "a", "operator": empty}],
            }
        )
        path = tmp_path / "sem.py"
        path.write_text(generate_source(spec))
        cls = __import__("autoair._compat", fromlist=["dagbag_class"]).dagbag_class()
        kwargs: dict[str, Any] = {"dag_folder": str(path)}
        if "include_examples" in inspect.signature(cls.__init__).parameters:
            kwargs["include_examples"] = False
        (dag,) = cls(**kwargs).dags.values()
        assert type(dag.timetable).__name__ == expected, (schedule, cron_intervals)
