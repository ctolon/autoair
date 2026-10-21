"""Astronomer Blueprint steps: static discovery, spec, code generation, exports, validation."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from autoair.blueprints.catalog import (
    BlueprintCatalog,
    build_catalog,
    name_and_version,
    parse_blueprints,
    scan_sources,
    to_snake_case,
)
from autoair.blueprints.config import BlueprintSettings
from autoair.blueprints.models import BlueprintCatalogData, BlueprintDetail
from autoair.codegen import CodegenError, generate_source
from autoair.codegen.dagfactory import export_dag_factory
from autoair.codegen.yaml_dag import export_yaml_dag
from autoair.spec import BlueprintNode, DagSpec
from autoair.validation import effective_task_id, validate_spec

EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "ExtractConfig",
    "properties": {
        "source_table": {"type": "string", "title": "Source Table"},
        "batch_size": {"type": "integer", "minimum": 1, "default": 1000},
        "comment": {"type": "string"},
    },
    "required": ["source_table"],
}


def bp_node(**over: Any) -> dict[str, Any]:
    node = {
        "kind": "blueprint",
        "id": "b",
        "step_id": "extract_customers",
        "blueprint": "extract",
        "version": 1,
        "class_path": "company.blueprints.Extract",
        "config_class": "ExtractConfig",
        "config": {"source_table": "raw.customers", "batch_size": 500},
    }
    return node | over


def spec_with(
    *nodes: dict[str, Any], edges: list[dict[str, str]] | None = None, **dag: Any
) -> DagSpec:
    return DagSpec.model_validate(
        {"dag_id": "bp", "nodes": list(nodes), "edges": edges or [], **dag}
    )


def detail(**over: Any) -> BlueprintDetail:
    data: dict[str, Any] = {
        "name": "extract",
        "version": 1,
        "class_name": "Extract",
        "module": "company.blueprints",
        "class_path": "company.blueprints.Extract",
        "config_class": "ExtractConfig",
        "source": "entry_point",
        "config_schema": EXTRACT_SCHEMA,
    }
    return BlueprintDetail.model_validate(data | over)


class FakeCatalog(BlueprintCatalog):
    """A catalog with fixed contents (no discovery, no child process)."""

    def __init__(self, *details: BlueprintDetail) -> None:
        super().__init__(BlueprintSettings(entry_points=False), read_schemas=False)
        self._fixed = BlueprintCatalogData(key="fixed", built_at=0, blueprints=list(details))

    def data(self, *, force: bool = False) -> BlueprintCatalogData:
        return self._fixed


# --------------------------------------------------------------------------- static analysis


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        ("Extract", ("extract", 1)),
        ("ExtractV2", ("extract", 2)),
        ("MultiSourceETL", ("multi_source_etl", 1)),
        ("MultiSourceETLV3", ("multi_source_etl", 3)),
        ("HTTPLoadV12", ("http_load", 12)),
    ],
)
def test_names_follow_the_blueprint_registry(cls: str, expected: tuple[str, int]) -> None:
    assert name_and_version(cls) == expected


def test_explicit_name_and_version_win() -> None:
    assert name_and_version("S3DataIngesterV4", "ingest", None) == ("ingest", 4)
    assert name_and_version("StreamingIngester", "ingest", 2) == ("ingest", 2)
    assert name_and_version("Thing", None, True) == ("thing", 1)  # bool is not a version
    assert to_snake_case("ETLJob") == "etl_job"


def test_parse_finds_blueprint_subclasses_without_executing() -> None:
    source = '''
import os
os.system("never executed")
import blueprint as bp
from blueprint import Blueprint, BaseModel

class Cfg(BaseModel):
    x: int = 1

class Extract(Blueprint[Cfg]):
    """Extract rows. More text."""

class LoadV2(bp.Blueprint[bp_config.LoadCfg]):
    supports_params = True

class Named(Blueprint[Cfg]):
    name: str = "notify"
    version = 3

class _Private(Blueprint[Cfg]):
    pass

class NotABlueprint(BaseModel):
    pass

def factory():
    class Inner(Blueprint[Cfg]):
        pass
'''
    found = {b.class_name: b for b in parse_blueprints(source)}
    assert set(found) == {"Extract", "LoadV2", "Named"}
    assert (found["Extract"].name, found["Extract"].version) == ("extract", 1)
    assert found["Extract"].config_class == "Cfg"
    assert found["Extract"].docstring == "Extract rows. More text."
    assert (found["LoadV2"].name, found["LoadV2"].version) == ("load", 2)
    assert found["LoadV2"].config_class == "LoadCfg"
    assert found["LoadV2"].supports_params is True
    assert (found["Named"].name, found["Named"].version) == ("notify", 3)


def test_parse_ignores_syntax_errors() -> None:
    assert parse_blueprints("class Broken(Blueprint[Cfg]:\n") == []


def test_template_dir_scan_uses_module_paths(tmp_path: Path) -> None:
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "etl.py").write_text("from blueprint import Blueprint\n")
    (tmp_path / "_hidden.py").write_text("")
    (tmp_path / "bad-name.py").write_text("")
    (tmp_path / "top.py").write_text("")
    files, errors = scan_sources(
        BlueprintSettings(entry_points=False, template_dirs=[str(tmp_path), str(tmp_path / "x")])
    )
    assert sorted(f.module for f in files) == ["team.etl", "top"]
    assert {f.path_entry for f in files} == {str(tmp_path.resolve())}
    assert [e.location for e in errors] == [str(tmp_path / "x")]


def test_max_files_is_reported(tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"m{i}.py").write_text("")
    files, errors = scan_sources(
        BlueprintSettings(entry_points=False, template_dirs=[str(tmp_path)], max_files=2)
    )
    assert len(files) == 2
    assert len(errors) == 1
    assert "max_files" in errors[0].error


def test_static_build_without_schemas(tmp_path: Path) -> None:
    (tmp_path / "etl.py").write_text(
        "from blueprint import Blueprint\n"
        "class Extract(Blueprint[Cfg]):\n    'Extract.'\n"
        "class ExtractV2(Blueprint[Cfg2]):\n    pass\n"
        "class Other(Blueprint[Cfg]):\n    name = 'extract'\n    version = 2\n"
        "class Bad(Blueprint[Cfg]):\n    name = 'Not-Snake'\n"
    )
    data = build_catalog(
        BlueprintSettings(entry_points=False, template_dirs=[str(tmp_path)]), read_schemas=False
    )
    by = {(d.name, d.version): d for d in data.blueprints}
    assert set(by) == {("extract", 1), ("extract", 2)}
    assert by[("extract", 2)].class_path == "etl.ExtractV2"  # first definition wins
    assert by[("extract", 2)].latest
    assert not by[("extract", 1)].latest
    assert by[("extract", 1)].summary == "Extract."
    messages = " ".join(e.error for e in data.errors)
    assert "duplicate blueprint 'extract' v2" in messages
    assert "not snake_case" in messages


def test_catalog_rescans_only_after_the_interval(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("class A(Blueprint[C]):\n    pass\n")
    settings = BlueprintSettings(
        entry_points=False, template_dirs=[str(tmp_path)], rescan_interval=3600
    )
    catalog = BlueprintCatalog(settings, read_schemas=False, cache_dir=tmp_path / "cache")
    assert [d.name for d in catalog.data().blueprints] == ["a"]
    (tmp_path / "b.py").write_text("class B(Blueprint[C]):\n    pass\n")
    assert [d.name for d in catalog.data().blueprints] == ["a"]  # within the interval
    assert [d.name for d in catalog.refresh().blueprints] == ["a", "b"]
    assert catalog.get("b") is not None
    assert catalog.get("b", 2) is None


# --------------------------------------------------------------------------- spec


def test_spec_accepts_blueprint_nodes() -> None:
    spec = spec_with(bp_node())
    node = spec.nodes[0]
    assert isinstance(node, BlueprintNode)
    assert spec.blueprint_nodes() == [node]
    assert spec.task_nodes() == []
    assert effective_task_id(node, {node.id: node}) == "extract_customers"


@pytest.mark.parametrize(
    "bad",
    [
        {"blueprint": "Extract"},
        {"version": 0},
        {"class_path": "Extract"},
        {"class_path": "os.system; x"},
        {"step_id": "has space"},
        {"config_class": "not valid"},
        {"unknown": 1},
    ],
)
def test_spec_rejects_bad_blueprint_fields(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec_with(bp_node(**bad))


# --------------------------------------------------------------------------- codegen


def _calls(source: str) -> list[str]:
    return [ast.unparse(n) for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Assign)]


def test_codegen_renders_the_blueprint_as_documented() -> None:
    spec = spec_with(
        {"kind": "operator", "id": "a", "task_id": "start", "operator": EMPTY},
        bp_node(),
        edges=[{"source": "a", "target": "b"}],
    )
    source = generate_source(spec)
    assert "from company.blueprints import Extract, ExtractConfig" in source
    assert "extract_customers_blueprint = Extract()" in source
    assert "extract_customers_blueprint.step_id = 'extract_customers'" in source
    assert (
        "g_extract_customers = extract_customers_blueprint.render("
        "ExtractConfig(source_table='raw.customers', batch_size=500))"
    ) in source
    assert "t_start >> g_extract_customers" in source


def test_codegen_inside_a_group_and_without_config_class() -> None:
    spec = spec_with(
        {"kind": "task_group", "id": "g", "group_id": "ingest"},
        bp_node(parent_group="g", config_class=None, config={"class": "x", "ok": 1}),
    )
    tree = ast.parse(generate_source(spec))
    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
    inner = [w for w in withs if ast.unparse(w.items[0].context_expr) == "g_ingest"]
    assert len(inner) == 1
    # "class" is a keyword: the config goes through model_validate.
    assert ast.unparse(inner[0].body[0]) == (
        "g_extract_customers = extract_customers_blueprint.render("
        "Extract.get_config_type().model_validate({'class': 'x', 'ok': 1}))"
    )


@pytest.mark.parametrize(
    "payload", ['"); import os; os.system("id"); ("', "'''\nimport os\n'''", "{{ ds }}\n\x00"]
)
def test_codegen_keeps_hostile_config_values_literal(payload: str) -> None:
    source = generate_source(spec_with(bp_node(config={"source_table": payload})))
    tree = ast.parse(source)
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert modules <= {"airflow.sdk", "company.blueprints"}
    assert payload in {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}


def test_codegen_rejects_tagged_config_values() -> None:
    spec = spec_with(
        {"kind": "operator", "id": "a", "task_id": "start", "operator": EMPTY},
        bp_node(config={"source_table": {"$type": "xcom", "node": "a"}}),
    )
    with pytest.raises(CodegenError, match="plain JSON"):
        generate_source(spec)


def test_disabled_blueprint_is_left_out_and_bridged() -> None:
    spec = spec_with(
        {"kind": "operator", "id": "a", "task_id": "a", "operator": EMPTY},
        bp_node(disabled=True),
        {"kind": "operator", "id": "c", "task_id": "c", "operator": EMPTY},
        edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
    )
    source = generate_source(spec)
    assert "Extract" not in source
    assert "t_a >> t_c" in source


def test_loop_group_is_refused() -> None:
    spec = spec_with(
        {"kind": "task_group", "id": "g", "group_id": "loop", "loop": {"max_iterations": 3}},
        bp_node(parent_group="g"),
    )
    with pytest.raises(CodegenError, match="loop group"):
        generate_source(spec)


# --------------------------------------------------------------------------- exports


def test_yaml_export_reports_blueprint_steps() -> None:
    spec = spec_with(
        {"kind": "operator", "id": "a", "task_id": "a", "operator": EMPTY},
        bp_node(),
        edges=[{"source": "a", "target": "b"}],
    )
    export = export_yaml_dag(spec)
    assert not export.lossless
    assert any(i.node_id == "b" and "YAML Dags" in i.message for i in export.issues)


def test_dag_factory_skips_blueprint_steps_with_a_warning() -> None:
    spec = spec_with(
        {"kind": "operator", "id": "a", "task_id": "a", "operator": EMPTY},
        bp_node(),
        edges=[{"source": "a", "target": "b"}],
    )
    result = export_dag_factory(spec)
    assert any("blueprint step 'extract_customers'" in w for w in result.warnings)
    assert "extract_customers" not in result.yaml


# --------------------------------------------------------------------------- validation


def codes(spec: DagSpec, catalog: BlueprintCatalog | None) -> dict[str, list[str | None]]:
    report = validate_spec(spec, blueprints=catalog)
    out: dict[str, list[str | None]] = {}
    for p in report.problems:
        out.setdefault(p.code, []).append(p.field)
    return out


def test_valid_step_has_only_the_runtime_hint() -> None:
    report = validate_spec(spec_with(bp_node()), blueprints=FakeCatalog(detail()))
    assert report.ok
    assert [(p.severity, p.code) for p in report.problems] == [("info", "blueprint_runtime")]
    assert "company.blueprints" in report.problems[0].message


def test_without_a_catalog_only_structure_is_checked() -> None:
    assert set(codes(spec_with(bp_node(config={"batch_size": "x"})), None)) == {"blueprint_runtime"}


def test_unknown_blueprint_or_version() -> None:
    found = codes(spec_with(bp_node(version=3)), FakeCatalog(detail(), detail(version=2)))
    assert found["blueprint_unknown"] == ["blueprint"]
    report = validate_spec(spec_with(bp_node(version=3)), blueprints=FakeCatalog(detail()))
    assert "available versions: 1" in report.problems[0].message
    assert not report.ok


def test_class_path_is_allowlisted_by_the_catalog() -> None:
    found = codes(
        spec_with(bp_node(class_path="os.path.Extract", config_class="Other")),
        FakeCatalog(detail()),
    )
    assert found["blueprint_class"] == ["class_path", "config_class"]


def test_config_is_checked_against_the_schema() -> None:
    catalog = FakeCatalog(detail())
    found = codes(spec_with(bp_node(config={"batch_size": 0})), catalog)
    assert found["blueprint_config"] == ["config", "config"]  # missing source_table, minimum
    # An explicit null of an optional field is unset for Pydantic (X | None).
    assert "blueprint_config" not in codes(
        spec_with(bp_node(config={"source_table": "t", "comment": None})), catalog
    )


def test_tagged_values_and_loops_and_yaml_mode() -> None:
    spec = spec_with(
        {"kind": "task_group", "id": "g", "group_id": "loop", "loop": {"max_iterations": 3}},
        bp_node(parent_group="g", config={"source_table": {"$type": "timedelta", "seconds": 1}}),
        output_mode="yaml",
    )
    found = codes(spec, FakeCatalog(detail()))
    assert found["blueprint_loop"] == ["parent_group"]
    assert found["blueprint_config"] == ["config.source_table"]  # schema check skipped
    assert found["blueprint_output_mode"] == ["output_mode"]


def test_missing_schema_is_a_warning_and_old_versions_are_info() -> None:
    catalog = FakeCatalog(
        detail(config_schema=None, schema_error="ImportError: boom", latest=False),
        detail(version=2, class_path="company.blueprints.ExtractV2"),
    )
    report = validate_spec(spec_with(bp_node()), blueprints=catalog)
    by_code = {p.code: p for p in report.problems}
    assert report.ok
    assert by_code["blueprint_schema"].severity == "warning"
    assert "ImportError: boom" in by_code["blueprint_schema"].message
    assert by_code["blueprint_outdated"].severity == "info"


def test_xcom_to_a_blueprint_step_and_duplicate_ids() -> None:
    spec = spec_with(
        bp_node(),
        {
            "kind": "python_task",
            "id": "p",
            "task_id": "extract_customers",
            "function_name": "f",
            "code": "return x",
            "arguments": {"x": {"$type": "xcom", "node": "b"}},
        },
    )
    found = codes(spec, FakeCatalog(detail()))
    assert "xcom_blueprint" in found
    assert "duplicate_task_id" in found


def test_catalog_travels_with_the_operator_catalog() -> None:
    from autoair.catalog import OperatorCatalog

    operators = OperatorCatalog(sources=[], use_cache=False)
    operators.blueprints = FakeCatalog(detail())
    spec = spec_with(bp_node(version=9))
    report = validate_spec(spec, catalog=operators)
    assert any(p.code == "blueprint_unknown" for p in report.problems)


def test_one_bad_template_does_not_break_the_catalog(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "class Zero(Blueprint[C]):\n    version = 0\nclass Good(Blueprint[C]):\n    pass\n"
    )
    data = build_catalog(
        BlueprintSettings(entry_points=False, template_dirs=[str(tmp_path)]), read_schemas=False
    )
    # An invalid explicit version falls back to the class name (version 1).
    assert {(d.name, d.version) for d in data.blueprints} == {("zero", 1), ("good", 1)}


def test_switched_off_feature_refuses_blueprint_steps() -> None:
    catalog = FakeCatalog(detail())
    catalog.enabled = lambda: False
    assert codes(spec_with(bp_node()), catalog) == {"blueprint_disabled": ["kind"]}
