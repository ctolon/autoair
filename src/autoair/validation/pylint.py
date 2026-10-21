"""Static lint of ``python_task`` bodies with ruff (pyflakes + bugbear subset).

Each body is wrapped in the function signature the code generator emits and linted
(nothing is executed); diagnostics are mapped back to body line numbers. All bodies of one
validation are linted by a single ruff process.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import textwrap
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoair.codegen.body import normalize_task_body
from autoair.codegen.format import _ruff_command
from autoair.spec import PythonTaskNode


@dataclass(frozen=True)
class LintIssue:
    line: int  # 1-based line inside the task body
    column: int
    code: str
    message: str
    severity: str  # "error" | "warning"
    url: str | None = None  # rule documentation (ruff's JSON output links it)


def _header(node: PythonTaskNode) -> str:
    expand = list(node.mapping.expand) if node.mapping else []
    args = list(dict.fromkeys([*node.arguments, *expand, *node.extra_parameters]))
    if node.uses_loop_context:
        args += ["*", "loop=None"]
    return f"def {node.function_name}({', '.join(args)}):\n"


def _source(node: PythonTaskNode) -> str:
    body = normalize_task_body(node.code) or "pass"
    return _header(node) + textwrap.indent(body, "    ") + "\n"


# Results by (hash of the wrapped source, select, ignore). Linting is a pure function of
# that key, so the memo never goes stale; it spares re-linting every unchanged task on each
# live validation of a large DAG. Bounded LRU, per process (derived data only).
_CACHE_SIZE = 4096
_cache: OrderedDict[tuple[bytes, str, str], tuple[LintIssue, ...]] = OrderedDict()
_cache_lock = threading.Lock()


def _issues(diagnostics: list[dict[str, Any]]) -> tuple[LintIssue, ...]:
    issues: list[LintIssue] = []
    for d in diagnostics:
        row = int(d.get("location", {}).get("row", 1)) - 1  # header line offset
        if row < 1:
            continue
        code = str(d.get("code") or "syntax")
        severity = (
            "error" if code.startswith(("F82", "E9", "PLE")) or code == "syntax" else "warning"
        )
        issues.append(
            LintIssue(
                line=row,
                column=max(1, int(d.get("location", {}).get("column", 1)) - 4),
                code=code,
                message=str(d.get("message", "")),
                severity=severity,
                url=str(d["url"])
                if isinstance(d.get("url"), str) and d["url"].startswith("https://")
                else None,
            )
        )
    return tuple(issues)


def _run_ruff(
    cmd: list[str], sources: list[str], select: str, ignore: str, timeout: float
) -> list[tuple[LintIssue, ...]] | None:
    """Lints every source in ONE ruff process (one file each in a temporary folder, so the
    bodies cannot see each other's names); None when ruff failed or timed out."""
    args = [
        *cmd,
        "check",
        "--isolated",
        "--no-cache",
        "--select",
        select,
        *(("--ignore", ignore) if ignore else ()),
        "--output-format",
        "json",
    ]
    try:
        if len(sources) == 1:
            proc = subprocess.run(
                [*args, "--stdin-filename", "task.py", "-"],
                input=sources[0],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            diagnostics = json.loads(proc.stdout or "[]")
            return [_issues(diagnostics)]
        with tempfile.TemporaryDirectory(prefix="autoair-lint-") as tmp:
            files = []
            for i, source in enumerate(sources):
                path = Path(tmp) / f"task_{i}.py"
                path.write_text(source, encoding="utf-8")
                files.append(str(path))
            # Files, not the folder: ruff's default excludes (build, venv, ...) never apply
            # to paths named on the command line, wherever the temporary folder lives.
            proc = subprocess.run(
                [*args, *files],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        diagnostics = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    by_file: dict[int, list[dict[str, Any]]] = {}
    for d in diagnostics:
        stem = Path(str(d.get("filename", ""))).stem
        if stem.startswith("task_") and stem[5:].isdigit():
            by_file.setdefault(int(stem[5:]), []).append(d)
    return [_issues(by_file.get(i, [])) for i in range(len(sources))]


def lint_python_tasks(
    nodes: Sequence[PythonTaskNode], timeout: float | None = None
) -> list[list[LintIssue]]:
    """Lint issues of each node (in input order). Unchanged bodies come from the memo; the
    rest are linted in one ruff run, so hundreds of Python tasks cost one process."""
    from autoair.sections import EditorSettings, active

    cfg = active(EditorSettings)
    timeout = cfg.python_lint_timeout if timeout is None else timeout
    select = ",".join([*cfg.python_lint_select, *cfg.python_lint_select_extra]) or "F"
    ignore = ",".join([*cfg.python_lint_ignore, *cfg.python_lint_ignore_extra])
    cmd = _ruff_command()
    if cmd is None or not nodes:
        return [[] for _ in nodes]
    sources = [_source(n) for n in nodes]
    # Keys hold a hash, not the body: the memo's memory stays bounded whatever the size.
    keys = [(hashlib.sha256(src.encode()).digest(), select, ignore) for src in sources]
    text = dict(zip(keys, sources, strict=True))
    found: dict[tuple[bytes, str, str], tuple[LintIssue, ...]] = {}
    with _cache_lock:
        for key in keys:
            if key in _cache:
                _cache.move_to_end(key)
                found[key] = _cache[key]
    missing = list(dict.fromkeys(k for k in keys if k not in found))
    if missing:
        results = _run_ruff(cmd, [text[k] for k in missing], select, ignore, timeout)
        if results is not None:
            with _cache_lock:
                for key, issues in zip(missing, results, strict=True):
                    found[key] = issues
                    _cache[key] = issues
                    _cache.move_to_end(key)
                while len(_cache) > _CACHE_SIZE:
                    _cache.popitem(last=False)
    return [list(found.get(key, ())) for key in keys]


def clear_cache() -> None:
    """Forget memoised lint results (benchmarks measure a cold validation with it)."""
    with _cache_lock:
        _cache.clear()


def lint_python_task(node: PythonTaskNode, timeout: float | None = None) -> list[LintIssue]:
    return lint_python_tasks([node], timeout)[0]
