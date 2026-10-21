"""Thin proxies to Airflow's REST API v2 (credentials forwarded → Airflow RBAC).

AutoAir permissions: ``airflow:read`` for the router (see ``create_api``), plus
``connection:write``, ``run:trigger`` or ``run:manage`` on the routes that change Airflow.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator

from autoair._compat import airflow_version_str
from autoair.api.deps import GatewayDep, ServicesDep, http_error
from autoair.api.features import requires_feature
from autoair.gateway import (
    AirflowError,
    ConnectionDraft,
    ConnectionHookMeta,
    ConnectionInfo,
    ConnectionTestResult,
    DagRunInfo,
    DagStatus,
    DagVersionInfo,
    ImportErrorInfo,
    PoolInfo,
    TaskInstanceInfo,
    VariableInfo,
    XComInfo,
    for_team,
    parse_version,
)
from autoair.iam import model as P
from autoair.iam.deps import require
from autoair.spec import DagSpec

router = APIRouter(prefix="/airflow", tags=["airflow"])
T = TypeVar("T")


async def _call(fn: Callable[[], Awaitable[T]]) -> T:
    try:
        return await fn()
    except AirflowError as exc:
        raise http_error(exc) from exc


class TriggerRequest(BaseModel):
    conf: dict[str, Any] | None = None
    unpause: bool = True
    logical_date: str | None = Field(
        default="now", description="ISO datetime, 'now', or null for a run without logical date"
    )
    partition_key: str | None = None
    note: str | None = None


class TriggerResult(BaseModel):
    dag_run_id: str | None
    state: str | None


class ClearRequest(BaseModel):
    only_failed: bool = False
    dry_run: bool = False
    only_new: bool = Field(
        default=False,
        description="Airflow >= 3.2: only queue tasks added in the latest DAG version "
        "(exclusive with only_failed).",
    )
    run_on_latest_version: bool | None = Field(
        default=None,
        description="Airflow >= 3.1: re-run on the latest bundle version (null: the DAG's / "
        "Airflow's default).",
    )
    keep_task_state: bool = Field(
        default=False,
        description="Airflow >= 3.4: keep the task state store so the next attempt resumes "
        "(uses clearTaskInstances; exclusive with only_new).",
    )

    @model_validator(mode="after")
    def _exclusive(self) -> ClearRequest:
        if self.only_new and self.only_failed:
            raise ValueError("only_new and only_failed are mutually exclusive")
        if self.only_new and self.keep_task_state:
            raise ValueError("only_new and keep_task_state cannot be combined")
        return self


class BackfillRequest(BaseModel):
    from_date: str
    to_date: str
    reprocess_behavior: Literal["none", "failed", "completed"] = "none"
    max_active_runs: int = Field(default=10, ge=1)
    run_backwards: bool = False
    dry_run: bool = True
    dag_run_conf: dict[str, Any] | None = None
    run_on_latest_version: bool | None = Field(
        default=None,
        description="Airflow >= 3.1: run each backfilled run on the latest bundle version "
        "(null: the DAG's / Airflow's default).",
    )
    drain_dag: bool = Field(
        default=False,
        description="Airflow >= 3.4: drain the DAG together with the backfill (no new "
        "scheduled runs; it pauses once unfinished runs finish). Ignored by the dry run.",
    )


class AssetEventRequest(BaseModel):
    extra: dict[str, Any] = Field(
        default_factory=dict, description="The event's extra (JSON object)."
    )


SchedulingState = Literal["active", "draining", "paused"]


class SchedulingStateRequest(BaseModel):
    state: SchedulingState


class AirflowCapabilities(BaseModel):
    """What the connected Airflow server's REST API supports (by its version)."""

    version: str | None = Field(description="The Airflow server's version (null: unknown).")
    source: Literal["server", "installed", "unknown"] = Field(
        description="server: GET /api/v2/version; installed: the Airflow installed next to "
        "AutoAir (the server was unreachable)."
    )
    materialize: bool = Field(description="POST /assets/{id}/materialize (>= 3.0).")
    clear_run_on_latest_version: bool = Field(description="Clear run_on_latest_version (>= 3.1).")
    clear_only_new: bool = Field(description="Clear only_new (>= 3.2).")
    keep_task_state: bool = Field(description="Clear keep_task_state (>= 3.4).")
    backfill_run_on_latest_version: bool = Field(
        description="Backfill run_on_latest_version (>= 3.1)."
    )
    scheduling_state: bool = Field(
        description="DAG scheduling_state active/draining/paused and drain (>= 3.4)."
    )
    retry_try_is_next: bool = Field(
        default=False,
        description="An up_for_retry task instance's try_number is the upcoming try, which "
        "has no log yet; the failed try is try_number - 1 (>= 3.4).",
    )


