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


# --------------------------------------------------------------------- classic python callables

CLASSIC_CALLABLES = '''from __future__ import annotations

import json
from datetime import datetime
from os.path import join as pjoin

from airflow.sdk import DAG
from airflow.providers.standard.operators.python import (
    BranchPythonOperator,
    PythonOperator,
    ShortCircuitOperator,
)
from airflow.providers.standard.sensors.python import PythonSensor
from airflow.providers.standard.operators.empty import EmptyOperator

PREFIX = "p"


def greet(name, **context):
    """Say hello."""
    # a comment stays
    return json.dumps({"hello": pjoin("a", name)})


def choose():
    return "a"


def always():
    return True


def uses_global():
    return PREFIX


with DAG("classic_callables", start_date=datetime(2024, 1, 1), schedule=None) as dag:
    g = PythonOperator(task_id="greet", python_callable=greet, op_kwargs={"name": "x"})
    b = BranchPythonOperator(task_id="branch", python_callable=choose)
    s = ShortCircuitOperator(task_id="sc", python_callable=always)
    ps = PythonSensor(task_id="ps", python_callable=always)
    ext = PythonOperator(task_id="ext", python_callable=json.dumps, op_args=[1])
    glob = PythonOperator(task_id="glob", python_callable=uses_global)
    lam = PythonOperator(task_id="lam", python_callable=lambda: 1)
    a = EmptyOperator(task_id="a")
    g >> b >> a
'''


def test_classic_python_callables_defined_in_the_file_are_kept():
    dag = _import(CLASSIC_CALLABLES)["classic_callables"]
    assert dag.error is None
    spec = dag.spec
    assert spec is not None
    greet = _node(spec, "greet").params["python_callable"]
    assert greet == {
        "$type": "function",
        "source": "from os.path import join as pjoin\nimport json\n\n\n"
        'def greet(name, **context):\n    """Say hello."""\n    # a comment stays\n'
        '    return json.dumps({"hello": pjoin("a", name)})\n',
    }
    for task_id, fn in (("branch", "choose"), ("sc", "always"), ("ps", "always")):
        value = _node(spec, task_id).params["python_callable"]
        assert value["$type"] == "function"
        assert value["source"].startswith(f"def {fn}():")
    # imported from another module: an import reference, not a copy of its source
    assert _node(spec, "ext").params["python_callable"] == {"$type": "import", "path": "json.dumps"}
    # what cannot be carried over is a warning, never a silent drop
    assert "python_callable" not in _node(spec, "lam").params
    assert any("lam: python_callable is a lambda" in w for w in dag.warnings)
    assert _node(spec, "glob").params["python_callable"]["$type"] == "function"
    assert any("glob: python_callable" in w and "PREFIX" in w for w in dag.warnings)

    # the generated DAG imports and its tasks call the same functions (without the
    # lambda task, which the warning asks to set again)
    spec = spec.model_copy(
        update={"nodes": [n for n in spec.nodes if getattr(n, "task_id", None) != "lam"]}
    )
    source = generate_source(spec)
    namespace: dict[str, Any] = {}
    exec(compile(source, "generated.py", "exec"), namespace)
    generated = namespace["dag"]
    assert generated.get_task("greet").python_callable(name="x") == '{"hello": "a/x"}'
    assert generated.get_task("branch").python_callable() == "a"
    assert generated.get_task("ext").python_callable([1]) == "[1]"

    from autoair.validation.dagbag_check import check_source

    checked = check_source(source, filename="classic_callables.py", use_pool=False)
    assert checked.ok, checked.import_errors

    # importing the generated code and generating again is stable (code mode)
    again = _import(source)["classic_callables"].spec
    assert again is not None
    greet_again = _node(again, "greet").params["python_callable"]["source"]
    assert "def greet(name, **context):" in greet_again
    assert generate_source(again) == source


# --------------------------------------------------------------------- task settings, labels

