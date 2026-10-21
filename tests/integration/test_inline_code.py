"""Functions and scripts written in the builder: generated, imported by Airflow and run."""

from __future__ import annotations

import pytest

from autoair import runner
from autoair.catalog import OperatorCatalog
from autoair.codegen import render
from autoair.codegen.context import CodegenError
from autoair.codegen.yaml_dag import export_yaml_dag
from autoair.spec import DagSpec
from autoair.validation import Policy, validate_spec
from autoair.validation.dagbag_check import check_source

pytestmark = pytest.mark.airflow
PYOP = "airflow.providers.standard.operators.python.PythonOperator"
BASH = "airflow.providers.standard.operators.bash.BashOperator"

GREET = '''import json


def greet(name, **context):
    """Say hello."""
    return json.dumps({"hello": name})
'''
SCRIPT = '#!/usr/bin/env bash\nset -euo pipefail\necho "rows: $((40 + 2))"\n'
PYFILE = "import sys\nprint('job args', sys.argv[1:])\n"


def spec(*nodes: dict, edges: list | None = None) -> DagSpec:
    return DagSpec.model_validate({"dag_id": "inline", "nodes": list(nodes), "edges": edges or []})


def op(node_id: str, operator: str, **params: object) -> dict:
    return {
        "kind": "operator",
        "id": node_id,
        "task_id": node_id,
        "operator": operator,
        "params": params,
    }


@pytest.fixture(scope="module")
def catalog() -> OperatorCatalog:
    return OperatorCatalog()


def test_inline_function_runs(catalog):
    s = spec(
        op(
            "greet",
            PYOP,
            python_callable={"$type": "function", "source": GREET},
            op_kwargs={"name": "airflow"},
        ),
        op("again", PYOP, python_callable={"$type": "function", "source": GREET}),
    )
    assert validate_spec(s, catalog=catalog).ok, validate_spec(s, catalog=catalog).problems
    source = render(s)
    assert source.count("def greet(") == 1  # identical functions are emitted once
    assert source.index("def greet(") < source.index("with DAG(")
    assert "python_callable=greet" in source
    assert check_source(source, "inline.py").ok
    result = runner.run_task_test(source, "inline", "greet")
    assert result.state == "success", result.error or result.log
    assert result.return_value == '{"hello": "airflow"}'


def test_script_files_are_written_and_run(catalog):
    s = spec(
        op(
            "sh",
            BASH,
            bash_command={
                "$type": "file",
                "name": "load.sh",
                "content": SCRIPT,
                "run_with": "bash",
            },
        ),
        op(
            "path",
            PYOP,
            python_callable={
                "$type": "function",
                "source": "def read(path):\n    return open(path).read()\n",
            },
            op_kwargs={"path": {"$type": "file", "name": "job.py", "content": PYFILE}},
        ),
    )
    assert validate_spec(s, catalog=catalog).ok
    source = render(s)
    assert "def _script_file(" in source
    assert check_source(source, "inline.py").ok
    sh = runner.run_task_test(source, "inline", "sh")
    assert sh.state == "success", sh.error or sh.log
    assert "rows: 42" in sh.log
    path = runner.run_task_test(source, "inline", "path")
    assert path.state == "success", path.error or path.log
    assert path.return_value == PYFILE


def test_policy_and_errors(catalog):
    s = spec(op("greet", PYOP, python_callable={"$type": "function", "source": GREET}))
    codes = {p.code for p in validate_spec(s, policy=Policy(allow_python_code=False)).problems}
    assert "policy" in codes
    bad = spec(op("g", PYOP, python_callable={"$type": "function", "source": "x = 1\n"}))
    assert any(p.code == "python_function" for p in validate_spec(bad).problems)
    with pytest.raises(CodegenError, match="exactly one"):
        render(spec(op("g", PYOP, python_callable={"$type": "function", "source": "import os\n"})))
    evil = spec(op("f", BASH, bash_command={"$type": "file", "name": "../x.sh", "content": ""}))
    assert any(p.code == "script_file" for p in validate_spec(evil).problems)
    assert not export_yaml_dag(s).lossless
