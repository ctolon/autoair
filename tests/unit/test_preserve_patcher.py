"""LibCST mapper and patcher on realistic hand-written DAG files (no Airflow needed).

The patcher gets the generated code of the spec the file stands for (``old``) and of the
intended spec (``new``); these tests build both with the real code generator from specs
that describe the hand-written files, and check that only the intended lines change.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from autoair.codegen import render
from autoair.preserve.mapper import MapError, map_source
from autoair.preserve.patcher import PatchResult, patch_source, unified_diff
from autoair.spec import DagSpec

BASH = "airflow.providers.standard.operators.bash.BashOperator"
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"

CLASSIC = '''"""ETL with classic operators."""

from __future__ import annotations

import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

CMD = "echo extract"  # shared command

with DAG(
    dag_id="classic",
    schedule="@daily",   # every day
    start_date=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    catchup=False,
) as dag:
    # the start marker
    start = EmptyOperator(task_id="start")
    extract = BashOperator(
        task_id="extract",
        bash_command=CMD,
        retries=2,  # flaky source
    )
    load = BashOperator(task_id="load", bash_command="echo load")
    report = BashOperator(task_id="report",bash_command='echo report')

    start >> extract >> load >> report
'''


def op(task_id: str, operator: str = BASH, **params: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"kind": "operator", "id": f"n_{task_id}", "task_id": task_id}
    node["operator"] = operator
    common = {k: params.pop(k) for k in list(params) if k in ("retries", "doc_md", "pool")}
    node["params"] = params
    node.update(common)
    return node


def edge(a: str, b: str, label: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"source": f"n_{a}", "target": f"n_{b}"}
    if label:
        out["label"] = label
    return out


CLASSIC_SPEC: dict[str, Any] = {
    "dag_id": "classic",
    "schedule": {"type": "preset", "preset": "@daily"},
    "start_date": "2026-01-01T00:00:00+00:00",
    "catchup": False,
    "nodes": [
        op("start", EMPTY),
        op("extract", bash_command="echo extract", retries=2),
        op("load", bash_command="echo load"),
        op("report", bash_command="echo report"),
    ],
    "edges": [edge("start", "extract"), edge("extract", "load"), edge("load", "report")],
}


def gen(data: dict[str, Any]) -> str:
    return render(DagSpec.model_validate(data), airflow_version=(3, 3, 2))


def patch(
    base: str,
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    strict: bool = False,
    renames: dict[str, str] | None = None,
) -> PatchResult:
    return patch_source(
        base, gen(old), gen(new), dag_id=old["dag_id"], strict=strict, renames=renames
    )


def node(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    return next(n for n in data["nodes"] if n.get("task_id") == task_id)


def changed_lines(before: str, after: str) -> list[str]:
    diff = unified_diff(before, after, "dag.py").splitlines()
    return [ln for ln in diff[2:] if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


# --------------------------------------------------------------------------- mapper


def test_map_classic_operators_and_dependencies() -> None:
    m = map_source(CLASSIC, "classic")
    assert m.dag.style == "with"
    assert set(m.tasks) == {"start", "extract", "load", "report"}
    assert m.tasks["extract"].var == "extract"
    assert all(t.statement is not None and not t.limited for t in m.tasks.values())
    [dep] = m.deps
    assert dep.edges == {("start", "extract"), ("extract", "load"), ("load", "report")}


TASKFLOW = '''import pendulum
from airflow.sdk import Label, TaskGroup, dag, task


@task(retries=1)
def extract():
    """Pull rows."""
    return [1, 2, 3]  # rows


@dag(schedule=None, start_date=pendulum.datetime(2026, 1, 1, tz="UTC"), catchup=False)
def taskflow_dag():
    rows = extract()

    @task
    def transform(values):
        # double every value
        return [v * 2 for v in values]

    with TaskGroup("publish", tooltip="ship it") as publish:
        @task(task_id="push")
        def push_rows(values):
            print(values)

        pushed = push_rows(transform(rows))

    for i in range(2):
        task(lambda: i, task_id=f"loop_{i}")()

    rows >> Label("raw") >> publish


taskflow_dag()
'''


def test_map_taskflow_groups_loops_and_labels() -> None:
    m = map_source(TASKFLOW)
    assert m.dag.style == "decorator"
    assert m.dag.dag_id == "taskflow_dag"
    assert {"extract", "publish.transform", "publish.push"} <= set(m.tasks)
    push = m.tasks["publish.push"]
    assert push.kind == "taskflow"
    assert push.group == "publish"
    assert push.var == "pushed"
    # transform(rows) is called inline: mapped (kwargs can change) but not on its own line
    assert m.tasks["publish.transform"].statement is None
    assert m.groups["publish"].with_stmt is not None
    assert ("extract", "publish.transform") in m.implicit_edges
    [dep] = m.deps
    assert dep.labels == {("extract", "@publish"): "raw"}
    assert any("computed task_id" in note or "loop" in note for note in m.unmapped) or not any(
        k.startswith("loop") for k in m.tasks
    )


def test_map_refuses_files_without_the_dag() -> None:
    with pytest.raises(MapError, match="does not define"):
        map_source(CLASSIC, "other")
    with pytest.raises(MapError, match="cannot be parsed"):
        map_source("with DAG(:\n")


def test_map_marks_duplicate_and_factory_tasks_limited() -> None:
    source = """from airflow.sdk import DAG, task
