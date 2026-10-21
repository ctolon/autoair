"""Specialised writers: ``@task`` bodies, inline functions, Bash, SQL, operator configs,
custom operator classes and whole DAG specs.

Every writer

1. builds its prompt from the **installed environment** (:func:`environment_profile`:
   Airflow version, providers, importable packages, available linters) and - for
   operator configs - the candidate operators of the installed catalog with their
   parameter schemas;
2. adds reference material retrieved from the installed providers' docs / the
   knowledge base (as untrusted data);
3. asks for a Pydantic-validated structured answer and runs a **validate-and-repair
   loop**: syntax, ruff, ShellCheck, import availability, catalog schema / AutoAir
   validation and the secret-leak rules; problems go back to the model until the code
   is clean or ``ai.writers.max_repairs`` is used up.

The result says whether the code passed (``ok``) and lists remaining issues, so the UI
never presents unchecked code as valid.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.ai import prompts
from autoair.ai.assist import body_syntax_error, compact_spec
from autoair.ai.config import ai_settings_of
from autoair.ai.events import Emit, StatusEvent
from autoair.ai.findings import Finding
from autoair.ai.guardrails import GuardrailPipeline
from autoair.ai.llm import ChatMessage, LLMError
from autoair.ai.rules import review_rules, scan_leaks
from autoair.ai.usage import Usage, track_usage
from autoair.spec import DagSpec, PythonTaskNode

log = logging.getLogger("autoair.ai.writers")

WriterKind = Literal[
    "task_body", "inline_function", "bash", "sql", "operator_config", "custom_operator", "dag_spec"
]
WRITER_KINDS: tuple[WriterKind, ...] = (
    "task_body",
    "inline_function",
    "bash",
    "sql",
    "operator_config",
    "custom_operator",
    "dag_spec",
)
LANGUAGE: dict[str, str] = {
    "task_body": "python",
    "inline_function": "python",
    "bash": "bash",
    "sql": "sql",
    "operator_config": "json",
    "custom_operator": "python",
    "dag_spec": "json",
}


PYTHON_KINDS: frozenset[str] = frozenset(k for k, lang in LANGUAGE.items() if lang == "python")


class PythonCodeDisabled(LLMError):
    """A Python/inline-code writer was asked for while ``features.python_code`` (or the policy
    switch ``allow_python_code``) is off. The API maps it to 403 rather than an upstream 502."""


def python_code_enabled(settings: Any, default: bool = True) -> bool:
    """``settings.feature("python_code")`` (the feature flag AND the policy switch), tolerant
    of settings-like stand-ins without ``feature()``."""
    if settings is None:
        return default
    fn = getattr(settings, "feature", None)
    if callable(fn):
        return bool(fn("python_code"))
    features = getattr(settings, "features", None)
    return bool(getattr(settings, "allow_python_code", default)) and bool(
        getattr(features, "python_code", True)
    )


# --------------------------------------------------------------------------- environment


class EnvironmentProfile(BaseModel):
    """What the generated code can rely on."""

    airflow_version: str | None = None
    python_version: str
    providers: list[dict[str, str]] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list, description="Importable modules.")
    shellcheck: bool = False
    ruff: bool = False
    sqlglot: bool = False
    allow_python_code: bool = True

    def prompt_block(self, max_packages: int = 120) -> str:
        providers = ", ".join(f"{p['name']} {p['version']}" for p in self.providers[:80])
        packages = ", ".join(self.packages[:max_packages])
        return "\n".join(
            [
                "Target environment:",
                f"- Apache Airflow {self.airflow_version or 'unknown'} on Python "
                f"{self.python_version}",
                f"- Installed providers: {providers or 'none'}",
                f"- Importable packages (subset): {packages or 'standard library only'}",
            ]
        )


_PROFILE_LOCK = threading.Lock()
_PROFILES: dict[int, tuple[float, EnvironmentProfile]] = {}


def environment_profile(services: Any = None, *, ttl: float = 300) -> EnvironmentProfile:
    key = id(services)
    with _PROFILE_LOCK:
        cached = _PROFILES.get(key)
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]
    from autoair._compat import airflow_version_str
    from autoair.ai.research.local import installed_packages, top_level_modules
    from autoair.codegen.format import _ruff_command
    from autoair.lsp.shell import shellcheck_path

    profile = EnvironmentProfile(
        airflow_version=airflow_version_str(),
        python_version=platform.python_version(),
        providers=[
            {"name": p.name, "version": p.version}
            for p in installed_packages(providers_only=True, limit=400)
        ],
        packages=top_level_modules(),
        shellcheck=shellcheck_path() is not None,
        ruff=_ruff_command() is not None,
        sqlglot=importlib.util.find_spec("sqlglot") is not None,
        allow_python_code=python_code_enabled(getattr(services, "settings", None)),
    )
    with _PROFILE_LOCK:
        _PROFILES[key] = (time.monotonic(), profile)
    return profile


# --------------------------------------------------------------------------- outputs


class WriterIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    source: str = Field(description="syntax, ruff, shellcheck, imports, catalog, sql, policy…")
    message: str
    line: int | None = None


class WriterResult(BaseModel):
    kind: WriterKind
    ok: bool = Field(description="True when the output passed every available check.")
    language: str
    code: str = Field(description="Code / script / SQL / module text (or JSON for configs).")
    function_name: str | None = None
    operator: str | None = None
    params: dict[str, Any] | None = None
    spec: DagSpec | None = None
    explanation: str = ""
    requirements: list[str] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)
    issues: list[WriterIssue] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list, description="Checks that ran.")
    rounds: int = 1
    model: str | None = None
    usage: Usage | None = None


class WriteRequest(BaseModel):
    kind: WriterKind
    description: str = Field(min_length=1, max_length=20_000)
    spec: DagSpec | None = Field(default=None, description="The DAG being edited, if any.")
    node_id: str | None = Field(default=None, description="Node the code is for.")
    existing: str | None = Field(default=None, max_length=200_000)
    function_name: str | None = Field(default=None, max_length=200)
    arguments: list[str] = Field(default_factory=list, max_length=50)
    dialect: str | None = Field(default=None, max_length=40, description="SQL dialect.")
    operator: str | None = Field(default=None, max_length=300, description="Preferred operator.")
    research: bool = Field(default=True, description="Add retrieved reference material.")
    model: str | None = Field(default=None, max_length=200)


class _CodeOut(BaseModel):
    code: str
    explanation: str = ""
    requirements: list[str] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)


class _FunctionOut(_CodeOut):
    function_name: str


class _OperatorOut(BaseModel):
    operator: str
    task_id: str
    params_json: str
    explanation: str = ""
    connections: list[str] = Field(default_factory=list)


class _ModuleOut(_CodeOut):
    class_name: str


# --------------------------------------------------------------------------- checks


_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


def _find_spec(name: str) -> Any:
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def import_issues(code: str, profile: EnvironmentProfile | None = None) -> list[WriterIssue]:
    """Imports of modules that are not installed (checked without importing them)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    known = set(profile.packages) if profile else set()
    issues: list[WriterIssue] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            parts = name.split(".")
            if top == "airflow" and len(parts) >= 3 and parts[1] == "providers":
                # Provider packages share the "airflow" top level: check the provider itself.
                provider = ".".join(parts[:3])
                if _find_spec(provider) is None:
                    issues.append(
                        WriterIssue(
                            severity="error",
                            source="imports",
                            message=f"provider package {provider!r} is not installed",
                            line=getattr(node, "lineno", None),
                        )
                    )
                continue
            if top in _STDLIB or top in known or top == "__future__":
                continue
            if importlib.util.find_spec(top) is None:
                issues.append(
                    WriterIssue(
                        severity="error",
                        source="imports",
                        message=f"module {top!r} is not installed in this environment",
                        line=getattr(node, "lineno", None),
                    )
                )
    return issues


