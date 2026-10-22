from __future__ import annotations

import copy
import json

from autoair.merge import MergeResult, SpecDiff, diff_specs, merge_specs
from autoair.spec import DagSpec


def spec(data, **changes):
    data = copy.deepcopy(data)
    data.update(changes)
    return DagSpec.model_validate(data)


def mutate(data, fn):
    data = copy.deepcopy(data)
    fn(data)
    return DagSpec.model_validate(data)


def node(data, node_id):
    return next(n for n in data["nodes"] if n["id"] == node_id)


def test_diff_identical(simple_spec_data):
    diff = diff_specs(spec(simple_spec_data), spec(simple_spec_data))
    assert diff.identical
    assert diff == SpecDiff()


def test_diff_reports_nodes_edges_and_fields(simple_spec_data):
    a = spec(simple_spec_data)

    def change(d):
        node(d, "b")["params"]["bash_command"] = "echo bye"
        node(d, "a")["position"] = {"x": 10, "y": 5}
        d["nodes"].append(
            {
                "kind": "operator",
                "id": "c",
                "task_id": "c",
                "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
            }
        )
        d["edges"] = [{"source": "b", "target": "c", "label": "next"}]
        d["description"] = "new"
        d["schedule"] = {"type": "cron", "expression": "0 6 * * *"}

    b = mutate(simple_spec_data, change)
    diff = diff_specs(a, b)
    assert not diff.identical
    assert [n["id"] for n in diff.added_nodes] == ["c"]
    assert diff.removed_nodes == []
    changed = {c.id: c for c in diff.changed_nodes}
    assert changed["a"].layout_only
    assert not changed["b"].layout_only
    assert [(c.path, c.before, c.after) for c in changed["b"].changes] == [
        ("params.bash_command", "echo hi", "echo bye")
    ]
    assert [(e.source, e.target, e.label) for e in diff.added_edges] == [("b", "c", "next")]
    assert [(e.source, e.target) for e in diff.removed_edges] == [("a", "b")]
    fields = {c.path: c for c in diff.changed_fields}
    assert fields["description"].after == "new"
    # discriminator changed: reported as one atomic change
    assert fields["schedule"].before == {"type": "none"}
    json.dumps(diff.model_dump(mode="json"))  # serializable for the frontend


def test_clean_merge_of_non_overlapping_changes(simple_spec_data):
    base = spec(simple_spec_data)

    def ours_change(d):
        node(d, "b")["params"]["bash_command"] = "echo ours"
        d["tags"] = ["ours"]

    def theirs_change(d):
        node(d, "b")["retries"] = 3
        node(d, "b")["params"]["env"] = {"A": "1"}
        d["description"] = "theirs"
        d["nodes"].append(
            {
                "kind": "operator",
                "id": "c",
                "task_id": "c",
                "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
            }
        )
        d["edges"].append({"source": "b", "target": "c"})

    result = merge_specs(
        base, mutate(simple_spec_data, ours_change), mutate(simple_spec_data, theirs_change)
    )
    assert isinstance(result, MergeResult)
    assert result.conflicts == []
    merged = result.merged
    assert merged is not None
    b = merged.node_map()["b"]
    assert b.params == {"bash_command": "echo ours", "env": {"A": "1"}}
    assert b.retries == 3
    assert merged.tags == ["ours"]
    assert merged.description == "theirs"
    assert [n.id for n in merged.nodes] == ["a", "b", "c"]
    assert {(e.source, e.target) for e in merged.edges} == {("a", "b"), ("b", "c")}


def test_same_change_on_both_sides_is_not_a_conflict(simple_spec_data):
    base = spec(simple_spec_data)
    both = spec(simple_spec_data, description="same")
    result = merge_specs(base, both, both)
    assert result.merged == both


def test_conflicting_field_changes(simple_spec_data):
    base = spec(simple_spec_data)
    ours = mutate(simple_spec_data, lambda d: node(d, "b")["params"].update(bash_command="o"))
    theirs = mutate(simple_spec_data, lambda d: node(d, "b")["params"].update(bash_command="t"))
    result = merge_specs(base, ours, theirs)
    assert result.merged is None
    [conflict] = result.conflicts
    assert conflict.path == "nodes[b].params.bash_command"
    assert (conflict.base, conflict.ours, conflict.theirs) == ("echo hi", "o", "t")
    assert result.draft is not None
    assert node(result.draft, "b")["params"]["bash_command"] == "o"


