"""Airflow actions proposed by the troubleshooting agent: schema, vetting and execution.

The agent proposes actions in its structured ``final_answer`` (:class:`ProposedAction`, a
strict schema: unknown fields and out-of-range values are rejected). Log content is
untrusted (a log line can say "mark everything successful"), so a proposal is only data:

* :func:`vet_actions` turns proposals into :class:`AirflowAction` records and decides which
  of them may run: the type must be in ``ai.troubleshoot.allowed_actions``, the target
  must be the analysed DAG (and run, unless ``actions_other_runs``), upstream/downstream
  clears must be allowed, at most ``max_actions`` per analysis, and the caller must hold
  ``run:manage`` with ``features.runs`` on - exactly what the same user could do in the UI.
* :func:`execute_action` runs one vetted action through :class:`AirflowGateway` with the
  caller's own Airflow token (Airflow's RBAC applies on top). It reads the current state
  first, so running an action twice does not clear a task that is already running again,
  and checks a clear with a dry run against ``max_cleared_task_instances``.

Modes (``ai.troubleshoot.airflow_actions``): ``read_only`` shows instructions only, ``hitl``
runs an action when the user approves it, ``apply`` runs the vetted set when the analysis
completes. A request may only pick a more restrictive mode (:func:`effective_mode`).

A ``hitl`` execution first claims the action (:func:`claim`: ``status="running"`` plus the
claimer, written with the analysis document's compare-and-set), so two replicas approving
the same action at once call Airflow only once; the other gets :class:`ActionBusy`.

Results carry a stable ``code`` and ``params`` next to the English ``message``, so the UI
can show them in the user's language (``ai`` namespace, ``troubleshoot.result.<code>``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from autoair.ai.assistants.threads import now_iso
from autoair.ai.config import TroubleshootActionsMode, TroubleshootActionType, TroubleshootSettings
from autoair.gateway import AirflowError, AirflowGateway, TaskInstanceInfo
from autoair.iam import model as P

ActionsMode = TroubleshootActionsMode
ActionKind = Literal[
    "clear",
    "retry",
    "clear_run",
    "mark_success",
    "mark_failed",
    "mark_run_success",
    "mark_run_failed",
    "fix_connection",
    "adjust_pool",
    "other",
]
#: Action types AutoAir can execute (the others are always instructions).
EXECUTABLE: frozenset[str] = frozenset(get_args(TroubleshootActionType))
TASK_ACTIONS = frozenset({"clear", "retry", "mark_success", "mark_failed"})
CLEAR_ACTIONS = frozenset({"clear", "retry", "clear_run"})
MODES: tuple[ActionsMode, ...] = ("read_only", "hitl", "apply")
#: Permission an executable action needs (the same as the run operations of the UI).
ACTION_PERMISSION = P.RUN_MANAGE
#: Feature switch the run operations need.
ACTION_FEATURE = "runs"

#: Task instance states in which a clear would re-run something that is already going.
ACTIVE_STATES = frozenset(
    {
        "running",
        "queued",
        "scheduled",
        "up_for_retry",
        "up_for_reschedule",
        "restarting",
        "deferred",
    }
)
RETRYABLE_STATES = frozenset({"failed", "upstream_failed"})
ACTIVE_RUN_STATES = frozenset({"running", "queued"})

ActionStatus = Literal[
    "instruction", "proposed", "blocked", "rejected", "running", "succeeded", "skipped", "failed"
]
#: Message codes of :class:`ActionResult` (the UI translates each as
#: ``troubleshoot.result.<code>``; ``blocked`` uses the block reason's text).
RESULT_CODES = frozenset(
    {
        "ti_not_found",
        "already_active",
        "not_failed",
        "not_started",
        "nothing_to_clear",
        "over_limit",
        "cleared",
        "run_not_found",
        "run_active",
        "already_state",
        "marked",
        "run_already_state",
        "run_marked",
        "no_run",
        "no_task",
        "airflow_refused",
        "not_executable",
        "blocked",
        "error",
    }
)
#: Statuses after which executing again does nothing.
DONE_STATUSES = frozenset({"succeeded", "skipped"})
BlockReason = Literal[
    "not_allowed",
    "other_dag",
    "other_run",
    "missing_task",
    "upstream_not_allowed",
    "downstream_not_allowed",
    "limit",
    "duplicate",
    "permission",
    "feature_off",
]
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


class ProposedAction(BaseModel):
    """One Airflow action in the agent's ``final_answer`` (strict: no other fields)."""

    model_config = ConfigDict(extra="forbid")

    action: ActionKind = Field(
        default="other",
        description="clear / retry (a task instance of the analysed run), clear_run, "
        "mark_success / mark_failed (task instance), mark_run_success / mark_run_failed, "
        "or fix_connection / adjust_pool / other (instructions only).",
    )
    target: str = Field(default="", max_length=250, description="Human-readable target.")
    instructions: str = Field(max_length=2_000, description="What to do and why.")
    dag_id: str | None = Field(
        default=None, max_length=250, description="Must be the analysed DAG (default)."
    )
    run_id: str | None = Field(
        default=None, max_length=250, description="Default: the analysed run."
    )
    task_id: str | None = Field(
        default=None, max_length=250, description="Task-level actions: the task id."
    )
    map_index: int = Field(default=-1, ge=-1, le=100_000)
    include_downstream: bool = False
    include_upstream: bool = False
    only_failed: bool | None = Field(
        default=None,
        description="Clears: only failed task instances (default: true for clear_run and "
        "retry, false for clear).",
    )


