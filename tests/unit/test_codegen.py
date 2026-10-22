from __future__ import annotations

import ast

import pytest
from pydantic import BaseModel

from autoair.codegen import CodegenError, generate_source, render
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec

INJECTIONS = [
    '"); import os; os.system("id"); ("',
    "'''\nimport os\n'''",
    "\\\"; __import__('os')",
    "{{ ds }}\n\x00",
]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_strings_cannot_inject_code(simple_spec_data, payload):
    simple_spec_data["nodes"][1]["params"]["bash_command"] = payload
    simple_spec_data["description"] = payload
    simple_spec_data["tags"] = [payload]
    source = generate_source(DagSpec.model_validate(simple_spec_data))
    tree = ast.parse(source)
    # The only imports are the ones we generate, and the payload survives verbatim.
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert {getattr(n, "module", None) or n.names[0].name for n in imports} <= {
        "airflow.sdk",
        "airflow.providers.standard.operators.empty",
        "airflow.providers.standard.operators.bash",
        "datetime",
    }
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert payload in constants


def test_generated_code_is_deterministic(golden_spec_data):
    spec = DagSpec.model_validate(golden_spec_data)
    assert generate_source(spec) == generate_source(spec)


def test_python_task_syntax_error_is_reported(simple_spec_data):
    simple_spec_data["nodes"].append(
        {"kind": "python_task", "id": "p", "task_id": "p", "function_name": "p", "code": "def ("}
    )
    with pytest.raises(CodegenError) as exc:
        generate_source(DagSpec.model_validate(simple_spec_data))
    assert exc.value.node_id == "p"


def test_xcom_consumer_defined_after_producer():
    spec = DagSpec.model_validate(
        {
            "dag_id": "order",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "c",
                    "task_id": "consumer",
                    "function_name": "c",
                    "arguments": {"x": {"$type": "xcom", "node": "p"}},
                    "code": "return x",
                },
                {
                    "kind": "python_task",
                    "id": "p",
                    "task_id": "producer",
                    "function_name": "p",
                    "code": "return 1",
                },
            ],
        }
    )
    source = generate_source(spec)
    assert source.index("t_producer =") < source.index("t_consumer =")


def test_name_collisions_get_unique_variables():
    spec = DagSpec.model_validate(
        {
            "dag_id": "names",
            "nodes": [
                {"kind": "task_group", "id": "g", "group_id": "x"},
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
                    "task_id": "x-1",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                },
                {
                    "kind": "operator",
                    "id": "c",
                    "task_id": "x_1",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                },
            ],
        }
    )
    tree = ast.parse(generate_source(spec))
    targets = [
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    ]
    assert len(targets) == len(set(targets)) == 4


class _Cfg(BaseModel):
    url: str


class _HttpHandler:
    name = "http_get"
    title = "HTTP GET"
    description = "test"
    category = "network"
    config_model = _Cfg

    def build(self, node, config, ctx):
        cls = ctx.import_name("airflow.providers.standard.operators.bash", "BashOperator")
        return ast.Call(
            func=cls,
            args=[],
            keywords=[
                *ctx.task_kwargs(node),
                ast.keyword(arg="bash_command", value=ast.Constant(f"curl -fsS {config.url!r}")),
            ],
        )


def test_custom_handler_extension():
    registry = HandlerRegistry()
    registry.register(_HttpHandler())
    spec = DagSpec.model_validate(
        {
            "dag_id": "ext",
            "nodes": [
                {
                    "kind": "custom",
                    "id": "h",
                    "task_id": "fetch",
                    "handler": "http_get",
                    "config": {"url": "https://example.com"},
                }
            ],
        }
    )
    source = render(spec, registry, fmt=False)
    assert "BashOperator(task_id='fetch'" in source
    assert "registry" not in source


def test_registry_rejects_duplicates():
    registry = HandlerRegistry()
    registry.register(_HttpHandler())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_HttpHandler())


def _single_operator(operator: str, params: dict) -> DagSpec:
    return DagSpec.model_validate(
        {
            "dag_id": "td",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "operator": operator,
                    "params": params,
                }
            ],
        }
    )


def test_hitl_numeric_response_timeout_is_generated_as_timedelta():
    """HITLOperator does ``utcnow() + response_timeout``: a number fails at run time."""
    spec = _single_operator(
        "airflow.providers.standard.operators.hitl.HITLOperator",
        {"subject": "s", "options": ["ok"], "response_timeout": 3600},
    )
    source = generate_source(spec)
    assert "response_timeout=datetime.timedelta(seconds=3600)" in source
    assert "import datetime" in source
    ast.parse(source)


@pytest.mark.parametrize(
    ("operator", "param"),
    [
        ("airflow.providers.standard.operators.bash.BashOperator", "execution_timeout"),
        ("airflow.providers.standard.operators.bash.BashOperator", "sla"),
        ("airflow.providers.standard.sensors.external_task.ExternalTaskSensor", "execution_delta"),
    ],
)
def test_timedelta_only_operator_params_are_coerced(operator, param):
    source = generate_source(_single_operator(operator, {param: 90}))
    assert f"{param}=datetime.timedelta(seconds=90)" in source


@pytest.mark.parametrize(
    "param", ["poke_interval", "timeout", "retry_delay", "max_retry_delay", "response_timeout"]
)
def test_params_accepting_numbers_stay_numbers(param):
    """Sensors and BaseOperator accept ``timedelta | float`` here; non-HITL operators keep
    ``response_timeout`` as given."""
    source = generate_source(
        _single_operator("airflow.providers.standard.sensors.filesystem.FileSensor", {param: 30})
    )
    assert f"{param}=30" in source


def test_explicit_timedelta_response_timeout_is_unchanged():
    spec = _single_operator(
        "airflow.providers.standard.operators.hitl.HITLBranchOperator",
        {
            "subject": "s",
            "options": ["a"],
            "response_timeout": {"$type": "timedelta", "seconds": 60},
        },
    )
    assert "response_timeout=datetime.timedelta(seconds=60)" in generate_source(spec)


def test_yaml_dag_writes_a_timedelta_argument_as_a_duration_string():
    import yaml

    from autoair.codegen.yaml_dag import export_yaml_dag

    # Airflow's YAML importer parses duration strings for arguments typed timedelta.
    spec = _single_operator(
        "airflow.providers.standard.operators.hitl.HITLOperator",
        {"subject": "s", "options": ["ok"], "response_timeout": 3600},
    )
    result = export_yaml_dag(spec)
    assert result.lossless, result.issues
    assert yaml.safe_load(result.yaml)["tasks"][0]["with"]["response_timeout"] == "1h"
    # a fraction of a second has no duration string
    spec = _single_operator(
        "airflow.providers.standard.operators.hitl.HITLOperator",
        {"subject": "s", "options": ["ok"], "response_timeout": 0.5},
    )
    result = export_yaml_dag(spec)
    assert any("timedelta" in i.message and i.severity == "error" for i in result.issues)


def test_datetime_param_accepts_a_utc_z_suffix(simple_spec_data):
    # Browsers serialise dates with toISOString ("...Z"); Python 3.10's fromisoformat
    # rejects the suffix, so codegen must normalise it.
    simple_spec_data["nodes"][1]["params"]["env"] = {
        "WHEN": {"$type": "datetime", "value": "2024-01-02T03:04:05Z"}
    }
    source = generate_source(DagSpec.model_validate(simple_spec_data))
    assert "2024, 1, 2, 3, 4, 5" in source
