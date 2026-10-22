"""One-way export to astronomer `dag-factory` (>= 1.0) YAML.

dag-factory has no notion of inline Python code or custom AutoAir handlers, so such
nodes are reported as warnings and skipped. Every other construct the file cannot carry is
a warning too: the export never drops a setting silently.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel

from autoair.codegen.builder import effective_edges
from autoair.codegen.context import timedelta_problem
from autoair.codegen.timedeltas import coerce_timedelta
from autoair.spec import (
    AssetSchedule,
    BlueprintNode,
    CronSchedule,
    CustomNode,
    DagSpec,
    IntervalSchedule,
    NoSchedule,
    OperatorNode,
    PresetSchedule,
    PythonTaskNode,
)


class DagFactoryExport(BaseModel):
    yaml: str
    warnings: list[str]


# Task arguments passed to the operator as they are (BaseOperator keyword arguments).
_TASK_ARGS = (
    "trigger_rule",
    "retries",
    "pool",
    "pool_slots",
    "queue",
    "owner",
    "doc_md",
    "priority_weight",
    "weight_rule",
    "task_display_name",
    "executor",
    "max_active_tis_per_dagrun",
    "depends_on_past",
    "retry_exponential_backoff",
    "executor_config",
)
# DAG settings this export does not write (dag-factory either lacks them or needs Python
# objects for them).
_DAG_SKIPPED = (
    "display_name",
    "dagrun_timeout_seconds",
    "is_paused_upon_creation",
    "fail_fast",
    "max_consecutive_failed_dag_runs",
    "render_template_as_native_obj",
    "rerun_with_latest_version",
)


def _seconds(value: float) -> dict[str, Any]:
    return {"__type__": "datetime.timedelta", "seconds": value}


def _value(value: Any, warnings: list[str], where: str) -> Any:
    if isinstance(value, dict):
        tag = value.get("$type")
        if tag == "timedelta":
            problem = timedelta_problem(float(value["seconds"]))
            if problem is not None:
                warnings.append(f"{where}: {problem} (dropped)")
                return None
            return _seconds(value["seconds"])
        if tag == "datetime":
            return str(value["value"])
        if tag == "xcom":
            warnings.append(f"{where}: XCom references are exported as plain strings")
            return str(value)
        if tag in ("function", "file", "output_model", "toolset"):
            warnings.append(f"{where}: {tag} values cannot be exported to dag-factory (dropped)")
            return None
        if tag == "import":
            warnings.append(
                f"{where}: Python reference {value.get('path')} is exported as a string "
                "(dag-factory cannot import objects for arbitrary parameters)"
            )
            return str(value.get("path"))
        return {k: _value(v, warnings, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_value(v, warnings, where) for v in value]
    return value


def export_dag_factory(spec: DagSpec) -> DagFactoryExport:
    warnings: list[str] = []
    nodes = spec.node_map()
    dag: dict[str, Any] = {}

    da = spec.default_args.model_dump(exclude_none=True)
    default_args: dict[str, Any] = {}
    for key, val in da.items():
        if key in ("retry_delay_seconds", "execution_timeout_seconds"):
            name = key.removesuffix("_seconds")
            problem = timedelta_problem(val)
            if problem is not None:
                warnings.append(f"default_args {name}: {problem} (dropped)")
            else:
                default_args[name] = _seconds(val)
        else:
            default_args[key] = val
    if spec.start_date:
        default_args["start_date"] = spec.start_date.isoformat()
    if spec.end_date:
        default_args["end_date"] = spec.end_date.isoformat()
    if spec.timezone:
        if spec.start_date or spec.end_date:
            default_args["timezone"] = spec.timezone  # dag-factory localises the dates
        else:
            warnings.append(f"DAG timezone {spec.timezone!r} is not exported (no start date)")
    if default_args:
        dag["default_args"] = default_args

    sched = spec.schedule
    if isinstance(sched, NoSchedule):
        dag["schedule"] = None
    elif isinstance(sched, PresetSchedule):
        dag["schedule"] = sched.preset
    elif isinstance(sched, CronSchedule):
        dag["schedule"] = sched.expression
        lost = [
            what
            for flag, what in (
                (sched.timezone and sched.timezone != spec.timezone, f"timezone {sched.timezone}"),
                (sched.data_interval, "data intervals"),
                (sched.run_immediately, "run_immediately"),
                (sched.interval_seconds, "interval"),
            )
            if flag
        ]
        if lost:
            warnings.append(
                f"cron schedule exported as a plain expression; {', '.join(lost)} not exported"
            )
    elif isinstance(sched, IntervalSchedule):
        dag["schedule"] = _seconds(sched.seconds)
        if sched.data_interval:
            warnings.append("interval schedule: data intervals are not exported")
    elif isinstance(sched, AssetSchedule):
        warnings.append("asset schedules are exported as a flat list of asset URIs/names")
        dag["schedule"] = [a for a in _asset_names(sched.expression.model_dump())]
    else:
        warnings.append(f"{sched.type} schedules are not supported by dag-factory (no schedule)")
        dag["schedule"] = None

    for key in ("description", "catchup", "tags", "max_active_runs", "max_active_tasks", "doc_md"):
        val = getattr(spec, key)
        if val not in (None, [], ""):
            dag[key] = val
    skipped = [k for k in _DAG_SKIPPED if getattr(spec, k) is not None]
    for flag, what in (
        (spec.params, "params"),
        (not spec.callbacks.is_empty(), "callbacks"),
        (spec.deadlines, "deadlines"),
        (spec.owner_links, "owner_links"),
        (spec.lineage != "inherit", "lineage"),
    ):
        if flag:
            skipped.append(what)
    if skipped:
        warnings.append(f"DAG settings not exported to dag-factory: {', '.join(skipped)}")

    groups = {g.id: g for g in spec.group_nodes()}
    if groups:
        dag["task_groups"] = [
            {
                "group_name": g.group_id,
                **({"tooltip": g.tooltip} if g.tooltip else {}),
                **(
                    {"parent_group_name": groups[g.parent_group].group_id}
                    if g.parent_group in groups
                    else {}
                ),
            }
            for g in groups.values()
        ]

    exported = {
        n.id: n for n in spec.task_nodes(include_disabled=False) if isinstance(n, OperatorNode)
    }
    members: dict[str, list[str]] = {}
    for n in exported.values():
        cur = n.parent_group
        while cur:
            members.setdefault(cur, []).append(n.id)
            cur = getattr(nodes.get(cur), "parent_group", None)

    def expand(node_id: str) -> list[str]:
        # A group's edges become edges of its tasks (the same dependencies in Airflow).
        return [node_id] if node_id in exported else members.get(node_id, [])

    upstream: dict[str, list[str]] = {}
    for e in effective_edges(spec):
        src, tgt = nodes.get(e.source), nodes.get(e.target)
        if isinstance(src, BlueprintNode) or isinstance(tgt, BlueprintNode):
            continue  # the step itself is skipped (warning below)
        if e.label:
            warnings.append(f"edge {e.source} → {e.target}: label {e.label!r} is not exported")
        for s_id in expand(e.source):
            for t_id in expand(e.target):
                names = upstream.setdefault(t_id, [])
                if s_id != t_id and exported[s_id].task_id not in names:
                    names.append(exported[s_id].task_id)

    for bp in spec.blueprint_nodes():
        warnings.append(
            f"blueprint step {bp.step_id!r} ({bp.blueprint}) is not supported by dag-factory "
            "and was skipped, with its dependencies"
        )
    tasks: list[dict[str, Any]] = []
    for node in spec.task_nodes(include_disabled=False):
        if isinstance(node, (PythonTaskNode, CustomNode)):
            warnings.append(
                f"node {node.task_id!r} ({node.kind}) is not supported by dag-factory and was "
                "skipped, with its dependencies"
            )
            continue
        assert isinstance(node, OperatorNode)
        n = node
        task: dict[str, Any] = {"task_id": n.task_id, "operator": n.operator}
        params = {
            k: _value(coerce_timedelta(n.operator, k, v), warnings, n.task_id)
            for k, v in n.params.items()
        }
        if n.mapping:
            params = {k: v for k, v in params.items() if k not in n.mapping.expand}
            task["expand"] = {
                k: _value(v, warnings, n.task_id) for k, v in n.mapping.expand.items()
            }
            if n.mapping.expand_kwargs is not None:
                warnings.append(f"{n.task_id}: expand_kwargs mapping is not exported")
            if n.mapping.map_index_template:
                task["map_index_template"] = n.mapping.map_index_template
        task.update(params)
        for attr in _TASK_ARGS:
            val = getattr(n, attr)
            if val is not None:
                task[attr] = val
        for attr, seconds in (
            ("retry_delay", n.retry_delay_seconds),
            ("execution_timeout", n.execution_timeout_seconds),
        ):
            if seconds is None:
                continue
            problem = timedelta_problem(seconds)
            if problem is not None:
                warnings.append(f"{n.task_id}: {attr}: {problem} (dropped)")
            else:
                task[attr] = _seconds(seconds)
        lost = [
            what
            for flag, what in (
                (n.is_setup or n.is_teardown, "setup/teardown"),
                (not n.callbacks.is_empty(), "callbacks"),
                (n.retry_policy, "retry_policy"),
                (n.outlets or n.inlets, "asset outlets/inlets"),
                (n.lineage != "inherit", "lineage"),
            )
            if flag
        ]
        if lost:
            warnings.append(f"{n.task_id}: {', '.join(lost)} not exported to dag-factory")
        if n.parent_group and n.parent_group in groups:
            task["task_group_name"] = groups[n.parent_group].group_id
        if n.id in upstream:
            task["dependencies"] = upstream[n.id]
        tasks.append(task)
    dag["tasks"] = tasks

    text = yaml.safe_dump({spec.dag_id: dag}, sort_keys=False, allow_unicode=True)
    return DagFactoryExport(yaml=text, warnings=warnings)


def _asset_names(expr: dict[str, Any]) -> list[str]:
    if expr.get("op") == "asset":
        a = expr["asset"]
        return [a.get("uri") or a.get("name")]
    return [n for item in expr.get("items", []) for n in _asset_names(item)]
