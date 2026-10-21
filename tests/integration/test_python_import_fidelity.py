"""Python import / code mode fidelity: what the code says comes back, nothing more."""

from __future__ import annotations

from typing import Any

import pytest

from autoair._compat import sdk_symbol_module
from autoair.codegen import generate_source
from autoair.importer.code_sync import sync_code
from autoair.importer.python_dag import import_python_source
from autoair.spec import DagSpec

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


def _import(source: str) -> dict[str, Any]:
    result = import_python_source(source)
    assert not result.import_errors, result.import_errors
    return {d.dag_id: d for d in result.dags}


def _node(spec: DagSpec, task_id: str) -> Any:
    return next(n for n in spec.nodes if getattr(n, "task_id", None) == task_id)


# --------------------------------------------------------------------- code mode apply


def _spec_with_scripts_and_doc_notes() -> DagSpec:
    return DagSpec.model_validate(
        {
            "dag_id": "fidelity",
            "doc_md": "Base doc",
            "nodes": [
                {
                    "id": "n1",
                    "kind": "operator",
                    "task_id": "run",
                    "operator": BASH,
                    "params": {
                        "bash_command": {
                            "$type": "file",
                            "name": "run.sh",
                            "content": "echo hi\n",
                            "run_with": "bash",
                        },
                        "env": {"QUERY": {"$type": "file", "name": "q.sql", "content": "select 1"}},
                    },
                },
                {"id": "note1", "kind": "note", "text": "Note **one**", "include_in_docs": True},
                {"id": "note2", "kind": "note", "text": "Canvas only"},
            ],
        }
    )


def test_code_mode_apply_keeps_embedded_scripts_and_is_idempotent():
    current = _spec_with_scripts_and_doc_notes()
    for _ in range(3):  # applying the generated code again changes nothing
        result = sync_code(generate_source(current), dag_id="fidelity", current=current)
        assert result.ok, result.problems
        assert result.spec is not None
        node = _node(result.spec, "run")
        assert node.params["bash_command"] == {
            "$type": "file",
            "name": "run.sh",
            "content": "echo hi\n",
            "run_with": "bash",
        }
        assert node.params["env"] == {
            "QUERY": {"$type": "file", "name": "q.sql", "content": "select 1"}
        }
        assert result.spec.doc_md == "Base doc"
        assert result.diff is not None
        assert not result.diff.changed_nodes
        current = result.spec
    assert "Note **one**" in generate_source(current)


def test_a_path_that_only_looks_like_a_script_stays_a_string():
    source = """
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator
with DAG("p"):
    BashOperator(task_id="b", bash_command="bash /tmp/airflow-scripts/p/0123456789abcdef/x.sh ")
"""
    spec = _import(source)["p"].spec
    assert spec is not None
    assert _node(spec, "b").params["bash_command"].startswith("bash /tmp/airflow-scripts/")


# --------------------------------------------------------------------- python import


def test_mapped_operators_import_with_their_mapping():
    source = """
from airflow.sdk import DAG, task
from airflow.providers.standard.operators.bash import BashOperator
with DAG("m", default_args={"retries": 2}):
    p = BashOperator(task_id="p", bash_command="echo")
    m = BashOperator.partial(task_id="m", cwd="/tmp", retries=3).expand(bash_command=["a", "b"])
    k = BashOperator.partial(task_id="k").expand_kwargs(p.output)
    x = BashOperator.partial(task_id="x", map_index_template="{{ i }}").expand(
        bash_command=p.output
    )

    @task
    def f(item, factor=1):
        return item * factor

    f.partial(factor=2).expand(item=[1, 2])
"""
    dag = _import(source)["m"]
    spec = dag.spec
    assert spec is not None
    assert not any("MappedOperator" in str(n.model_dump()) for n in spec.nodes)
    m = _node(spec, "m")
    assert m.operator == BASH
    assert m.params == {"cwd": "/tmp"}
    assert m.retries == 3
    assert m.mapping.expand == {"bash_command": ["a", "b"]}
    p_id = _node(spec, "p").id
    assert _node(spec, "k").mapping.expand_kwargs == {"$type": "xcom", "node": p_id}
    x = _node(spec, "x")
    assert x.mapping.expand == {"bash_command": {"$type": "xcom", "node": p_id}}
    assert x.mapping.map_index_template == "{{ i }}"
    f = _node(spec, "f")
    assert f.kind == "python_task"
    assert f.arguments == {"factor": 2}
    assert f.mapping.expand == {"item": [1, 2]}
    assert "item" not in f.extra_parameters
    assert not [w for w in dag.warnings if "mapping" in w]
    # and the result generates the same mapping again
    code = generate_source(spec)
    assert "BashOperator.partial(task_id='m'" in code
    assert ".expand(bash_command=['a', 'b'])" in code
    assert ".expand_kwargs(" in code


