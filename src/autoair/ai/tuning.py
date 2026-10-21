"""Parameter tuning from Airflow run history (retries, timeouts, concurrency, deferrable).

:func:`fetch_history` reads recent runs + task instances through the Airflow REST API;
:func:`history_stats` aggregates them; :func:`tuning_findings` turns the numbers into
findings with patches. All heuristics are deterministic - the LLM (if configured) only
refines and explains them.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, Field

from autoair.ai.findings import Finding, PatchOp, finding_id, sort_findings
from autoair.catalog import OperatorCatalog
from autoair.gateway import AirflowGateway, DagRunInfo, TaskInstanceInfo
from autoair.spec import DagSpec, OperatorNode

MIN_SAMPLES = 3


class RunRecord(BaseModel):
    run: DagRunInfo
    tasks: list[TaskInstanceInfo] = Field(default_factory=list)


class TaskStats(BaseModel):
    task_id: str
    samples: int = 0
    success: int = 0
    failed: int = 0
    retried_then_succeeded: int = 0
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    max_seconds: float | None = None


class HistoryStats(BaseModel):
    dag_id: str
    runs: int = 0
    run_success_rate: float | None = None
    run_p50_seconds: float | None = None
    run_p95_seconds: float | None = None
    tasks: list[TaskStats] = Field(default_factory=list)

    def task(self, task_id: str) -> TaskStats | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)


async def fetch_history(gateway: AirflowGateway, dag_id: str, runs: int = 15) -> list[RunRecord]:
    """Recent finished runs with their task instances (one request per run, concurrent)."""
    dag_runs = [r for r in await gateway.dag_runs(dag_id, limit=runs) if r.end_date]

    async def one(run: DagRunInfo) -> RunRecord:
        return RunRecord(run=run, tasks=await gateway.task_instances(dag_id, run.dag_run_id))

    return list(await asyncio.gather(*(one(r) for r in dag_runs)))


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        a = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (b - a).total_seconds())


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def history_stats(dag_id: str, history: Sequence[RunRecord]) -> HistoryStats:
    durations: list[float] = []
    ok = 0
    per_task: dict[str, list[TaskInstanceInfo]] = defaultdict(list)
    for record in history:
        d = _seconds(record.run.start_date, record.run.end_date)
        if d is not None:
            durations.append(d)
        ok += record.run.state == "success"
        for ti in record.tasks:
            per_task[ti.task_id].append(ti)
    tasks: list[TaskStats] = []
    for task_id, tis in sorted(per_task.items()):
        times = [
            t.duration if t.duration is not None else (_seconds(t.start_date, t.end_date) or 0)
            for t in tis
            if t.state == "success"
        ]
        tasks.append(
            TaskStats(
                task_id=task_id,
                samples=len(tis),
                success=sum(t.state == "success" for t in tis),
                failed=sum(t.state in ("failed", "upstream_failed") for t in tis),
                retried_then_succeeded=sum(t.state == "success" and t.try_number > 1 for t in tis),
                p50_seconds=round(statistics.median(times), 1) if times else None,
                p95_seconds=_pct(times, 0.95),
                max_seconds=round(max(times), 1) if times else None,
            )
        )
    return HistoryStats(
        dag_id=dag_id,
        runs=len(history),
        run_success_rate=round(ok / len(history), 3) if history else None,
        run_p50_seconds=round(statistics.median(durations), 1) if durations else None,
        run_p95_seconds=_pct(durations, 0.95),
        tasks=tasks,
    )


def suggested_timeout(p95: float, peak: float | None) -> int:
    """3x p95 (at least 5 minutes, at least 1.5x the slowest run), rounded up to 5 min."""
    base = max(p95 * 3, (peak or 0) * 1.5, 300)
    return int(math.ceil(base / 300) * 300)


def _path(node_id: str, field: str) -> str:
    return f"/nodes/@{node_id.replace('~', '~0').replace('/', '~1')}/{field}"


def tuning_findings(
    spec: DagSpec, stats: HistoryStats, catalog: OperatorCatalog | None = None
) -> list[Finding]:
    findings: list[Finding] = []

    def add(
        rule: str,
        severity: str,
        category: str,
        title: str,
        message: str,
        *,
        node_id: str | None = None,
        field: str | None = None,
        patch: list[PatchOp] | None = None,
    ) -> None:
        path = _path(node_id, field) if node_id and field else (f"/{field}" if field else None)
        findings.append(
            Finding.model_validate(
                {
                    "id": finding_id(rule, node_id, field),
                    "severity": severity,
                    "category": category,
                    "title": title,
                    "message": message,
                    "node_id": node_id,
                    "field": field,
                    "path": path,
                    "rule": rule,
                    "patch": patch or [],
                }
            )
        )

    by_task = {t.task_id: t for t in spec.task_nodes(include_disabled=False)}
    default_timeout = spec.default_args.execution_timeout_seconds
    default_retries = spec.default_args.retries
    for ts in stats.tasks:
        node = by_task.get(ts.task_id)
        if node is None or ts.samples < MIN_SAMPLES:
            continue
        if ts.p95_seconds is not None:
            timeout = node.execution_timeout_seconds or default_timeout
            target = suggested_timeout(ts.p95_seconds, ts.max_seconds)
            if timeout is None:
                add(
                    "tune-timeout-missing",
                    "medium",
                    "reliability",
                    f"Set execution_timeout for {ts.task_id}",
                    f"p95 duration {ts.p95_seconds:.0f}s (max {ts.max_seconds or 0:.0f}s over "
                    f"{ts.success} successful runs); suggested timeout {target}s.",
                    node_id=node.id,
                    field="execution_timeout_seconds",
                    patch=[
                        PatchOp(
                            op="add",
                            path=_path(node.id, "execution_timeout_seconds"),
                            value=target,
                        )
                    ],
                )
            elif ts.max_seconds is not None and timeout < ts.max_seconds * 1.2:
                add(
                    "tune-timeout-tight",
                    "high",
                    "reliability",
                    f"execution_timeout of {ts.task_id} is too tight",
                    f"Timeout {timeout:.0f}s vs slowest successful run {ts.max_seconds:.0f}s; "
                    f"normal variance will time it out. Suggested {target}s.",
                    node_id=node.id,
                    field="execution_timeout_seconds",
                    patch=[
                        PatchOp(
                            op="add",
                            path=_path(node.id, "execution_timeout_seconds"),
                            value=target,
                        )
                    ],
                )
            elif timeout > target * 8 and timeout > 3600:
                add(
                    "tune-timeout-loose",
                    "low",
                    "cost",
                    f"execution_timeout of {ts.task_id} is very loose",
                    f"Timeout {timeout:.0f}s vs p95 {ts.p95_seconds:.0f}s: a hung run holds a "
                    f"slot for a long time before failing. Suggested {target}s.",
                    node_id=node.id,
                    field="execution_timeout_seconds",
                    patch=[
                        PatchOp(
                            op="add",
                            path=_path(node.id, "execution_timeout_seconds"),
                            value=target,
                        )
                    ],
                )
        retries = node.retries if node.retries is not None else default_retries
        flaky = ts.retried_then_succeeded
        if flaky and (retries or 0) < 3:
            add(
                "tune-retries-flaky",
                "medium",
                "reliability",
                f"{ts.task_id} is flaky: raise retries",
                f"{flaky} of {ts.samples} runs succeeded only after a retry. Use 3 retries with "
                "exponential backoff.",
                node_id=node.id,
                field="retries",
                patch=[
                    PatchOp(op="add", path=_path(node.id, "retries"), value=3),
                    PatchOp(op="add", path=_path(node.id, "retry_exponential_backoff"), value=2.0),
                ],
            )
        hard_fail = ts.failed - flaky
        if ts.failed >= 2 and ts.success == 0:
            add(
                "tune-always-failing",
                "high",
                "correctness",
                f"{ts.task_id} fails every time",
                f"Failed in {ts.failed} of {ts.samples} recent runs and never succeeded: this is "
                "a bug, not a transient error - retries only delay the failure. Use AI > Fix "
                "with the task log.",
                node_id=node.id,
                field=None,
            )
        elif hard_fail > 0 and (retries or 0) == 0 and ts.failed / ts.samples >= 0.2:
            add(
                "tune-retries-none",
                "medium",
                "reliability",
                f"{ts.task_id} fails often without retries",
                f"Failed {ts.failed} of {ts.samples} runs with retries=0.",
                node_id=node.id,
                field="retries",
                patch=[PatchOp(op="add", path=_path(node.id, "retries"), value=2)],
            )
        if isinstance(node, OperatorNode) and catalog is not None and ts.p50_seconds:
            detail = catalog.get(node.operator)
            if (
                detail is not None
                and detail.deferrable_param
                and node.params.get("deferrable") is not True
                and ts.p50_seconds >= 300
            ):
                add(
                    "tune-deferrable",
                    "medium",
                    "cost",
                    f"Run {ts.task_id} deferrable",
                    f"It mostly waits (p50 {ts.p50_seconds:.0f}s) and supports deferrable mode, "
                    "which frees the worker slot while waiting.",
                    node_id=node.id,
                    field="deferrable",
                    patch=[PatchOp(op="add", path=_path(node.id, "params/deferrable"), value=True)],
                )
    if stats.run_p95_seconds and stats.runs >= MIN_SAMPLES:
        target = suggested_timeout(stats.run_p95_seconds, None)
        current = spec.dagrun_timeout_seconds
        if current is None or current < stats.run_p95_seconds * 1.2:
            add(
                "tune-dagrun-timeout",
                "low",
                "reliability",
                "Set dagrun_timeout from run history",
                f"Run p95 {stats.run_p95_seconds:.0f}s; suggested dagrun_timeout {target}s.",
                field="dagrun_timeout_seconds",
                patch=[PatchOp(op="add", path="/dagrun_timeout_seconds", value=target)],
            )
    return sort_findings(findings)
