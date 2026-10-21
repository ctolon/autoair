"""Shell script linting with ShellCheck (optional: ``pip install autoair[shell]``).

Bash commands of BashOperator / ``@task.bash`` and embedded ``.sh`` files are checked as the
user types. Jinja (``{{ ds }}``, ``{% if %}``) is masked with same-length placeholders first,
so ShellCheck sees plain shell and the reported columns still match the editor.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from autoair._subprocess import user_code_env

_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)


class ShellLintRequest(BaseModel):
    code: str = Field(max_length=10_000_000)  # AUTOAIR_EDITOR__MAX_SOURCE_CHARS is enforced
    shell: Literal["bash", "sh", "dash", "ksh"] = "bash"


class ShellDiagnostic(BaseModel):
    line: int
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    severity: Literal["error", "warning", "info"]
    code: str
    message: str


class ShellLintResult(BaseModel):
    available: bool = Field(description="False when ShellCheck is not installed.")
    diagnostics: list[ShellDiagnostic] = Field(default_factory=list)


@cache
def shellcheck_path() -> str | None:
    """The ShellCheck binary: PATH, or the one the ``shellcheck-py`` wheel put next to Python."""
    local = Path(sys.executable).parent / "shellcheck"
    return shutil.which("shellcheck") or (str(local) if local.is_file() else None)


def mask_jinja(code: str) -> str:
    """Jinja → same-length shell-neutral text (line breaks kept), so columns don't move."""

    def repl(m: re.Match[str]) -> str:
        text = m.group(0)
        # Expressions behave like a word ("x…x"), statements/comments like blanks.
        fill = "x" if text.startswith("{{") else " "
        return "".join(c if c == "\n" else fill for c in text)

    return _JINJA.sub(repl, code)


_SEVERITY: dict[str, Literal["error", "warning", "info"]] = {
    "error": "error",
    "warning": "warning",
    "info": "info",
    "style": "info",
}


def lint_shell(req: ShellLintRequest) -> ShellLintResult:
    binary = shellcheck_path()
    if binary is None:
        return ShellLintResult(available=False)
    if not req.code.strip():
        return ShellLintResult(available=True)
    source = mask_jinja(req.code)
    if not source.startswith("#!"):
        source = f"# shellcheck shell={req.shell}\n{source}"
        offset = 1
    else:
        offset = 0
    from autoair.sections import EditorSettings, active

    cfg = active(EditorSettings)
    exclude = ",".join([*cfg.shellcheck_exclude, *cfg.shellcheck_exclude_extra])
    try:
        proc = subprocess.run(
            [binary, "--format=json1", *([f"--exclude={exclude}"] if exclude else []), "-"],
            input=source,
            capture_output=True,
            text=True,
            timeout=cfg.shellcheck_timeout,
            check=False,
            env=user_code_env(),
        )
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ShellLintResult(available=True)
    out: list[ShellDiagnostic] = []
    for c in data.get("comments", []):
        line = int(c.get("line", 1)) - offset
        if line < 1:
            continue
        out.append(
            ShellDiagnostic(
                line=line,
                column=c.get("column"),
                end_line=(int(c["endLine"]) - offset) if c.get("endLine") else None,
                end_column=c.get("endColumn"),
                severity=_SEVERITY.get(str(c.get("level")), "info"),
                code=f"SC{c.get('code')}",
                message=str(c.get("message", "")),
            )
        )
    return ShellLintResult(available=True, diagnostics=out)


__all__ = [
    "ShellDiagnostic",
    "ShellLintRequest",
    "ShellLintResult",
    "lint_shell",
    "mask_jinja",
    "shellcheck_path",
]
