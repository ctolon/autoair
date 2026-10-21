"""Astronomer Blueprint steps against real Airflow and airflow-blueprint: catalog schemas in a
child process, generated code in DagBag, task previews in the worker pool, the Python importer
and the API routes."""

from __future__ import annotations

import sys
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.blueprints import catalog as catalog_module
from autoair.blueprints.catalog import BlueprintCatalog, build_catalog
from autoair.blueprints.config import BlueprintSettings
from autoair.blueprints.models import BlueprintPreviewRequest
from autoair.blueprints.preview import preview_step
from autoair.codegen import render
from autoair.importer.python_dag import import_python_file
from autoair.loader import build_dag
from autoair.settings import Settings
from autoair.spec import DagSpec
from autoair.validation import validate_spec
from autoair.validation.dagbag_check import check_source
from tests.conftest import ROOT

pytest.importorskip("blueprint")
pytestmark = pytest.mark.airflow

TEMPLATES = ROOT / "blueprint_templates"
MODULE = "tests.blueprint_templates.etl"
REPO = ROOT.parent
GOLDEN = ROOT / "golden" / "specs" / "blueprints.autoair.json"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory: pytest.TempPathFactory) -> BlueprintCatalog:
    settings = BlueprintSettings(entry_points=False, modules=[MODULE])
    return BlueprintCatalog(settings, cache_dir=tmp_path_factory.mktemp("bp-cache"))


def test_schemas_and_default_previews_come_from_a_child_process(
    catalog: BlueprintCatalog,
) -> None:
    before = set(sys.modules)
    data = catalog.data()
    assert data.errors == []
    by = {(d.name, d.version): d for d in data.blueprints}
    assert set(by) == {("extract", 1), ("extract", 2), ("load", 1), ("notify_team", 1)}
    extract = by[("extract", 1)]
    assert extract.class_path == f"{MODULE}.Extract"
    assert extract.config_class == "ExtractConfig"
    assert extract.config_schema is not None
    assert extract.config_schema["required"] == ["source_table"]
    assert extract.config_schema["properties"]["batch_size"]["minimum"] == 1
    assert extract.summary == "Extract rows from a source table."
    assert not extract.latest
    assert by[("extract", 2)].latest
    # Required fields: the defaults alone cannot render a preview.
    assert extract.preview is not None
    assert "config needed" in (extract.preview.error or "")
    notify = by[("notify_team", 1)]
    assert notify.preview is not None
    assert (notify.preview.kind, notify.preview.tasks) == ("group", ["compose", "send"])
    assert by[("load", 1)].config_schema["properties"]["mode"]["enum"] == ["append", "overwrite"]
    # Blueprint modules never run in this process (other tests may have imported it before).
    assert MODULE not in set(sys.modules) - before


def test_template_dir_modules_are_relative_and_importable(tmp_path: Path) -> None:
    data = build_catalog(BlueprintSettings(entry_points=False, template_dirs=[str(TEMPLATES)]))
    paths = {d.class_path for d in data.blueprints}
    assert "etl.Extract" in paths
    assert all(d.config_schema for d in data.blueprints), data.errors


def test_entry_point_packages_are_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = EntryPoint(name="test_bps", value="tests.blueprint_templates", group="x")
    monkeypatch.setattr(catalog_module, "entry_points", lambda group: [ep])
    data = build_catalog(BlueprintSettings(), read_schemas=False)
    assert {d.class_path for d in data.blueprints} >= {f"{MODULE}.Extract", f"{MODULE}.Load"}
    assert all(d.source == "entry_point" for d in data.blueprints)


def test_broken_modules_are_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text(
        "from blueprint import Blueprint, BaseModel\n"
        "raise RuntimeError('boom at import')\n"
        "class C(BaseModel):\n    x: int\n"
        "class Thing(Blueprint[C]):\n    pass\n"
    )
    data = build_catalog(BlueprintSettings(entry_points=False, template_dirs=[str(tmp_path)]))
    [thing] = data.blueprints
    assert thing.config_schema is None
    assert "boom at import" in (thing.schema_error or "")
    assert any("boom at import" in e.error for e in data.errors)