def ruff_issues(
    source: str, *, timeout: float = 15, line_offset: int = 0
) -> tuple[bool, list[WriterIssue]]:
    """(ran, issues) of ``ruff check`` (pyflakes, syntax, likely bugs) on ``source``."""
    from autoair.codegen.format import _ruff_command

    cmd = _ruff_command()
    if cmd is None:
        return False, []
    try:
        proc = subprocess.run(
            [
                *cmd,
                "check",
                "--isolated",
                "--no-cache",
                "--select",
                "F,E9,B006,B008,B012,B018,PLE,S102,S307",
                "--ignore",
                "F401",
                "--output-format",
                "json",
                "--stdin-filename",
                "writer.py",
                "-",
            ],
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        diagnostics = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, []
    issues: list[WriterIssue] = []
    for d in diagnostics:
        code = str(d.get("code") or "syntax")
        row = int((d.get("location") or {}).get("row", 1)) - line_offset
        severity: Literal["error", "warning"] = (
            "error"
            if code.startswith(("F82", "F63", "E9", "PLE")) or code == "syntax"
            else "warning"
        )
        issues.append(
            WriterIssue(
                severity=severity,
                source="ruff",
                message=f"{code}: {d.get('message', '')}",
                line=max(1, row),
            )
        )
    return True, issues


def shell_issues(script: str) -> tuple[bool, list[WriterIssue]]:
    from autoair.lsp.shell import ShellLintRequest, lint_shell

    result = lint_shell(ShellLintRequest(code=script[:200_000]))
    if not result.available:
        return False, []
    return True, [
        WriterIssue(
            severity="error"
            if d.severity == "error"
            else ("warning" if d.severity == "warning" else "info"),
            source="shellcheck",
            message=f"{d.code}: {d.message}",
            line=d.line,
        )
        for d in result.diagnostics
    ]


_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def sql_issues(sql: str, dialect: str | None) -> tuple[str, list[WriterIssue]]:
    """(checker, issues): sqlglot parse when installed, else structural heuristics."""
    issues: list[WriterIssue] = []
    text = _JINJA.sub("x", sql)
    if not text.strip():
        return "sql", [WriterIssue(severity="error", source="sql", message="empty SQL")]
    for stmt in re.split(r";\s*(?:\n|$)", text):
        flat = " ".join(stmt.split()).lower()
        if re.match(r"^(delete\s+from|update)\b", flat) and " where " not in f" {flat} ":
            issues.append(
                WriterIssue(
                    severity="error",
                    source="sql",
                    message="UPDATE/DELETE without WHERE affects every row",
                )
            )
    if importlib.util.find_spec("sqlglot") is not None:
        import importlib as _il

        sqlglot = _il.import_module("sqlglot")
        try:
            sqlglot.parse(text, read=(dialect or None))
        except Exception as exc:
            issues.append(WriterIssue(severity="error", source="sqlglot", message=str(exc)[:500]))
        return "sqlglot", issues
    stripped = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "", text)
    if stripped.count("(") != stripped.count(")"):
        issues.append(WriterIssue(severity="error", source="sql", message="unbalanced parentheses"))
    if text.count("'") % 2:
        issues.append(WriterIssue(severity="error", source="sql", message="unbalanced quotes"))
    return "sql", issues


