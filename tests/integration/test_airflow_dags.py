"""Generated code and loader output must import cleanly in real Airflow and agree."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoair.codegen import render
from autoair.loader import build_dag, load_dag, render_stub
from autoair.spec import DagSpec
from autoair.validation import validate_spec
from autoair.validation.dagbag_check import check_file, check_source
from tests.conftest import GOLDEN_SPECS, ROOT

pytestmark = pytest.mark.airflow
EXPECTED = ROOT / "golden" / "expected"


def _shape(dag):
    return {
        t.task_id: (type(t).__name__, sorted(t.upstream_task_ids), getattr(t, "trigger_rule", None))
        for t in dag.tasks
    }


def _timetable(dag):
    tt = dag.timetable
    # attrs-based timetables have a value repr; others (e.g. NullTimetable) are stateless.
    return repr(tt) if type(tt).__repr__ is not object.__repr__ else type(tt).__name__


@pytest.mark.parametrize("path", GOLDEN_SPECS, ids=lambda p: p.name)
def test_golden_source(path: Path):
    spec = DagSpec.model_validate_json(path.read_text())
    # pinned version: golden files must not depend on the installed Airflow
    source = render(spec, airflow_version=(3, 3, 2))
    expected = EXPECTED / f"{spec.dag_id}.py"
    if os.environ.get("AUTOAIR_UPDATE_GOLDEN") or not expected.exists():
        expected.write_text(source)
    assert source == expected.read_text()


def _skip_if_newer(spec: DagSpec) -> None:
    from autoair._compat import airflow_version
    from autoair.codegen.features import required_features

    installed = airflow_version() or (0, 0, 0)
    if any(f.min_version > installed for f in required_features(spec)):
        pytest.skip("spec uses features newer than the installed Airflow")
    if "airflow.providers.common.ai" in spec.model_dump_json() or any(
        getattr(n, "decorator", None) in ("llm", "agent", "llm_branch", "llm_sql")
        for n in spec.nodes
    ):
        import importlib.util

        if importlib.util.find_spec("airflow.providers.common.ai") is None:
            pytest.skip("apache-airflow-providers-common-ai is not installed")


@pytest.mark.parametrize("path", GOLDEN_SPECS, ids=lambda p: p.name)
def test_generated_code_imports_in_dagbag(path: Path):
    spec = DagSpec.model_validate_json(path.read_text())
    _skip_if_newer(spec)
    result = check_source(render(spec), filename=f"{spec.dag_id}.py")
    assert result.ok, result.import_errors
    assert result.dag_ids == [spec.dag_id]


@pytest.mark.parametrize("path", GOLDEN_SPECS, ids=lambda p: p.name)
def test_loader_matches_codegen(path: Path, tmp_path: Path):
    spec = DagSpec.model_validate_json(path.read_text())
    _skip_if_newer(spec)
    dag_file = tmp_path / f"{spec.dag_id}.py"
    dag_file.write_text(render(spec))
    namespace: dict[str, object] = {}
    exec(compile(dag_file.read_text(), str(dag_file), "exec"), namespace)
    from_code = namespace["dag"]
    from_loader = build_dag(spec)
    assert _shape(from_code) == _shape(from_loader)
    assert _timetable(from_code) == _timetable(from_loader)
    assert from_code.start_date == from_loader.start_date


def test_loader_stub_imports_in_dagbag(tmp_path: Path, golden_spec_data):
    spec = DagSpec.model_validate(golden_spec_data | {"output_mode": "loader"})
    (tmp_path / f"{spec.dag_id}.autoair.json").write_text(spec.model_dump_json())
    stub = tmp_path / f"{spec.dag_id}.py"
    stub.write_text(render_stub(spec.dag_id))
    assert load_dag(stub).dag_id == spec.dag_id
    result = check_file(stub)
    assert result.ok, result.import_errors
    assert "extract.download" in result.task_ids[spec.dag_id]


def test_import_check_reports_runtime_errors(simple_spec_data):
    simple_spec_data["nodes"][1]["params"] = {"bash_command": "x", "no_such_kwarg": 1}
    report = validate_spec(DagSpec.model_validate(simple_spec_data), import_check=True)
    assert not report.ok
    assert any(p.code == "import" for p in report.problems)
