from __future__ import annotations

import pytest
from pydantic import ValidationError

from autoair.spec import AssetSchedule, DagSpec, OperatorNode, iter_xcom_refs


def test_roundtrip(golden_spec_data):
    spec = DagSpec.model_validate(golden_spec_data)
    again = DagSpec.model_validate_json(spec.model_dump_json())
    assert again == spec


def test_discriminated_nodes(golden_spec_data):
    spec = DagSpec.model_validate(golden_spec_data)
    kinds = {n.id: n.kind for n in spec.nodes}
    assert kinds["g1"] == "task_group"
    assert isinstance(spec.node_map()["n2"], OperatorNode)


def test_unknown_fields_are_rejected(simple_spec_data):
    simple_spec_data["nodes"][0]["unexpected"] = 1
    with pytest.raises(ValidationError):
        DagSpec.model_validate(simple_spec_data)


@pytest.mark.parametrize("dag_id", ["bad id", "../etc", "", "x" * 251, "a/b"])
def test_invalid_dag_ids(dag_id):
    with pytest.raises(ValidationError):
        DagSpec(dag_id=dag_id)


def test_operator_fqn_must_be_dotted_identifier():
    with pytest.raises(ValidationError):
        OperatorNode(kind="operator", id="a", task_id="a", operator="os; import sys")


def test_asset_schedule_requires_name_or_uri():
    with pytest.raises(ValidationError):
        AssetSchedule.model_validate({"type": "assets", "expression": {"op": "asset", "asset": {}}})


def test_iter_xcom_refs():
    value = {
        "a": [{"$type": "xcom", "node": "n1"}, 1],
        "b": {"$type": "xcom", "node": "n2", "key": "k"},
    }
    assert [r.node for r in iter_xcom_refs(value)] == ["n1", "n2"]