def leak_findings(code: str, field_kind: str = "python") -> list[Finding]:
    """Secret / logging rules of the leak scan applied to generated code."""
    node: dict[str, Any]
    if field_kind == "bash":
        node = {
            "kind": "operator",
            "id": "task",
            "task_id": "task",
            "operator": "airflow.providers.standard.operators.bash.BashOperator",
            "params": {"bash_command": code},
        }
    else:
        node = {
            "kind": "python_task",
            "id": "task",
            "task_id": "task",
            "function_name": "task_fn",
            "code": code,
        }
    try:
        probe = DagSpec.model_validate({"dag_id": "writer_probe", "nodes": [node]})
    except ValueError:
        return []
    return [
        f
        for f in (*scan_leaks(probe), *review_rules(probe))
        if f.category in ("secret", "security") or f.severity in ("critical", "high")
    ]


def custom_operator_issues(code: str, class_name: str) -> list[WriterIssue]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [WriterIssue(severity="error", source="syntax", message=exc.msg, line=exc.lineno)]
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if cls is None:
        return [
            WriterIssue(severity="error", source="policy", message=f"class {class_name} missing")
        ]
    issues: list[WriterIssue] = []
    bases = [ast.unparse(b) for b in cls.bases]
    if not any(b.split(".")[-1].endswith("Operator") or "Sensor" in b for b in bases):
        issues.append(
            WriterIssue(
                severity="error",
                source="policy",
                message=f"{class_name} must subclass BaseOperator (bases: {bases or 'none'})",
                line=cls.lineno,
            )
        )
    execute = next(
        (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "execute"), None
    )
    if execute is None or len(execute.args.args) < 2:
        issues.append(
            WriterIssue(
                severity="error",
                source="policy",
                message="define execute(self, context)",
                line=cls.lineno,
            )
        )
    if "airflow.models.baseoperator" in code or "from airflow.models import BaseOperator" in code:
        issues.append(
            WriterIssue(
                severity="warning",
                source="airflow3",
                message="import BaseOperator from airflow.sdk (Airflow 3 Task SDK)",
            )
        )
    return issues