def capabilities_for(
    version: str | None, source: Literal["server", "installed", "unknown"]
) -> AirflowCapabilities:
    """Unknown versions get no optional features (sending an unknown field to Airflow's
    strict request bodies fails the whole call)."""
    v = parse_version(version)

    def at_least(*minimum: int) -> bool:
        return v is not None and v >= minimum

    return AirflowCapabilities(
        version=version,
        source=source if v is not None else "unknown",
        materialize=at_least(3, 0),
        clear_run_on_latest_version=at_least(3, 1),
        clear_only_new=at_least(3, 2),
        keep_task_state=at_least(3, 4),
        backfill_run_on_latest_version=at_least(3, 1),
        scheduling_state=at_least(3, 4),
        retry_try_is_next=at_least(3, 4),
    )


class RunsOverlay(BaseModel):
    """Everything the canvas needs to paint a run: run + task instance states."""

    run: DagRunInfo | None
    task_instances: list[TaskInstanceInfo]


class NextRunsRequest(BaseModel):
    spec: DagSpec
    count: int = Field(default=5, ge=1, le=50)


class NextRunsResult(BaseModel):
    runs: list[str]
    summary: str | None = None
    error: str | None = None


@router.get("/connections", operation_id="listConnections")
async def connections(gw: GatewayDep, team: str | None = None) -> list[ConnectionInfo]:
    return for_team(await _call(gw.connections), team)


@router.post(
    "/connections",
    operation_id="createConnection",
    status_code=201,
    dependencies=[require(P.CONNECTION_WRITE)],
)
async def create_connection(gw: GatewayDep, body: ConnectionDraft) -> ConnectionInfo:
    """Create the connection in Airflow (the caller's Airflow permissions apply)."""
    return await _call(lambda: gw.create_connection(body))


@router.get(
    "/connections/hook-meta",
    operation_id="listConnectionHookMeta",
    dependencies=[require(P.CONNECTION_WRITE)],
)
async def connection_hook_meta(gw: GatewayDep) -> list[ConnectionHookMeta]:
    """Per conn_type form metadata (standard field labels/visibility, typed extra fields)
    for the new-connection form; empty when Airflow does not provide it."""
    return await _call(gw.connection_hook_meta)


@router.post(
    "/connections/test",
    operation_id="testConnection",
    dependencies=[require(P.CONNECTION_WRITE)],
)
async def test_connection(gw: GatewayDep, body: ConnectionDraft) -> ConnectionTestResult:
    return await _call(lambda: gw.test_connection(body))


@router.get("/pools", operation_id="listPools")
async def pools(gw: GatewayDep, team: str | None = None) -> list[PoolInfo]:
    return for_team(await _call(gw.pools), team)


@router.get("/variables", operation_id="listVariables")
async def variables(gw: GatewayDep, team: str | None = None) -> list[VariableInfo]:
    return for_team(await _call(gw.variables), team)


@router.get("/assets", operation_id="listAssets")
async def assets(gw: GatewayDep, name_pattern: str | None = None) -> list[dict[str, Any]]:
    return await _call(lambda: gw.assets(name_pattern))


@router.post(
    "/assets/{asset_id}/events",
    operation_id="createAssetEvent",
    dependencies=[require(P.RUN_TRIGGER), requires_feature("runs")],
)
async def create_asset_event(
    asset_id: int, gw: GatewayDep, body: AssetEventRequest | None = None
) -> dict[str, Any]:
    """Send a (test) asset event: downstream asset-scheduled DAGs react to it."""
    return await _call(lambda: gw.create_asset_event(asset_id, (body or AssetEventRequest()).extra))


