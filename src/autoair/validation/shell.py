"""ShellCheck findings as validation problems (warnings in the Problems tab).

Checked: ``bash_command`` of operators (BashOperator and friends), shell-script file assets
(``*.sh`` / ``run_with: bash|sh``) in any operator parameter, and the string literals an
``@task.bash`` function returns. Needs ShellCheck (``autoair[shell]``); without it nothing is
reported. Off with ``AUTOAIR_EDITOR__SHELLCHECK_IN_VALIDATION=false``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from autoair.lsp.shell import ShellDiagnostic, ShellLintRequest, lint_shell

if TYPE_CHECKING:
    from autoair.spec import DagSpec
    from autoair.validation import Problem

_SHELLS = {"bash", "sh", "dash", "ksh"}
_SCRIPT_SUFFIXES = (".sh", ".bash")


@lru_cache(maxsize=256)
def _lint(code: str, shell: str, settings_key: tuple[Any, ...]) -> tuple[ShellDiagnostic, ...]:
    # settings_key: the active exclude list, so a configuration change invalidates the cache.
    del settings_key
    result = lint_shell(ShellLintRequest(code=code, shell=shell))
    return tuple(result.diagnostics)


def _is_script_path(text: str) -> bool:
    """Airflow treats a ``bash_command`` ending in .sh as a template file path."""
    stripped = text.strip()
    return stripped.endswith(_SCRIPT_SUFFIXES) and not any(c.isspace() for c in stripped)


def _scripts(value: Any, path: str) -> Iterator[tuple[str, str, str]]:
    """(field path, code, shell) of shell file assets nested in a parameter value."""
    if isinstance(value, dict):
        if value.get("$type") == "file" and isinstance(value.get("content"), str):
            name = str(value.get("name", ""))
            run_with = str(value.get("run_with") or "").rsplit("/", 1)[-1]
            if name.endswith(_SCRIPT_SUFFIXES) or run_with in _SHELLS:
                shell = run_with if run_with in _SHELLS else "bash"
                yield path, value["content"], shell
            return
        for key, item in value.items():
            yield from _scripts(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _scripts(item, f"{path}.{i}")


def _returned_literals(code: str) -> Iterator[tuple[int, str]]:
    """(1-based body line, text) of string constants returned by an @task.bash body."""
    from autoair.codegen.body import normalize_task_body

    try:
        tree = ast.parse(normalize_task_body(code) or "pass")
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            yield node.value.lineno, node.value.value


def _problems(
    diagnostics: tuple[ShellDiagnostic, ...],
    node_id: str,
    field: str,
    line_offset: int | None = None,
) -> list[Problem]:
    """``line_offset``: the script is a string literal in Python code (columns unknown)."""
    from autoair.validation import Problem

    out: list[Problem] = []
    for d in diagnostics:
        line = d.line + (line_offset or 0)
        out.append(
            Problem(
                severity="info" if d.severity == "info" else "warning",
                code=f"shellcheck:{d.code}",
                message=f"line {line}: {d.message}",
                node_id=node_id,
                field=field,
                line=line,
                column=d.column if line_offset is None else None,
                doc_url=f"https://www.shellcheck.net/wiki/{d.code}",
            )
        )
    return out


def shell_problems(spec: DagSpec) -> list[Problem]:
    from autoair.lsp.shell import shellcheck_path
    from autoair.sections import EditorSettings, active
    from autoair.spec import OperatorNode, PythonTaskNode

    cfg = active(EditorSettings)
    if not cfg.shellcheck_in_validation or shellcheck_path() is None:
        return []
    key = (tuple(cfg.shellcheck_exclude), tuple(cfg.shellcheck_exclude_extra))
    problems: list[Problem] = []
    for n in spec.task_nodes():
        if isinstance(n, OperatorNode):
            for name, value in n.params.items():
                if name == "bash_command" and isinstance(value, str) and value.strip():
                    if not _is_script_path(value):
                        problems += _problems(_lint(value, "bash", key), n.id, name)
                    continue
                for field, code, shell in _scripts(value, name):
                    problems += _problems(_lint(code, shell, key), n.id, field)
        elif isinstance(n, PythonTaskNode) and n.decorator == "bash":
            for line, text in _returned_literals(n.code):
                if text.strip() and not _is_script_path(text):
                    problems += _problems(_lint(text, "bash", key), n.id, "code", line - 1)
    return problems


__all__ = ["shell_problems"]