def test_delete_vs_modify_conflicts(simple_spec_data):
    base = spec(simple_spec_data)

    def delete_b(d):
        d["nodes"] = [n for n in d["nodes"] if n["id"] != "b"]
        d["edges"] = []

    ours = mutate(simple_spec_data, delete_b)
    theirs = mutate(simple_spec_data, lambda d: node(d, "b").update(retries=2))
    result = merge_specs(base, ours, theirs)
    assert result.merged is None
    paths = {c.path: c for c in result.conflicts}
    assert "nodes[b]" in paths
    assert not paths["nodes[b]"].ours_present
    assert paths["nodes[b]"].reason.startswith("deleted on one side")


def test_delete_without_modification_merges(simple_spec_data):
    base = spec(simple_spec_data)

    def delete_b(d):
        d["nodes"] = [n for n in d["nodes"] if n["id"] != "b"]
        d["edges"] = []

    ours = mutate(simple_spec_data, delete_b)
    theirs = spec(simple_spec_data, description="x")
    result = merge_specs(base, ours, theirs)
    assert result.merged is not None
    assert [n.id for n in result.merged.nodes] == ["a"]
    assert result.merged.description == "x"


def test_edge_to_deleted_node_is_a_conflict(simple_spec_data):
    base = spec(simple_spec_data)

    def delete_b(d):
        d["nodes"] = [n for n in d["nodes"] if n["id"] != "b"]
        d["edges"] = []

    def add_edge(d):
        d["nodes"].append(
            {
                "kind": "operator",
                "id": "c",
                "task_id": "c",
                "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
            }
        )
        d["edges"].append({"source": "b", "target": "c"})

    result = merge_specs(
        base, mutate(simple_spec_data, delete_b), mutate(simple_spec_data, add_edge)
    )
    assert result.merged is None
    assert any(c.path == "edges[b->c]" for c in result.conflicts)


def test_schedule_type_change_vs_edit_conflicts(simple_spec_data):
    base = spec(simple_spec_data, schedule={"type": "cron", "expression": "0 6 * * *"})
    ours = spec(simple_spec_data, schedule={"type": "preset", "preset": "@daily"})
    theirs = spec(
        simple_spec_data, schedule={"type": "cron", "expression": "0 7 * * *", "timezone": "UTC"}
    )
    result = merge_specs(base, ours, theirs)
    assert [c.path for c in result.conflicts] == ["schedule"]
    assert result.conflicts[0].reason == "the object type changed"


def test_both_sides_add_same_edge_with_different_ids(simple_spec_data):
    base = spec(simple_spec_data, edges=[])
    ours = spec(simple_spec_data, edges=[{"id": "e1", "source": "a", "target": "b"}])
    theirs = spec(simple_spec_data, edges=[{"id": "e2", "source": "a", "target": "b"}])
    result = merge_specs(base, ours, theirs)
    assert result.merged is not None
    assert [e.id for e in result.merged.edges] == ["e1"]


def test_diff_tells_an_absent_key_from_an_explicit_null() -> None:
    """B07 (0.2.3 review): adding ``execution_timeout: null`` (overrides a DAG default
    timeout) was reported as identical, so the version diff hid a behaviour change."""
    from autoair.merge import diff_specs
    from autoair.spec import DagSpec

    def spec(params: dict[str, object]) -> DagSpec:
        return DagSpec.model_validate(
            {
                "dag_id": "d",
                "nodes": [
                    {
                        "kind": "operator",
                        "id": "a",
                        "task_id": "a",
                        "operator": "airflow.providers.standard.operators.bash.BashOperator",
                        "params": params,
                    }
                ],
            }
        )

    before, after = (
        spec({"bash_command": "x"}),
        spec({"bash_command": "x", "execution_timeout": None}),
    )
    diff = diff_specs(before, after)
    assert not diff.identical
    (change,) = diff.changed_nodes[0].changes
    assert change.path == "params.execution_timeout"
    assert (change.before_present, change.after_present) == (False, True)
    back = diff_specs(after, before).changed_nodes[0].changes[0]
    assert (back.before_present, back.after_present) == (True, False)
    assert diff_specs(after, after).identical