@router.post(
    "/assets/{asset_id}/materialize",
    operation_id="materializeAsset",
    dependencies=[require(P.RUN_TRIGGER), requires_feature("runs")],
)
async def materialize_asset(asset_id: int, gw: GatewayDep) -> dict[str, Any]:
    """Trigger the DAG that produces the asset (Airflow's own DAG run permission applies)."""
    return await _call(lambda: gw.materialize_asset(asset_id))


@router.get("/capabilities", operation_id="getAirflowCapabilities")
async def capabilities(gw: GatewayDep) -> AirflowCapabilities:
    """Version-dependent run operations of the connected Airflow (falls back to the
    installed Airflow's version when the server cannot be asked)."""
    try:
        version = str((await gw.version()).get("version") or "") or None
    except AirflowError:
        version = None
    if version:
        return capabilities_for(version, "server")
    return capabilities_for(airflow_version_str(), "installed")


@router.get("/import-errors", operation_id="listImportErrors")
async def import_errors(gw: GatewayDep, filename: str | None = None) -> list[ImportErrorInfo]:
    return await _call(lambda: gw.import_errors(filename))


@router.get("/bundles", operation_id="listDagBundles")
async def bundles(gw: GatewayDep) -> list[dict[str, Any]]:
    return await _call(gw.dag_bundles)


@router.get("/dags/{dag_id}/status", operation_id="getDagStatus")
async def dag_status(dag_id: str, gw: GatewayDep) -> DagStatus:
    return await _call(lambda: gw.dag_status(dag_id))


class PauseRequest(BaseModel):
    paused: bool


@router.put(
    "/dags/{dag_id}/paused",
    operation_id="setDagPaused",
    dependencies=[require(P.RUN_MANAGE), requires_feature("runs")],
)
async def set_paused(dag_id: str, body: PauseRequest, gw: GatewayDep) -> DagStatus:
    """Pause / unpause in Airflow (the caller's token: Airflow enforces DAG edit rights)."""

    async def run() -> DagStatus:
        await gw.set_paused(dag_id, body.paused)
        return await gw.dag_status(dag_id)

    return await _call(run)


@router.put(
    "/dags/{dag_id}/scheduling-state",
    operation_id="setDagSchedulingState",
    dependencies=[require(P.RUN_MANAGE), requires_feature("runs")],
)
async def set_scheduling_state(
    dag_id: str, body: SchedulingStateRequest, gw: GatewayDep
) -> DagStatus:
    """Airflow >= 3.4: activate, drain or pause the DAG (PATCH scheduling_state)."""

    async def run() -> DagStatus:
        await gw.set_scheduling_state(dag_id, body.state)
        return await gw.dag_status(dag_id)

    return await _call(run)


@router.get("/dags/{dag_id}/runs", operation_id="listDagRuns")
async def dag_runs(
    dag_id: str, gw: GatewayDep, limit: Annotated[int, Query(ge=1, le=1000)] = 25
) -> list[DagRunInfo]:
    return await _call(lambda: gw.dag_runs(dag_id, limit))


@router.get("/dags/{dag_id}/runs/{run_id}/overlay", operation_id="getRunOverlay")
async def run_overlay(dag_id: str, run_id: str, gw: GatewayDep) -> RunsOverlay:
    async def fetch() -> RunsOverlay:
        # One run by id (not the last 100 runs scanned), concurrently with its tasks.
        run, task_instances = await asyncio.gather(
            gw.dag_run(dag_id, run_id), gw.task_instances(dag_id, run_id)
        )
        return RunsOverlay(run=run, task_instances=task_instances)

    return await _call(fetch)


@router.get("/dags/{dag_id}/runs/{run_id}/tasks/{task_id}/log", operation_id="getTaskLog")
async def task_log(
    dag_id: str, run_id: str, task_id: str, gw: GatewayDep, try_number: int = 1, map_index: int = -1
) -> str:
    return await _call(lambda: gw.task_log(dag_id, run_id, task_id, try_number, map_index))