class ActionResult(BaseModel):
    status: Literal["succeeded", "skipped", "failed"]
    message: str = Field(default="", description="English text (logs, audit, older clients).")
    code: str | None = Field(
        default=None,
        description="Stable message code; the UI shows troubleshoot.result.<code> with params.",
    )
    params: dict[str, str | int] = Field(
        default_factory=dict, description="Interpolation values of the code's message."
    )
    at: str = Field(default_factory=now_iso)
    by: str | None = None
    automatic: bool = Field(default=False, description="Executed by apply mode.")
    affected: int | None = Field(default=None, description="Task instances changed.")


class AirflowAction(BaseModel):
    """A proposed Airflow action as stored with the analysis (older analyses only have
    ``action``, ``target`` and ``instructions``)."""

    id: str = ""
    action: ActionKind = "other"
    target: str = ""
    instructions: str = ""
    dag_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    map_index: int = -1
    include_downstream: bool = False
    include_upstream: bool = False
    only_failed: bool | None = None
    status: ActionStatus = "instruction"
    blocked: BlockReason | None = Field(
        default=None, description="Why the action cannot be executed."
    )
    results: list[ActionResult] = Field(default_factory=list)
    claimed_by: str | None = Field(
        default=None, description="Who claimed the last execution (status running)."
    )
    claimed_at: str | None = Field(default=None, description="When that claim was made.")

    @property
    def executable(self) -> bool:
        return self.action in EXECUTABLE

    def effective_only_failed(self) -> bool:
        if self.only_failed is not None:
            return self.only_failed
        return self.action in ("retry", "clear_run")