# --------------------------------------------------------------------------- writer specs


class _Spec:
    """How one writer kind prompts, parses and validates."""

    def __init__(
        self,
        prompt: str,
        output: type[BaseModel],
        instructions: Callable[[WriteRequest], str],
    ) -> None:
        self.prompt = prompt
        self.output = output
        self.instructions = instructions


def _task_instructions(req: WriteRequest) -> str:
    args = ", ".join(req.arguments) or "(none)"
    return f"Function arguments already defined: {args}. Return only the body."


def _function_instructions(req: WriteRequest) -> str:
    name = req.function_name or "callback"
    return f"Function name: {name}; parameters: {', '.join(req.arguments) or '(choose)'}."


SPECS: dict[str, _Spec] = {
    "task_body": _Spec("WRITER_TASK_BODY_SYSTEM", _CodeOut, _task_instructions),
    "inline_function": _Spec("WRITER_INLINE_FUNCTION_SYSTEM", _FunctionOut, _function_instructions),
    "bash": _Spec("WRITER_BASH_SYSTEM", _CodeOut, lambda r: "Return the script in `code`."),
    "sql": _Spec(
        "WRITER_SQL_SYSTEM",
        _CodeOut,
        lambda r: f"SQL dialect: {r.dialect or 'ANSI / PostgreSQL'}. Return the SQL in `code`.",
    ),
    "operator_config": _Spec("WRITER_OPERATOR_SYSTEM", _OperatorOut, lambda r: ""),
    "custom_operator": _Spec(
        "WRITER_CUSTOM_OPERATOR_SYSTEM",
        _ModuleOut,
        lambda r: "Return the whole module in `code` and the operator class name in `class_name`.",
    ),
}


def _operator_candidates(services: Any, req: WriteRequest, limit: int) -> list[str]:
    from autoair.ai.corpus import retriever_for
    from autoair.ai.generator import rank_operators

    catalog = services.catalog
    found: list[str] = []
    if req.operator and catalog.get(req.operator) is not None:
        found.append(req.operator)
    try:
        hits = retriever_for(services).search(req.description, limit, ["operator"])
        found.extend(str(h.meta.get("fqn")) for h in hits if h.meta.get("fqn"))
    except Exception:  # retrieval is best effort
        log.debug("operator retrieval failed", exc_info=True)
    found.extend(s.fqn for s in rank_operators(req.description, catalog.summaries(), limit))
    return list(dict.fromkeys(f for f in found if catalog.get(f) is not None))[:limit]


