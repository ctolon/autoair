from __future__ import annotations

import pytest

from autoair._compat import airflow_version
from autoair.catalog import ModulesSource, OperatorCatalog
from autoair.spec import DagSpec
from autoair.validation import validate_spec

pytestmark = pytest.mark.airflow
BASH = "airflow.providers.standard.operators.bash.BashOperator"
APPROVAL = "airflow.providers.standard.operators.hitl.ApprovalOperator"
HAS_HITL = (airflow_version() or (0, 0, 0)) >= (3, 1, 0)  # HITL operators need Airflow 3.1


@pytest.fixture(scope="module")
def catalog() -> OperatorCatalog:
    return OperatorCatalog()


def test_discovers_standard_operators(catalog):
    fqns = {s.fqn for s in catalog.summaries()}
    assert BASH in fqns
    assert "airflow.providers.standard.sensors.filesystem.FileSensor" in fqns
    assert not any(s.class_name.startswith("Base") for s in catalog.summaries())


def test_bash_schema(catalog):
    detail = catalog.get(BASH)
    props = detail.params_schema["properties"]
    assert detail.params_schema["required"] == ["bash_command"]
    assert props["bash_command"]["x-autoair-templated"] is True
    assert "task_id" not in props
    assert "retries" not in props
    assert props["append_env"]["default"] is False


def test_callable_params_take_python_references(catalog):
    detail = catalog.get("airflow.providers.standard.operators.python.PythonOperator")
    assert detail.builder_supported is True
    assert detail.requires_python_code is True
    assert "python_callable" in detail.python_ref_params
    assert detail.params_schema["properties"]["python_callable"]["x-autoair-python-ref"] == (
        "required"
    )
    assert "python_callable" in detail.params_schema["required"]
    op = "airflow.providers.standard.operators.python.PythonOperator"
    ref = {"$type": "import", "path": "json.dumps"}
    assert catalog.validate_params(op, {"python_callable": ref}) == []
    missing = dict(catalog.validate_params(op, {}))
    assert "python_callable" in missing
    literal = dict(catalog.validate_params(op, {"python_callable": "json.dumps"}))
    assert "Python reference" in literal["python_callable"]


def test_param_validation(catalog):
    problems = dict(catalog.validate_params(BASH, {"bash_command": 1, "nope": 2}))
    assert set(problems) == {"bash_command", "nope"}
    assert catalog.validate_params(BASH, {"bash_command": "{{ ds }}"}) == []


def test_allow_deny_lists():
    cat = OperatorCatalog(deny=["*.bash.*"])
    assert BASH not in cat
    spec = DagSpec.model_validate(
        {
            "dag_id": "x",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "operator": BASH,
                    "params": {"bash_command": "ls"},
                }
            ],
        }
    )
    report = validate_spec(spec, catalog=cat)
    assert any("allowlist" in p.message for p in report.problems)


def test_custom_modules_source():
    cat = OperatorCatalog([ModulesSource(["airflow.providers.standard.operators.empty"])])
    assert [s.class_name for s in cat.summaries()] == ["EmptyOperator"]
    assert cat.summaries()[0].provider == "custom"


# --------------------------------------------------------------------------- static vs import

SQL = "airflow.providers.common.sql.operators.sql.SQLExecuteQueryOperator"
SMTP_NOTIFIER = "airflow.providers.smtp.notifications.smtp.SmtpNotifier"


@pytest.fixture(scope="module")
def import_catalog(tmp_path_factory) -> OperatorCatalog:
    return OperatorCatalog(strategy="import", cache_dir=tmp_path_factory.mktemp("cache"))


def test_static_is_default_and_matches_import_strategy(catalog, import_catalog):
    assert catalog.stats().strategy == "static"
    assert {s.fqn for s in catalog.summaries()} == {s.fqn for s in import_catalog.summaries()}
    for fqn in (BASH, SQL, "airflow.providers.standard.sensors.filesystem.FileSensor"):
        s, i = catalog.get(fqn), import_catalog.get(fqn)
        assert set(s.params_schema["properties"]) == set(i.params_schema["properties"])
        assert s.params_schema.get("required") == i.params_schema.get("required")
        assert s.builder_supported == i.builder_supported
        assert s.category == i.category
        assert s.ui_color == i.ui_color
        assert set(s.template_fields) == set(i.template_fields)


def test_bash_schema_static_parity(catalog, import_catalog):
    props = catalog.get(BASH).params_schema["properties"]
    iprops = import_catalog.get(BASH).params_schema["properties"]
    for name in ("bash_command", "append_env", "env", "output_encoding"):
        strip = {"description", "x-autoair-conn-type"}
        assert {k: v for k, v in props[name].items() if k not in strip} == {
            k: v for k, v in iprops[name].items() if k not in strip
        }


def test_detail_flags(catalog):
    fs = catalog.get("airflow.providers.standard.sensors.filesystem.FileSensor")
    assert fs.is_sensor
    assert fs.category == "sensor"
    assert fs.deferrable_param is True
    assert fs.connection_types == ["fs"]
    assert fs.params_schema["properties"]["fs_conn_id"]["x-autoair-conn-type"] == ["fs"]
    assert fs.required_airflow
    assert fs.required_airflow.startswith(">=")
    if HAS_HITL:
        assert catalog.get(APPROVAL).hitl
    else:  # module requires Airflow 3.1: must not be offered in the palette
        assert catalog.get(APPROVAL) is None
    assert not catalog.get(BASH).hitl


