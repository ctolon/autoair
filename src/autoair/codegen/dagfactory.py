"""One-way export to astronomer `dag-factory` (>= 1.0) YAML.

dag-factory has no notion of inline Python code or custom AutoAir handlers, so such
nodes are reported as warnings and skipped.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel

from autoair.codegen.timedeltas import coerce_timedelta
from autoair.spec import (
    AssetSchedule,
    CronSchedule,
    CustomNode,
    DagSpec,
    IntervalSchedule,
    NoSchedule,
    OperatorNode,
    PresetSchedule,
    PythonTaskNode,
    TaskGroupNode,
)


class DagFactoryExport(BaseModel):
    yaml: str
    warnings: list[str]


def _seconds(value: float) -> dict[str, Any]:
    return {"__type__": "datetime.timedelta", "seconds": value}


def _value(value: Any, warnings: list[str], where: str) -> Any:
    if isinstance(value, dict):
        tag = value.get("$type")
        if tag == "timedelta":
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
        if key == "retry_delay_seconds":
            default_args["retry_delay"] = _seconds(val)
        elif key == "execution_timeout_seconds":
            default_args["execution_timeout"] = _seconds(val)
        else:
            default_args[key] = val
    if spec.start_date:
        default_args["start_date"] = spec.start_date.isoformat()
        if spec.timezone:
            default_args["timezone"] = spec.timezone  # dag-factory localises start_date
    if default_args:
        dag["default_args"] = default_args

    sched = spec.schedule
    if isinstance(sched, NoSchedule):
        dag["schedule"] = None
    elif isinstance(sched, PresetSchedule):
        dag["schedule"] = sched.preset
    elif isinstance(sched, CronSchedule):
        dag["schedule"] = sched.expression
    elif isinstance(sched, IntervalSchedule):
        dag["schedule"] = _seconds(sched.seconds)
    elif isinstance(sched, AssetSchedule):
        warnings.append("asset schedules are exported as a flat list of asset URIs/names")
        dag["schedule"] = [a for a in _asset_names(sched.expression.model_dump())]

    for key in ("description", "catchup", "tags", "max_active_runs", "max_active_tasks", "doc_md"):
        val = getattr(spec, key)
        if val not in (None, [], ""):
            dag[key] = val

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

    upstream: dict[str, list[str]] = {}
    for e in spec.edges:
        src, tgt = nodes.get(e.source), nodes.get(e.target)
        if isinstance(src, TaskGroupNode) or isinstance(tgt, TaskGroupNode):
            warnings.append(f"edge {e.source} → {e.target}: group dependencies are not exported")
            continue
        if src is not None and tgt is not None and src.kind != "note" and tgt.kind != "note":
            upstream.setdefault(e.target, []).append(src.task_id)

    tasks: list[dict[str, Any]] = []
    for n in spec.task_nodes():
        if isinstance(n, (PythonTaskNode, CustomNode)):
            warnings.append(
                f"node {n.task_id!r} ({n.kind}) is not supported by dag-factory and was skipped"
            )
            continue
        assert isinstance(n, OperatorNode)
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
        task.update(params)
        for attr in ("trigger_rule", "retries", "pool", "pool_slots", "queue", "owner"):
            val = getattr(n, attr)
            if val is not None:
                task[attr] = val
        if n.retry_delay_seconds is not None:
            task["retry_delay"] = _seconds(n.retry_delay_seconds)
        if n.execution_timeout_seconds is not None:
            task["execution_timeout"] = _seconds(n.execution_timeout_seconds)
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