def _operator_block(services: Any, fqns: Sequence[str]) -> str:
    parts: list[str] = []
    for fqn in fqns:
        detail = services.catalog.get(fqn)
        if detail is None:
            continue
        props = detail.params_schema.get("properties") or {}
        compact = {
            name: {k: v for k, v in schema.items() if k in ("type", "enum", "default")}
            | (
                {"description": str(schema["description"])[:140]}
                if schema.get("description")
                else {}
            )
            for name, schema in props.items()
        }
        parts.append(
            f"### {fqn}\n{detail.summary}\nRequired: "
            f"{', '.join(detail.params_schema.get('required', [])) or '-'}\n"
            f"Templated: {', '.join(detail.template_fields) or '-'}\n"
            f"Params: {json.dumps(compact, sort_keys=True)[:6000]}"
        )
    return "Candidate operators (installed):\n\n" + "\n\n".join(parts)


# --------------------------------------------------------------------------- run


async def run_writer(
    req: WriteRequest,
    *,
    llm: Any,
    services: Any,
    emit: Emit | None = None,
    guardrails: GuardrailPipeline | None = None,
) -> WriterResult:
    """Write, validate and repair (see the module docstring)."""
    emit = emit or (lambda _e: None)
    ai = ai_settings_of(services.settings)
    if req.kind == "dag_spec":
        return await _write_dag_spec(req, llm=llm, services=services, emit=emit)
    if req.kind in PYTHON_KINDS and not python_code_enabled(services.settings):
        raise PythonCodeDisabled(
            "Python code is disabled on this server (features.python_code / allow_python_code)"
        )
    spec = SPECS[req.kind]
    emit(StatusEvent(stage="environment", message="Reading the installed environment"))
    profile = await asyncio.to_thread(environment_profile, services)
    parts = [f"Task:\n{req.description}", profile.prompt_block(ai.writers.max_packages_in_prompt)]
    extra = spec.instructions(req)
    if extra:
        parts.append(extra)
    node = req.spec.node_map().get(req.node_id) if (req.spec and req.node_id) else None
    if isinstance(node, PythonTaskNode) and req.kind == "task_body":
        arguments = [*node.arguments.keys(), *node.extra_parameters]
        req = req.model_copy(update={"arguments": req.arguments or arguments})
        parts[2:3] = [_task_instructions(req)]
        if req.existing is None and node.code.strip() not in ("", "pass"):
            req = req.model_copy(update={"existing": node.code})
    if req.existing:
        lang = LANGUAGE[req.kind]
        parts.append(f"Current version (rewrite or extend it):\n```{lang}\n{req.existing}\n```")
    if req.spec is not None:
        parts.append("The DAG (context):\n" + compact_spec(req.spec)[:6000])
    candidates: list[str] = []
    if req.kind == "operator_config":
        candidates = await asyncio.to_thread(
            _operator_candidates, services, req, ai.writers.max_operators_in_prompt
        )
        parts.append(_operator_block(services, candidates))
    if req.research:
        emit(StatusEvent(stage="research", message="Searching installed docs"))
        reference = await _reference(services, req)
        if reference:
            wrapped = (
                guardrails.untrusted(reference, "reference docs")
                if guardrails is not None
                else reference
            )
            parts.append("Reference material:\n" + wrapped)
    from autoair.ai.memory import aconventions_for

    block = prompts.conventions_block(await aconventions_for(services))
    if block:
        parts.append(block)
    system = prompts.get(spec.prompt)
    messages: list[ChatMessage] = [{"role": "user", "content": "\n\n".join(parts)}]
    result: WriterResult | None = None
    rounds = 0
    with track_usage() as meter:
        for round_ in range(ai.writers.max_repairs + 1):
            rounds = round_ + 1
            emit(
                StatusEvent(
                    stage="write" if round_ == 0 else "repair",
                    message="Writing" if round_ == 0 else f"Repairing (round {round_})",
                )
            )
            out = await llm.acomplete_structured(
                system, messages, spec.output, max_tokens=ai.assist.task_code_max_tokens
            )
            emit(StatusEvent(stage="validate", message="Checking the result"))
            result = await asyncio.to_thread(
                _validate, req, out, services, profile, ai.writers.lint_timeout_seconds
            )
            errors = [i for i in result.issues if i.severity == "error"]
            if result.ok or not ai.guardrails.check_generated_code:
                break
            if round_ == ai.writers.max_repairs:
                break
            messages.append({"role": "assistant", "content": out.model_dump_json()})
            messages.append(
                {
                    "role": "user",
                    "content": "Your answer has problems:\n"
                    + "\n".join(
                        f"- [{i.source}{f' line {i.line}' if i.line else ''}] {i.message}"
                        for i in (errors or result.issues)[:30]
                    )
                    + "".join(f"\n- [leak] {f.title}: {f.message}" for f in result.findings[:10])
                    + "\nFix all of them and answer again.",
                }
            )
    assert result is not None
    result.rounds = rounds
    result.model = str(llm.model)
    result.usage = meter.total
    return result