def test_generic_sql_operator_connection(catalog):
    detail = catalog.get(SQL)
    conn = detail.params_schema["properties"]["conn_id"]
    assert conn["x-autoair-widget"] == "connection"
    assert "x-autoair-conn-type" not in conn  # any DbApiHook connection
    assert detail.connection_types == []
    email = catalog.get("airflow.providers.smtp.operators.smtp.EmailOperator")
    assert email.connection_types == ["smtp"]


@pytest.mark.parametrize("strategy", ["static", "import"])
def test_notset_default_is_optional_and_supplied_kwargs_hidden(strategy, tmp_path):
    cat = OperatorCatalog(strategy=strategy, cache_dir=tmp_path)
    trigger = "airflow.providers.standard.operators.trigger_dagrun.TriggerDagRunOperator"
    if HAS_HITL:
        assert cat.validate_params(APPROVAL, {"subject": "x"}) == []
        assert "options" not in cat.get(APPROVAL).params_schema["properties"]
    assert cat.validate_params(trigger, {"trigger_dag_id": "x"}) == []
    assert cat.get(trigger).params_schema["required"] == ["trigger_dag_id"]
    assert cat.get(BASH).params_schema["required"] == ["bash_command"]


def test_notifiers(catalog):
    assert SMTP_NOTIFIER in {n.fqn for n in catalog.notifiers()}
    detail = catalog.get_notifier(SMTP_NOTIFIER)
    props = detail.params_schema["properties"]
    assert detail.params_schema["required"] == ["to"]
    assert "context" not in props
    assert props["subject"]["x-autoair-templated"] is True
    assert detail.connection_types == ["smtp"]
    assert catalog.validate_notifier_params(SMTP_NOTIFIER, {"to": "a@b.c"}) == []
    problems = dict(catalog.validate_notifier_params(SMTP_NOTIFIER, {"to": 1, "x": 2}))
    assert set(problems) == {"to", "x"}
    assert catalog.validate_notifier_params("nope.Notifier", {})[0][0] is None


def test_decorators_queue_schemes_connection_types(catalog):
    decorators = {d.name: d for d in catalog.decorators()}
    assert decorators["bash"].provider == "apache-airflow-providers-standard"
    assert {"python", "virtualenv", "sensor", "sql"} <= set(decorators)
    schemes = {q.scheme: q for q in catalog.queue_schemes()}
    assert {"sqs", "kafka", "redis+pubsub", "azure+servicebus", "google+pubsub"} <= set(schemes)
    assert schemes["sqs"].available is False  # amazon provider not installed
    conn_types = {c.conn_type: c for c in catalog.connection_types()}
    assert conn_types["smtp"].hook_class == "airflow.providers.smtp.hooks.smtp.SmtpHook"
    assert conn_types["smtp"].default_conn_name == "smtp_default"
    assert conn_types["fs"].provider == "apache-airflow-providers-standard"


def test_callable_operators_map_to_taskflow_equivalents():
    from autoair.catalog.service import TASKFLOW_EQUIVALENTS

    catalog = OperatorCatalog()
    by_name = {s.class_name: s for s in catalog.summaries()}
    for name, decorator in TASKFLOW_EQUIVALENTS.items():
        summary = by_name[name]
        assert summary.builder_supported is True, name  # via a Python reference
        assert summary.requires_python_code is True, name
        assert summary.python_ref_params == ["python_callable"], name
        assert summary.equivalent_decorator == decorator, name  # inline alternative
    # Every equivalent is a decorator the spec (and so the builder) supports.
    from typing import get_args

    from autoair.spec.models import PythonDecorator

    assert set(TASKFLOW_EQUIVALENTS.values()) <= set(get_args(PythonDecorator))


def test_refresh_forgets_failed_imports(tmp_path, monkeypatch):
    """A module that failed to import (missing optional dependency) is retried on refresh."""
    from autoair.catalog.service import _PROBE_CACHE

    pkg = tmp_path / "opt_ops"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "ops.py").write_text(
        "import optional_dep_xyz  # noqa\n"
        "from airflow.sdk import BaseOperator\n\n"
        "class OptOperator(BaseOperator):\n"
        "    def execute(self, context):\n        return 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    cat = OperatorCatalog([ModulesSource(["opt_ops.ops"])], use_cache=False)
    assert "opt_ops.ops.OptOperator" not in {s.fqn for s in cat.summaries()}
    (tmp_path / "optional_dep_xyz.py").write_text("")  # "pip install" the dependency
    assert "opt_ops.ops" in _PROBE_CACHE
    cat.refresh()
    assert "opt_ops.ops.OptOperator" in {s.fqn for s in cat.summaries()}


def test_required_params_must_not_be_empty(catalog: OperatorCatalog) -> None:
    """UX review: "fill with defaults" used to write "" and the DAG validated (and failed at
    runtime). An empty required string is a problem, not a value."""
    spec = DagSpec.model_validate(
        {
            "dag_id": "empty_required",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "b",
                    "task_id": "b",
                    "operator": "airflow.providers.standard.operators.bash.BashOperator",
                    "params": {"bash_command": "  "},
                }
            ],
        }
    )
    problems = validate_spec(spec, catalog=catalog).problems
    assert any(p.field == "bash_command" and "empty" in p.message for p in problems)
