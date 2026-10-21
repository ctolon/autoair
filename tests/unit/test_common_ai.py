"""common.ai provider tasks: structured outputs, toolsets and the @task.llm/agent decorators."""

from __future__ import annotations

import ast
import json

import pytest
from pydantic import ValidationError

from autoair.codegen import render
from autoair.codegen.dagfactory import export_dag_factory
from autoair.codegen.yaml_dag import export_yaml_dag
from autoair.spec import DagSpec, OutputModel, ToolsetSpec
from autoair.validation import validate_spec
from tests.conftest import ROOT

LLM = "airflow.providers.common.ai.operators.llm.LLMOperator"


def _spec(params: dict, **node) -> DagSpec:
    return DagSpec.model_validate(
        {
            "dag_id": "ai",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "operator": LLM,
                    "params": {"llm_conn_id": "llm", "prompt": "hi", **params},
                    **node,
                }
            ],
        }
    )


def _model(**extra) -> dict:
    return {
        "$type": "output_model",
        "name": "Answer",
        "fields": [{"name": "text"}, {"name": "n", "type": "int", "required": False}],
        **extra,
    }


def test_output_model_validation():
    OutputModel.model_validate(_model())
    with pytest.raises(ValidationError, match="duplicate"):
        OutputModel.model_validate(_model(fields=[{"name": "a"}, {"name": "a"}]))
    with pytest.raises(ValidationError, match="choices"):
        OutputModel.model_validate(_model(fields=[{"name": "a", "type": "int", "choices": ["x"]}]))
    with pytest.raises(ValidationError):
        OutputModel.model_validate(_model(name="lower"))
    with pytest.raises(ValidationError):  # field types are a closed set
        OutputModel.model_validate(_model(fields=[{"name": "a", "type": "__import__('os')"}]))


def test_toolset_options_per_kind():
    ToolsetSpec.model_validate({"$type": "toolset", "kind": "sql", "conn_id": "db", "max_rows": 5})
    with pytest.raises(ValidationError, match="sql options"):
        ToolsetSpec.model_validate(
            {"$type": "toolset", "kind": "mcp", "conn_id": "m", "max_rows": 5}
        )
    with pytest.raises(ValidationError, match="mcp option"):
        ToolsetSpec.model_validate(
            {"$type": "toolset", "kind": "sql", "conn_id": "db", "tool_prefix": "x"}
        )


def test_output_model_codegen_is_constant_only_and_deduplicated():
    evil = 'x"""\nimport os\nos.system("rm -rf /")\n"""'
    model = _model(description=evil)
    model["fields"][0]["choices"] = [evil, "ok"]
    spec = _spec({"output_type": model, "system_prompt": "s"})
    spec.nodes.append(
        spec.nodes[0].model_copy(update={"id": "b", "task_id": "b", "params": spec.nodes[0].params})
    )
    source = render(spec)
    tree = ast.parse(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert [c.name for c in classes] == ["Answer"]  # one class shared by both tasks
    assert "import os" not in [ast.unparse(n) for n in tree.body]
    assert not any(
        isinstance(n, ast.Call) and ast.unparse(n.func) == "os.system" for n in ast.walk(tree)
    )
    assert "output_type=Answer" in source
    assert "n: int | None = Field(default=None)" in source


def test_toolsets_codegen():
    spec = _spec(
        {
            "toolsets": [
                {"$type": "toolset", "kind": "sql", "conn_id": "db", "allow_writes": True},
                {"$type": "toolset", "kind": "mcp", "conn_id": "m"},
            ]
        }
    )
    source = render(spec)
    assert "from airflow.providers.common.ai.toolsets.sql import SQLToolset" in source
    assert "SQLToolset(db_conn_id='db', allow_writes=True)" in source.replace('"', "'")
    assert "MCPToolset(mcp_conn_id='m')" in source.replace('"', "'")


def test_validation_reports_llm_problems():
    bad = _spec({"output_type": _model(fields=[])})
    report = validate_spec(bad)
    assert any(p.code == "output_model" and p.field == "output_type" for p in report.problems)
    writes = _spec(
        {"toolsets": [{"$type": "toolset", "kind": "sql", "conn_id": "db", "allow_writes": True}]}
    )
    warn = [p for p in validate_spec(writes).problems if p.code == "toolset"]
    assert warn
    assert warn[0].severity == "warning"
    deco = DagSpec.model_validate(
        {
            "dag_id": "ai",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "t",
                    "task_id": "t",
                    "function_name": "t",
                    "decorator": "llm",
                    "code": "return 'prompt'",
                }
            ],
        }
    )
    assert any(
        p.code == "decorator" and "llm_conn_id" in p.message for p in validate_spec(deco).problems
    )


def test_exports_refuse_python_objects():
    spec = _spec({"output_type": _model()})
    assert any(i.severity == "error" for i in export_yaml_dag(spec).issues)
    assert any("output_model" in w for w in export_dag_factory(spec).warnings)


def test_environment_mapping_rewrites_toolset_connections():
    data = json.loads((ROOT / "golden" / "specs" / "common_ai.autoair.json").read_text())
    data["environments"] = {"prod": {"connections": {"warehouse": "prod_wh"}}}
    spec = DagSpec.model_validate(data).with_environment("prod")
    toolsets = spec.node_map()["investigate"].params["toolsets"]  # type: ignore[union-attr]
    assert toolsets[0]["conn_id"] == "prod_wh"