from airflow.providers.standard.operators.bash import BashOperator


def make(name):
    return BashOperator(task_id=name, bash_command="x")


@task
def twice():
    return 1


with DAG("dupes", schedule=None) as dag:
    a = twice()
    b = twice()
    c = make("c")
"""
    m = map_source(source, "dupes")
    assert "c" not in m.tasks  # a factory: no literal task_id at the call site
    assert m.tasks["twice"].limited  # the function is called twice


# --------------------------------------------------------------------------- kwargs


def test_changing_one_argument_changes_one_line() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    node(new, "load")["params"]["bash_command"] = "echo load --full"
    result = patch(CLASSIC, CLASSIC_SPEC, new)
    assert result.ok, result.refusals
    assert changed_lines(CLASSIC, result.source or "") == [
        '-    load = BashOperator(task_id="load", bash_command="echo load")',
        '+    load = BashOperator(task_id="load", bash_command="echo load --full")',
    ]


def test_adding_and_removing_arguments_keeps_layout_and_comments() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    del node(new, "extract")["retries"]
    node(new, "report")["retries"] = 4
    result = patch(CLASSIC, CLASSIC_SPEC, new)
    assert result.ok, result.refusals
    out = result.source or ""
    assert "retries=2" not in out
    # the closing layout of the call is kept; the comment of the removed line goes with it
    assert "        bash_command=CMD,\n    )\n" in out
    assert "report = BashOperator(task_id=\"report\",bash_command='echo report',retries=4)" in out
    for kept in ("# shared command", "# every day", "# the start marker", '"""ETL with'):
        assert kept in out


def test_appending_to_a_multiline_call_keeps_the_trailing_comment() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    node(new, "extract")["doc_md"] = "Extract rows"
    result = patch(CLASSIC, CLASSIC_SPEC, new)
    assert result.ok, result.refusals
    assert ('        retries=2,  # flaky source\n        doc_md="Extract rows",\n    )\n') in (
        result.source or ""
    )


def test_dag_arguments_and_needed_imports() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    new["schedule"] = {"type": "preset", "preset": "@hourly"}
    new["dagrun_timeout_seconds"] = 600
    new["tags"] = ["etl"]
    result = patch(CLASSIC, CLASSIC_SPEC, new)
    assert result.ok, result.refusals
    out = result.source or ""
    assert '    schedule="@hourly",   # every day\n' in out
    assert "tags=[" in out
    assert "dagrun_timeout=datetime.timedelta(seconds=600" in out
    assert out.count("import datetime") == 1  # already imported: not added again


def test_replacing_a_variable_value_is_risky() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    node(new, "extract")["params"]["bash_command"] = "echo other"
    loose = patch(CLASSIC, CLASSIC_SPEC, new)
    assert loose.ok
    assert any("refers to a variable" in w for w in loose.warnings)
    assert 'bash_command="echo other",' in (loose.source or "")
    strict = patch(CLASSIC, CLASSIC_SPEC, new, strict=True)
    assert not strict.ok
    assert strict.refusals[0].code == "risky"


def test_removing_an_argument_that_is_not_written_is_refused() -> None:
    old = copy.deepcopy(CLASSIC_SPEC)
    node(old, "load")["retries"] = 3  # e.g. from default_args: not in the file
    result = patch(CLASSIC, old, CLASSIC_SPEC)
    assert not result.ok
    assert "cannot be removed" in result.refusals[0].message


