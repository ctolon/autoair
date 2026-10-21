"""Pasted (indented) @task bodies are dedented; ShellCheck findings become warnings."""

from __future__ import annotations

import pytest

from autoair import sections
from autoair.codegen import render
from autoair.codegen.body import normalize_task_body
from autoair.lsp.shell import shellcheck_path
from autoair.registry import default_registry
from autoair.spec import DagSpec
from autoair.validation import validate_spec

BASH = "airflow.providers.standard.operators.bash.BashOperator"


@pytest.fixture(autouse=True)
def _reset_sections():
    yield
    sections.reset()


def spec(*nodes: dict) -> DagSpec:
    return DagSpec.model_validate({"dag_id": "d", "nodes": list(nodes)})


def py(code: str, **kw: object) -> dict:
    return {
        "kind": "python_task",
        "id": "t",
        "task_id": "t",
        "function_name": "f",
        "code": code,
        **kw,
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("    x = 1\n    return x\n", "x = 1\nreturn x\n"),
        ("\tx = 1\n\n\tif x:\n\t\treturn x", "x = 1\n\nif x:\n\treturn x"),
        ("x = 1\n    return x", "x = 1\nreturn x"),  # selection began after the indentation
        ("x = 1\nreturn x", "x = 1\nreturn x"),
        ("", ""),
    ],
)
def test_normalize_task_body(code, expected):
    assert normalize_task_body(code) == expected


def test_invalid_body_stays_invalid():
    assert normalize_task_body("x = (\n") == "x = (\n"
    assert normalize_task_body("if x:\nreturn 1") == "if x:\nreturn 1"


def test_indented_body_validates_and_generates():
    s = spec(py("    import json\n    return json.dumps({})\n"))
    report = validate_spec(s)
    assert not [p for p in report.problems if p.code in {"syntax", "codegen"}]
    source = render(s, default_registry(), fmt=False)
    assert "    return json.dumps({})" in source


def test_syntax_error_reported_once():
    report = validate_spec(spec(py("x = (\n")))
    codes = [p.code for p in report.problems if p.severity == "error"]
    assert codes == ["syntax"]


needs_shellcheck = pytest.mark.skipif(shellcheck_path() is None, reason="shellcheck missing")


def shellcheck(report) -> list:
    return [p for p in report.problems if p.code.startswith("shellcheck:")]


@needs_shellcheck
def test_bash_command_findings_are_warnings_on_the_field():
    node = {
        "kind": "operator",
        "id": "b",
        "task_id": "b",
        "operator": BASH,
        "params": {"bash_command": "cd {{ params.dir }}\nrm -rf $X/*"},
    }
    report = validate_spec(spec(node))
    found = shellcheck(report)
    assert found
    assert report.ok
    assert {p.field for p in found} == {"bash_command"}
    assert {p.node_id for p in found} == {"b"}
    assert all(p.severity in {"warning", "info"} for p in found)
    sc2164 = next(p for p in found if p.code == "shellcheck:SC2164")
    assert sc2164.line == 1
    assert sc2164.doc_url
    assert sc2164.doc_url.endswith("SC2164")


@needs_shellcheck
def test_script_path_bash_command_is_not_linted():
    node = {
        "kind": "operator",
        "id": "b",
        "task_id": "b",
        "operator": BASH,
        "params": {"bash_command": "scripts/run.sh"},
    }
    assert shellcheck(validate_spec(spec(node))) == []


@needs_shellcheck
def test_sh_file_asset_and_task_bash_are_linted():
    asset = {"$type": "file", "name": "load.sh", "content": "#!/bin/bash\ncd /tmp\necho ok\n"}
    op = {
        "kind": "operator",
        "id": "b",
        "task_id": "b",
        "operator": BASH,
        "params": {"env": {"SCRIPT": asset}},
    }
    task = py('x = 1\nreturn "cd /data\\nls"', decorator="bash", id="c", task_id="c")
    found = shellcheck(validate_spec(spec(op, task)))
    fields = {(p.node_id, p.field) for p in found}
    assert ("b", "env.SCRIPT") in fields
    on_task = [p for p in found if p.node_id == "c"]
    assert on_task
    assert on_task[0].field == "code"
    assert on_task[0].line == 2
    assert on_task[0].column is None


@needs_shellcheck
def test_shellcheck_in_validation_can_be_switched_off():
    from autoair.sections import _ACTIVE, EditorSettings

    _ACTIVE[EditorSettings] = EditorSettings(shellcheck_in_validation=False)
    node = {
        "kind": "operator",
        "id": "b",
        "task_id": "b",
        "operator": BASH,
        "params": {"bash_command": "cd /x"},
    }
    assert shellcheck(validate_spec(spec(node))) == []