def test_unsupported_mapping_input_is_a_clear_warning():
    source = """
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator
with DAG("z"):
    p = BashOperator(task_id="p", bash_command="echo")
    BashOperator.partial(task_id="z").expand(bash_command=p.output.map(lambda v: v))
"""
    dag = _import(source)["z"]
    assert dag.spec is not None
    assert _node(dag.spec, "z").mapping is None
    assert any("z: mapped" in w and "without mapping" in w for w in dag.warnings), dag.warnings


@pytest.mark.parametrize(
    ("written", "preset"),
    [("@daily", "@daily"), ("@hourly", "@hourly"), ("@annually", "@yearly")],
)
def test_schedule_presets_are_preserved(written, preset):
    source = f"""
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
with DAG("s", schedule={written!r}):
    EmptyOperator(task_id="e")
"""
    spec = _import(source)["s"].spec
    assert spec is not None
    assert spec.schedule.model_dump() == {"type": "preset", "preset": preset}


def test_preset_timetable_in_the_dag_timezone_is_preserved():
    # airflow.sdk exports timetables from 3.2 on; 3.0/3.1 only have airflow.timetables.*.
    timetable_module = sdk_symbol_module("CronTriggerTimetable")
    source = f"""
import pendulum
from airflow.sdk import DAG
from {timetable_module} import CronTriggerTimetable
from airflow.providers.standard.operators.empty import EmptyOperator
start = pendulum.datetime(2024, 1, 1, tz="Europe/Berlin")
with DAG("a", schedule=CronTriggerTimetable("@weekly", timezone="Europe/Berlin"), start_date=start):
    EmptyOperator(task_id="e")
with DAG("b", schedule=CronTriggerTimetable("@weekly", timezone="Asia/Tokyo"), start_date=start):
    EmptyOperator(task_id="e")
with DAG("c", schedule="0 0 * * *"):
    EmptyOperator(task_id="e")
"""
    dags = _import(source)
    assert dags["a"].spec.schedule.model_dump() == {"type": "preset", "preset": "@weekly"}
    other_tz = dags["b"].spec.schedule.model_dump()
    assert (other_tz["type"], other_tz["expression"]) == ("cron", "0 0 * * 0")
    assert other_tz["timezone"] == "Asia/Tokyo"
    assert dags["c"].spec.schedule.model_dump()["expression"] == "0 0 * * *"


def test_operator_defaults_are_not_emitted():
    source = """
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator
with DAG("d", default_args={"env": {"A": "1"}}):
    BashOperator(task_id="plain", bash_command="echo")
    BashOperator(task_id="explicit", bash_command="echo", skip_on_exit_code=[99, 100])
"""
    spec = _import(source)["d"].spec
    assert spec is not None
    assert _node(spec, "plain").params == {"bash_command": "echo"}
    assert _node(spec, "explicit").params == {
        "bash_command": "echo",
        "skip_on_exit_code": [99, 100],
    }
    assert "skip_on_exit_code" not in generate_source(
        DagSpec.model_validate(
            {"dag_id": "d", "nodes": [_node(spec, "plain").model_dump(by_alias=True)]}
        )
    )