@router.get("/dags/{dag_id}/runs/{run_id}/tasks/{task_id}/xcom", operation_id="getTaskXcom")
async def task_xcom(
    dag_id: str, run_id: str, task_id: str, gw: GatewayDep, map_index: int = -1
) -> list[XComInfo]:
    return await _call(lambda: gw.xcom_entries(dag_id, run_id, task_id, map_index))


@router.get("/dags/{dag_id}/versions", operation_id="listDagVersions")
async def dag_versions(dag_id: str, gw: GatewayDep) -> list[DagVersionInfo]:
    return await _call(lambda: gw.dag_versions(dag_id))


@router.post(
    "/dags/{dag_id}/trigger",
    operation_id="triggerDag",
    dependencies=[require(P.RUN_TRIGGER), requires_feature("runs")],
)
async def trigger(dag_id: str, body: TriggerRequest, gw: GatewayDep) -> TriggerResult:
    async def run() -> TriggerResult:
        if body.unpause:
            await gw.set_paused(dag_id, False)
        logical: str | bool | None = True if body.logical_date == "now" else body.logical_date
        result = await gw.trigger(
            dag_id,
            body.conf,
            logical_date=logical,
            partition_key=body.partition_key,
            note=body.note,
        )
        return TriggerResult(dag_run_id=result.get("dag_run_id"), state=result.get("state"))

    return await _call(run)


@router.post(
    "/dags/{dag_id}/runs/{run_id}/clear",
    operation_id="clearDagRun",
    dependencies=[require(P.RUN_MANAGE), requires_feature("runs")],
)
async def clear_run(dag_id: str, run_id: str, body: ClearRequest, gw: GatewayDep) -> dict[str, Any]:
    return await _call(
        lambda: gw.clear_run(
            dag_id,
            run_id,
            only_failed=body.only_failed,
            dry_run=body.dry_run,
            only_new=body.only_new,
            run_on_latest_version=body.run_on_latest_version,
            keep_task_state=body.keep_task_state,
        )
    )


@router.post(
    "/dags/{dag_id}/backfill",
    operation_id="createBackfill",
    dependencies=[require(P.RUN_MANAGE), requires_feature("runs")],
)
async def backfill(dag_id: str, body: BackfillRequest, gw: GatewayDep) -> dict[str, Any]:
    return await _call(
        lambda: gw.backfill(
            dag_id,
            body.from_date,
            body.to_date,
            reprocess_behavior=body.reprocess_behavior,
            max_active_runs=body.max_active_runs,
            run_backwards=body.run_backwards,
            dry_run=body.dry_run,
            dag_run_conf=body.dag_run_conf,
            run_on_latest_version=body.run_on_latest_version,
            drain_dag=body.drain_dag,
        )
    )


class MappingCheckRequest(BaseModel):
    spec: DagSpec
    environment: str = Field(min_length=1, description="The environment whose mapping is checked.")


class MappingEntry(BaseModel):
    logical: str = Field(description="The id the DAG uses.")
    actual: str = Field(description="The id in this environment's Airflow.")
    found: bool | None = Field(
        description="Whether Airflow's metastore has it (null: could not be checked, e.g. no "
        "permission to list them)."
    )


class MappingCheckResult(BaseModel):
    environment: str
    connections: list[MappingEntry]
    variables: list[MappingEntry]
    secrets_backend: bool = Field(
        description="airflow_secrets_backend: ids missing from the metastore may still "
        "resolve from a secrets backend (not an error)."
    )


@router.post("/mapping-check", operation_id="checkEnvironmentMapping")
async def mapping_check(
    body: MappingCheckRequest, gw: GatewayDep, services: ServicesDep
) -> MappingCheckResult:
    """Check an environment's connection/variable mapping against the connected Airflow
    (the Airflow AutoAir deploys that environment to). Never an error: ids can come from a
    secrets backend the REST API cannot list."""
    from autoair.spec.env_mapping import mapping_targets

    try:
        connections, variables = mapping_targets(body.spec, body.environment)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc.args[0])) from exc

    async def ids(fetch: Callable[[], Awaitable[list[Any]]], attr: str) -> set[str] | None:
        try:
            return {str(getattr(item, attr)) for item in await fetch()}
        except AirflowError:
            return None  # forbidden / unreachable: unknown, not missing

    known_conns, known_vars = await asyncio.gather(
        ids(gw.connections, "connection_id") if connections else _none(),
        ids(gw.variables, "key") if variables else _none(),
    )

    def entries(mapping: dict[str, str], known: set[str] | None) -> list[MappingEntry]:
        return [
            MappingEntry(logical=k, actual=v, found=None if known is None else v in known)
            for k, v in sorted(mapping.items())
        ]

    return MappingCheckResult(
        environment=body.environment,
        connections=entries(connections, known_conns),
        variables=entries(variables, known_vars),
        secrets_backend=services.settings.airflow_secrets_backend,
    )