def test_generated_dag_imports_in_dagbag_and_matches_the_loader(catalog: BlueprintCatalog) -> None:
    spec = DagSpec.model_validate_json(GOLDEN.read_text())
    report = validate_spec(spec, blueprints=catalog)
    assert report.ok, report.problems
    result = check_source(render(spec), filename="blueprints.py")
    assert result.ok, result.import_errors
    assert result.task_ids["blueprints"] == sorted(
        [
            "start",
            "ingest.extract_customers.validate",
            "ingest.extract_customers.extract",
            "ingest.extract_orders.extract_orders",
            "ingest.extract_orders.extract_order_lines",
            "load",
            "notify.compose",
            "notify.send",
        ]
    )
    dag = build_dag(spec)
    upstream = {t.task_id: sorted(t.upstream_task_ids) for t in dag.tasks}
    assert upstream["ingest.extract_customers.extract"] == ["ingest.extract_customers.validate"]
    assert upstream["load"] == [
        "ingest.extract_customers.extract",
        "ingest.extract_orders.extract_order_lines",
        "ingest.extract_orders.extract_orders",
    ]
    assert upstream["notify.compose"] == ["load"]


def test_invalid_config_fails_validation_and_import(catalog: BlueprintCatalog) -> None:
    data = GOLDEN.read_text().replace('"batch_size": 500', '"batch_size": 0')
    spec = DagSpec.model_validate_json(data)
    report = validate_spec(spec, blueprints=catalog)
    assert not report.ok
    assert any(p.code == "blueprint_config" and p.node_id == "customers" for p in report.problems)
    # The DAG processor would refuse it too (Pydantic validates at parse time).
    result = check_source(render(spec), filename="blueprints.py")
    assert not result.ok
    assert "batch_size" in " ".join(result.import_errors)


def test_preview_renders_the_current_config(catalog: BlueprintCatalog) -> None:
    def preview(**body: Any) -> Any:
        return preview_step(catalog, BlueprintPreviewRequest(**body), timeout=60)

    group = preview(name="extract", version=2, step_id="ex", config={"tables": ["a", "b"]})
    assert (group.kind, group.tasks, group.error) == ("group", ["extract_a", "extract_b"], None)
    task = preview(name="load", version=1, step_id="ld", config={"target_table": "t"})
    assert (task.kind, task.tasks) == ("task", ["ld"])
    invalid = preview(name="extract", version=2, config={"tables": []})
    assert invalid.error
    assert not invalid.tasks
    missing = preview(name="nope", version=1)
    assert "not in the catalog" in (missing.error or "")
    tagged = preview(
        name="load", version=1, config={"target_table": {"$type": "xcom", "node": "x"}}
    )
    assert tagged.error


