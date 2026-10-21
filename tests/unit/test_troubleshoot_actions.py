"""Airflow actions of AI troubleshooting: schema, vetting, modes and state-checked execution."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from autoair.ai.assistants.airflow_actions import (
    AirflowAction,
    ProposedAction,
    effective_mode,
    execute_action,
    vet_actions,
)
from autoair.ai.config import TroubleshootSettings
from autoair.gateway import AirflowError, DagRunInfo, TaskInstanceInfo
from tests.unit.test_gateway_run_ops import Recorder

ALL = lambda _name: True  # noqa: E731
NONE = lambda _name: False  # noqa: E731


def vet(
    proposals: list[dict[str, Any]],
    *,
    mode: str = "hitl",
    can: Any = ALL,
    feature: Any = ALL,
    **settings: Any,
) -> list[AirflowAction]:
    return vet_actions(
        [ProposedAction.model_validate(p) for p in proposals],
        dag_id="etl",
        run_id="r1",
        mode=mode,  # type: ignore[arg-type]
        settings=TroubleshootSettings(**settings),
        can=can,
        feature=feature,
    )


# --------------------------------------------------------------------------- schema / modes


def test_proposal_schema_is_strict() -> None:
    with pytest.raises(ValidationError):
        ProposedAction.model_validate({"action": "clear", "instructions": "x", "sql": "drop"})
    with pytest.raises(ValidationError):
        ProposedAction.model_validate({"action": "delete_dag", "instructions": "x"})
    with pytest.raises(ValidationError):
        ProposedAction.model_validate({"action": "clear", "instructions": "x", "map_index": -5})


def test_defaults_are_read_only_with_safe_action_types() -> None:
    ts = TroubleshootSettings()
    assert ts.airflow_actions == "read_only"
    assert set(ts.allowed_actions) == {"clear", "retry", "clear_run"}
    assert ts.allow_include_upstream is False


def test_request_may_only_be_more_restrictive() -> None:
    assert effective_mode("hitl", "apply") == "hitl"
    assert effective_mode("apply", "hitl") == "hitl"
    assert effective_mode("apply", "read_only") == "read_only"
    assert effective_mode("apply", None) == "apply"
    assert effective_mode("read_only", "apply") == "read_only"
    assert effective_mode("apply", "apply", allowed=False) == "read_only"


# --------------------------------------------------------------------------- vetting


def test_read_only_keeps_instructions() -> None:
    out = vet([{"action": "clear", "task_id": "load", "instructions": "clear"}], mode="read_only")
    assert [a.status for a in out] == ["instruction"]
    assert out[0].blocked is None


def test_allow_list_dag_run_and_bounds() -> None:
    out = vet(
        [
            {"action": "clear", "task_id": "load", "instructions": "ok"},
            {"action": "mark_success", "task_id": "load", "instructions": "hide it"},
            {"action": "clear", "task_id": "x", "dag_id": "payroll", "instructions": "other"},
            {"action": "clear_run", "run_id": "r0", "instructions": "older run"},
            {"action": "retry", "instructions": "no task"},
            {"action": "clear", "task_id": "y", "include_upstream": True, "instructions": "up"},
            {"action": "fix_connection", "target": "pg", "instructions": "set sslmode"},
        ]
    )
    by = {(a.action, a.task_id or (a.run_id if a.executable else a.target)): a for a in out}
    assert by[("clear", "load")].status == "proposed"
    assert by[("clear", "load")].dag_id == "etl"
    assert by[("clear", "load")].run_id == "r1"
    assert by[("mark_success", "load")].blocked == "not_allowed"
    assert by[("clear", "x")].blocked == "other_dag"
    assert by[("clear_run", "r0")].blocked == "other_run"
    assert by[("retry", "r1")].blocked == "missing_task"
    assert by[("clear", "y")].blocked == "upstream_not_allowed"
    assert by[("fix_connection", "pg")].status == "instruction"
    assert all(a.id for a in out)


def test_other_runs_and_marking_when_configured() -> None:
    out = vet(
        [
            {"action": "clear_run", "run_id": "r0", "instructions": "older run"},
            {"action": "mark_failed", "task_id": "t", "instructions": "stop"},
        ],
        actions_other_runs=True,
        allowed_actions=["clear_run", "mark_failed"],
    )
    assert [a.status for a in out] == ["proposed", "proposed"]


def test_task_id_from_target_limit_and_duplicates() -> None:
    out = vet(
        [
            {"action": "retry", "target": "load", "instructions": "a"},
            {"action": "retry", "target": "load", "instructions": "same again"},
            {"action": "clear", "task_id": "b", "instructions": "b"},
            {"action": "clear", "task_id": "c", "instructions": "c"},
            {"action": "retry", "target": "not a task id!", "instructions": "d"},
        ],
        max_actions=2,
    )
    assert [(a.task_id, a.status, a.blocked) for a in out] == [
        ("load", "proposed", None),
        ("b", "proposed", None),
        ("c", "blocked", "limit"),
        (None, "blocked", "missing_task"),
    ]


def test_permission_and_feature_are_required() -> None:
    proposal = [{"action": "clear", "task_id": "load", "instructions": "x"}]
    assert vet(proposal, can=NONE)[0].blocked == "permission"
    assert vet(proposal, feature=NONE)[0].blocked == "feature_off"


# --------------------------------------------------------------------------- execution


class FakeGateway:
    def __init__(
        self,
        tis: list[TaskInstanceInfo],
        run_state: str = "failed",
        dry: int = 2,
        fail: AirflowError | None = None,
    ) -> None:
        self.tis = tis
        self.run_state = run_state
        self.dry = dry
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def task_instances(self, dag_id: str, run_id: str) -> list[TaskInstanceInfo]:
        return self.tis

    async def dag_run(self, dag_id: str, run_id: str) -> DagRunInfo | None:
        return DagRunInfo(dag_run_id=run_id, state=self.run_state)

    async def clear_task_instances(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.fail and not kwargs.get("dry_run"):
            raise self.fail
        self.calls.append(("clear_task_instances", {"args": args, **kwargs}))
        return {"task_instances": [{}] * self.dry}

    async def clear_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("clear_run", {"args": args, **kwargs}))
        return {"task_instances": [{}] * self.dry}

    async def set_task_instance_state(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("set_task_instance_state", {"args": args, **kwargs}))
        return {}

    async def set_dag_run_state(self, *args: Any, **kwargs: Any) -> DagRunInfo:
        self.calls.append(("set_dag_run_state", {"args": args, **kwargs}))
        return DagRunInfo(dag_run_id="r1", state=args[2])


def action(kind: str, **kw: Any) -> AirflowAction:
    return AirflowAction(action=kind, dag_id="etl", run_id="r1", **kw)  # type: ignore[arg-type]


def run(gw: FakeGateway, a: AirflowAction, **settings: Any) -> Any:
    return asyncio.run(execute_action(gw, a, TroubleshootSettings(**settings)))  # type: ignore[arg-type]


def ti(task_id: str, state: str | None, map_index: int = -1) -> TaskInstanceInfo:
    return TaskInstanceInfo(task_id=task_id, state=state, map_index=map_index, try_number=1)


def test_retry_failed_task_dry_run_first() -> None:
    gw = FakeGateway([ti("load", "failed")])
    result = run(gw, action("retry", task_id="load", include_downstream=True))
    assert result.status == "succeeded"
    assert result.affected == 2
    (dry_name, dry), (name, real) = gw.calls
    assert dry_name == name == "clear_task_instances"
    assert dry["dry_run"] is True
    assert "dry_run" not in real
    assert real["only_failed"] is True
    assert real["include_downstream"] is True
    assert real["args"] == ("etl", "r1", "load")


def test_running_task_is_not_cleared_again() -> None:
    gw = FakeGateway([ti("load", "running")])
    result = run(gw, action("clear", task_id="load"))
    assert result.status == "skipped"
    assert "running" in result.message
    assert gw.calls == []


def test_retry_of_a_successful_task_is_skipped() -> None:
    gw = FakeGateway([ti("load", "success")])
    assert run(gw, action("retry", task_id="load")).status == "skipped"
    assert gw.calls == []


def test_clear_over_the_limit_is_refused() -> None:
    gw = FakeGateway([ti("load", "failed")], dry=30)
    result = run(gw, action("clear", task_id="load", include_downstream=True))
    assert result.status == "failed"
    assert "limit" in result.message
    assert [c[1]["dry_run"] for c in gw.calls] == [True]  # only the dry run


def test_missing_task_and_airflow_errors_fail() -> None:
    assert run(FakeGateway([]), action("clear", task_id="load")).status == "failed"
    gw = FakeGateway([ti("load", "failed")], fail=AirflowError(403, "forbidden"))
    result = run(gw, action("clear", task_id="load"))
    assert result.status == "failed"
    assert "forbidden" in result.message


def test_mapped_instance_and_mark_states() -> None:
    gw = FakeGateway([ti("load", "failed", 0), ti("load", "success", 1)])
    result = run(gw, action("mark_success", task_id="load", map_index=0))
    assert result.status == "succeeded"
    assert gw.calls[0][1]["map_index"] == 0
    already = FakeGateway([ti("load", "success", 1)])
    assert run(already, action("mark_success", task_id="load", map_index=1)).status == "skipped"


def test_run_level_actions_check_the_run_state() -> None:
    active = FakeGateway([], run_state="running")
    assert run(active, action("clear_run")).status == "skipped"
    assert active.calls == []
    failed = FakeGateway([], run_state="failed")
    assert run(failed, action("clear_run")).status == "succeeded"
    assert failed.calls[-1][1]["only_failed"] is True
    done = FakeGateway([], run_state="success")
    assert run(done, action("mark_run_success")).status == "skipped"
    assert run(done, action("mark_run_failed")).status == "succeeded"


# --------------------------------------------------------------------------- gateway bodies


def test_gateway_clear_task_instances_body() -> None:
    rec = Recorder({"task_instances": []})
    asyncio.run(
        rec.gateway().clear_task_instances(
            "etl", "r/1", "load", map_index=3, only_failed=True, dry_run=True
        )
    )
    method, path, _, body = rec.calls[0]
    assert (method, path) == ("POST", "/api/v2/dags/etl/clearTaskInstances")
    assert body == {
        "dry_run": True,
        "dag_run_id": "r/1",
        "task_ids": [["load", 3]],
        "only_failed": True,
        "include_downstream": False,
        "include_upstream": False,
        "reset_dag_runs": True,
    }


def test_gateway_mark_state_bodies() -> None:
    rec = Recorder({"dag_run_id": "r1", "state": "success"})
    gw = rec.gateway()
    asyncio.run(gw.set_task_instance_state("etl", "r1", "load", "success", map_index=2))
    asyncio.run(gw.set_dag_run_state("etl", "r1", "failed"))
    (m1, p1, q1, b1), (m2, p2, q2, b2) = rec.calls
    assert (m1, p1, q1, b1) == (
        "PATCH",
        "/api/v2/dags/etl/dagRuns/r1/taskInstances/load/2",
        {"update_mask": "new_state"},
        {"new_state": "success"},
    )
    assert (m2, p2, q2, b2) == (
        "PATCH",
        "/api/v2/dags/etl/dagRuns/r1",
        {"update_mask": "state"},
        {"state": "failed"},
    )