TASK_SETTINGS = """
import datetime
from airflow.sdk import DAG, task
from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator

try:
    from airflow.sdk import Label
except ImportError:  # Airflow 3.0
    from airflow.utils.edgemodifier import Label


class Helpers:
    def describe(self):
        return self.task_id


class TaggedBash(Helpers, BashOperator):
    def __init__(self, *, tag="x", **kwargs):
        super().__init__(**kwargs)
        self.tag = tag


with DAG(
    "settings",
    start_date=datetime.datetime(2024, 1, 1),
    schedule=None,
    dagrun_timeout=datetime.timedelta(hours=2),
    default_args={"retries": 3, "retry_delay": datetime.timedelta(minutes=5)},
):
    a = BashOperator(
        task_id="a",
        bash_command="echo",
        retries=0,
        retry_delay=datetime.timedelta(seconds=30),
        execution_timeout=datetime.timedelta(minutes=10),
        email=["ops@example.com"],
    )
    b = EmptyOperator(task_id="b")
    c = EmptyOperator(task_id="c")
    inherited = EmptyOperator(task_id="inherited")
    mixed = TaggedBash(task_id="mixed", bash_command="echo hi", env={"A": "1"}, tag="y")

    @task(retry_delay=9, execution_timeout=datetime.timedelta(minutes=1))
    def f():
        return 1

    a >> Label("ok path") >> b
    a >> c
    f()
"""


def test_task_settings_overrides_and_edge_labels_are_imported():
    dag = _import(TASK_SETTINGS)["settings"]
    assert dag.error is None
    spec = dag.spec
    assert spec is not None
    a = _node(spec, "a")
    # retries=0 overrides default_args retries=3 (it equals Airflow's default)
    assert a.retries == 0
    assert a.retry_delay_seconds == 30
    assert a.execution_timeout_seconds == 600
    inherited = _node(spec, "inherited")
    assert inherited.retries is None
    assert inherited.retry_delay_seconds is None
    assert spec.default_args.retries == 3
    assert spec.default_args.retry_delay_seconds == 300
    assert spec.dagrun_timeout_seconds == 7200
    f = _node(spec, "f")
    assert f.retry_delay_seconds == 9
    assert f.execution_timeout_seconds == 60
    # arguments of the operator classes after a mixin are still imported
    assert _node(spec, "mixed").params == {"tag": "y", "bash_command": "echo hi", "env": {"A": "1"}}
    labels = {(e.source, e.target): e.label for e in spec.edges}
    ids = {n.task_id: n.id for n in spec.nodes if hasattr(n, "task_id")}
    assert labels[(ids["a"], ids["b"])] == "ok path"
    assert labels[(ids["a"], ids["c"])] is None
    # what the spec cannot hold is a warning, not a silent drop
    assert "a: argument email is not imported" in dag.warnings

    # an operator class defined in the DAG file cannot be imported by the generated file
    assert any(
        "mixed: operator class TaggedBash is defined in the DAG file" in w for w in dag.warnings
    )

    # generated code imports back to the same settings and labels (code mode apply)
    current = spec.model_copy(
        update={"nodes": [n for n in spec.nodes if getattr(n, "task_id", None) != "mixed"]}
    )
    for _ in range(2):
        result = sync_code(generate_source(current), dag_id="settings", current=current)
        assert result.ok, result.problems
        assert result.spec is not None
        again = result.spec
        assert _node(again, "a").retries == 0
        assert _node(again, "a").retry_delay_seconds == 30
        assert sorted(e.label or "" for e in again.edges) == sorted(
            e.label or "" for e in spec.edges
        )
        current = again


def test_out_of_range_durations_are_a_warning_not_a_broken_spec():
    source = """
import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
with DAG("big", schedule=None, dagrun_timeout=datetime.timedelta.max):
    EmptyOperator(task_id="a", execution_timeout=datetime.timedelta.max)
"""
    dag = _import(source)["big"]
    assert dag.error is None, dag.error
    spec = dag.spec
    assert spec is not None
    # timedelta.max as float seconds rounds past the limit; whole seconds still fit
    assert _node(spec, "a").execution_timeout_seconds == 999_999_999 * 86_400 + 86_399
    assert "timedelta(seconds=86399999999999)" in generate_source(spec)
