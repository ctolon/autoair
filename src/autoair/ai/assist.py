"""End-user AI features: review, leak scan, optimise, fix, explain, write task code.

Each feature combines deterministic analysis (always available, also without an LLM)
with one structured LLM call (findings + JSON patches validated with Pydantic). The
patches of all findings are applied to produce a *proposed spec* that is validated
before the UI shows it as a reviewable diff.

Single-shot calls are used whenever the context is known up front (cheaper and faster
than an agent loop); :mod:`autoair.ai.agent` is used for open-ended questions.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.ai import prompts
from autoair.ai.config import active_ai
from autoair.ai.findings import (
    Finding,
    FindingsReport,
    PatchOp,
    apply_patch,
    attach_proposal,
    dedupe,
    finding_id,
    sort_findings,
)
from autoair.ai.llm import LLM, ChatMessage
from autoair.ai.problems import SpecProblem, check_spec, format_problems
from autoair.ai.rules import resource_findings, review_rules, scan_leaks
from autoair.ai.tuning import HistoryStats, tuning_findings
from autoair.ai.usage import Usage, track_usage
from autoair.catalog import OperatorCatalog
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec, OperatorNode, PythonTaskNode
from autoair.validation import DEFAULT_POLICY, Policy

# Defaults; the effective limits are ``ai.assist.*`` (autoair.ai.config.active_ai()).
MAX_CODE_CHARS = 40_000
SEVERITIES = ["critical", "high", "medium", "low", "info"]
CATEGORIES = [
    "secret",
    "security",
    "reliability",
    "performance",
    "cost",
    "correctness",
    "idempotency",
    "airflow3",
    "best_practice",
    "maintainability",
    "documentation",
]


class _RawPatch(BaseModel):
    op: Literal["add", "replace", "remove"]
    path: str = Field(description="JSON pointer into the DagSpec (/nodes/@<node id>/...).")
    value_json: str = Field(default="null", description="The new value as JSON text.")


class _RawFinding(BaseModel):
    severity: str = Field(default="medium", description=" | ".join(SEVERITIES))
    category: str = Field(default="best_practice", description=" | ".join(CATEGORIES))
    title: str
    message: str = ""
    node_id: str | None = None
    field: str | None = None
    patch: list[_RawPatch] = Field(default_factory=list)


class _RawFindings(BaseModel):
    summary: str = ""
    findings: list[_RawFinding] = Field(default_factory=list)


# --------------------------------------------------------------------------- context helpers


def compact_spec(spec: DagSpec | Mapping[str, Any]) -> str:
    """Token-lean JSON of a spec (no canvas positions / labels / icons)."""
    if isinstance(spec, DagSpec):
        data: dict[str, Any] = spec.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"environments"}
        )
    else:
        data = json.loads(json.dumps(dict(spec), default=str))
    for node in data.get("nodes") or []:
        if isinstance(node, dict):
            for key in ("position", "label", "icon"):
                node.pop(key, None)
            for key in ("callbacks", "outlets", "inlets", "params", "arguments", "config"):
                if node.get(key) in ({}, [], {"on_success": [], "on_failure": []}):
                    node.pop(key, None)
            if isinstance(node.get("callbacks"), dict) and not any(node["callbacks"].values()):
                node.pop("callbacks", None)
    for key in ("callbacks", "params", "deadlines", "owner_links"):
        value = data.get(key)
        if value in ({}, []) or (isinstance(value, dict) and not any(value.values())):
            data.pop(key, None)
    return json.dumps(data, separators=(",", ":"), sort_keys=False)


def operator_brief(spec: DagSpec, catalog: OperatorCatalog | None) -> str:
    """Per operator class: templated fields + parameter names (lets the model patch params)."""
    if catalog is None:
        return ""
    lines: list[str] = []
    for fqn in sorted({n.operator for n in spec.task_nodes() if isinstance(n, OperatorNode)}):
        detail = catalog.get(fqn)
        if detail is None:
            continue
        props = sorted((detail.params_schema.get("properties") or {}).keys())
        flags = [
            f
            for f, on in (("sensor", detail.is_sensor), ("deferrable", detail.deferrable_param))
            if on
        ]
        lines.append(
            f"- {fqn}{' [' + ', '.join(flags) + ']' if flags else ''}: params "
            f"{', '.join(props[:60]) or '-'}; templated {', '.join(detail.template_fields) or '-'}"
        )
    return "Operators used:\n" + "\n".join(lines) if lines else ""


def _brief_findings(findings: Sequence[Finding], limit: int = 40) -> str:
    if not findings:
        return "(none)"
    return "\n".join(
        f"- [{f.severity}] {f.title} @ {f.node_id or 'DAG'}{'.' + f.field if f.field else ''}"
        for f in findings[:limit]
    )


def _decode_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def to_findings(raw: _RawFindings, node_ids: set[str]) -> list[Finding]:
    out: list[Finding] = []
    for item in raw.findings:
        node_id = item.node_id if item.node_id in node_ids else None
        patch = [
            PatchOp.model_validate(
                {
                    "op": p.op,
                    "path": p.path,
                    "value": None if p.op == "remove" else _decode_value(p.value_json),
                }
            )
            for p in item.patch
            if p.op in ("add", "replace", "remove") and p.path.startswith("/")
        ]
        out.append(
            Finding.model_validate(
                {
                    "id": finding_id("ai", node_id, item.field, item.title.lower()),
                    "severity": item.severity if item.severity in SEVERITIES else "medium",
                    "category": item.category if item.category in CATEGORIES else "best_practice",
                    "title": item.title.strip()[:200],
                    "message": item.message.strip(),
                    "node_id": node_id,
                    "field": item.field,
                    "path": patch[0].path if patch else None,
                    "source": "ai",
                    "patch": patch,
                }
            )
        )
    return out


def _ask_findings(
    llm: LLM, system: str, user: str, node_ids: set[str]
) -> tuple[str, list[Finding]]:
    raw = llm.complete_structured(
        system, user, _RawFindings, max_tokens=active_ai().assist.findings_max_tokens
    )
    return raw.summary.strip(), to_findings(raw, node_ids)


def _request(
    spec_json: str,
    *,
    conventions: Sequence[str] = (),
    knowledge: str = "",
    extra: Sequence[str] = (),
) -> str:
    parts = [f"DagSpec:\n{spec_json}"]
    parts.extend(p for p in extra if p)
    block = prompts.conventions_block(list(conventions))
    if block:
        parts.append(block)
    if knowledge:
        parts.append("Reference material (may help, may be irrelevant):\n" + knowledge)
    return "\n\n".join(parts)


def _finish(
    report: FindingsReport,
    spec: DagSpec,
    *,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    policy: Policy,
) -> FindingsReport:
    report.findings = sort_findings(dedupe(report.findings))
    return attach_proposal(report, spec, catalog=catalog, registry=registry, policy=policy)


def _summary(kind: str, findings: Sequence[Finding]) -> str:
    if not findings:
        return f"No {kind} findings."
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    detail = ", ".join(f"{counts[s]} {s}" for s in SEVERITIES if s in counts)
    return f"{len(findings)} {kind} finding(s): {detail}."


# --------------------------------------------------------------------------- features


def review_dag(
    spec: DagSpec,
    *,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    llm: LLM | None = None,
    policy: Policy = DEFAULT_POLICY,
    conventions: Sequence[str] = (),
    knowledge: str = "",
) -> FindingsReport:
    """Best practices, Airflow 3 idioms, idempotency, retries, deadlines."""
    rules = review_rules(spec, catalog)
    report = FindingsReport(kind="review", findings=rules, summary=_summary("review", rules))
    if llm is not None:
        with track_usage() as meter:
            summary, extra = _ask_findings(
                llm,
                prompts.get("REVIEW_SYSTEM"),
                _request(
                    compact_spec(spec),
                    conventions=conventions,
                    knowledge=knowledge,
                    extra=[
                        operator_brief(spec, catalog),
                        "Already reported (do not repeat):\n" + _brief_findings(rules),
                    ],
                ),
                {n.id for n in spec.nodes},
            )
        report.findings = [*rules, *extra]
        report.summary = summary or report.summary
        report.ai_used, report.model, report.usage = True, llm.model, meter.total
    return _finish(report, spec, catalog=catalog, registry=registry, policy=policy)


def scan_dag_leaks(
    spec: DagSpec,
    *,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    llm: LLM | None = None,
    policy: Policy = DEFAULT_POLICY,
) -> FindingsReport:
    """Secrets, credential exposure and unbounded resources (rules + optional LLM pass)."""
    rules = scan_leaks(spec, catalog)
    report = FindingsReport(kind="leaks", findings=rules, summary=_summary("leak", rules))
    if llm is not None:
        with track_usage() as meter:
            summary, extra = _ask_findings(
                llm,
                prompts.get("LEAKS_SYSTEM"),
                _request(
                    compact_spec(_masked(spec, rules)),
                    extra=["Already reported (do not repeat):\n" + _brief_findings(rules)],
                ),
                {n.id for n in spec.nodes},
            )
        report.findings = [*rules, *extra]
        report.summary = summary or report.summary
        report.ai_used, report.model, report.usage = True, llm.model, meter.total
    return _finish(report, spec, catalog=catalog, registry=registry, policy=policy)


def _masked(spec: DagSpec, findings: Sequence[Finding]) -> DagSpec:
    """Replace values of detected secrets before sending the spec to a third-party model."""
    from autoair.ai.findings import spec_document

    document = spec_document(spec)
    for f in findings:
        if f.category != "secret" or not f.path or not f.rule or not f.rule.startswith("secret:"):
            continue
        try:
            document = apply_patch(
                document, [PatchOp(op="replace", path=f.path, value="***REDACTED***")]
            )
        except ValueError:
            continue
    try:
        return DagSpec.model_validate(document)
    except ValueError:
        return spec


def optimize_dag(
    spec: DagSpec,
    *,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    stats: HistoryStats | None = None,
    llm: LLM | None = None,
    policy: Policy = DEFAULT_POLICY,
    conventions: Sequence[str] = (),
) -> FindingsReport:
    """Retries, timeouts, pools, concurrency, deferrable - from run history when available."""
    rules = [
        *(tuning_findings(spec, stats, catalog) if stats else []),
        *resource_findings(spec, catalog),
    ]
    report = FindingsReport(kind="optimize", findings=rules, summary=_summary("tuning", rules))
    if stats is None or stats.runs == 0:
        report.notes.append(
            "No run history available: suggestions are static defaults. Deploy and run the "
            "DAG (or make Airflow reachable) for data-driven tuning."
        )
    if llm is not None:
        stats_text = stats.model_dump_json(exclude_none=True) if stats and stats.runs else "(none)"
        with track_usage() as meter:
            summary, extra = _ask_findings(
                llm,
                prompts.get("OPTIMIZE_SYSTEM"),
                _request(
                    compact_spec(spec),
                    conventions=conventions,
                    extra=[
                        operator_brief(spec, catalog),
                        f"Run statistics (seconds):\n{stats_text}",
                        "Already reported (keep unless contradicted):\n" + _brief_findings(rules),
                    ],
                ),
                {n.id for n in spec.nodes},
            )
        report.findings = [*rules, *extra]
        report.summary = summary or report.summary
        report.ai_used, report.model, report.usage = True, llm.model, meter.total
    return _finish(report, spec, catalog=catalog, registry=registry, policy=policy)


def fix_dag(
    document: Mapping[str, Any],
    *,
    llm: LLM,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    policy: Policy = DEFAULT_POLICY,
    problems: Sequence[SpecProblem] | None = None,
    import_errors: Sequence[str] = (),
    task_log: str | None = None,
    task_id: str | None = None,
    conventions: Sequence[str] = (),
    knowledge: str = "",
    max_rounds: int | None = None,
) -> FindingsReport:
    """Root-cause and patch validation problems, import errors or a failed task."""
    max_rounds = max_rounds or active_ai().assist.fix_rounds
    check = check_spec(document, catalog=catalog, registry=registry, policy=policy)
    found = list(problems) if problems is not None else check.problems
    node_ids = {str(n.get("id")) for n in document.get("nodes") or [] if isinstance(n, Mapping)}
    evidence: list[str] = []
    if found:
        evidence.append("Validation problems:\n" + format_problems(found))
    if import_errors:
        evidence.append("DAG import errors:\n" + "\n\n".join(e[-4000:] for e in import_errors[:3]))
    if task_log:
        tail = task_log[-active_ai().assist.max_log_chars :]
        evidence.append(
            f"Failed task log{f' (task {task_id})' if task_id else ''} - last "
            f"{len(tail)} characters:\n{tail}"
        )
    report = FindingsReport(kind="fix")
    if not evidence:
        report.summary = "Nothing to fix: the spec validates and no errors or logs were given."
        if check.spec is not None:
            report.proposed_ok = True
        return report
    user = _request(
        compact_spec(document if check.spec is None else check.spec),
        conventions=conventions,
        knowledge=knowledge,
        extra=[
            operator_brief(check.spec, catalog) if check.spec is not None else "",
            *evidence,
        ],
    )
    messages: list[ChatMessage] = [{"role": "user", "content": user}]
    current: dict[str, Any] = dict(document)
    findings: list[Finding] = []
    summary = ""
    with track_usage() as meter:
        for round_ in range(max(1, max_rounds)):
            raw = llm.complete_structured(
                prompts.get("FIX_SYSTEM"),
                messages,
                _RawFindings,
                max_tokens=active_ai().assist.findings_max_tokens,
            )
            summary = summary or raw.summary
            new = to_findings(raw, node_ids)
            failed: list[str] = []
            for f in new:
                if not f.patch:
                    continue
                try:
                    current = apply_patch(current, f.patch)
                except ValueError as exc:
                    failed.append(f"{f.title}: {exc}")
            findings.extend(new)
            after = check_spec(current, catalog=catalog, registry=registry, policy=policy)
            errors = [p for p in after.problems if p.severity == "error"]
            if (not errors and not failed) or round_ == max_rounds - 1:
                break
            messages.append({"role": "assistant", "content": raw.model_dump_json()})
            messages.append(
                {
                    "role": "user",
                    "content": "After applying your patches the spec still has problems:\n"
                    + format_problems(errors)
                    + ("\nPatches that failed to apply:\n" + "\n".join(failed) if failed else "")
                    + "\nCurrent spec:\n"
                    + compact_spec(current)
                    + "\nReturn ONLY the additional findings/patches needed (patches apply to "
                    "the current spec).",
                }
            )
    final = check_spec(current, catalog=catalog, registry=registry, policy=policy)
    report.findings = sort_findings(dedupe(findings))
    report.summary = summary or _summary("fix", report.findings)
    report.proposed_spec = final.spec
    report.proposed_ok = final.ok
    report.problems = final.problems
    report.ai_used, report.model, report.usage = True, llm.model, meter.total
    return report


# --------------------------------------------------------------------------- explain / write


class ExplainStep(BaseModel):
    node_id: str | None = Field(description="Task node id this step describes.")
    title: str
    explanation: str


class _Explanation(BaseModel):
    summary: str = Field(description="What the DAG does and why, 1-3 sentences.")
    schedule: str = Field(description="When it runs, in plain language.")
    steps: list[ExplainStep] = Field(description="Tasks in execution order.")
    data_flow: str = Field(description="How data moves between tasks and systems.")
    connections: list[str] = Field(description="Airflow connections / variables it needs.")
    risks: list[str] = Field(description="Operational risks worth knowing.")


class DagExplanation(_Explanation):
    model: str | None = None
    usage: Usage | None = None


def explain_dag(
    spec: DagSpec,
    *,
    llm: LLM,
    catalog: OperatorCatalog | None = None,
    focus_node: str | None = None,
    question: str | None = None,
) -> DagExplanation:
    extra = [operator_brief(spec, catalog)]
    if focus_node:
        extra.append(f"Focus on node {focus_node!r} and its neighbours.")
    if question:
        extra.append(f"The user asks: {question}")
    with track_usage() as meter:
        out = llm.complete_structured(
            prompts.get("EXPLAIN_DAG_SYSTEM"),
            _request(compact_spec(spec), extra=extra),
            _Explanation,
            max_tokens=active_ai().assist.explain_max_tokens,
        )
    result = DagExplanation.model_validate(out.model_dump())
    node_ids = {n.id for n in spec.nodes}
    for step in result.steps:
        if step.node_id not in node_ids:
            step.node_id = None
    result.model, result.usage = llm.model, meter.total
    return result


class _TaskCode(BaseModel):
    code: str = Field(description="Function body.")
    explanation: str = ""
    requirements: list[str] = Field(default_factory=list, description="pip packages needed.")
    connections: list[str] = Field(default_factory=list, description="Airflow connections used.")


class TaskCodeResult(_TaskCode):
    syntax_ok: bool = True
    findings: list[Finding] = Field(default_factory=list)
    model: str | None = None
    usage: Usage | None = None


def body_syntax_error(code: str, arguments: Sequence[str] = ()) -> str | None:
    wrapped = f"def _task({', '.join(arguments)}):\n" + "\n".join(
        "    " + line for line in (code.splitlines() or ["pass"])
    )
    try:
        ast.parse(wrapped)
    except SyntaxError as exc:
        return f"line {(exc.lineno or 2) - 1}: {exc.msg}"
    return None


def write_task_code(
    description: str,
    *,
    llm: LLM,
    spec: DagSpec | None = None,
    node_id: str | None = None,
    existing_code: str | None = None,
    catalog: OperatorCatalog | None = None,
    conventions: Sequence[str] = (),
) -> TaskCodeResult:
    node = spec.node_map().get(node_id) if (spec is not None and node_id) else None
    arguments: list[str] = []
    parts = [f"Task to implement:\n{description}"]
    if isinstance(node, PythonTaskNode):
        arguments = [*node.arguments.keys(), *node.extra_parameters]
        parts.append(
            f"Function: {node.function_name}({', '.join(arguments)}) with decorator "
            f"@{node.decorator}; argument values: {json.dumps(node.arguments, default=str)[:2000]}"
        )
        existing_code = existing_code if existing_code is not None else node.code
    if existing_code and existing_code.strip() not in ("", "pass"):
        parts.append(f"Current body (rewrite or extend it):\n```python\n{existing_code}\n```")
    if spec is not None:
        upstream = [e.source for e in spec.edges if e.target == node_id] if node_id else []
        parts.append(
            f"DAG {spec.dag_id!r}: {spec.description or ''} Upstream tasks: "
            f"{', '.join(upstream) or 'none'}."
        )
    block = prompts.conventions_block(list(conventions))
    if block:
        parts.append(block)
    user = "\n\n".join(parts)
    with track_usage() as meter:
        draft = llm.complete_structured(
            prompts.get("TASK_CODE_SYSTEM"),
            user,
            _TaskCode,
            max_tokens=active_ai().assist.task_code_max_tokens,
        )
        error = body_syntax_error(draft.code, arguments)
        if error is not None:
            draft = llm.complete_structured(
                prompts.get("TASK_CODE_SYSTEM"),
                user + f"\n\nYour previous body had a syntax error ({error}):\n```python\n"
                f"{draft.code}\n```\nReturn a corrected body.",
                _TaskCode,
                max_tokens=active_ai().assist.task_code_max_tokens,
            )
            error = body_syntax_error(draft.code, arguments)
    result = TaskCodeResult.model_validate(draft.model_dump())
    result.syntax_ok = error is None
    probe = DagSpec.model_validate(
        {
            "dag_id": "probe",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "task",
                    "task_id": "task",
                    "function_name": "task_fn",
                    "code": result.code,
                }
            ],
        }
    )
    result.findings = [f for f in (*scan_leaks(probe), *review_rules(probe)) if f.field == "code"]
    result.model, result.usage = llm.model, meter.total
    return result
