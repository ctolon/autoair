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
import tempfile
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any, Literal

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


def _prepared(req: ShellLintRequest) -> tuple[str, int]:
    """Masked source with the shell directive, and the number of lines it added."""
    source = mask_jinja(req.code)
    if source.startswith("#!"):
        return source, 0
    return f"# shellcheck shell={req.shell}\n{source}", 1


def _diagnostics(comments: list[dict[str, Any]], offset: int) -> list[ShellDiagnostic]:
    out: list[ShellDiagnostic] = []
    for c in comments:
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
    return out


# Scripts per ShellCheck process: `shellcheck_timeout` applies to each run, and one run over
# hundreds of scripts can take longer than a run over one (a cold validation of a large DAG).
_BATCH = 50


def lint_shell(req: ShellLintRequest) -> ShellLintResult:
    return lint_shell_many([req])[0] or ShellLintResult(available=True)


def lint_shell_many(reqs: Sequence[ShellLintRequest]) -> list[ShellLintResult | None]:
    """Lints several scripts with few ShellCheck processes (up to 50 scripts each, one
    temporary file per script), so validating a DAG with hundreds of Bash tasks does not start
    hundreds of processes. None for a script whose run failed (timeout, crash, bad output):
    callers must not remember that as "no findings"."""
    binary = shellcheck_path()
    if binary is None:
        return [ShellLintResult(available=False) for _ in reqs]
    results: list[ShellLintResult | None] = [ShellLintResult(available=True) for _ in reqs]
    todo = [i for i, r in enumerate(reqs) if r.code.strip()]
    if not todo:
        return results
    from autoair.sections import EditorSettings, active

    cfg = active(EditorSettings)
    exclude = ",".join([*cfg.shellcheck_exclude, *cfg.shellcheck_exclude_extra])
    # --norc: no .shellcheckrc of the server's or the temporary folder's parents changes
    # the result (it must depend only on the script and these settings).
    args = [binary, "--norc", "--format=json1", *([f"--exclude={exclude}"] if exclude else [])]
    prepared = {i: _prepared(reqs[i]) for i in todo}
    for chunk in (todo[k : k + _BATCH] for k in range(0, len(todo), _BATCH)):
        comments = _run(args, chunk, prepared, cfg.shellcheck_timeout)
        for i in chunk:
            results[i] = (
                None
                if comments is None
                else ShellLintResult(
                    available=True, diagnostics=_diagnostics(comments.get(i, []), prepared[i][1])
                )
            )
    return results


def _run(
    args: list[str], chunk: list[int], prepared: dict[int, tuple[str, int]], timeout: float
) -> dict[int, list[dict[str, Any]]] | None:
    """ShellCheck comments per script index of one run; None when the run failed."""
    try:
        if len(chunk) == 1:
            proc = subprocess.run(
                [*args, "-"],
                input=prepared[chunk[0]][0],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=user_code_env(),
            )
            data = json.loads(proc.stdout or "{}")
            return {chunk[0]: list(data.get("comments", []))}
        with tempfile.TemporaryDirectory(prefix="autoair-shell-") as tmp:
            files = {}
            for i in chunk:
                path = Path(tmp) / f"script_{i}.sh"
                path.write_text(prepared[i][0], encoding="utf-8")
                files[str(path)] = i
            proc = subprocess.run(
                [*args, *files],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=user_code_env(),
                cwd=tmp,
            )
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    comments: dict[int, list[dict[str, Any]]] = {}
    for c in data.get("comments", []):
        owner = files.get(str(c.get("file", "")))
        if owner is not None:
            comments.setdefault(owner, []).append(c)
    return comments


__all__ = [
    "ShellDiagnostic",
    "ShellLintRequest",
    "ShellLintResult",
    "lint_shell",
    "lint_shell_many",
    "mask_jinja",
    "shellcheck_path",
]
