"""Validation of raw code DAGs (``DagSpec.raw_code``): hand-written DAG files.

Spec rules (graph, catalog parameters, templates…) do not apply to free-form Python. A raw
file is checked with what works on any source:

* syntax (``ast``) and the editor's ruff lint rules (:mod:`autoair.lsp.validate`);
* hard-coded secrets (``policy.secret_assignment_pattern``), per line;
* policy: ``allow_python_code`` (raw code is Python), privileged and forbidden operators
  found by a static scan of imports and calls, the dag_id rules, and OPA (the input carries
  the code, ``context.document_type`` is ``raw``);
* the DagBag import check when enabled, which also confirms the file defines the DAG.

The operator scan is static and best effort: code that builds class paths at run time
escapes it, which is why raw code needs its own permission (``code:raw``).
"""

from __future__ import annotations

import ast
import fnmatch
import re
from collections.abc import Mapping
from typing import Any

from autoair.spec import DagSpec
from autoair.validation import (
    Policy,
    Problem,
    ValidationReport,
    _organisation_rules,
    _regexes,
)
from autoair.validation.dagbag_check import ImportCheckResult, check_source

# TaskFlow decorators that run what the matching operator runs (``@task.bash``).
_DECORATOR_OPERATORS: Mapping[str, str] = {
    "bash": "airflow.providers.standard.operators.bash.BashOperator",
}
_TASK_DECORATOR_MODULES = ("airflow.sdk", "airflow.decorators")


def _problem(
    code: str,
    message: str,
    *,
    line: int | None = None,
    column: int | None = None,
    severity: Any = "error",
    doc_url: str | None = None,
) -> Problem:
    return Problem(
        severity=severity,
        code=code,
        message=message,
        field="raw_code",
        line=line,
        column=column,
        doc_url=doc_url,
    )


class _Imports(ast.NodeVisitor):
    """Local names bound by imports -> the dotted path they refer to."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.names[alias.asname] = alias.name
            else:
                root = alias.name.split(".", 1)[0]
                self.names[root] = root

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        for alias in node.names:
            if alias.name != "*":
                self.names[alias.asname or alias.name] = f"{node.module}.{alias.name}"


def _dotted(expr: ast.expr, names: Mapping[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name) or expr.id not in names:
        return None
    return ".".join([names[expr.id], *reversed(parts)])


def operator_uses(tree: ast.Module) -> list[tuple[str, int, int]]:
    """``(class path, line, column)`` of every call (and TaskFlow decorator) resolved
    through the module's imports."""
    imports = _Imports()
    imports.visit(tree)
    found: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            path = _dotted(node.func, imports.names)
            if path:
                found.append((path, node.lineno, node.col_offset + 1))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                path = _dotted(target, imports.names)
                if not path:
                    continue
                module, _, attr = path.rpartition(".")
                if module in (f"{m}.task" for m in _TASK_DECORATOR_MODULES):
                    operator = _DECORATOR_OPERATORS.get(attr)
                    if operator:
                        found.append((operator, deco.lineno, deco.col_offset + 1))
    return found


def _matches_operator(path: str, patterns: Any) -> bool:
    """``path`` matches a pattern, or is an Airflow re-export of a matched class (same class
    name under ``airflow.``, e.g. ``airflow.operators.bash.BashOperator``)."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        name = pattern.rsplit(".", 1)[-1]
        if (
            path.startswith("airflow.")
            and not any(c in name for c in "*?[")
            and path.rsplit(".", 1)[-1] == name
        ):
            return True
    return False


def defined_dag_ids(tree: ast.Module) -> set[str] | None:
    """dag_ids the file defines literally (``DAG("x")``, ``DAG(dag_id="x")``, ``@dag``-
    decorated functions); ``None`` when a DAG is created in a way a static scan cannot
    read (e.g. a computed dag_id)."""
    ids: set[str] = set()
    dynamic = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "DAG":
                arg: ast.expr | None = node.args[0] if node.args else None
                for kw in node.keywords:
                    if kw.arg == "dag_id":
                        arg = kw.value
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    ids.add(arg.value)
                else:
                    dynamic = True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                if name != "dag":
                    continue
                explicit = None
                if isinstance(deco, ast.Call):
                    for kw in deco.keywords:
                        if kw.arg == "dag_id":
                            explicit = kw.value
                    if deco.args:
                        explicit = deco.args[0]
                if explicit is None:
                    ids.add(node.name)
                elif isinstance(explicit, ast.Constant) and isinstance(explicit.value, str):
                    ids.add(explicit.value)
                else:
                    dynamic = True
    return None if dynamic else ids


def _lint(code: str) -> list[Problem]:
    from autoair.lsp.validate import ValidateRequest, validate_code

    result = validate_code(ValidateRequest(language="python", code=code))
    problems: list[Problem] = []
    for d in result.diagnostics:
        rule = d.code or "syntax"
        problems.append(
            _problem(
                "syntax" if rule == "syntax" or d.source == "ast" else f"lint:{rule}",
                d.message,
                line=d.line,
                column=d.column,
                severity=d.severity,
                doc_url=f"https://docs.astral.sh/ruff/rules/{rule}/"
                if d.source == "ruff"
                else None,
            )
        )
    return problems


def _secrets(code: str, policy: Policy) -> list[Problem]:
    problems: list[Problem] = []
    for regex in _regexes(tuple(policy.secret_assignment_patterns)):
        for match in regex.finditer(code):
            line = code.count("\n", 0, match.start()) + 1
            problems.append(
                _problem(
                    "secret",
                    "this line assigns what looks like a hard-coded secret: DAG files are "
                    "readable by anyone with access to the repository; read it from a "
                    "Connection (BaseHook.get_connection) or a Variable instead",
                    line=line,
                    severity="warning",
                )
            )
    seen: set[int | None] = set()
    unique = []
    for p in problems:
        if p.line not in seen:
            seen.add(p.line)
            unique.append(p)
    return unique


def _operators(tree: ast.Module, policy: Policy) -> list[Problem]:
    problems: list[Problem] = []
    for path, line, column in operator_uses(tree):
        short = path.rsplit(".", 1)[-1]
        if not policy.allow_privileged_operators and _matches_operator(
            path, policy.privileged_operators
        ):
            problems.append(
                _problem(
                    "policy",
                    f"{short} runs arbitrary commands and is disabled by policy "
                    "(allow_privileged_operators)",
                    line=line,
                    column=column,
                )
            )
        if _matches_operator(path, policy.forbidden_operators):
            problems.append(
                _problem("policy", f"{short} is forbidden by policy", line=line, column=column)
            )
    return problems


def blocked_operators(code: str, policy: Policy) -> list[str]:
    """Class paths in ``code`` that ``policy`` refuses (privileged while not allowed, or
    forbidden); empty for code that does not parse (validation reports it)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    return [p.message for p in _operators(tree, policy)]


