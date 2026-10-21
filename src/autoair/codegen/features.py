"""Minimum Airflow versions for spec features (used by validation and the UI)."""

from __future__ import annotations

from dataclasses import dataclass

from autoair.spec import (
    AssetAndTimeScheduleSpec,
    AssetSchedule,
    DagSpec,
    PartitionedAssetSchedule,
    PartitionedCronSchedule,
)

Version = tuple[int, int, int]


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    min_version: Version
    node_id: str | None = None


def _v(text: str) -> Version:
    major, minor, patch = (int(p) for p in text.split("."))
    return major, minor, patch


def required_features(spec: DagSpec) -> list[Feature]:
    """Features used by ``spec`` that need a specific Airflow version."""
    out: list[Feature] = []
    sched = spec.schedule
    if isinstance(sched, AssetAndTimeScheduleSpec):
        out.append(Feature("asset_and_time", "AssetAndTimeSchedule", _v("3.4.0")))
    if isinstance(sched, AssetSchedule) and sched.batch_asset_events is not None:
        out.append(Feature("batch_asset_events", "batch_asset_events", _v("3.4.0")))
    if isinstance(sched, (PartitionedCronSchedule, PartitionedAssetSchedule)):
        out.append(Feature("partitions", "Asset partitions", _v("3.2.0")))
    if spec.deadlines:
        out.append(Feature("deadlines", "Deadline alerts", _v("3.1.0")))
    if spec.rerun_with_latest_version is not None:
        out.append(Feature("rerun_latest", "rerun_with_latest_version", _v("3.3.0")))
    for group in spec.group_nodes():
        if group.loop is not None:
            out.append(Feature("task_loops", "Task loops", _v("3.4.0"), group.id))
    for node in spec.task_nodes(include_disabled=False):
        if getattr(node, "is_result", False):
            out.append(Feature("dag_result", "@result (DAG result)", _v("3.3.0"), node.id))
        if getattr(node, "decorator", None) == "stub":
            out.append(Feature("task_stub", "@task.stub (language SDKs)", _v("3.3.0"), node.id))
        if node.retry_policy is not None:
            out.append(Feature("retry_policy", "Retry policies", _v("3.3.0"), node.id))
        if node.trigger_rule == "all_done_min_one_success":
            out.append(
                Feature("trigger_rule_adms", "all_done_min_one_success", _v("3.1.0"), node.id)
            )
    return out


def format_version(v: Version) -> str:
    return ".".join(map(str, v))
