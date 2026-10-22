"""Round trips between the spec, generated Python, Airflow 3.4 YAML and dag-factory YAML.

What a format can carry comes back unchanged; what it cannot is an export issue or warning,
never a silent loss."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from autoair.codegen import generate_source
from autoair.codegen.dagfactory import export_dag_factory
from autoair.codegen.yaml_dag import export_yaml_dag, import_yaml_dag, validate_yaml_text
from autoair.spec import DagSpec

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "specs"
BASH = "airflow.providers.standard.operators.bash.BashOperator"
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"

# A DAG that every format can express: operators, Jinja, XCom, task settings, a timezone.
PORTABLE: dict[str, Any] = {
    "dag_id": "portable",
    "display_name": "Portable DAG",
    "description": "Runs in every output format",
    "doc_md": "# Portable",
    "tags": ["etl", "portable"],
    "timezone": "Europe/Istanbul",
    "schedule": {"type": "preset", "preset": "@daily"},
    "start_date": "2024-01-01T06:30:00",
    "end_date": "2030-01-01T00:00:00",
    "catchup": True,
    "max_active_runs": 2,
    "max_active_tasks": 4,
    "dagrun_timeout_seconds": 7200,
    "is_paused_upon_creation": True,
    "max_consecutive_failed_dag_runs": 3,
    "render_template_as_native_obj": True,
    "default_args": {"owner": "data", "retries": 3, "retry_delay_seconds": 300},
    "params": {"limit": {"default": 10}},
    "nodes": [
        {
            "id": "extract",
            "kind": "operator",
            "task_id": "extract",
            "operator": BASH,
            "params": {"bash_command": "echo {{ ds }}", "env": {"LIMIT": "10"}},
            "retries": 0,
            "retry_delay_seconds": 30,
            "execution_timeout_seconds": 5400,
            "pool": "etl",
            "priority_weight": 5,
            "doc_md": "Extract step",
            "retry_exponential_backoff": 2.0,
        },
        {
            "id": "load",
            "kind": "operator",
            "task_id": "load",
            "operator": BASH,
            "params": {
                "bash_command": "echo load",
                "env": {"ROWS": {"$type": "xcom", "node": "extract"}},
            },
            "trigger_rule": "all_done",
            "owner": "ops",
        },
        {"id": "done", "kind": "operator", "task_id": "done", "operator": EMPTY},
    ],
    "edges": [{"source": "extract", "target": "load"}, {"source": "load", "target": "done"}],
}

# Canvas-only state (and the output mode, which names the format) is not part of a DAG.
_CANVAS = {"id", "position", "label", "icon", "template_ref"}


def _key(node: dict[str, Any]) -> str:
    return str(node.get("task_id") or node.get("group_id") or node.get("step_id"))


def _norm(spec: DagSpec) -> dict[str, Any]:
    data = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    names = {n["id"]: _key(n) for n in data.get("nodes", [])}

    def refs(value: Any) -> Any:  # XCom references by task id, not by node id
        if isinstance(value, dict):
            if value.get("$type") == "xcom":
                return {**value, "node": names.get(value["node"], value["node"])}
            return {k: refs(v) for k, v in value.items()}
        if isinstance(value, list):
            return [refs(v) for v in value]
        return value

    tasks = {
        _key(n): refs({k: v for k, v in n.items() if k not in _CANVAS})
        for n in data.get("nodes", [])
        if n["kind"] != "note"
    }
    edges = sorted(
        (names[e["source"]], names[e["target"]], e.get("label") or "")
        for e in data.get("edges", [])
    )
    top = {k: v for k, v in data.items() if k not in ("nodes", "edges", "output_mode")}
    return {"top": top, "tasks": tasks, "edges": edges}


def _golden(name: str) -> DagSpec:
    return DagSpec.model_validate_json((GOLDEN / name).read_text(encoding="utf-8"))


GOLDEN_SPECS = sorted(p.name for p in GOLDEN.glob("*.autoair.json"))


# --------------------------------------------------------------------- Airflow 3.4 YAML


def test_portable_dag_round_trips_through_airflow_yaml():
    spec = DagSpec.model_validate(PORTABLE)
    export = export_yaml_dag(spec)
    assert export.issues == []
    assert validate_yaml_text(export.yaml) == []
    doc = yaml.safe_load(export.yaml)
    task = doc["tasks"][0]
    assert task["retry_delay"] == "30s"
    assert task["execution_timeout"] == "1h30m"
    assert doc["dagrun_timeout"] == "2h"
    # Airflow's YAML Dags reject default_args: they are a template every task extends
    assert "default_args" not in doc
    assert doc["templates"]["default_args"] == {"owner": "data", "retries": 3, "retry_delay": "5m"}
    assert all(t["extends"] == "default_args" for t in doc["tasks"])
    back, issues = import_yaml_dag(export.yaml)
    assert issues == []
    assert _norm(back) == _norm(spec)


@pytest.mark.parametrize("name", GOLDEN_SPECS)
def test_golden_specs_never_lose_data_silently_in_yaml(name):
    spec = _golden(name)
    export = export_yaml_dag(spec)
    back, _ = import_yaml_dag(export.yaml)
    if _norm(back) != _norm(spec):
        # environment overlays are spec-only (applied before export), disabled tasks are
        # excluded by design: anything else lost must be reported
        assert export.issues, f"{name}: data lost without an export issue"
    if export.lossless:
        lossy = {i.message for i in export.issues}
        assert lossy or _norm(back) == _norm(spec)


def test_the_yaml_expressible_part_of_etl_full_round_trips():
    data = json.loads((GOLDEN / "etl_full.autoair.json").read_text(encoding="utf-8"))
    keep = {n["id"] for n in data["nodes"] if n["kind"] == "operator" and not n.get("mapping")}
    data["nodes"] = [{**n, "outlets": []} for n in data["nodes"] if n["id"] in keep]
    for n in data["nodes"]:
        n.pop("parent_group", None)
    data["edges"] = [e for e in data["edges"] if e["source"] in keep and e["target"] in keep]
    data.pop("environments", None)
    for p in data.get("params", {}).values():  # YAML keeps param defaults only
        for k in list(p):
            if k != "default":
                del p[k]
    spec = DagSpec.model_validate(data)
    export = export_yaml_dag(spec)
    assert export.lossless, export.issues
    back, issues = import_yaml_dag(export.yaml)
    assert not [i for i in issues if i.severity == "error"]
    assert _norm(back) == _norm(spec)


def test_yaml_reports_what_it_cannot_carry():
    data = copy.deepcopy(PORTABLE)
    data["nodes"][0]["retry_delay_seconds"] = 0.5
    data["nodes"][0]["lineage"] = "disabled"
    data["default_args"]["execution_timeout_seconds"] = 60.5
    data["edges"][0]["label"] = "rows"
    messages = [i.message for i in export_yaml_dag(DagSpec.model_validate(data)).issues]
    assert any("retry_delay of 0.5s" in m for m in messages)
    assert any("OpenLineage" in m for m in messages)
    assert any("default_args execution_timeout" in m for m in messages)
    assert any("edge label 'rows' is dropped" in m for m in messages)


def _build_yaml_dag(text: str) -> Any:
    builder = pytest.importorskip("airflow.sdk.importers.yaml_importer.builder")
    parser = pytest.importorskip("airflow.sdk.importers.yaml_importer.parser")

    class Definition:
        def get_relative_loc(self, root: Any = None) -> str:
            return "portable.yaml"

        def sibling(self, path: str) -> Any:
            raise OSError(path)

    class Bundle:
        name = "tests"
        path = Path("/nonexistent")

    (doc,) = parser.parse_documents(text, source="portable.yaml")
    return builder.DagBuilder(definition=Definition(), bundle=Bundle()).build(doc)


@pytest.mark.airflow
def test_airflow_builds_the_yaml_export_like_the_python_export():
    spec = DagSpec.model_validate(PORTABLE)
    from_yaml = _build_yaml_dag(export_yaml_dag(spec).yaml)
    namespace: dict[str, Any] = {}
    exec(compile(generate_source(spec), "portable.py", "exec"), namespace)
    from_python = namespace["dag"]
    for attr in (
        "start_date",
        "end_date",
        "dagrun_timeout",
        "max_active_runs",
        "max_active_tasks",
        "catchup",
        "render_template_as_native_obj",
        "tags",
    ):
        assert getattr(from_yaml, attr) == getattr(from_python, attr), attr
    assert type(from_yaml.timetable) is type(from_python.timetable)
    for attr in ("expression", "_expression", "timezone", "_timezone", "run_immediately"):
        assert getattr(from_yaml.timetable, attr, None) == getattr(
            from_python.timetable, attr, None
        ), attr
    for task_id in ("extract", "load", "done"):
        y, p = from_yaml.get_task(task_id), from_python.get_task(task_id)
        for attr in (
            "retries",
            "retry_delay",
            "execution_timeout",
            "pool",
            "priority_weight",
            "owner",
            "trigger_rule",
            "retry_exponential_backoff",
            "upstream_task_ids",
        ):
            assert getattr(y, attr) == getattr(p, attr), (task_id, attr)


# --------------------------------------------------------------------- Python


@pytest.mark.airflow
def test_portable_dag_round_trips_through_python_import():
    from autoair.importer.python_dag import import_python_source

    spec = DagSpec.model_validate(PORTABLE)
    result = import_python_source(generate_source(spec), filename="portable.py")
    assert not result.import_errors, result.import_errors
    (dag,) = result.dags
    assert dag.warnings == []
    assert dag.spec is not None
    got = _norm(dag.spec)
    want = _norm(spec)
    # Python carries a display name and the timezone only through dates: same DAG
    assert got == want


# --------------------------------------------------------------------- dag-factory

_TASK_FIELDS = (
    "trigger_rule",
    "retries",
    "retry_delay_seconds",
    "execution_timeout_seconds",
    "pool",
    "pool_slots",
    "queue",
    "owner",
    "doc_md",
    "priority_weight",
    "weight_rule",
    "task_display_name",
    "executor",
    "max_active_tis_per_dagrun",
    "depends_on_past",
    "retry_exponential_backoff",
    "executor_config",
    "outlets",
    "inlets",
    "retry_policy",
    "is_setup",
    "is_teardown",
)


@pytest.mark.parametrize("name", [*GOLDEN_SPECS, "portable"])
def test_dag_factory_exports_or_warns_about_every_setting(name):
    spec = DagSpec.model_validate(PORTABLE) if name == "portable" else _golden(name)
    result = export_dag_factory(spec)
    dag = yaml.safe_load(result.yaml)[spec.dag_id]
    tasks = {t["task_id"]: t for t in dag["tasks"]}
    text = " ".join(result.warnings)
    for node in spec.task_nodes(include_disabled=False):
        if node.task_id not in tasks:
            assert f"node {node.task_id!r}" in text, node.task_id
            continue
        exported = tasks[node.task_id]
        for field in _TASK_FIELDS:
            value = getattr(node, field)
            if value in (None, False, [], {}):
                continue
            key = field.removesuffix("_seconds")
            assert key in exported or node.task_id in text, (node.task_id, field)
    for field in (
        "display_name",
        "dagrun_timeout_seconds",
        "is_paused_upon_creation",
        "fail_fast",
        "max_consecutive_failed_dag_runs",
        "render_template_as_native_obj",
        "params",
        "deadlines",
        "owner_links",
    ):
        if getattr(spec, field) not in (None, [], {}):
            assert field in text, field
    if spec.end_date:
        assert dag["default_args"]["end_date"] == spec.end_date.isoformat()
    for edge in spec.edges:
        if edge.label:
            assert repr(edge.label) in text


def test_dag_factory_exports_group_dependencies_as_task_dependencies():
    result = export_dag_factory(_golden("etl_full.autoair.json"))
    tasks = {t["task_id"]: t for t in yaml.safe_load(result.yaml)["etl_full"]["tasks"]}
    assert "group dependencies" not in " ".join(result.warnings)
    # start >> extract(download, list_files) >> process
    assert tasks["download"]["dependencies"] == ["start"]