def _dag_id_rules(spec: DagSpec, tree: ast.Module, policy: Policy) -> list[Problem]:
    problems: list[Problem] = []
    dag_id = spec.dag_id
    if policy.team_dag_id_prefix and spec.team and not dag_id.startswith(f"{spec.team}_"):
        problems.append(
            _problem("policy", f"dag_id must start with the team prefix '{spec.team}_'")
        )
    if policy.dag_id_prefix and not dag_id.startswith(policy.dag_id_prefix):
        problems.append(_problem("policy", f"dag_id must start with {policy.dag_id_prefix!r}"))
    for regex in _regexes(tuple(policy.dag_id_patterns)):
        if not regex.fullmatch(dag_id):
            problems.append(_problem("policy", f"dag_id must match {regex.pattern!r}"))
    ids = defined_dag_ids(tree)
    if ids is not None and dag_id not in ids:
        found = ", ".join(sorted(ids)) or "none"
        problems.append(
            _problem(
                "raw_dag_id",
                f"the code does not define a DAG with dag_id {dag_id!r} (found: {found}); "
                "the dag_id in the code must match the document's",
                severity="warning",
            )
        )
    return problems


def validate_raw(
    spec: DagSpec,
    *,
    policy: Policy,
    import_check: bool = False,
    import_timeout: float = 60,
    target_version: tuple[int, int, int] | None = None,
    environment: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> ValidationReport:
    """The checks of a raw code DAG (see the module docstring). Blocking."""
    from autoair.codegen import render_raw

    code = spec.raw_code or ""
    problems: list[Problem] = []
    if not policy.allow_python_code:
        problems.append(
            _problem(
                "policy",
                "raw code DAGs are Python and need allow_python_code (features.python_code)",
            )
        )
    if not code.strip():
        problems.append(_problem("raw_empty", "the DAG file is empty"))
        return ValidationReport(ok=False, problems=problems)
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        problems.append(
            _problem("syntax", exc.msg or "invalid syntax", line=exc.lineno or 1, column=exc.offset)
        )
        return ValidationReport(ok=False, problems=problems)
    problems += _lint(code)
    problems += _secrets(code, policy)
    problems += _operators(tree, policy)
    problems += _dag_id_rules(spec, tree, policy)
    facts = {**(context or {}), "document_type": "raw"}
    if policy.opa is not None:  # policy.rules address spec nodes and fields: not raw code
        problems += _organisation_rules(
            spec, Policy(opa=policy.opa), environment, target_version, facts
        )
    result: ImportCheckResult | None = None
    if import_check and not any(p.severity == "error" for p in problems):
        source = render_raw(spec)
        shift = source.count("\n") - (code if code.endswith("\n") else code + "\n").count("\n")
        result = check_source(source, filename=f"{spec.dag_id}.py", timeout=import_timeout)
        for err in result.import_errors:
            problems.append(_problem("import", err, line=_error_line(err, spec.dag_id, shift)))
        if result.ok and spec.dag_id not in result.dag_ids:
            found = ", ".join(result.dag_ids) or "none"
            problems.append(
                _problem("import", f"the file did not define DAG {spec.dag_id!r} (found: {found})")
            )
    ok = not any(p.severity == "error" for p in problems)
    return ValidationReport(ok=ok, problems=problems, import_check=result)


def _error_line(message: str, dag_id: str, shift: int) -> int | None:
    """The last line of the DAG file named in an import error traceback, in the editor's
    numbering (the ownership header, when prepended, shifts the deployed file by ``shift``)."""
    lines = re.findall(rf'{re.escape(dag_id)}\.py", line (\d+)', message)
    if not lines:
        return None
    line = int(lines[-1]) - shift
    return line if line >= 1 else None


__all__ = ["blocked_operators", "defined_dag_ids", "operator_uses", "validate_raw"]
