"""Cyclic TaskGroup dependencies (deprecated by Airflow 3.4, planned error in 3.5)."""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from autoair.codegen import render
from autoair.spec import DagSpec
from autoair.validation import validate_spec

EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"


def _op(node_id: str, group: str | None = None, **extra: Any) -> dict[str, Any]:
    node = {"kind": "operator", "id": node_id, "task_id": node_id, "operator": EMPTY, **extra}
    if group:
        node["parent_group"] = group
    return node


def _group(node_id: str, parent: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"kind": "task_group", "id": node_id, "group_id": node_id}
    if parent:
        node["parent_group"] = parent
    return node


def _spec(nodes: list[dict[str, Any]], edges: list[tuple[str, str]]) -> DagSpec:
    return DagSpec.model_validate(
        {
            "dag_id": "tg_cycle",
            "nodes": nodes,
            "edges": [{"source": s, "target": t} for s, t in edges],
        }
    )


def _warnings(spec: DagSpec) -> list[Any]:
    return [p for p in validate_spec(spec).problems if p.code == "task_group_cycle"]


# a >> bridge >> b with a and b in group g and bridge outside it (Airflow's own example).
BRIDGE = _spec(
    [_group("g"), _op("a", "g"), _op("b", "g"), _op("bridge")], [("a", "bridge"), ("bridge", "b")]
)


def test_path_leaving_and_reentering_a_group_is_a_warning() -> None:
    report = validate_spec(BRIDGE)
    assert report.ok  # still valid: the task graph is acyclic
    (problem,) = _warnings(BRIDGE)
    assert problem.severity == "warning"
    assert problem.node_id == "g"
    assert "group 'g'" in problem.message
    assert "task 'bridge'" in problem.message
    assert "Airflow 3.5" in problem.message


def test_one_direction_between_groups_is_fine() -> None:
    spec = _spec(
        [_group("g1"), _group("g2"), _op("a", "g1"), _op("b", "g2"), _op("c", "g2")],
        [("a", "b"), ("g1", "c")],
    )
    assert _warnings(spec) == []


def test_groups_depending_on_each_other() -> None:
    spec = _spec(
        [_group("g1"), _group("g2"), _op("a", "g1"), _op("b", "g1"), _op("c", "g2")],
        [("a", "c"), ("c", "b")],
    )
    (problem,) = _warnings(spec)
    assert "group 'g1', group 'g2'" in problem.message


def test_cycle_inside_a_parent_group_names_prefixed_ids() -> None:
    spec = _spec(
        [
            _group("outer"),
            _group("inner", "outer"),
            _op("a", "inner"),
            _op("b", "inner"),
            _op("x", "outer"),
        ],
        [("a", "x"), ("x", "b")],
    )
    (problem,) = _warnings(spec)
    assert "group 'outer.inner'" in problem.message
    assert "task 'outer.x'" in problem.message


def test_edges_inside_one_group_do_not_count() -> None:
    spec = _spec([_group("g"), _op("a", "g"), _op("b", "g"), _op("c")], [("a", "b"), ("g", "c")])
    assert _warnings(spec) == []


def test_xcom_dependency_counts() -> None:
    spec = _spec(
        [
            _group("g"),
            _op("a", "g"),
            _op("bridge", params={"x": {"$type": "xcom", "node": "a"}}),
            _op("b", "g"),
        ],
        [("bridge", "b")],
    )
    assert len(_warnings(spec)) == 1


def test_disabled_bridge_removes_the_cycle() -> None:
    spec = _spec(
        [_group("g"), _op("a", "g"), _op("b", "g"), _op("bridge", disabled=True)],
        [("a", "bridge"), ("bridge", "b")],
    )
    assert _warnings(spec) == []


def test_real_cycle_is_an_error_not_a_group_warning() -> None:
    spec = _spec([_group("g"), _op("a", "g"), _op("b")], [("g", "b"), ("b", "a")])
    report = validate_spec(spec)
    assert "cycle" in {p.code for p in report.problems}
    assert _warnings(spec) == []


@pytest.mark.airflow
@pytest.mark.parametrize(
    "spec",
    [
        BRIDGE,
        _spec(
            [_group("g1"), _group("g2"), _op("a", "g1"), _op("b", "g1"), _op("c", "g2")],
            [("a", "c"), ("c", "b")],
        ),
        _spec(
            [
                _group("outer"),
                _group("inner", "outer"),
                _op("a", "inner"),
                _op("b", "inner"),
                _op("x", "outer"),
            ],
            [("a", "x"), ("x", "b")],
        ),
        _spec(
            [_group("g1"), _group("g2"), _op("a", "g1"), _op("b", "g2"), _op("c", "g2")],
            [("a", "b"), ("g1", "c")],
        ),
    ],
    ids=["bridge", "two-groups", "nested", "acyclic"],
)
def test_matches_airflow(spec: DagSpec) -> None:
    """Same verdict as Airflow's own check, where it exists (3.4+)."""
    try:
        from airflow.sdk.exceptions import TaskGroupCycleDeprecationWarning
    except ImportError:
        pytest.skip("installed Airflow has no TaskGroup cycle check (< 3.4)")
    namespace: dict[str, Any] = {"__name__": "tg_cycle_check"}
    exec(compile(render(spec), "tg_cycle.py", "exec"), namespace)
    dag = next(v for v in namespace.values() if type(v).__name__ == "DAG")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", TaskGroupCycleDeprecationWarning)
        dag.check_cycle()
    airflow_warns = any(issubclass(w.category, TaskGroupCycleDeprecationWarning) for w in caught)
    assert airflow_warns is bool(_warnings(spec))
