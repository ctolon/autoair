"""Large DAGs: batched ruff/ShellCheck linting, memoised results, and time budgets.

The budgets are deliberately generous (CI machines are slow and shared); they catch
regressions to per-task subprocesses or quadratic loops, not small slowdowns. Measured
numbers: ``uv run python scripts/bench_large_dag.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from autoair.codegen import render
from autoair.lsp.shell import ShellLintRequest, lint_shell, lint_shell_many, shellcheck_path
from autoair.spec import DagSpec, PythonTaskNode
from autoair.validation import pylint, shell, validate_spec
from autoair.validation.pylint import lint_python_task, lint_python_tasks

ROOT = Path(__file__).resolve().parents[2]
BASH = "airflow.providers.standard.operators.bash.BashOperator"


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_large_dag", ROOT / "scripts" / "gen_large_dag.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _cold_caches():
    pylint.clear_cache()
    shell.clear_cache()
    yield
    pylint.clear_cache()
    shell.clear_cache()


def task(i: int, code: str, **kw: Any) -> PythonTaskNode:
    return PythonTaskNode.model_validate(
        {
            "kind": "python_task",
            "id": f"n{i}",
            "task_id": f"t{i}",
            "function_name": f"f{i}",
            "code": code,
            **kw,
        }
    )


class _Spy:
    """Counts subprocess.run calls (passing them through)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
        self.calls = 0
        real = subprocess.run

        def run(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(module.subprocess, "run", run)


# --------------------------------------------------------------------------- generator


def test_generator_is_deterministic_and_valid():
    gen = _generator()
    a = gen.large_dag_spec(120, seed=3)
    assert a == gen.large_dag_spec(120, seed=3)
    spec = DagSpec.model_validate(a)
    tasks = spec.task_nodes()
    assert len(tasks) == 120
    kinds = {type(t).__name__ for t in tasks}
    assert {"OperatorNode", "PythonTaskNode"} <= kinds
    assert any(t.mapping for t in tasks)
    assert spec.group_nodes()
    assert len(spec.edges) > len(tasks)
    report = validate_spec(spec)
    assert report.ok, [p for p in report.problems if p.severity == "error"]


def test_generator_without_layout_leaves_positions_at_origin():
    data = _generator().large_dag_spec(30, layout=False)
    assert all("position" not in n for n in data["nodes"])


# --------------------------------------------------------------------------- ruff batch


def test_batch_lints_every_body_in_one_process(monkeypatch):
    if pylint._ruff_command() is None:
        pytest.skip("ruff missing")
    spy = _Spy(monkeypatch, pylint)
    nodes = [task(i, f"x{i} = 1\nreturn y{i}") for i in range(20)]
    results = lint_python_tasks(nodes)
    assert spy.calls == 1
    assert len(results) == 20
    for i, issues in enumerate(results):
        codes = {(x.code, x.line) for x in issues}
        assert ("F841", 1) in codes, issues  # x{i} unused
        assert ("F821", 2) in codes, issues  # y{i} undefined
        assert all(f"y{i}" in x.message or f"x{i}" in x.message for x in issues)


def test_batched_bodies_do_not_see_each_other():
    if pylint._ruff_command() is None:
        pytest.skip("ruff missing")
    # f1 refers to f0, the function of ANOTHER task: still undefined in its own file.
    nodes = [task(0, "return 1"), task(1, "return f0()")]
    first, second = lint_python_tasks(nodes)
    assert first == []
    assert [x.code for x in second] == ["F821"]


def test_batch_matches_single_lint():
    if pylint._ruff_command() is None:
        pytest.skip("ruff missing")
    nodes = [task(0, "import os\nreturn 1"), task(1, "return undefined_name")]
    batch = lint_python_tasks(nodes)
    pylint.clear_cache()
    assert batch == [lint_python_task(n) for n in nodes]


def test_memo_skips_unchanged_bodies(monkeypatch):
    if pylint._ruff_command() is None:
        pytest.skip("ruff missing")
    nodes = [task(i, f"return v{i}") for i in range(5)]
    lint_python_tasks(nodes)
    spy = _Spy(monkeypatch, pylint)
    again = lint_python_tasks(nodes)
    assert spy.calls == 0
    assert all(x and x[0].code == "F821" for x in again)
    lint_python_tasks([*nodes, task(9, "return 1")])
    assert spy.calls == 1  # only the new body


def test_failed_ruff_run_reports_nothing_and_is_not_memoised(monkeypatch):
    if pylint._ruff_command() is None:
        pytest.skip("ruff missing")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired("ruff", 1)

    monkeypatch.setattr(pylint.subprocess, "run", boom)
    nodes = [task(0, "return nope"), task(1, "return nope2")]
    assert lint_python_tasks(nodes) == [[], []]
    monkeypatch.undo()
    assert lint_python_tasks(nodes)[0][0].code == "F821"


def test_duplicate_node_ids_keep_their_own_lint():
    spec = DagSpec.model_validate(
        {
            "dag_id": "d",
            "nodes": [
                {"kind": "python_task", "id": "a", "task_id": "a", "function_name": "fa",
                 "code": "return 1"},
                {"kind": "python_task", "id": "a", "task_id": "b", "function_name": "fb",
                 "code": "return missing"},
            ],
        }
    )  # fmt: skip
    report = validate_spec(spec)
    assert [p.code for p in report.problems if p.code.startswith("lint:")] == ["lint:F821"]


# --------------------------------------------------------------------------- shellcheck batch

needs_shellcheck = pytest.mark.skipif(shellcheck_path() is None, reason="shellcheck missing")


@needs_shellcheck
def test_shellcheck_batch_maps_findings_to_their_script():
    reqs = [
        ShellLintRequest(code="cd /tmp\necho ok"),
        ShellLintRequest(code="echo fine"),
        ShellLintRequest(code=""),
        ShellLintRequest(code="#!/bin/sh\necho $1"),
        ShellLintRequest(code="echo {{ ds }} $X", shell="sh"),
    ]
    batch = lint_shell_many(reqs)
    assert all(r is not None for r in batch)
    assert [r.available for r in batch if r] == [True] * 5
    assert batch == [lint_shell(r) for r in reqs]
    assert {d.code for d in batch[0].diagnostics} == {"SC2164"}
    assert batch[0].diagnostics[0].line == 1
    assert batch[1].diagnostics == []
    assert batch[2].diagnostics == []
    assert {d.code for d in batch[3].diagnostics} == {"SC2086"}
    assert batch[3].diagnostics[0].line == 2  # a shebang adds no directive line


@needs_shellcheck
def test_validation_runs_shellcheck_once_and_memoises(monkeypatch):
    from autoair.lsp import shell as lsp_shell

    nodes = [
        {"kind": "operator", "id": f"b{i}", "task_id": f"b{i}", "operator": BASH,
         "params": {"bash_command": f"cd /data/{i}\necho {i}"}}
        for i in range(300)  # more scripts than the old 256-entry cache held
    ]  # fmt: skip
    spec = DagSpec.model_validate({"dag_id": "d", "nodes": nodes})
    spy = _Spy(monkeypatch, lsp_shell)
    first = validate_spec(spec)
    assert spy.calls == 300 // lsp_shell._BATCH  # batches, not one process per script
    found = [p for p in first.problems if p.code == "shellcheck:SC2164"]
    assert {p.node_id for p in found} == {f"b{i}" for i in range(300)}
    second = validate_spec(spec)
    assert spy.calls == 300 // lsp_shell._BATCH
    assert second.problems == first.problems


@needs_shellcheck
def test_failed_shellcheck_run_is_retried_not_memoised(monkeypatch):
    from autoair.lsp import shell as lsp_shell

    node = {"kind": "operator", "id": "b", "task_id": "b", "operator": BASH,
            "params": {"bash_command": "cd /data\necho ok"}}  # fmt: skip
    spec = DagSpec.model_validate({"dag_id": "d", "nodes": [node]})

    def boom(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired("shellcheck", 1)

    monkeypatch.setattr(lsp_shell.subprocess, "run", boom)
    assert not [p for p in validate_spec(spec).problems if p.code.startswith("shellcheck:")]
    assert lint_shell(ShellLintRequest(code="cd /x")).diagnostics == []
    monkeypatch.undo()
    codes = {p.code for p in validate_spec(spec).problems}
    assert "shellcheck:SC2164" in codes


@needs_shellcheck
def test_shellcheckrc_files_are_ignored(tmp_path, monkeypatch):
    # A .shellcheckrc above the temporary folder must not change results (--norc).
    (tmp_path / ".shellcheckrc").write_text("disable=all\n")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    batch = lint_shell_many([ShellLintRequest(code="cd /a"), ShellLintRequest(code="cd /b")])
    assert all(r is not None and {d.code for d in r.diagnostics} == {"SC2164"} for r in batch)


# --------------------------------------------------------------------------- budgets


@pytest.mark.parametrize(("tasks", "budget"), [(500, 10.0), (1000, 20.0)])
def test_large_dag_validate_and_codegen_budget(tasks: int, budget: float):
    data = _generator().large_dag_spec(tasks)
    spec = DagSpec.model_validate(data)
    start = time.perf_counter()
    report = validate_spec(spec)
    source = render(spec)
    elapsed = time.perf_counter() - start
    assert report.ok, [p for p in report.problems if p.severity == "error"][:5]
    assert source.count("task_id=") + source.count("@task") >= tasks
    assert elapsed < budget, f"validate+codegen of {tasks} tasks took {elapsed:.1f}s"
    # Warm (memoised lint): a live re-validation must be far cheaper.
    start = time.perf_counter()
    validate_spec(spec)
    assert time.perf_counter() - start < budget / 2