def test_dag_id_rename_is_refused() -> None:
    new = {**copy.deepcopy(CLASSIC_SPEC), "dag_id": "renamed"}
    result = patch_source(CLASSIC, gen(CLASSIC_SPEC), gen(new), dag_id="classic")
    assert not result.ok


# --------------------------------------------------------------------------- tasks and edges


def test_adding_a_task_and_its_dependency() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    new["nodes"].append(op("notify", bash_command="echo done"))
    new["edges"].append(edge("report", "notify"))
    result = patch(CLASSIC, CLASSIC_SPEC, new)
    assert result.ok, result.refusals
    out = result.source or ""
    lines = changed_lines(CLASSIC, out)
    assert all(ln.startswith("+") for ln in lines)  # nothing existing changes
    assert '    notify = BashOperator(task_id="notify", bash_command="echo done")' in out
    assert out.rstrip().endswith("report >> notify")
    # inserted after the last task definition, before the dependencies
    assert out.index("notify = BashOperator") < out.index("start >> extract")


def test_removing_a_task_at_the_end_of_a_chain() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    new["nodes"] = [n for n in new["nodes"] if n["task_id"] != "report"]
    new["edges"] = [e for e in new["edges"] if e["target"] != "n_report"]
    result = patch(CLASSIC, CLASSIC_SPEC, new)
    assert result.ok, result.refusals
    out = result.source or ""
    assert "report" not in out.split("as dag:")[1]
    assert "    start >> extract >> load\n" in out


def test_removing_an_edge_in_the_middle_splits_the_chain() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    new["edges"] = [e for e in new["edges"] if e != edge("extract", "load")]
    result = patch(CLASSIC, CLASSIC_SPEC, new)
    assert result.ok, result.refusals
    out = result.source or ""
    assert "    start >> extract\n    load >> report\n" in out


def test_removing_one_target_of_a_list_operand() -> None:
    base = CLASSIC.replace("start >> extract >> load >> report", "start >> [extract, load, report]")
    old = copy.deepcopy(CLASSIC_SPEC)
    old["edges"] = [edge("start", "extract"), edge("start", "load"), edge("start", "report")]
    new = copy.deepcopy(old)
    new["edges"] = [edge("start", "extract"), edge("start", "report")]
    result = patch(base, old, new)
    assert result.ok, result.refusals
    assert "    start >> [extract, report]\n" in (result.source or "")


def test_removing_an_edge_inside_a_list_chain_is_refused() -> None:
    base = CLASSIC.replace(
        "start >> extract >> load >> report", "start >> [extract, load] >> report"
    )
    old = copy.deepcopy(CLASSIC_SPEC)
    old["edges"] = [
        edge("start", "extract"),
        edge("start", "load"),
        edge("extract", "report"),
        edge("load", "report"),
    ]
    new = copy.deepcopy(old)
    new["edges"] = [e for e in old["edges"] if e != edge("start", "load")]
    result = patch(base, old, new)
    assert not result.ok
    assert "rewriting the dependency statement" in result.refusals[0].message


def test_renaming_a_literal_task_id_keeps_the_quote_style() -> None:
    new = copy.deepcopy(CLASSIC_SPEC)
    node(new, "report")["task_id"] = "summary"
    result = patch(CLASSIC, CLASSIC_SPEC, new, renames={"report": "summary"})
    assert result.ok, result.refusals
    assert changed_lines(CLASSIC, result.source or "") == [
        "-    report = BashOperator(task_id=\"report\",bash_command='echo report')",
        "+    report = BashOperator(task_id=\"summary\",bash_command='echo report')",
    ]


def test_changes_to_a_task_created_in_a_loop_are_refused() -> None:
    base = CLASSIC.replace(
        "    start >> extract >> load >> report\n",
        "    start >> extract >> load >> report\n"
        "    for i in range(2):\n"
        '        BashOperator(task_id=f"part_{i}", bash_command="x")\n',
    )
    old = copy.deepcopy(CLASSIC_SPEC)
    old["nodes"] += [op("part_0", bash_command="x"), op("part_1", bash_command="x")]
    new = copy.deepcopy(old)
    node(new, "part_1")["params"]["bash_command"] = "y"
    result = patch(base, old, new)
    assert not result.ok
    assert result.refusals[0].code == "unmappable"
    # unrelated changes still apply; the loop is reported as left alone
    other = copy.deepcopy(old)
    node(other, "load")["retries"] = 1
    ok = patch(base, old, other)
    assert ok.ok
    assert any("computed task_id" in w for w in ok.warnings)


# --------------------------------------------------------------------------- taskflow, groups

