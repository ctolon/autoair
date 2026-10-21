"""Evaluate declarative ``policy.rules`` (see :mod:`autoair.policy_rules`) against a spec.

Every violation becomes a ``policy_rule`` problem (severity from the rule) located at the
DAG field or the task it concerns, so the Problems panel can jump to it. Evaluation is pure:
it reads the spec only and never imports or runs user code.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

from autoair.policy_rules import (
    CatchupCheck,
    DagFieldCheck,
    ForbidCheck,
    MaxTasksCheck,
    MinRetriesCheck,
    ParamCondition,
    ParamValueCheck,
    PolicyRule,
    RequireDescriptionCheck,
    RequireOwnerCheck,
    RequireParamCheck,
    RuleMatch,
    TagsIncludeCheck,
    TaskFieldCheck,
)
from autoair.spec import CustomNode, DagSpec, OperatorNode, PythonTaskNode, TaskNode
from autoair.validation import Problem

PRESET_CRON = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@quarterly": "0 0 1 */3 *",
    "@yearly": "0 0 1 1 *",
}
_UNSET: Any = object()
# Task fields that fall back to the DAG's default_args when the task does not set them.
_DEFAULT_ARGS_FALLBACK = frozenset(
    {
        "owner",
        "retries",
        "retry_delay_seconds",
        "execution_timeout_seconds",
        "pool",
        "queue",
        "depends_on_past",
    }
)


# --------------------------------------------------------------------------- matching


def operator_name(task: TaskNode) -> str:
    """What ``match.operators`` patterns are matched against."""
    if isinstance(task, OperatorNode):
        return task.operator
    if isinstance(task, PythonTaskNode):
        return "@task" if task.decorator == "task" else f"@task.{task.decorator}"
    if isinstance(task, CustomNode):
        return f"custom:{task.handler}"
    return ""


def _operator_matches(task: TaskNode, patterns: Sequence[str]) -> bool:
    name = operator_name(task)
    short = name.rsplit(".", 1)[-1] if not name.startswith("@") else name
    for pattern in patterns:
        target = name if ("." in pattern or pattern.startswith(("@", "custom:"))) else short
        if fnmatch.fnmatchcase(target, pattern):
            return True
    return False


def task_params(task: TaskNode) -> dict[str, Any]:
    """Operator ``params``, TaskFlow ``arguments`` + ``decorator_kwargs``, custom ``config``
    (JSON form)."""
    data = task.model_dump(mode="json", by_alias=True)
    if isinstance(task, OperatorNode):
        return dict(data.get("params") or {})
    if isinstance(task, PythonTaskNode):
        return {**(data.get("decorator_kwargs") or {}), **(data.get("arguments") or {})}
    if isinstance(task, CustomNode):
        return dict(data.get("config") or {})
    return {}


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def condition_holds(condition: ParamCondition, params: dict[str, Any]) -> bool:
    value = params.get(condition.name, _UNSET)
    present = value is not _UNSET and value is not None and value != ""
    if condition.present is not None and condition.present != present:
        return False
    checks = condition.equals is not None or condition.one_of is not None or condition.pattern
    if not checks:
        return present if condition.present is None else True
    if not present:
        return False
    if condition.equals is not None and value != condition.equals:
        return False
    if condition.one_of is not None and value not in condition.one_of:
        return False
    return not (condition.pattern and not re.search(condition.pattern, _as_text(value)))


def _schedule_matches(spec: DagSpec, patterns: Sequence[str]) -> bool:
    schedule = spec.schedule
    candidates: set[str] = {schedule.type}
    preset = getattr(schedule, "preset", None)
    if preset:
        candidates.add(preset)
        if preset in PRESET_CRON:
            candidates.add(PRESET_CRON[preset])
    expression = getattr(schedule, "expression", None)
    if isinstance(expression, str):
        candidates.add(" ".join(expression.split()))
    for expr in getattr(schedule, "expressions", None) or ():
        candidates.add(" ".join(str(expr).split()))
    reverse = {cron: name for name, cron in PRESET_CRON.items()}
    candidates |= {reverse[c] for c in list(candidates) if c in reverse}
    return any(" ".join(p.split()) in candidates for p in patterns)


def dag_matches(match: RuleMatch, spec: DagSpec, environment: str | None) -> bool:
    if match.environments and not (
        environment and any(fnmatch.fnmatchcase(environment, p) for p in match.environments)
    ):
        return False
    if match.teams and not (
        spec.team and any(fnmatch.fnmatchcase(spec.team, p) for p in match.teams)
    ):
        return False
    if match.dag_ids and not any(fnmatch.fnmatchcase(spec.dag_id, p) for p in match.dag_ids):
        return False
    if match.tags and not set(match.tags) & set(spec.tags):
        return False
    return not (match.schedule and not _schedule_matches(spec, match.schedule))


def task_matches(match: RuleMatch, task: TaskNode) -> bool:
    if match.operators and not _operator_matches(task, match.operators):
        return False
    if match.params:
        params = task_params(task)
        if not all(condition_holds(c, params) for c in match.params):
            return False
    return True


# --------------------------------------------------------------------------- field access


def _dag_value(spec: DagSpec, path: str) -> Any:
    data = spec.model_dump(mode="json", by_alias=True)
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _task_value(spec: DagSpec, task: TaskNode, name: str) -> Any:
    value = getattr(task, name, None)
    if value is None and name in _DEFAULT_ARGS_FALLBACK:
        value = getattr(spec.default_args, name, None)
    return value


def _field_violation(
    value: Any, present: bool | None, equals: Any, low: float | None, high: float | None
) -> str | None:
    is_set = value is not None and value != "" and value != []
    if present is True and not is_set:
        return "must be set"
    if present is False and is_set:
        return "must not be set"
    if equals is not None and value != equals:
        return f"must be {json.dumps(equals)}"
    if low is not None or high is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "must be a number" + (f" ≥ {low:g}" if low is not None else "")
        if low is not None and value < low:
            return f"must be ≥ {low:g}"
        if high is not None and value > high:
            return f"must be ≤ {high:g}"
    return None


# --------------------------------------------------------------------------- evaluation


def _problem(
    rule: PolicyRule, default: str, node_id: str | None = None, field: str | None = None
) -> Problem:
    text = rule.message or default
    return Problem(
        severity=rule.severity,
        code="policy_rule",
        message=f"{text} [policy rule {rule.id}]",
        node_id=node_id,
        field=field,
    )


def _evaluate(rule: PolicyRule, spec: DagSpec, environment: str | None) -> Iterable[Problem]:
    if not rule.enabled or not dag_matches(rule.match, spec, environment):
        return
    check = rule.check
    all_tasks = spec.task_nodes(include_disabled=False)
    selected = [t for t in all_tasks if task_matches(rule.match, t)]
    if rule.match.selects_tasks and not selected:
        return

    if isinstance(check, ForbidCheck):
        if rule.match.selects_tasks:
            for t in selected:
                yield _problem(rule, f"{operator_name(t)} is forbidden here", t.id)
        else:
            yield _problem(rule, "this DAG is forbidden here")
    elif isinstance(check, RequireOwnerCheck):
        for t in selected:
            if not _task_value(spec, t, "owner"):
                yield _problem(rule, "an owner is required", t.id, "owner")
    elif isinstance(check, MinRetriesCheck):
        for t in selected:
            if (_task_value(spec, t, "retries") or 0) < check.value:
                yield _problem(rule, f"retries must be ≥ {check.value}", t.id, "retries")
    elif isinstance(check, CatchupCheck):
        if spec.catchup != check.value:
            yield _problem(rule, f"catchup must be {str(check.value).lower()}", field="catchup")
    elif isinstance(check, TagsIncludeCheck):
        required = list(check.tags) + ([spec.team] if check.team and spec.team else [])
        missing = [tag for tag in required if tag not in spec.tags]
        if missing:
            yield _problem(rule, f"required tags missing: {', '.join(missing)}", field="tags")
    elif isinstance(check, RequireDescriptionCheck):
        if not (spec.description or spec.doc_md):
            yield _problem(rule, "a description is required", field="description")
    elif isinstance(check, MaxTasksCheck):
        if len(all_tasks) > check.value:
            yield _problem(rule, f"at most {check.value} tasks are allowed")
    elif isinstance(check, RequireParamCheck):
        for t in selected:
            value = task_params(t).get(check.name)
            if value is None or value == "":
                yield _problem(rule, f"parameter {check.name!r} is required", t.id, check.name)
    elif isinstance(check, ParamValueCheck):
        cond = check.condition
        for t in selected:
            if not condition_holds(cond, task_params(t)):
                yield _problem(
                    rule, f"parameter {cond.name!r} violates the policy", t.id, cond.name
                )
    elif isinstance(check, DagFieldCheck):
        why = _field_violation(
            _dag_value(spec, check.field), check.present, check.equals, check.min, check.max
        )
        if why:
            yield _problem(rule, f"{check.field} {why}", field=check.field)
    elif isinstance(check, TaskFieldCheck):
        for t in selected:
            why = _field_violation(
                _task_value(spec, t, check.field), check.present, check.equals, check.min, check.max
            )
            if why:
                yield _problem(rule, f"{check.field} {why}", t.id, check.field)


def evaluate_rules(
    spec: DagSpec, rules: Sequence[PolicyRule], environment: str | None = None
) -> list[Problem]:
    """Problems of every rule ``spec`` violates (in rule order)."""
    out: list[Problem] = []
    for rule in rules:
        out.extend(_evaluate(rule, spec, environment))
    return out


__all__ = [
    "condition_holds",
    "dag_matches",
    "evaluate_rules",
    "operator_name",
    "task_matches",
    "task_params",
]