def action_id(action: AirflowAction) -> str:
    key = json.dumps(
        [
            action.action,
            action.dag_id,
            action.run_id,
            action.task_id,
            action.map_index,
            action.include_downstream,
            action.include_upstream,
            action.only_failed,
            "" if action.executable else action.target + action.instructions,
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def effective_mode(
    setting: ActionsMode,
    requested: ActionsMode | None = None,
    *,
    allowed: bool = True,
) -> ActionsMode:
    """The server's mode, lowered to the request's choice; ``read_only`` when the caller
    may not run actions (``allowed`` false: no run:manage or features.runs off)."""
    if not allowed:
        return "read_only"
    rank = MODES.index(setting)
    if requested is not None:
        rank = min(rank, MODES.index(requested))
    return MODES[rank]


def _looks_like_task_id(value: str) -> bool:
    return bool(value) and len(value) <= 250 and set(value) <= _ID_CHARS


def block_reason(
    action: AirflowAction,
    *,
    dag_id: str,
    run_id: str,
    settings: TroubleshootSettings,
    can: Callable[[str], bool],
    feature: Callable[[str], bool],
) -> BlockReason | None:
    """Why ``action`` may not be executed for this caller (``None``: it may)."""
    if action.action not in settings.allowed_actions:
        return "not_allowed"
    if action.dag_id != dag_id:
        return "other_dag"
    if action.run_id != run_id and not settings.actions_other_runs:
        return "other_run"
    if action.action in TASK_ACTIONS and not action.task_id:
        return "missing_task"
    if action.action in ("clear", "retry"):
        if action.include_upstream and not settings.allow_include_upstream:
            return "upstream_not_allowed"
        if action.include_downstream and not settings.allow_include_downstream:
            return "downstream_not_allowed"
    if not feature(ACTION_FEATURE):
        return "feature_off"
    if not can(ACTION_PERMISSION):
        return "permission"
    return None


def vet_actions(
    proposed: Sequence[ProposedAction],
    *,
    dag_id: str,
    run_id: str,
    mode: ActionsMode,
    settings: TroubleshootSettings,
    can: Callable[[str], bool],
    feature: Callable[[str], bool],
) -> list[AirflowAction]:
    """Stored actions for the proposals: instructions in ``read_only`` mode, else
    ``proposed`` (may run) or ``blocked`` (with the reason)."""
    out: list[AirflowAction] = []
    seen: set[str] = set()
    runnable = 0
    for p in proposed:
        task_id = p.task_id
        if task_id is None and p.action in TASK_ACTIONS and _looks_like_task_id(p.target):
            task_id = p.target
        action = AirflowAction(
            action=p.action,
            target=p.target or task_id or "",
            instructions=p.instructions,
            dag_id=p.dag_id or dag_id,
            run_id=p.run_id or run_id,
            task_id=task_id if p.action in TASK_ACTIONS else None,
            map_index=p.map_index if p.action in TASK_ACTIONS else -1,
            include_downstream=p.include_downstream and p.action in ("clear", "retry"),
            include_upstream=p.include_upstream and p.action in ("clear", "retry"),
            only_failed=p.only_failed if p.action in CLEAR_ACTIONS else None,
        )
        action.id = action_id(action)
        duplicate = action.id in seen
        seen.add(action.id)
        if not action.executable or mode == "read_only":
            if not duplicate:
                out.append(action)
            continue
        reason: BlockReason | None = "duplicate" if duplicate else None
        if reason is None:
            reason = block_reason(
                action, dag_id=dag_id, run_id=run_id, settings=settings, can=can, feature=feature
            )
        if reason is None and runnable >= settings.max_actions:
            reason = "limit"
        if reason == "duplicate":
            continue
        action.status = "blocked" if reason else "proposed"
        action.blocked = reason
        runnable += reason is None
        out.append(action)
    return out


# --------------------------------------------------------------------------- execution


def _matching(tis: Sequence[TaskInstanceInfo], action: AirflowAction) -> list[TaskInstanceInfo]:
    picked = [t for t in tis if t.task_id == action.task_id]
    if action.map_index >= 0:
        picked = [t for t in picked if t.map_index == action.map_index]
    return picked


def _count(data: dict[str, Any]) -> int:
    items = data.get("task_instances")
    if isinstance(items, list):
        return len(items)
    total = data.get("total_entries")
    return int(total) if isinstance(total, int) else 0


def _result(
    status: Literal["succeeded", "skipped", "failed"],
    code: str,
    message: str,
    affected: int | None = None,
    **params: str | int,
) -> ActionResult:
    return ActionResult(status=status, code=code, message=message, params=params, affected=affected)


def _skip(code: str, message: str, **params: str | int) -> ActionResult:
    return ActionResult(status="skipped", code=code, message=message, params=params)


def _over_limit(count: int, limit: int) -> ActionResult:
    return _result(
        "failed",
        "over_limit",
        f"would clear {count} task instances, more than the limit of {limit} "
        "(ai.troubleshoot.max_cleared_task_instances)",
        count=count,
        limit=limit,
    )


def _cleared(affected: int) -> ActionResult:
    return _result(
        "succeeded",
        "cleared",
        f"cleared {affected} task instance(s)",
        affected=affected,
        count=affected,
    )


_TI_NOT_FOUND = ("ti_not_found", "task instance not found in the run")
_RUN_NOT_FOUND = ("run_not_found", "run not found")


async def _clear_task(
    gw: AirflowGateway, action: AirflowAction, settings: TroubleshootSettings
) -> ActionResult:
    dag, run, task = action.dag_id or "", action.run_id or "", action.task_id or ""
    tis = _matching(await gw.task_instances(dag, run), action)
    if not tis:
        return _result("failed", *_TI_NOT_FOUND)
    active = sorted({str(t.state) for t in tis if t.state in ACTIVE_STATES})
    if active:
        states = ", ".join(active)
        return _skip("already_active", f"already {states}: not cleared again", states=states)
    if action.action == "retry" and not any(t.state in RETRYABLE_STATES for t in tis):
        states = ", ".join(sorted({str(t.state or "none") for t in tis}))
        return _skip("not_failed", f"not failed ({states}): nothing to retry", states=states)
    if all(t.state is None for t in tis):
        return _skip("not_started", "not started yet: nothing to clear")
    kwargs: dict[str, Any] = {
        "map_index": action.map_index,
        "only_failed": action.effective_only_failed(),
        "include_downstream": action.include_downstream,
        "include_upstream": action.include_upstream,
    }
    dry = await gw.clear_task_instances(dag, run, task, dry_run=True, **kwargs)
    count = _count(dry)
    if count == 0:
        return _skip("nothing_to_clear", "nothing to clear")
    if count > settings.max_cleared_task_instances:
        return _over_limit(count, settings.max_cleared_task_instances)
    done = await gw.clear_task_instances(dag, run, task, **kwargs)
    return _cleared(_count(done) or count)


async def _clear_run(
    gw: AirflowGateway, action: AirflowAction, settings: TroubleshootSettings
) -> ActionResult:
    dag, run_id = action.dag_id or "", action.run_id or ""
    run = await gw.dag_run(dag, run_id)
    if run is None:
        return _result("failed", *_RUN_NOT_FOUND)
    if run.state in ACTIVE_RUN_STATES:
        return _skip(
            "run_active", f"run is already {run.state}: not cleared again", state=str(run.state)
        )
    only_failed = action.effective_only_failed()
    dry = await gw.clear_run(dag, run_id, only_failed=only_failed, dry_run=True)
    count = _count(dry)
    if count == 0:
        return _skip("nothing_to_clear", "nothing to clear")
    if count > settings.max_cleared_task_instances:
        return _over_limit(count, settings.max_cleared_task_instances)
    done = await gw.clear_run(dag, run_id, only_failed=only_failed)
    return _cleared(_count(done) or count)


async def _mark_task(gw: AirflowGateway, action: AirflowAction) -> ActionResult:
    dag, run, task = action.dag_id or "", action.run_id or "", action.task_id or ""
    state = "success" if action.action == "mark_success" else "failed"
    tis = _matching(await gw.task_instances(dag, run), action)
    if not tis:
        return _result("failed", *_TI_NOT_FOUND)
    if all(t.state == state for t in tis):
        return _skip("already_state", f"already {state}", state=state)
    await gw.set_task_instance_state(dag, run, task, state, map_index=action.map_index)
    return _result("succeeded", "marked", f"marked {state}", affected=len(tis), state=state)


async def _mark_run(gw: AirflowGateway, action: AirflowAction) -> ActionResult:
    dag, run_id = action.dag_id or "", action.run_id or ""
    state = "success" if action.action == "mark_run_success" else "failed"
    run = await gw.dag_run(dag, run_id)
    if run is None:
        return _result("failed", *_RUN_NOT_FOUND)
    if run.state == state:
        return _skip("run_already_state", f"run is already {state}", state=state)
    await gw.set_dag_run_state(dag, run_id, state)
    return _result("succeeded", "run_marked", f"run marked {state}", state=state)


async def execute_action(
    gw: AirflowGateway, action: AirflowAction, settings: TroubleshootSettings
) -> ActionResult:
    """Run one vetted action (state checked first); Airflow errors become a failed result."""
    if not action.dag_id or not action.run_id:
        return _result("failed", "no_run", "the action has no DAG or run")
    if action.action in TASK_ACTIONS and not action.task_id:
        return _result("failed", "no_task", "the action has no task")
    try:
        if action.action in ("clear", "retry"):
            return await _clear_task(gw, action, settings)
        if action.action == "clear_run":
            return await _clear_run(gw, action, settings)
        if action.action in ("mark_success", "mark_failed"):
            return await _mark_task(gw, action)
        if action.action in ("mark_run_success", "mark_run_failed"):
            return await _mark_run(gw, action)
    except AirflowError as exc:
        detail = str(exc.detail)[:1_000]
        message = f"Airflow refused: {detail}"[:1_000]
        return _result("failed", "airflow_refused", message, detail=detail)
    return _result(
        "failed", "not_executable", f"{action.action} cannot be executed", action=action.action
    )


def blocked_result(reason: str) -> ActionResult:
    return _result("failed", "blocked", f"blocked: {reason}", reason=reason)


def record_result(action: AirflowAction, result: ActionResult) -> AirflowAction:
    action.results.append(result)
    action.status = result.status
    return action


class ActionBusy(Exception):
    """:func:`claim` refused: the action is ``running`` (claimed by someone else, claim not
    expired), already ``done`` (succeeded / skipped) or ``rejected``."""

    def __init__(
        self, state: Literal["running", "done", "rejected"], by: str | None = None
    ) -> None:
        super().__init__(f"the action is {state}" + (f" (claimed by {by})" if by else ""))
        self.state = state
        self.by = by


def claim_expired(claimed_at: str | None, timeout: float, now: datetime) -> bool:
    """Whether a claim made at ``claimed_at`` is older than ``timeout`` seconds (or has no
    readable time). Shared with tool approvals (:mod:`autoair.ai.approvals`)."""
    if not claimed_at:
        return True
    try:
        at = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return now - at > timedelta(seconds=timeout)


def claim(
    action: AirflowAction, by: str, *, timeout: float, now: datetime | None = None
) -> AirflowAction:
    """Mark ``action`` as running for ``by`` (the caller persists it with compare-and-set).
    A running claim older than ``timeout`` seconds (a replica died mid-call) may be taken
    over; Airflow's state checks in :func:`execute_action` keep a second run harmless."""
    current = now or datetime.now(timezone.utc)
    if action.status in DONE_STATUSES:
        raise ActionBusy("done", action.claimed_by)
    if action.status == "rejected":
        raise ActionBusy("rejected")
    if action.status == "running" and not claim_expired(action.claimed_at, timeout, current):
        raise ActionBusy("running", action.claimed_by)
    action.status = "running"
    action.claimed_by = by
    action.claimed_at = current.isoformat().replace("+00:00", "Z")
    return action


def audit_view(action: AirflowAction) -> dict[str, Any]:
    """The fields of an action that go into audit records."""
    return action.model_dump(
        include={
            "id",
            "action",
            "dag_id",
            "run_id",
            "task_id",
            "map_index",
            "include_downstream",
            "include_upstream",
            "only_failed",
            "status",
            "blocked",
        }
    )


__all__ = [
    "ACTION_FEATURE",
    "ACTION_PERMISSION",
    "DONE_STATUSES",
    "EXECUTABLE",
    "MODES",
    "RESULT_CODES",
    "ActionBusy",
    "ActionResult",
    "ActionsMode",
    "AirflowAction",
    "ProposedAction",
    "audit_view",
    "block_reason",
    "blocked_result",
    "claim",
    "claim_expired",
    "effective_mode",
    "execute_action",
    "record_result",
    "vet_actions",
]