async def _none() -> None:
    return None


@router.post("/next-runs", operation_id="previewNextRuns")
async def next_runs(body: NextRunsRequest) -> NextRunsResult:
    """Compute upcoming run times of the spec's schedule with Airflow's own timetable."""
    return await run_in_threadpool(_next_runs, body.spec, body.count)


_CODE_TYPES = frozenset({"function", "import", "file"})


def _scrub_assets(value: Any) -> Any:
    """Asset references without ``extra`` / ``watchers`` (they can carry code values and
    play no part in time-based scheduling)."""
    if isinstance(value, dict):
        return {k: _scrub_assets(v) for k, v in value.items() if k not in ("extra", "watchers")}
    if isinstance(value, list):
        return [_scrub_assets(v) for v in value]
    return value


def _has_code(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("$type") in _CODE_TYPES or any(_has_code(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_code(v) for v in value)
    return False


def schedule_only(spec: DagSpec) -> DagSpec:
    """A copy of ``spec`` with only what determines run times.

    The preview builds the DAG *in this process*; params, callbacks, tasks, environments
    and asset extras may hold Python (inline functions, imports) whose definitions run
    when the module executes, so none of it may reach :func:`build_dag`.
    """
    data = {
        "dag_id": spec.dag_id,
        "schedule": _scrub_assets(spec.schedule.model_dump(mode="json", by_alias=True)),
        "timezone": spec.timezone,
        "start_date": spec.start_date,
        "end_date": spec.end_date,
        "catchup": spec.catchup,
    }
    if _has_code(data):
        raise ValueError("the schedule preview does not evaluate Python code")
    return DagSpec.model_validate(data)


def _next_runs(spec: DagSpec, count: int) -> NextRunsResult:
    try:
        from airflow.timetables.base import DataInterval, TimeRestriction

        from autoair.loader import build_dag
    except ImportError as exc:  # pragma: no cover - Airflow missing
        raise HTTPException(status_code=501, detail=f"Airflow not available: {exc}") from exc
    try:
        bare = schedule_only(spec)
    except ValueError as exc:
        return NextRunsResult(runs=[], error=str(exc))
    try:
        dag = build_dag(bare)
        tt: Any = dag.timetable
        if not hasattr(tt, "next_dagrun_info"):  # Task SDK definition -> core timetable
            try:
                from airflow.serialization.encoders import coerce_to_core_timetable

                tt = coerce_to_core_timetable(tt)
            except ImportError:  # Airflow < 3.2: SDK timetables are core timetables
                pass
        # The built DAG's dates are timezone-aware (spec dates are wall clock in the DAG tz).
        start = dag.start_date or datetime.datetime.now(datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        restriction = TimeRestriction(earliest=start, latest=dag.end_date, catchup=spec.catchup)
        last: DataInterval | None = None
        runs: list[str] = []
        guard = 0
        while len(runs) < count and guard < count * 50:
            guard += 1
            info = tt.next_dagrun_info(last_automated_data_interval=last, restriction=restriction)
            if info is None:
                break
            last = info.data_interval
            run_after = getattr(info, "run_after", None) or info.logical_date
            if run_after is not None and (
                spec.catchup or run_after >= now or (len(runs) == 0 and guard > count * 40)
            ):
                runs.append(run_after.isoformat())
            elif run_after is None:
                break
        summary = getattr(tt, "summary", None) or getattr(tt, "description", None)
        return NextRunsResult(runs=runs, summary=str(summary) if summary else None)
    except Exception as exc:
        return NextRunsResult(runs=[], error=f"{type(exc).__name__}: {exc}")
