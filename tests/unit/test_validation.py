from __future__ import annotations

from autoair.spec import DagSpec
from autoair.validation import Policy, validate_spec


def codes(report):
    return {p.code for p in report.problems if p.severity == "error"}


def test_valid_spec(simple_spec_data):
    assert validate_spec(DagSpec.model_validate(simple_spec_data)).ok


def test_cycle_detected(simple_spec_data):
    simple_spec_data["edges"].append({"source": "b", "target": "a"})
    report = validate_spec(DagSpec.model_validate(simple_spec_data))
    assert "cycle" in codes(report)


def test_cycle_through_group_detected():
    spec = DagSpec.model_validate(
        {
            "dag_id": "g",
            "nodes": [
                {"kind": "task_group", "id": "g", "group_id": "g"},
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "parent_group": "g",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                },
                {
                    "kind": "operator",
                    "id": "b",
                    "task_id": "b",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                },
            ],
            "edges": [{"source": "g", "target": "b"}, {"source": "b", "target": "a"}],
        }
    )
    assert "cycle" in codes(validate_spec(spec))


def test_duplicate_effective_task_ids():
    spec = DagSpec.model_validate(
        {
            "dag_id": "d",
            "nodes": [
                {"kind": "task_group", "id": "g", "group_id": "grp", "prefix_group_id": False},
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "x",
                    "parent_group": "g",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                },
                {
                    "kind": "operator",
                    "id": "b",
                    "task_id": "x",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                },
            ],
        }
    )
    assert "duplicate_task_id" in codes(validate_spec(spec))


def test_policy_blocks_python_and_bash(simple_spec_data):
    simple_spec_data["nodes"].append(
        {"kind": "python_task", "id": "p", "task_id": "p", "function_name": "p", "code": "return 1"}
    )
    report = validate_spec(
        DagSpec.model_validate(simple_spec_data),
        policy=Policy(allow_python_code=False, allow_privileged_operators=False),
    )
    nodes = {p.node_id for p in report.problems if p.code == "policy"}
    assert nodes == {"p", "b"}


def test_reserved_and_mapping_problems(simple_spec_data):
    simple_spec_data["nodes"][1]["params"]["retries"] = 3
    simple_spec_data["nodes"][1]["mapping"] = {"expand": {"bash_command": "not-a-list"}}
    report = validate_spec(DagSpec.model_validate(simple_spec_data))
    assert {"reserved_param", "mapping"} <= codes(report)


def test_xcom_to_unknown_node(simple_spec_data):
    simple_spec_data["nodes"][1]["params"]["env"] = {"X": {"$type": "xcom", "node": "zzz"}}
    assert "xcom_unknown" in codes(validate_spec(DagSpec.model_validate(simple_spec_data)))


def test_unknown_custom_handler():
    spec = DagSpec.model_validate(
        {"dag_id": "c", "nodes": [{"kind": "custom", "id": "h", "task_id": "h", "handler": "nope"}]}
    )
    assert "unknown_handler" in codes(validate_spec(spec))
