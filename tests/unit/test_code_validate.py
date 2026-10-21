"""POST /api/lsp/validate checkers (autoair.lsp.validate) with graceful fallbacks."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from autoair import sections
from autoair.lsp import validate as v
from autoair.lsp.shell import shellcheck_path
from autoair.lsp.validate import ValidateRequest, validate_code


@pytest.fixture(autouse=True)
def _reset_sections():
    yield
    sections.reset()


def run(language: str, code: str, **kw: object) -> v.ValidateResult:
    return validate_code(ValidateRequest(language=language, code=code, **kw))  # type: ignore[arg-type]


def tools(result: v.ValidateResult) -> dict[str, bool]:
    return {t.name: t.ran for t in result.tools}


def test_python_syntax_error_is_located_and_skips_ruff():
    r = run("python", "x = (\n")
    assert not r.ok
    assert r.validated
    assert tools(r) == {"ast": True}
    assert r.diagnostics[0].line == 1
    assert r.diagnostics[0].source == "ast"


def test_python_ruff_findings_map_back_to_task_body_lines():
    r = run("python", "    x = 1\n    return y\n", python_mode="task_body")
    by_code = {d.code: d for d in r.diagnostics}
    assert by_code["F821"].line == 2
    assert by_code["F821"].severity == "error"
    assert by_code["F821"].column == 8
    assert by_code["F841"].severity == "warning"


def test_python_clean_module():
    r = run("python", "import json\n\nprint(json.dumps({}))\n")
    assert r.ok
    assert r.diagnostics == []


def test_json_strict_with_line_and_column():
    r = run("json", '{\n  "a": 1,\n}')
    assert not r.ok
    # Python <= 3.12 points at the closing brace; 3.13+ reports the trailing comma itself
    # ("Illegal trailing comma before end of object").
    assert (r.diagnostics[0].line, r.diagnostics[0].column) in {(3, 1), (2, 9)}
    assert run("json", "").ok
    assert run("json", '{"a": [1, 2]}').ok


def test_yaml_error_is_located():
    r = run("yaml", "a: [1\nb: 2\n")
    assert not r.ok
    assert r.diagnostics[0].line == 2
    assert run("yaml", "a: 1\n---\nb: [1, 2]\n").ok


def test_markdown_unclosed_fence_is_a_warning():
    r = run("markdown", "# Doc\n\n```python\nx = 1\n")
    assert r.ok
    assert r.diagnostics[0].code == "unclosed-fence"
    assert r.diagnostics[0].line == 3
    assert run("markdown", "```\nx\n```\n").diagnostics == []


def test_language_without_validator():
    r = run("text", "anything")
    assert r.ok
    assert not r.validated
    assert r.tools[0].available is False


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_bash_syntax_error_from_bash_n_clamped_to_document():
    r = run("bash", "if true; then\n  echo hi\n")
    hits = [d for d in r.diagnostics if d.source == "bash -n"]
    assert hits
    assert hits[0].line == 2
    assert not r.ok


@pytest.mark.skipif(shellcheck_path() is None, reason="shellcheck missing")
def test_bash_shellcheck_findings_and_jinja_is_masked():
    r = run("bash", "cd {{ params.dir }}\necho $1\n")
    codes = {d.code for d in r.diagnostics}
    assert "SC2164" in codes
    assert tools(r)["shellcheck"]
    assert all(d.source != "bash -n" for d in r.diagnostics)


@pytest.mark.skipif(shutil.which("gofmt") is None, reason="gofmt missing")
def test_go_gofmt_errors_are_deduplicated():
    r = run("go", "package main\nfunc main() {\n x := \n}\n")
    assert not r.ok
    assert len({(d.line, d.column, d.message) for d in r.diagnostics}) == len(r.diagnostics)
    assert run("go", "package main\n\nfunc main() {}\n").ok


@pytest.mark.skipif(shutil.which("javac") is None, reason="javac missing")
def test_java_is_syntax_only():
    bad = run("java", "public class Job {\n  void f() { int x = foo(; }\n}\n")
    assert not bad.ok
    assert bad.diagnostics[0].line == 2
    assert bad.diagnostics[0].column
    # Unknown symbols are not syntax errors.
    assert run("java", "public class Job { void f() { Unknown.call(); } }").ok


def test_js_ts_tools_missing_are_reported(monkeypatch):
    monkeypatch.setattr(v, "find_tool", lambda name: None)
    r = run("typescript", "const x: number = 1;")
    assert r.ok
    assert not r.validated
    assert {t.name for t in r.tools} == {"tsc", "esbuild"}
    assert all(not t.available for t in r.tools)


def test_sql_without_sqlglot_says_so(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.startswith("sqlglot"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    r = run("sql", "SELECT 1")
    assert r.tools[0].name == "sqlglot"
    assert not r.tools[0].available


def test_timeout_is_reported_not_raised(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gofmt", timeout=1)

    monkeypatch.setattr(v, "find_tool", lambda name: ["gofmt"])
    monkeypatch.setattr(v.subprocess, "run", slow)
    r = run("go", "package main")
    assert r.tools[0].ran is False
    assert "timed out" in (r.tools[0].note or "")


def test_subprocess_env_is_scrubbed(monkeypatch):
    seen: dict[str, object] = {}

    def fake(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("AUTOAIR_STORAGE__GIT__TOKEN", "secret")
    monkeypatch.setattr(v, "find_tool", lambda name: ["gofmt"])
    monkeypatch.setattr(v.subprocess, "run", fake)
    run("go", "package main")
    env = seen["env"]
    assert isinstance(env, dict)
    assert "AUTOAIR_STORAGE__GIT__TOKEN" not in env
    assert seen["timeout"] == sections.active(sections.EditorSettings).validate_timeout


def test_configured_command_overrides_path(monkeypatch, tmp_path):
    tool = tmp_path / "mytsc"
    tool.write_text("#!/bin/sh\n")
    from autoair.sections import _ACTIVE, EditorSettings

    _ACTIVE[EditorSettings] = EditorSettings(validate_commands={"tsc": [str(tool)]})
    assert v.find_tool("tsc") == [str(tool)]
    _ACTIVE[EditorSettings] = EditorSettings(validate_commands={"tsc": ["/nonexistent/tsc"]})
    assert v.find_tool("tsc") is None