async def _reference(services: Any, req: WriteRequest) -> str:
    try:
        from autoair.ai.research.tools import research_for

        ctx = research_for(services)
        kinds = ["doc", "source", "package"]
        hits = await ctx.retriever.asearch(req.description, 4, kinds)
        local = ctx.retriever.format_context(hits, 3_500)
    except Exception:
        local = ""
    from autoair.kb.service import kb_context

    docs = await kb_context(services, req.description, k=3)
    return "\n\n---\n\n".join(p for p in (local, docs) if p)


def _validate(
    req: WriteRequest,
    out: BaseModel,
    services: Any,
    profile: EnvironmentProfile,
    timeout: float,
) -> WriterResult:
    kind = req.kind
    issues: list[WriterIssue] = []
    checks: list[str] = []
    findings: list[Finding] = []
    base: dict[str, Any] = {"kind": kind, "language": LANGUAGE[kind]}
    if isinstance(out, _OperatorOut):
        return _validate_operator(req, out, services)
    assert isinstance(out, _CodeOut)
    code = out.code.strip("\n")
    base.update(
        code=code,
        explanation=out.explanation,
        requirements=out.requirements,
        connections=out.connections,
    )
    if kind == "task_body":
        checks.append("syntax")
        error = body_syntax_error(code, req.arguments)
        if error:
            issues.append(WriterIssue(severity="error", source="syntax", message=error))
        else:
            header = f"def _task({', '.join(req.arguments)}):\n"
            body = "\n".join("    " + line for line in (code.splitlines() or ["pass"]))
            ran, found = ruff_issues(header + body + "\n", timeout=timeout, line_offset=1)
            if ran:
                checks.append("ruff")
                issues.extend(found)
            checks.append("imports")
            issues.extend(import_issues(header + body, profile))
        findings = leak_findings(code)
    elif kind in ("inline_function", "custom_operator"):
        checks.append("syntax")
        try:
            ast.parse(code)
        except SyntaxError as exc:
            issues.append(
                WriterIssue(severity="error", source="syntax", message=exc.msg, line=exc.lineno)
            )
        else:
            if isinstance(out, _FunctionOut):
                base["function_name"] = out.function_name
                wanted = req.function_name or out.function_name
                if not re.search(rf"^\s*(async\s+)?def\s+{re.escape(wanted)}\s*\(", code, re.M):
                    issues.append(
                        WriterIssue(
                            severity="error",
                            source="policy",
                            message=f"define a function named {wanted}",
                        )
                    )
            if isinstance(out, _ModuleOut):
                checks.append("operator-structure")
                issues.extend(custom_operator_issues(code, out.class_name))
                base["operator"] = out.class_name
            ran, found = ruff_issues(code + "\n", timeout=timeout)
            if ran:
                checks.append("ruff")
                issues.extend(found)
            checks.append("imports")
            issues.extend(import_issues(code, profile))
        findings = leak_findings(code)
    elif kind == "bash":
        ran, found = shell_issues(code)
        if ran:
            checks.append("shellcheck")
            issues.extend(found)
        findings = leak_findings(code, "bash")
    elif kind == "sql":
        checker, found = sql_issues(code, req.dialect)
        checks.append(checker)
        issues.extend(found)
    if findings:
        checks.append("leak-scan")
    errors = [i for i in issues if i.severity == "error"]
    secret = [f for f in findings if f.category == "secret"]
    blocking_warnings = [i for i in issues if i.severity == "warning" and i.source == "ruff"]
    ok = not errors and not secret and not blocking_warnings
    return WriterResult(**base, ok=ok, issues=issues, findings=findings, checks=checks)