TASKFLOW_BASE = '''import pendulum
from airflow.sdk import TaskGroup, dag, task


@dag(schedule=None, start_date=pendulum.datetime(2026, 1, 1, tz="UTC"), catchup=False)
def taskflow_dag():
    @task
    def extract():
        """Pull rows."""
        return [1, 2, 3]  # rows

    rows = extract()

    with TaskGroup("publish") as publish:
        @task(task_id="push")
        def push_rows():
            print("push")

        pushed = push_rows()

    rows >> publish


taskflow_dag()
'''


def py(task_id: str, function_name: str, code: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "python_task",
        "id": f"n_{task_id}",
        "task_id": task_id,
        "function_name": function_name,
        "code": code,
        **extra,
    }


TASKFLOW_SPEC: dict[str, Any] = {
    "dag_id": "taskflow_dag",
    "start_date": "2026-01-01T00:00:00+00:00",
    "nodes": [
        py("extract", "extract", '"""Pull rows."""\nreturn [1, 2, 3]  # rows'),
        {"kind": "task_group", "id": "n_publish", "group_id": "publish"},
        py("push", "push_rows", 'print("push")', parent_group="n_publish"),
    ],
    "edges": [{"source": "n_extract", "target": "n_push"}],
}


def test_taskflow_decorator_arguments_and_body() -> None:
    new = copy.deepcopy(TASKFLOW_SPEC)
    node(new, "extract")["retries"] = 3
    node(new, "push")["code"] = 'print("push v2")'
    result = patch(TASKFLOW_BASE, TASKFLOW_SPEC, new)
    assert result.ok, result.refusals
    out = result.source or ""
    assert "    @task(retries=3)\n    def extract():" in out  # bare @task becomes a call
    assert 'print("push v2")' in out
    assert 'print("push")\n' not in out
    assert '"""Pull rows."""' in out
    assert "# rows" in out


def test_new_task_inside_a_with_task_group() -> None:
    new = copy.deepcopy(TASKFLOW_SPEC)
    new["nodes"].append(op("archive", bash_command="echo archive") | {"parent_group": "n_publish"})
    result = patch(TASKFLOW_BASE, TASKFLOW_SPEC, new)
    assert result.ok, result.refusals
    out = result.source or ""
    block = out.split('with TaskGroup("publish") as publish:')[1].split("rows >> publish")[0]
    assert 'BashOperator(task_id="archive", bash_command="echo archive")' in block
    assert "task_group=" not in block  # the with block gives the membership
    assert "from airflow.providers.standard.operators.bash import BashOperator" in out


def test_taskflow_rename_adds_task_id() -> None:
    new = copy.deepcopy(TASKFLOW_SPEC)
    node(new, "extract")["task_id"] = "pull"
    result = patch(TASKFLOW_BASE, TASKFLOW_SPEC, new, renames={"extract": "pull"})
    assert result.ok, result.refusals
    assert "@task(task_id='pull')\n    def extract():" in (result.source or "")


def test_removing_a_task_group_that_still_has_tasks_is_refused() -> None:
    new = copy.deepcopy(TASKFLOW_SPEC)
    new["nodes"] = [n for n in new["nodes"] if n["id"] != "n_publish"]
    node(new, "push").pop("parent_group")
    result = patch(TASKFLOW_BASE, TASKFLOW_SPEC, new)
    assert not result.ok


def test_classic_assign_style_refuses_new_tasks() -> None:
    base = """from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator

dag = DAG("assigned", schedule=None)
a = BashOperator(task_id="a", bash_command="echo a", dag=dag)
"""
    old = {
        "dag_id": "assigned",
        "nodes": [op("a", bash_command="echo a")],
    }
    changed = copy.deepcopy(old)
    node(changed, "a")["params"]["bash_command"] = "echo b"
    ok = patch(base, old, changed)
    assert ok.ok, ok.refusals
    assert 'a = BashOperator(task_id="a", bash_command="echo b", dag=dag)' in (ok.source or "")
    added = copy.deepcopy(old)
    added["nodes"].append(op("b", bash_command="echo b"))
    refused = patch(base, old, added)
    assert not refused.ok
    assert "dag = DAG" in refused.refusals[0].message


def test_unchanged_spec_leaves_the_file_byte_for_byte() -> None:
    result = patch(CLASSIC, CLASSIC_SPEC, CLASSIC_SPEC)
    assert result.ok
    assert result.source == CLASSIC
    assert not result.changed
