"""AI troubleshooting of DAG runs: a scoped, read-only, agentic log analysis.

1. **Evidence pack** (deterministic, with the user's Airflow token): run + task instance
   metadata, upstream states, numbered log tails of every try of the failed (or chosen)
   task instances, the DagSpec + generated code + DAG version / bundle version, import
   errors, XCom keys, Airflow version and providers. Logs are scrubbed and wrapped as
   untrusted data.
2. **Agent** with tools limited to the effective scope (``ai.troubleshoot.resources`` AND
   the user's choice): connection metadata (never secrets), variable keys, pools, XComs,
   other runs, import errors, Airflow configuration (masked), the sandboxed filesystem,
   research, memory and the attached external MCP servers.
3. **Structured report**: root-cause hypotheses with evidence (log line references) and
   confidence, remediation steps, spec/code patches (as a validated proposed spec and a
   generated-code diff), config snippets and Airflow-side instructions.

Spec and code changes are never applied automatically: an analysis is stored with its
proposals; applying them needs the user's explicit approval (``decide``) and then goes
through the normal deploy pipeline. Proposed Airflow actions (clear, retry, mark state)
follow ``ai.troubleshoot.airflow_actions`` (:mod:`autoair.ai.assistants.airflow_actions`):
instructions only, run after the user approves each, or run automatically after the
analysis - always vetted and with the user's own Airflow token. Every analysis, decision,
action and result is audited.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from autoair.ai.approvals import ToolApprovalView
from autoair.ai.assist import _RawFinding
from autoair.ai.assistants.airflow_actions import (
    ActionBusy,
    ActionResult,
    ActionsMode,
    AirflowAction,
    BlockReason,
    ProposedAction,
    claim,
    record_result,
    vet_actions,
)
from autoair.ai.assistants.mcp_client import McpSelection
from autoair.ai.assistants.sources import SourceCollector
from autoair.ai.assistants.threads import Citation, new_id, now_iso
from autoair.ai.config import TroubleshootSettings
from autoair.ai.findings import Finding, apply_findings
from autoair.ai.problems import SpecProblem
from autoair.ai.tools import AgentStep, AgentTool
from autoair.ai.usage import Usage
from autoair.gateway import AirflowError, AirflowGateway, TaskInstanceInfo
from autoair.persistence.documents import DocumentConflict, DocumentStore
from autoair.spec import DagSpec

FAILED_STATES = ("failed", "upstream_failed", "up_for_retry", "skipped", "removed")
Category = Literal[
    "spec", "code", "configuration", "infrastructure", "data", "dependency", "platform", "unknown"
]
Status = Literal["proposed", "no_change", "approved", "rejected", "applied", "awaiting_approval"]


# --------------------------------------------------------------------------- request


class TroubleshootScope(BaseModel):
    """What the agent may read (each also needs the server setting to allow it)."""

    connections: bool = True
    variables: bool = True
    pools: bool = True
    xcom_keys: bool = True
    xcom_values: bool = False
    other_runs: bool = True
    import_errors: bool = True
    airflow_config: bool = False
    filesystem: bool = False
    research: bool = True
    online: bool = False


class TroubleshootRequest(BaseModel):
    dag_id: str = Field(min_length=1, max_length=250)
    run_id: str = Field(min_length=1, max_length=250)
    task_id: str | None = Field(default=None, max_length=250, description="Focus task.")
    map_index: int = -1
    try_number: int | None = Field(default=None, ge=1)
    spec: DagSpec | None = Field(
        default=None, description="The DAG open in the editor (default: the saved spec)."
    )
    question: str | None = Field(default=None, max_length=4_000)
    scope: TroubleshootScope = Field(default_factory=TroubleshootScope)
    mcp: list[McpSelection] = Field(default_factory=list, max_length=10)
    model: str | None = Field(default=None, max_length=200)
    rerun_of: str | None = Field(default=None, max_length=40)
    airflow_actions: ActionsMode | None = Field(
        default=None,
        description="Mode for the proposed Airflow actions; only as permissive as "
        "ai.troubleshoot.airflow_actions (default: that setting).",
    )


def effective_scope(
    requested: TroubleshootScope, settings: TroubleshootSettings, *, online: bool, fs: bool
) -> TroubleshootScope:
    r = settings.resources
    return TroubleshootScope(
        connections=requested.connections and r.connections,
        variables=requested.variables and r.variables,
        pools=requested.pools and r.pools,
        xcom_keys=requested.xcom_keys and r.xcom_keys,
        xcom_values=requested.xcom_values and r.xcom_values,
        other_runs=requested.other_runs and r.other_runs,
        import_errors=requested.import_errors and r.import_errors,
        airflow_config=requested.airflow_config and r.airflow_config,
        filesystem=requested.filesystem and fs,
        research=requested.research,
        online=requested.online and online and requested.research,
    )


# --------------------------------------------------------------------------- report


class Evidence(BaseModel):
    source: str = Field(description='Where: "log:<task>#<try>", "tool:<name>", "spec", …')
    line: int | None = Field(default=None, description="Line number in the log (from the pack).")
    quote: str = Field(default="", description="The relevant line(s), verbatim.")


class Hypothesis(BaseModel):
    title: str
    explanation: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    category: Category = "unknown"
    evidence: list[Evidence] = Field(default_factory=list)


class RemediationStep(BaseModel):
    title: str
    detail: str = ""
    kind: Literal["spec", "code", "config", "airflow", "infrastructure", "other"] = "other"


class ConfigChange(BaseModel):
    target: str = Field(description='What it changes, e.g. "task load: retries".')
    format: Literal["yaml", "json", "env", "text"] = "text"
    snippet: str
    explanation: str = ""


class TroubleshootFinal(BaseModel):
    """``final_answer`` of the troubleshooting agent."""

    answer: str = Field(description="Summary: the root cause in 1-3 sentences.")
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    remediation: list[RemediationStep] = Field(default_factory=list)
    findings: list[_RawFinding] = Field(
        default_factory=list,
        description="Spec/code fixes as findings with JSON patches against the DagSpec.",
    )
    config_changes: list[ConfigChange] = Field(default_factory=list)
    airflow_actions: list[ProposedAction] = Field(
        default_factory=list,
        description="Airflow actions (clear / retry a task instance, clear the run, mark a "
        "state) on the analysed DAG; AutoAir decides whether they may run.",
    )


class Decision(BaseModel):
    user: str | None
    decision: Literal["approve", "reject", "applied"]
    at: str = Field(default_factory=now_iso)
    comment: str | None = None
    finding_ids: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class TroubleshootAnalysis(BaseModel):
    id: str = Field(default_factory=new_id)
    user: str
    created_at: str = Field(default_factory=now_iso)
    dag_id: str
    run_id: str
    task_id: str | None = None
    map_index: int = -1
    question: str | None = None
    rerun_of: str | None = None
    scope: TroubleshootScope
    mcp: list[McpSelection] = Field(default_factory=list)
    run_state: str | None = None
    focus_tasks: list[str] = Field(default_factory=list)
    dag_version: int | None = None
    bundle_version: str | None = None
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)
    tools_used: list[str] = Field(default_factory=list)
    sources: list[Citation] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    stopped: str | None = None
    summary: str = ""
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    remediation: list[RemediationStep] = Field(default_factory=list)
    config_changes: list[ConfigChange] = Field(default_factory=list)
    airflow_actions: list[AirflowAction] = Field(default_factory=list)
    actions_mode: ActionsMode = Field(
        default="read_only", description="How the Airflow actions of this analysis run."
    )
    findings: list[Finding] = Field(default_factory=list)
    base_spec: DagSpec | None = None
    proposed_spec: DagSpec | None = None
    proposed_ok: bool | None = None
    problems: list[SpecProblem] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    code_before: str | None = None
    code_after: str | None = None
    status: Status = "no_change"
    decisions: list[Decision] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    approvals: list[ToolApprovalView] = Field(
        default_factory=list,
        description="Tool approvals of the analysis run (the last one is current); "
        "status awaiting_approval while one is pending.",
    )


class AnalysisSummary(BaseModel):
    id: str
    created_at: str
    dag_id: str
    run_id: str
    task_id: str | None = None
    summary: str = ""
    status: Status
    model: str | None = None


# --------------------------------------------------------------------------- store


def _user_key(user: str) -> str:
    return hashlib.sha256(user.encode()).hexdigest()[:24]


#: Compare-and-set retries of :meth:`AnalysisStore.mutate` (each retry re-reads the analysis;
#: only concurrent writers of the same analysis cause one).
_MUTATE_ATTEMPTS = 20


class AnalysisStore:
    def __init__(self, documents: DocumentStore, max_per_user: int = 300) -> None:
        self.documents = documents
        self.max_per_user = max_per_user
        self._lock = threading.Lock()

    def _key(self, user: str, analysis_id: str) -> str:
        if not analysis_id.isalnum() or len(analysis_id) > 40:
            raise LookupError(analysis_id)
        return f"{_user_key(user)}.{analysis_id}"

    @staticmethod
    def _parse(data: bytes, user: str, analysis_id: str) -> TroubleshootAnalysis:
        try:
            analysis = TroubleshootAnalysis.model_validate_json(data)
        except (ValidationError, ValueError) as exc:
            raise LookupError(analysis_id) from exc
        if analysis.user != user:
            raise LookupError(analysis_id)
        return analysis

    @staticmethod
    def _dump(analysis: TroubleshootAnalysis) -> bytes:
        return json.dumps(analysis.model_dump(mode="json", exclude_none=True)).encode()

    def get(self, user: str, analysis_id: str) -> TroubleshootAnalysis:
        doc = self.documents.get(self._key(user, analysis_id))
        if doc is None:
            raise LookupError(analysis_id)
        return self._parse(doc.data, user, analysis_id)

    def save(self, analysis: TroubleshootAnalysis) -> TroubleshootAnalysis:
        """Write the whole analysis unconditionally (a new analysis, or its only writer)."""
        with self._lock:
            self.documents.put(self._key(analysis.user, analysis.id), self._dump(analysis))
        return analysis

    def mutate(
        self, user: str, analysis_id: str, fn: Callable[[TroubleshootAnalysis], None]
    ) -> TroubleshootAnalysis:
        """Apply ``fn`` to the stored analysis and write it back with compare-and-set on the
        document version, re-reading and re-applying on a conflict. Replicas changing the
        same analysis at once never overwrite each other; ``fn`` may raise to abort."""
        key = self._key(user, analysis_id)
        for _ in range(_MUTATE_ATTEMPTS):
            doc = self.documents.get(key)
            if doc is None:
                raise LookupError(analysis_id)
            analysis = self._parse(doc.data, user, analysis_id)
            fn(analysis)
            data = self._dump(analysis)
            if data == doc.data:
                return analysis
            try:
                self.documents.put(key, data, expected_version=doc.version)
            except DocumentConflict:
                continue
            return analysis
        raise DocumentConflict(f"analysis {analysis_id} keeps changing; try again")

    def claim_action(
        self, user: str, analysis_id: str, action_id: str, by: str, *, timeout: float
    ) -> TroubleshootAnalysis:
        """Atomically mark one action ``running`` for ``by`` before Airflow is called.
        Raises :class:`ActionBusy` when it is already running, done or rejected, and
        ``LookupError`` when the analysis or the action does not exist."""

        def take(analysis: TroubleshootAnalysis) -> None:
            claim(_action_of(analysis, action_id), by, timeout=timeout)

        return self.mutate(user, analysis_id, take)

    def finish_action(
        self,
        user: str,
        analysis_id: str,
        action_id: str,
        result: ActionResult,
        *,
        blocked: BlockReason | None = None,
    ) -> TroubleshootAnalysis:
        """Record the result of a claimed action on the current document (compare-and-set),
        keeping whatever other replicas changed meanwhile."""

        def record(analysis: TroubleshootAnalysis) -> None:
            action = _action_of(analysis, action_id)
            record_result(action, result)
            if blocked is not None:
                action.blocked = blocked
                action.status = "blocked"

        return self.mutate(user, analysis_id, record)

    def list(self, user: str, dag_id: str | None = None, limit: int = 50) -> list[AnalysisSummary]:
        prefix = f"{_user_key(user)}."
        out: list[AnalysisSummary] = []
        keys = self.documents.keys()
        for key in keys:
            if not key.startswith(prefix):
                continue
            doc = self.documents.get(key)
            if doc is None:
                continue
            try:
                a = TroubleshootAnalysis.model_validate_json(doc.data)
            except (ValidationError, ValueError):
                continue
            if a.user != user or (dag_id and a.dag_id != dag_id):
                continue
            out.append(
                AnalysisSummary(
                    id=a.id,
                    created_at=a.created_at,
                    dag_id=a.dag_id,
                    run_id=a.run_id,
                    task_id=a.task_id,
                    summary=a.summary[:300],
                    status=a.status,
                    model=a.model,
                )
            )
        out.sort(key=lambda a: a.created_at, reverse=True)
        return out[:limit]


def _action_of(analysis: TroubleshootAnalysis, action_id: str) -> AirflowAction:
    for action in analysis.airflow_actions:
        if action.id == action_id:
            return action
    raise LookupError(action_id)


_STORES: dict[str, AnalysisStore] = {}
_STORES_LOCK = threading.Lock()


def analysis_store_for(services: Any) -> AnalysisStore:
    from autoair.ai.config import ai_settings_of
    from autoair.persistence import document_store, is_sql

    settings = services.settings
    cfg = ai_settings_of(settings).troubleshoot
    url = cfg.store_url or str(settings.state_dir / "ai-troubleshoot")
    key = f"{'sql' if is_sql(settings) else 'file'}:{url}"
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = AnalysisStore(
                document_store(settings, "ai_troubleshoot", file_url=url)
            )
        return store


# --------------------------------------------------------------------------- evidence


def number_lines(text: str, first: int = 1) -> str:
    return "\n".join(f"{i:>5}: {line}" for i, line in enumerate(text.splitlines(), first))


def log_tail(text: str, max_chars: int) -> tuple[str, int]:
    """The last ``max_chars`` of ``text`` cut at a line start, and its first line number."""
    lines = text.splitlines()
    total = 0
    start = len(lines)
    while start > 0 and total + len(lines[start - 1]) + 1 <= max_chars:
        start -= 1
        total += len(lines[start]) + 1
    return "\n".join(lines[start:]), start + 1


def _upstream(spec: DagSpec | None) -> dict[str, list[str]]:
    """node id -> upstream node ids (from the spec's edges)."""
    if spec is None:
        return {}
    ups: dict[str, list[str]] = {}
    for edge in spec.edges:
        ups.setdefault(edge.target, []).append(edge.source)
    return ups


def _task_id_of(spec: DagSpec | None) -> dict[str, str]:
    """node id -> Airflow task id (task groups prefix their children)."""
    if spec is None:
        return {}
    out: dict[str, str] = {}
    for node in spec.nodes:
        task_id = getattr(node, "task_id", None)
        if task_id:
            out[node.id] = str(task_id)
    return out


def focus_instances(
    tis: Sequence[TaskInstanceInfo], task_id: str | None, map_index: int, limit: int
) -> list[TaskInstanceInfo]:
    if task_id:
        picked = [t for t in tis if t.task_id == task_id]
        if map_index >= 0:
            picked = [t for t in picked if t.map_index == map_index] or picked
        return picked[:limit]
    failed = [t for t in tis if t.state == "failed"]
    if not failed:
        failed = [t for t in tis if t.state in FAILED_STATES]
    if not failed:
        failed = [t for t in tis if t.state not in ("success", None)]
    failed.sort(key=lambda t: t.start_date or "")
    return failed[:limit]


class EvidencePack(BaseModel):
    text: str
    run_state: str | None = None
    focus: list[str] = Field(default_factory=list)
    dag_version: int | None = None
    bundle_version: str | None = None
    notes: list[str] = Field(default_factory=list)
    code: str | None = None


async def collect_evidence(
    gw: AirflowGateway,
    body: TroubleshootRequest,
    spec: DagSpec | None,
    *,
    settings: TroubleshootSettings,
    scope: TroubleshootScope,
    sources: SourceCollector,
    scrub: Callable[[str], str],
    registry: Any,
) -> EvidencePack:
    notes: list[str] = []
    run = await gw.dag_run(body.dag_id, body.run_id)
    if run is None:
        raise LookupError(f"run {body.run_id!r} of {body.dag_id!r} not found in Airflow")
    tis = await gw.task_instances(body.dag_id, body.run_id)
    parts = [
        "## Run",
        json.dumps(
            run.model_dump(exclude_none=True, exclude={"conf"})
            | {"conf_keys": sorted((run.conf or {}).keys())},
            default=str,
        ),
        "## Task instances (task_id[map_index] state try duration operator)",
    ]
    for t in tis:
        idx = f"[{t.map_index}]" if t.map_index >= 0 else ""
        parts.append(
            f"- {t.task_id}{idx} {t.state or 'none'} try={t.try_number} "
            f"duration={t.duration if t.duration is not None else '-'} {t.operator or ''}"
        )
    focus = focus_instances(tis, body.task_id, body.map_index, settings.max_tasks)
    node_task = _task_id_of(spec)
    task_node = {v: k for k, v in node_task.items()}
    ups = _upstream(spec)
    states = {t.task_id: t.state for t in tis}
    for ti in focus:
        idx = f"[{ti.map_index}]" if ti.map_index >= 0 else ""
        parts.append(f"\n## Focus task {ti.task_id}{idx} ({ti.state}, try {ti.try_number})")
        node = task_node.get(ti.task_id)
        if node:
            parts.append(f"DagSpec node id (use it in patches): {node}")
        if node and ups.get(node):
            listed = ", ".join(
                f"{node_task.get(u, u)}={states.get(node_task.get(u, u), 'unknown')}"
                for u in ups[node]
            )
            parts.append(f"Upstream: {listed}")
        if not ti.try_number:
            parts.append("Never started (try 0): no log.")
            continue
        last = await gw.latest_log_try(ti)
        if body.try_number and body.task_id == ti.task_id:
            last = min(last, body.try_number)
        first = max(1, last - settings.max_tries + 1)
        for attempt in range(first, last + 1):
            ref = f"log:{ti.task_id}{idx}#{attempt}"
            try:
                raw = await gw.task_log(body.dag_id, body.run_id, ti.task_id, attempt, ti.map_index)
            except AirflowError as exc:
                notes.append(f"log of {ti.task_id} try {attempt} unavailable: {exc.detail}")
                continue
            tail, start = log_tail(raw, settings.max_log_chars)
            n = sources.add("log", f"{ti.task_id}{idx} try {attempt}", ref)
            parts.append(
                f"### Log {ref} [cite {n}] (lines {start}-{start + tail.count(chr(10))})\n"
                + scrub(number_lines(tail, start))
            )
        if scope.xcom_keys:
            try:
                keys = await gw.xcom_keys(body.dag_id, body.run_id, ti.task_id, ti.map_index)
                parts.append("XCom keys: " + (", ".join(str(k["key"]) for k in keys) or "none"))
            except AirflowError as exc:
                notes.append(f"XCom keys unavailable: {exc.detail}")
    if not focus:
        parts.append("\nNo failed task instance in this run.")
    dag_version: int | None = None
    try:
        versions = await gw.dag_versions(body.dag_id)
        match = next(
            (v for v in versions if run.bundle_version and v.bundle_version == run.bundle_version),
            versions[0] if versions else None,
        )
        if match is not None:
            dag_version = match.version_number
            parts.append(
                f"\n## DAG version {match.version_number} (bundle {match.bundle_name} "
                f"{match.bundle_version or ''})"
            )
    except AirflowError as exc:
        notes.append(f"DAG versions unavailable: {exc.detail}")
    code: str | None = None
    if spec is not None:
        from autoair.ai.assist import compact_spec
        from autoair.codegen import render

        n = sources.add("spec", spec.dag_id, f"spec:{spec.dag_id}")
        parts.append(f"\n## DagSpec [cite {n}]\n" + compact_spec(spec))
        try:
            code = render(spec, registry)
            parts.append("\n## Generated code (head)\n```python\n" + code[:6_000] + "\n```")
        except Exception as exc:
            notes.append(f"code generation failed: {exc}")
    else:
        notes.append("no AutoAir spec for this DAG: proposals can only be instructions")
    if scope.import_errors:
        try:
            errors = await gw.import_errors(body.dag_id)
            if errors:
                parts.append("\n## Import errors")
                for e in errors[:3]:
                    parts.append(f"{e.filename}:\n" + scrub((e.stack_trace or "")[-3_000:]))
        except AirflowError as exc:
            notes.append(f"import errors unavailable: {exc.detail}")
    try:
        version = await gw.version()
        parts.append(f"\n## Environment\nAirflow {version.get('version')}")
        providers = await gw.providers()
        if providers:
            parts.append(
                "Providers: "
                + ", ".join(f"{p['package_name']}=={p['version']}" for p in providers[:60])
            )
    except AirflowError as exc:
        notes.append(f"version/providers unavailable: {exc.detail}")
    if notes:
        parts.append("\n## Notes\n" + "\n".join(f"- {n}" for n in notes))
    return EvidencePack(
        text="\n".join(parts),
        run_state=run.state,
        focus=[t.task_id for t in focus],
        dag_version=dag_version,
        bundle_version=run.bundle_version,
        notes=notes,
        code=code,
    )


# --------------------------------------------------------------------------- tools


class LogInput(BaseModel):
    task_id: str
    try_number: int = Field(default=1, ge=1)
    map_index: int = -1
    mode: Literal["tail", "grep", "lines"] = "tail"
    pattern: str | None = Field(default=None, max_length=300, description="mode=grep: regex.")
    start_line: int = Field(default=1, ge=1)
    line_count: int = Field(default=200, ge=1, le=3_000)


class RunsInput(BaseModel):
    limit: int = Field(default=15, ge=1, le=50)


class TaskHistoryInput(BaseModel):
    task_id: str
    runs: int = Field(default=10, ge=1, le=30)


class ConnInput(BaseModel):
    conn_id: str = Field(max_length=250)


class PatternInput(BaseModel):
    pattern: str | None = Field(default=None, max_length=200, description="Substring filter.")


class XcomInput(BaseModel):
    task_id: str
    map_index: int = -1


class ConfigInput(BaseModel):
    section: str | None = Field(default=None, max_length=100, description="e.g. core, scheduler")


class NoInput(BaseModel):
    pass


def troubleshoot_tools(
    services: Any,
    body: TroubleshootRequest,
    *,
    scope: TroubleshootScope,
    gateway_factory: Callable[[], AirflowGateway],
    sources: SourceCollector,
    scrub: Callable[[str], str],
    spec: DagSpec | None,
    code: str | None,
) -> list[AgentTool]:
    from autoair.ai.assistants.fs_sandbox import FsListInput, FsReadInput, sandbox_for
    from autoair.ai.config import ai_settings_of

    ai = ai_settings_of(services.settings)
    dag_id, run_id = body.dag_id, body.run_id

    async def call(fn: Callable[[AirflowGateway], Any]) -> Any:
        async with gateway_factory() as gw:
            return await fn(gw)

    async def read_log(args: LogInput) -> Any:
        raw = await call(
            lambda gw: gw.task_log(dag_id, run_id, args.task_id, args.try_number, args.map_index)
        )
        lines = raw.splitlines()
        if args.mode == "grep":
            if not args.pattern:
                raise ValueError("mode=grep needs a pattern")
            rx = re.compile(args.pattern, re.IGNORECASE)
            picked = [(i, line) for i, line in enumerate(lines, 1) if rx.search(line)][:300]
        elif args.mode == "lines":
            s = args.start_line - 1
            picked = list(enumerate(lines[s : s + args.line_count], args.start_line))
        else:
            first = max(0, len(lines) - args.line_count)
            picked = list(enumerate(lines[first:], first + 1))
        idx = f"[{args.map_index}]" if args.map_index >= 0 else ""
        ref = f"log:{args.task_id}{idx}#{args.try_number}"
        n = sources.add("log", f"{args.task_id}{idx} try {args.try_number}", ref)
        text = "\n".join(f"{i:>5}: {line[:2000]}" for i, line in picked)
        return {"cite": n, "ref": ref, "total_lines": len(lines), "text": scrub(text[-30_000:])}

    async def task_state_now(_: NoInput) -> Any:
        tis = await call(lambda gw: gw.task_instances(dag_id, run_id))
        return [t.model_dump(exclude_none=True) for t in tis]

    tools = [
        AgentTool(
            "get_task_log",
            "A task log of the analysed run (any task/try/map index): tail, a line window "
            "or grep matches, with line numbers for evidence references.",
            LogInput,
            read_log,
        ),
        AgentTool(
            "get_task_instances",
            "Current task instances of the analysed run (states, tries, durations).",
            NoInput,
            task_state_now,
        ),
    ]

    if spec is not None:

        def current_spec(_: NoInput) -> Any:
            return spec.model_dump(mode="json", exclude_none=True, by_alias=True)

        def generated_code(_: NoInput) -> Any:
            return code or "code generation failed"

        tools += [
            AgentTool("get_current_spec", "The DagSpec of the DAG.", NoInput, current_spec),
            AgentTool(
                "get_generated_code", "The Python file AutoAir generates.", NoInput, generated_code
            ),
        ]

    if scope.other_runs:

        async def other_runs(args: RunsInput) -> Any:
            runs = await call(lambda gw: gw.dag_runs(dag_id, limit=args.limit))
            return [r.model_dump(exclude_none=True, exclude={"conf"}) for r in runs]

        async def task_history(args: TaskHistoryInput) -> Any:
            runs = await call(lambda gw: gw.dag_runs(dag_id, limit=args.runs))

            async def one(run_id_: str) -> Any:
                tis = await call(lambda gw: gw.task_instances(dag_id, run_id_))
                return [
                    {
                        "run_id": run_id_,
                        "state": t.state,
                        "try_number": t.try_number,
                        "duration": t.duration,
                        "map_index": t.map_index,
                    }
                    for t in tis
                    if t.task_id == args.task_id
                ]

            rows = await asyncio.gather(*(one(r.dag_run_id) for r in runs))
            return [row for chunk in rows for row in chunk]

        tools += [
            AgentTool(
                "list_other_runs",
                "Recent runs of this DAG (state, dates, bundle version): flakiness, trends.",
                RunsInput,
                other_runs,
            ),
            AgentTool(
                "get_task_history",
                "State, tries and duration of one task over recent runs of this DAG.",
                TaskHistoryInput,
                task_history,
            ),
        ]
    if scope.connections:

        async def connection(args: ConnInput) -> Any:
            meta = await call(lambda gw: gw.connection_metadata(args.conn_id))
            if meta is None:
                return f"connection {args.conn_id!r} does not exist in Airflow"
            n = sources.add("config", f"connection {args.conn_id}", f"connection:{args.conn_id}")
            return {"cite": n, **meta.model_dump(by_alias=True, exclude_none=True)}

        tools.append(
            AgentTool(
                "get_connection_metadata",
                "A connection's conn_type, host, schema, port and the NAMES of its extra "
                "fields (never logins, passwords or values) - or that it does not exist.",
                ConnInput,
                connection,
            )
        )
    if scope.variables:

        async def variables(args: PatternInput) -> Any:
            items = await call(lambda gw: gw.variables())
            keys = [v.key for v in items if not args.pattern or args.pattern in v.key]
            return {"keys": keys[:300], "note": "values are never shown"}

        tools.append(
            AgentTool(
                "list_variable_keys",
                "Airflow variable keys (values are never shown).",
                PatternInput,
                variables,
            )
        )
    if scope.pools:

        async def pools(_: NoInput) -> Any:
            items = await call(lambda gw: gw.pool_states())
            return [p.model_dump(exclude_none=True) for p in items]

        tools.append(
            AgentTool(
                "get_pools",
                "Pools with slots, running/queued/scheduled/deferred and open slots.",
                NoInput,
                pools,
            )
        )
    if scope.xcom_keys or scope.xcom_values:

        async def xcom(args: XcomInput) -> Any:
            if scope.xcom_values:
                entries = await call(
                    lambda gw: gw.xcom_entries(dag_id, run_id, args.task_id, args.map_index)
                )
                out = []
                for e in entries:
                    value = json.dumps(e.value, default=str)
                    out.append({"key": e.key, "value": scrub(value[:2_000])})
                return out
            return await call(lambda gw: gw.xcom_keys(dag_id, run_id, args.task_id, args.map_index))

        tools.append(
            AgentTool(
                "get_xcom",
                "XCom keys of a task of the analysed run"
                + (" with values (truncated, scrubbed)." if scope.xcom_values else " (no values)."),
                XcomInput,
                xcom,
            )
        )
    if scope.import_errors:

        async def import_errors(args: PatternInput) -> Any:
            errors = await call(lambda gw: gw.import_errors(args.pattern or None))
            return [
                {"filename": e.filename, "stack_trace": scrub((e.stack_trace or "")[-4_000:])}
                for e in errors[:20]
            ]

        tools.append(
            AgentTool(
                "get_import_errors",
                "DAG import errors (filter by a file name substring).",
                PatternInput,
                import_errors,
            )
        )
    if scope.airflow_config:

        async def airflow_config(args: ConfigInput) -> Any:
            sections = await call(lambda gw: gw.config_section(args.section))
            n = sources.add(
                "config", f"airflow config {args.section or ''}".strip(), "airflow:config"
            )
            return {"cite": n, "sections": json.loads(scrub(json.dumps(sections, default=str)))}

        tools.append(
            AgentTool(
                "get_airflow_config",
                "Airflow's configuration of a section (sensitive values masked by Airflow).",
                ConfigInput,
                airflow_config,
            )
        )
    if scope.filesystem:
        sandbox = sandbox_for(services, ai.troubleshoot.filesystem)
        if sandbox.enabled:

            def fs_list(args: FsListInput) -> Any:
                return sandbox.list(args.path, args.pattern)

            def fs_read(args: FsReadInput) -> Any:
                out = sandbox.read(args)
                n = sources.add("file", str(out["path"]).rsplit("/", 1)[-1], str(out["path"]))
                return {"cite": n, **out}

            tools += [
                AgentTool(
                    "fs_list",
                    "List a folder of the allowed log/DAG folders (read-only sandbox).",
                    FsListInput,
                    fs_list,
                ),
                AgentTool(
                    "fs_read",
                    "Read a file in the allowed folders: tail/head/lines/grep with line "
                    "numbers (size-limited, secrets scrubbed).",
                    FsReadInput,
                    fs_read,
                ),
            ]
    if scope.research:
        from autoair.ai.research.tools import research_tools

        tools += research_tools(services, online=scope.online)
    from autoair.ai.toolbox import build_toolbox

    keep = {"search_catalog", "get_operator", "validate_spec", "lint_spec"}
    tools += [t for t in build_toolbox(services) if t.name in keep]
    return tools


# --------------------------------------------------------------------------- result


def build_analysis(
    services: Any,
    body: TroubleshootRequest,
    *,
    user: str,
    scope: TroubleshootScope,
    pack: EvidencePack,
    spec: DagSpec | None,
    result: Any,
    sources: SourceCollector,
    notes: Sequence[str],
    actions_mode: ActionsMode = "read_only",
    can: Callable[[str], bool] = lambda _p: False,
    feature: Callable[[str], bool] = lambda _f: False,
) -> TroubleshootAnalysis:
    from autoair.ai.config import ai_settings_of
    from autoair.validation import policy_from_settings

    extra = result.extra or {}

    def parse(model: type[BaseModel], items: Any) -> list[Any]:
        out = []
        for item in items or []:
            try:
                out.append(model.model_validate(item))
            except ValidationError:
                continue
        return out

    analysis = TroubleshootAnalysis(
        user=user,
        dag_id=body.dag_id,
        run_id=body.run_id,
        task_id=body.task_id,
        map_index=body.map_index,
        question=body.question,
        rerun_of=body.rerun_of,
        scope=scope,
        mcp=list(body.mcp),
        run_state=pack.run_state,
        focus_tasks=pack.focus,
        dag_version=pack.dag_version,
        bundle_version=pack.bundle_version,
        model=result.model,
        usage=result.usage,
        tools_used=sorted(set(sources.used_tools)),
        sources=sources.citations,
        steps=result.steps,
        stopped=None if result.stopped in ("answer", "approval") else result.stopped,
        summary=result.answer.strip(),
        hypotheses=sorted(parse(Hypothesis, extra.get("hypotheses")), key=lambda h: -h.confidence),
        remediation=parse(RemediationStep, extra.get("remediation")),
        config_changes=parse(ConfigChange, extra.get("config_changes")),
        airflow_actions=[],
        actions_mode=actions_mode,
        findings=list(result.findings),
        base_spec=spec,
        code_before=pack.code,
        notes=[*pack.notes, *notes],
        guardrails=list(result.guardrails),
    )
    raw_actions = list(extra.get("airflow_actions") or [])
    proposed = parse(ProposedAction, raw_actions)
    if len(proposed) < len(raw_actions):
        analysis.notes.append(
            f"{len(raw_actions) - len(proposed)} proposed Airflow action(s) did not match the "
            "action schema and were dropped"
        )
    analysis.airflow_actions = vet_actions(
        proposed,
        dag_id=body.dag_id,
        run_id=body.run_id,
        mode=actions_mode,
        settings=ai_settings_of(services.settings).troubleshoot,
        can=can,
        feature=feature,
    )
    if spec is not None and any(f.patch for f in analysis.findings):
        applied = apply_findings(
            spec,
            analysis.findings,
            catalog=services.catalog,
            registry=services.registry,
            policy=policy_from_settings(services.settings),
        )
        analysis.proposed_spec = applied.spec
        analysis.proposed_ok = applied.ok
        analysis.problems = applied.problems
        analysis.skipped = applied.skipped
        if applied.spec is not None:
            from autoair.codegen import render

            try:
                analysis.code_after = render(applied.spec, services.registry)
            except Exception as exc:
                analysis.notes.append(f"code generation of the proposal failed: {exc}")
    has_proposal = analysis.proposed_spec is not None or bool(analysis.config_changes)
    analysis.status = "proposed" if has_proposal else "no_change"
    return analysis


__all__ = [
    "ActionBusy",
    "AirflowAction",
    "AnalysisStore",
    "AnalysisSummary",
    "Decision",
    "EvidencePack",
    "TroubleshootAnalysis",
    "TroubleshootFinal",
    "TroubleshootRequest",
    "TroubleshootScope",
    "analysis_store_for",
    "build_analysis",
    "collect_evidence",
    "effective_scope",
    "focus_instances",
    "log_tail",
    "troubleshoot_tools",
]