def _validate_operator(req: WriteRequest, out: _OperatorOut, services: Any) -> WriterResult:
    from autoair.ai.problems import check_spec
    from autoair.validation import policy_from_settings

    issues: list[WriterIssue] = []
    try:
        params = json.loads(out.params_json or "{}")
        if not isinstance(params, dict):
            raise ValueError("params_json must be a JSON object")
    except ValueError as exc:
        params = {}
        issues.append(WriterIssue(severity="error", source="json", message=str(exc)))
    task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", out.task_id or "task")[:200] or "task"
    if services.catalog.get(out.operator) is None:
        issues.append(
            WriterIssue(
                severity="error",
                source="catalog",
                message=f"{out.operator} is not in the installed catalog; pick a candidate",
            )
        )
    else:
        document = {
            "dag_id": "writer_probe",
            "nodes": [
                {
                    "kind": "operator",
                    "id": task_id,
                    "task_id": task_id,
                    "operator": out.operator,
                    "params": params,
                }
            ],
        }
        check = check_spec(
            document,
            catalog=services.catalog,
            registry=services.registry,
            policy=policy_from_settings(services.settings),
        )
        for p in check.problems:
            if p.code in ("missing_owner", "missing_description", "missing_tags"):
                continue
            issues.append(
                WriterIssue(
                    severity="error" if p.severity == "error" else "warning",
                    source="catalog",
                    message=f"{p.field or p.path or ''}: {p.message}".strip(": "),
                )
            )
    errors = [i for i in issues if i.severity == "error"]
    return WriterResult(
        kind="operator_config",
        ok=not errors,
        language="json",
        code=json.dumps({"operator": out.operator, "task_id": task_id, "params": params}, indent=2),
        operator=out.operator,
        params=params,
        explanation=out.explanation,
        connections=out.connections,
        issues=issues,
        checks=["catalog-schema", "autoair-validation"],
    )


async def _write_dag_spec(
    req: WriteRequest, *, llm: Any, services: Any, emit: Emit
) -> WriterResult:
    from autoair.ai.generator import generate_spec
    from autoair.validation import policy_from_settings

    emit(StatusEvent(stage="write", message="Generating the DAG spec"))
    with track_usage() as meter:
        result = await asyncio.to_thread(
            generate_spec,
            req.description,
            catalog=services.catalog,
            registry=services.registry,
            llm=llm,
            existing=req.spec,
            max_repairs=ai_settings_of(services.settings).writers.max_repairs,
            policy=policy_from_settings(services.settings),
        )
    issues = [
        WriterIssue(
            severity="error" if p.severity == "error" else "warning",
            source="validation",
            message=f"{p.path or ''} {p.message}".strip(),
        )
        for p in result.problems
    ]
    return WriterResult(
        kind="dag_spec",
        ok=result.ok,
        language="json",
        code=result.spec.model_dump_json(indent=2, exclude_none=True, by_alias=True)
        if result.spec
        else "",
        spec=result.spec,
        explanation=result.explanation,
        issues=issues,
        checks=["autoair-validation"],
        rounds=result.attempts,
        model=str(llm.model),
        usage=meter.total,
    )


__all__ = [
    "LANGUAGE",
    "PYTHON_KINDS",
    "WRITER_KINDS",
    "EnvironmentProfile",
    "PythonCodeDisabled",
    "WriteRequest",
    "WriterIssue",
    "WriterKind",
    "WriterResult",
    "custom_operator_issues",
    "environment_profile",
    "import_issues",
    "leak_findings",
    "python_code_enabled",
    "ruff_issues",
    "run_writer",
    "shell_issues",
    "sql_issues",
]
