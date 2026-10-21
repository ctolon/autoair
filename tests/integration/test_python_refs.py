"""Python references ({"$type": "import"}) make every operator configurable: callables
(python_callable, producer_function…) and classes are imported by the generated DAG."""

from __future__ import annotations

import textwrap

import pytest

from autoair import runner
from autoair.catalog import OperatorCatalog
from autoair.catalog.discovery import PathsSource
from autoair.catalog.service import ProvidersSource
from autoair.codegen import render
from autoair.codegen.yaml_dag import export_yaml_dag
from autoair.spec import DagSpec
from autoair.validation import Policy, validate_spec
from autoair.validation.dagbag_check import check_source

pytestmark = pytest.mark.airflow
PYOP = "airflow.providers.standard.operators.python.PythonOperator"
BRANCH = "airflow.providers.standard.operators.python.BranchPythonOperator"
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory) -> OperatorCatalog:
    root = tmp_path_factory.mktemp("refs")
    (root / "company_funcs.py").write_text(
        textwrap.dedent(
            """
            def greet(name, punctuation="!"):
                print(f"hello {name}{punctuation}")
                return f"hello {name}{punctuation}"


            def choose(**context):
                return "left"


            RETRIES = 3
            """
        )
    )
    # PathsSource registers the folder as an import root (here and in subprocesses).
    return OperatorCatalog([ProvidersSource(scan_packages=False), PathsSource([root])])


def spec_with(callable_path: str, operator: str = PYOP, **params: object) -> DagSpec:
    return DagSpec.model_validate(
        {
            "dag_id": "refs",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "greet",
                    "task_id": "greet",
                    "operator": operator,
                    "params": {
                        "python_callable": {"$type": "import", "path": callable_path},
                        **params,
                    },
                },
                {"kind": "operator", "id": "left", "task_id": "left", "operator": EMPTY},
                {"kind": "operator", "id": "right", "task_id": "right", "operator": EMPTY},
            ],
            "edges": [
                {"source": "greet", "target": "left"},
                {"source": "greet", "target": "right"},
            ],
        }
    )


def test_reference_generates_import_validates_and_runs(catalog):
    spec = spec_with("company_funcs.greet", op_kwargs={"name": "airflow"})
    report = validate_spec(spec, catalog=catalog)
    assert report.ok, report.problems
    source = render(spec)
    assert "from company_funcs import greet" in source
    assert "python_callable=greet" in source
    check = check_source(source, "refs.py")
    assert check.ok, check.import_errors
    result = runner.run_task_test(source, "refs", "greet")
    assert result.state == "success", result.error or result.log
    assert result.return_value == "hello airflow!"
    assert "hello airflow!" in result.log


def test_branch_operator_with_reference(catalog):
    spec = spec_with("company_funcs.choose", operator=BRANCH)
    assert validate_spec(spec, catalog=catalog).ok
    result = runner.run_task_test(render(spec), "refs", "greet")
    assert result.state == "success", result.error or result.log
    assert result.skipped_downstream == ["right"]


def test_reference_problems(catalog):
    def problems(spec: DagSpec, **policy: object) -> dict[str, str]:
        report = validate_spec(spec, catalog=catalog, policy=Policy(**policy))  # type: ignore[arg-type]
        return {p.code: f"{p.severity}: {p.message}" for p in report.problems}

    off = problems(spec_with("company_funcs.greet"), allow_python_code=False)
    assert off["policy"].startswith("error")
    assert "allow_python_code" in off["policy"]
    denied = problems(spec_with("company_funcs.greet"), python_ref_allow=("acme.*",))
    assert "not allowed by policy" in denied["policy"]
    missing = problems(spec_with("company_funcs.nope"))
    assert missing["python_ref"].startswith("warning")
    assert "does not define 'nope'" in missing["python_ref"]
    no_module = problems(spec_with("nowhere_pkg.mod.fn"))
    assert "cannot find module" in no_module["python_ref"]
    bad = problems(spec_with("os.system; import x"))
    assert "invalid Python reference" in bad["python_ref"]
    literal = DagSpec.model_validate(
        {
            "dag_id": "refs",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "g",
                    "task_id": "g",
                    "operator": PYOP,
                    "params": {"python_callable": "company_funcs.greet"},
                }
            ],
        }
    )
    assert "Python reference" in problems(literal)["param"]


def test_loader_mode_and_yaml_export(catalog):
    spec = spec_with("company_funcs.greet", op_kwargs={"name": "loader"})
    spec = spec.model_copy(update={"output_mode": "loader"})
    from autoair.loader import build_dag

    dag = build_dag(spec)
    op = dag.get_task("greet")
    assert op.python_callable.__name__ == "greet"
    export = export_yaml_dag(spec)
    assert not export.lossless
    assert any("Python reference" in i.message for i in export.issues)


def test_references_work_for_any_parameter(catalog):
    # Not only callables: e.g. a module-level constant for an int parameter.
    spec = spec_with("company_funcs.greet", op_kwargs={"name": "x"})
    spec.nodes[0].retries = None  # type: ignore[union-attr]
    spec.nodes[0].params["show_return_value_in_logs"] = {  # type: ignore[union-attr]
        "$type": "import",
        "path": "company_funcs.RETRIES",
    }
    assert validate_spec(spec, catalog=catalog).ok
    assert "from company_funcs import RETRIES, greet" in render(spec)


def test_whole_dag_test_with_operator_branch(catalog):
    spec = spec_with("company_funcs.choose", operator=BRANCH)
    result = runner.run_dag_test(render(spec), "refs")
    states = {t.task_id: t.state for t in result.tasks}
    assert states == {"greet": "success", "left": "success", "right": "skipped"}, result
