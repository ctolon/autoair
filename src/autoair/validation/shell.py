"""ShellCheck findings as validation problems (warnings in the Problems tab).

Checked: ``bash_command`` of operators (BashOperator and friends), shell-script file assets
(``*.sh`` / ``run_with: bash|sh``) in any operator parameter, and the string literals an
``@task.bash`` function returns. Needs ShellCheck (``autoair[shell]``); without it nothing is
reported. Off with ``AUTOAIR_EDITOR__SHELLCHECK_IN_VALIDATION=false``.
"""

from __future__ import annotations

import ast
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from autoair.lsp.shell import ShellDiagnostic, ShellLintRequest, lint_shell_many

if TYPE_CHECKING:
    from autoair.spec import DagSpec
    from autoair.validation import Problem

_SHELLS = {"bash", "sh", "dash", "ksh"}
_SCRIPT_SUFFIXES = (".sh", ".bash")


# Results by (hash of the script, shell, exclude settings). ShellCheck output is a pure
# function of that key, so the memo never goes stale; it spares re-linting every unchanged
# Bash task on each live validation of a large DAG. Bounded LRU, per process (derived data).
_CACHE_SIZE = 4096
_cache: OrderedDict[tuple[bytes, str, tuple[Any, ...]], tuple[ShellDiagnostic, ...]] = OrderedDict()
_cache_lock = threading.Lock()


def _digest(code: str) -> bytes:
    # Keys hold a hash, not the script: the memo's memory stays bounded whatever the size.
    return hashlib.sha256(code.encode()).digest()


def _lint_all(
    scripts: list[tuple[str, str]], settings_key: tuple[Any, ...]
) -> dict[tuple[str, str], tuple[ShellDiagnostic, ...]]:
    """Diagnostics per (code, shell): memo hits, the misses in one ShellCheck run."""
    found: dict[tuple[str, str], tuple[ShellDiagnostic, ...]] = {}
    with _cache_lock:
        for code, shell in scripts:
            key = (_digest(code), shell, settings_key)
            if key in _cache:
                _cache.move_to_end(key)
                found[(code, shell)] = _cache[key]
    missing = list(dict.fromkeys(s for s in scripts if s not in found))
    if missing:
        results = lint_shell_many([ShellLintRequest(code=c, shell=sh) for c, sh in missing])
        with _cache_lock:
            for (code, shell), result in zip(missing, results, strict=True):
                if result is None:  # the run failed: report nothing now, retry next time
                    continue
                found[(code, shell)] = tuple(result.diagnostics)
                key = (_digest(code), shell, settings_key)
                _cache[key] = found[(code, shell)]
                _cache.move_to_end(key)
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
    return found


def clear_cache() -> None:
    """Forget memoised ShellCheck results (benchmarks measure a cold validation with it)."""
    with _cache_lock:
        _cache.clear()


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
    # (node id, field, code, shell, line offset) of every script, then one batched lint.
    found: list[tuple[str, str, str, str, int | None]] = []
    for n in spec.task_nodes():
        if isinstance(n, OperatorNode):
            for name, value in n.params.items():
                if name == "bash_command" and isinstance(value, str) and value.strip():
                    if not _is_script_path(value):
                        found.append((n.id, name, value, "bash", None))
                    continue
                for field, code, shell in _scripts(value, name):
                    found.append((n.id, field, code, shell, None))
        elif isinstance(n, PythonTaskNode) and n.decorator == "bash":
            for line, text in _returned_literals(n.code):
                if text.strip() and not _is_script_path(text):
                    found.append((n.id, "code", text, "bash", line - 1))
    diagnostics = _lint_all([(code, shell) for _, _, code, shell, _ in found], key)
    problems: list[Problem] = []
    for node_id, field, code, shell, offset in found:
        problems += _problems(diagnostics.get((code, shell), ()), node_id, field, offset)
    return problems


__all__ = ["clear_cache", "shell_problems"]