def test_preview_reports_an_unavailable_validator(
    catalog: BlueprintCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validator.url set and the service down (fail_mode open/closed): a preview error, not
    an HTTP 500."""
    from autoair.validation import dagbag_check
    from autoair.validation.dagbag_check import ImportCheckUnavailable

    def unavailable(*_a: Any, **_k: Any) -> Any:
        raise ImportCheckUnavailable("not reachable: ConnectError", "closed")

    monkeypatch.setattr(dagbag_check, "check_source", unavailable)
    request = BlueprintPreviewRequest(
        name="load", version=1, step_id="ld", config={"target_table": "t"}
    )
    result = preview_step(catalog, request, timeout=60)
    assert result.tasks == []
    assert "import check service is unavailable" in (result.error or "")


def test_python_importer_folds_blueprint_steps(tmp_path: Path) -> None:
    spec = DagSpec.model_validate_json(GOLDEN.read_text())
    dag_file = tmp_path / "blueprints.py"
    dag_file.write_text(render(spec))
    result = import_python_file(dag_file, pythonpath=[REPO])
    assert result.import_errors == []
    [imported] = result.dags
    assert imported.spec is not None, imported.error
    nodes = {
        getattr(n, "step_id", None) or getattr(n, "task_id", None) or n.group_id: n
        for n in imported.spec.nodes
    }  # type: ignore[union-attr]
    customers = nodes["extract_customers"]
    assert customers.kind == "blueprint"
    assert (customers.blueprint, customers.version) == ("extract", 1)
    assert customers.class_path == f"{MODULE}.Extract"
    assert customers.config_class == "ExtractConfig"
    assert customers.config == {"source_table": "raw.customers", "batch_size": 500}
    assert customers.parent_group == nodes["ingest"].id
    orders = nodes["extract_orders"]
    assert (orders.version, orders.config) == (2, {"tables": ["orders", "order_lines"]})
    assert nodes["notify"].config == {}
    # No task of a step survives as a separate node; dependencies attach to the steps.
    assert {n.kind for n in imported.spec.nodes} == {"operator", "task_group", "blueprint"}
    assert len(imported.spec.nodes) == 6
    pairs = {(e.source, e.target) for e in imported.spec.edges}
    ids = {k: n.id for k, n in nodes.items()}
    assert (ids["start"], ids["extract_customers"]) in pairs
    assert (ids["extract_orders"], ids["load"]) in pairs
    assert (ids["load"], ids["notify"]) in pairs
    # The imported spec generates the same DAG again.
    again = check_source(render(imported.spec), filename="again.py")
    assert again.ok, again.import_errors
    assert len(again.task_ids["blueprints"]) == 8


# --------------------------------------------------------------------------- API


def _client(tmp_path: Path, **features: bool) -> TestClient:
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        import_check_on_deploy=False,
        features=features,
        blueprints=BlueprintSettings(entry_points=False, modules=[MODULE]),
    )
    services = build_services(settings, AuthBackend())
    services.catalog.refresh = lambda: services.catalog.stats()  # type: ignore[method-assign]
    return TestClient(create_app(services))


def test_api_lists_describes_and_previews(tmp_path: Path) -> None:
    client = _client(tmp_path)
    listed = client.get("/api/blueprints")
    assert listed.status_code == 200
    assert {(b["name"], b["version"]) for b in listed.json()} >= {("extract", 1), ("load", 1)}
    assert "config_schema" not in listed.json()[0]
    detail = client.get("/api/blueprints/extract/2").json()
    assert detail["config_schema"]["required"] == ["tables"]
    assert client.get("/api/blueprints/extract/7").status_code == 404
    assert client.get("/api/blueprints/Bad-Name/1").status_code == 422
    status = client.get("/api/blueprints/status").json()
    assert status["available"] is True
    assert status["blueprints"] == 4
    preview = client.post(
        "/api/blueprints/preview",
        json={"name": "load", "version": 1, "step_id": "ld", "config": {"target_table": "t"}},
    )
    assert preview.json() == {"kind": "task", "tasks": ["ld"], "error": None}
    assert client.post("/api/blueprints/refresh").json()["blueprints"] == 4
    meta = client.get("/api/meta").json()
    assert meta["features"]["blueprints"] is True


def test_api_is_gated_by_the_blueprints_feature(tmp_path: Path) -> None:
    client = _client(tmp_path, blueprints=False)
    assert client.get("/api/blueprints").status_code == 404
    assert (
        client.post("/api/blueprints/preview", json={"name": "a", "version": 1}).status_code == 404
    )
    assert client.post("/api/blueprints/refresh").status_code == 404


def test_validate_route_checks_blueprint_steps(tmp_path: Path) -> None:
    client = _client(tmp_path)
    spec = GOLDEN.read_text().replace('"version": 2', '"version": 5')
    report = client.post(
        "/api/validate", content=spec, headers={"content-type": "application/json"}
    )
    assert report.status_code == 200
    problems = report.json()["problems"]
    assert any(p["code"] == "blueprint_unknown" and p["node_id"] == "orders" for p in problems)


def test_templates_defined_in_the_dag_file_stay_tasks(tmp_path: Path) -> None:
    dag_file = tmp_path / "inline.py"
    dag_file.write_text(
        "from airflow.sdk import DAG\n"
        "from airflow.providers.standard.operators.empty import EmptyOperator\n"
        "from blueprint import BaseModel, Blueprint\n"
        "class Cfg(BaseModel):\n    x: int = 1\n"
        "class Inline(Blueprint[Cfg]):\n"
        "    def render(self, config):\n        return EmptyOperator(task_id=self.step_id)\n"
        "with DAG(dag_id='inline', schedule=None) as dag:\n"
        "    b = Inline()\n    b.step_id = 'step'\n    b.render(Cfg())\n"
    )
    result = import_python_file(dag_file)
    [imported] = result.dags
    assert imported.spec is not None, imported.error
    assert [n.kind for n in imported.spec.nodes] == ["operator"]
    assert any("not importable" in w for w in imported.warnings)


def test_preview_needs_code_execute_when_import_checks_do(tmp_path: Path) -> None:
    import anyio

    from autoair.api.routers import blueprints as router
    from autoair.iam.model import Principal

    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        blueprints=BlueprintSettings(entry_points=False, modules=[MODULE]),
    )
    services = build_services(settings, AuthBackend())
    body = BlueprintPreviewRequest(name="load", version=1, config={"target_table": "t"})
    out = anyio.run(router.preview_blueprint, body, services, Principal(user="u"))
    assert "code:execute" in (out.error or "")
    assert not out.tasks
