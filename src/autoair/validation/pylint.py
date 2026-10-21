"""Static lint of ``python_task`` bodies with ruff (pyflakes + bugbear subset).

Each body is wrapped in the function signature the code generator emits, linted from
stdin (nothing is executed) and diagnostics are mapped back to body line numbers.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from dataclasses import dataclass

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


def lint_python_task(node: PythonTaskNode, timeout: float | None = None) -> list[LintIssue]:
    from autoair.sections import EditorSettings, active

    cfg = active(EditorSettings)
    timeout = cfg.python_lint_timeout if timeout is None else timeout
    select = ",".join([*cfg.python_lint_select, *cfg.python_lint_select_extra]) or "F"
    ignore = ",".join([*cfg.python_lint_ignore, *cfg.python_lint_ignore_extra])
    cmd = _ruff_command()
    if cmd is None:
        return []
    body = normalize_task_body(node.code) or "pass"
    source = _header(node) + textwrap.indent(body, "    ") + "\n"
    try:
        proc = subprocess.run(
            [
                *cmd,
                "check",
                "--isolated",
                "--no-cache",
                "--select",
                select,
                *(("--ignore", ignore) if ignore else ()),
                "--output-format",
                "json",
                "--stdin-filename",
                "task.py",
                "-",
            ],
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        diagnostics = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
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
    return issues
