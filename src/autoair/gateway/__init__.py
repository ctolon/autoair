"""Typed client for the subset of Airflow's REST API v2 the builder needs.

The caller's credentials (Bearer header or the UI's ``_token`` cookie) are forwarded,
so Airflow's own auth manager enforces RBAC for every call.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from typing import Any, TypeVar
from urllib.parse import quote

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from autoair.net import tls

_T = TypeVar("_T")
DEFAULT_TIMEOUT = 15.0  # default of AUTOAIR_AIRFLOW_TIMEOUT
# Concurrent requests when one view needs several Airflow calls (e.g. XCom values); default of
# AUTOAIR_SERVER__AIRFLOW_FETCH_CONCURRENCY.
FETCH_CONCURRENCY = 8
# Entries requested per list call (connections, pools, variables, task instances…); default of
# AUTOAIR_SERVER__AIRFLOW_PAGE_SIZE.
log = logging.getLogger(__name__)

PAGE_SIZE = 1000
MAX_LIST_ENTRIES = 50_000


def _seg(value: str) -> str:
    """One URL path segment: percent-encoded, so ids containing ``/``, ``?``, ``#`` or
    ``%`` cannot change the Airflow endpoint that is called."""
    if value in ("", ".", ".."):
        raise AirflowError(400, f"invalid path segment {value!r}")
    return quote(value, safe="")


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ConnectionInfo(_Loose):
    connection_id: str
    conn_type: str | None = None
    description: str | None = None
    host: str | None = None
    team_name: str | None = None


class ConnectionDraft(BaseModel):
    """A new Airflow connection; secrets are sent to Airflow and never kept by AutoAir."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,200}$")
    conn_type: str = Field(min_length=1, max_length=200)
    description: str | None = None
    host: str | None = None
    login: str | None = None
    password: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    port: int | None = Field(default=None, ge=0, le=65535)
    extra: str | None = None

    def payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class ConnectionTestResult(_Loose):
    status: bool
    message: str = ""


class HookFieldBehaviour(_Loose):
    """How a connection type shows one standard field (Airflow's ``field_behaviour``)."""

    hidden: bool = False
    title: str | None = None
    placeholder: str | None = None


class HookStandardFields(_Loose):
    description: HookFieldBehaviour | None = None
    url_schema: HookFieldBehaviour | None = Field(
        default=None, description="The `schema` field (Airflow names it url_schema here)."
    )
    host: HookFieldBehaviour | None = None
    port: HookFieldBehaviour | None = None
    login: HookFieldBehaviour | None = None
    password: HookFieldBehaviour | None = None


class HookExtraField(_Loose):
    """One provider-specific field stored in the connection's ``extra`` JSON."""

    schema_: dict[str, Any] = Field(
        default_factory=dict,
        alias="schema",
        serialization_alias="schema",
        description="JSON Schema of the value (type, title, format, enum…).",
    )
    description: str | None = None
    default: Any = Field(default=None, description="Default value (never a masked secret).")


class ConnectionHookMeta(_Loose):
    """Form metadata of a connection type (Airflow's ``/ui/connections/hook_meta``)."""

    connection_type: str
    hook_name: str | None = None
    hook_class_name: str | None = None
    default_conn_name: str | None = None
    standard_fields: HookStandardFields | None = None
    extra_fields: dict[str, HookExtraField] = Field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> ConnectionHookMeta | None:
        """Tolerant parsing: unknown shapes become string fields, bad entries are skipped."""
        if not isinstance(raw, dict) or not raw.get("connection_type"):
            return None
        extras: dict[str, HookExtraField] = {}
        for name, spec in (raw.get("extra_fields") or {}).items():
            if not isinstance(name, str):
                continue
            if isinstance(spec, dict) and isinstance(spec.get("schema"), dict):
                value = spec.get("value")
                extras[name] = HookExtraField(
                    schema=dict(spec["schema"]),
                    description=spec.get("description")
                    if isinstance(spec.get("description"), str)
                    else None,
                    default=None if value == "***" else value,
                )
            else:
                extras[name] = HookExtraField(schema={"type": "string", "title": name})
        standard = raw.get("standard_fields")
        try:
            fields = HookStandardFields.model_validate(standard) if standard else None
        except ValueError:
            fields = None
        return cls(
            connection_type=str(raw["connection_type"]),
            hook_name=raw.get("hook_name"),
            hook_class_name=raw.get("hook_class_name"),
            default_conn_name=raw.get("default_conn_name"),
            standard_fields=fields,
            extra_fields=extras,
        )


class PoolInfo(_Loose):
    name: str
    slots: int | None = None
    description: str | None = None
    team_name: str | None = None


class VariableInfo(_Loose):
    key: str
    description: str | None = None
    team_name: str | None = None


def for_team(items: list[_T], team: str | None) -> list[_T]:
    """Multi-team (Airflow 3.2+): keep global objects and the team's own ones."""
    if not team:
        return items
    return [i for i in items if getattr(i, "team_name", None) in (None, team)]


class ImportErrorInfo(_Loose):
    import_error_id: int | None = None
    filename: str | None = None
    stack_trace: str | None = None
    timestamp: str | None = None
    bundle_name: str | None = None


class DagStatus(_Loose):
    dag_id: str
    exists: bool
    is_paused: bool | None = None
    file_token: str | None = None
    fileloc: str | None = None
    last_parsed_time: str | None = None
    scheduling_state: str | None = Field(
        default=None,
        description="Airflow >= 3.4: active / draining / paused (null on older versions).",
    )
    latest_runs: list[dict[str, Any]] = []


class DagRunInfo(_Loose):
    dag_run_id: str
    state: str | None = None
    run_type: str | None = None
    logical_date: str | None = None
    run_after: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    conf: dict[str, Any] | None = None
    bundle_version: str | None = None
    note: str | None = None


class TaskInstanceInfo(_Loose):
    task_id: str
    map_index: int = -1
    state: str | None = None
    try_number: int = 0
    start_date: str | None = None
    end_date: str | None = None
    duration: float | None = None
    operator: str | None = None
    rendered_map_index: str | None = None


class XComInfo(_Loose):
    key: str
    map_index: int = -1
    value: Any = None


class DagVersionInfo(_Loose):
    version_number: int
    bundle_name: str | None = None
    bundle_version: str | None = None
    created_at: str | None = None
    bundle_url: str | None = None


class ConnectionMetadata(_Loose):
    """What AI analysis may know about a connection: never logins, passwords or values of
    ``extra`` - only its type, endpoint and the names of the extra fields."""

    connection_id: str
    conn_type: str | None = None
    host: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    port: int | None = None
    extra_keys: list[str] = []
    description: str | None = None


class PoolState(_Loose):
    name: str
    slots: int | None = None
    occupied_slots: int | None = None
    running_slots: int | None = None
    queued_slots: int | None = None
    scheduled_slots: int | None = None
    deferred_slots: int | None = None
    open_slots: int | None = None
    include_deferred: bool | None = None


def parse_version(version: str | None) -> tuple[int, int, int] | None:
    """``"3.4.0b2"`` -> ``(3, 4, 0)``; ``None`` for a missing or unparsable version."""
    if not version:
        return None
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def log_try_number(ti: TaskInstanceInfo, server: tuple[int, int, int] | None) -> int:
    """The newest try of ``ti`` that can have a log.

    From Airflow 3.4 an ``up_for_retry`` task instance already carries the *upcoming* try
    number (the failed try is archived as ``try_number - 1``); before 3.4 it keeps the failed
    try's number until the retry starts. An unknown server version keeps the old reading.
    """
    latest = max(1, ti.try_number)
    if ti.state == "up_for_retry" and server is not None and server >= (3, 4, 0):
        return max(1, latest - 1)
    return latest


class AirflowError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Airflow API {status}: {detail}")
        self.status = status
        self.detail = detail


class AirflowGateway:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx2.AsyncBaseTransport | None = None,
        fetch_concurrency: int = FETCH_CONCURRENCY,
        page_size: int = PAGE_SIZE,
        max_entries: int = MAX_LIST_ENTRIES,
    ) -> None:
        self._server_version: tuple[int, int, int] | None = None
        self._server_version_read = False
        self.fetch_concurrency = max(1, fetch_concurrency)
        self.page_size = max(1, page_size)
        self.max_entries = max(1, max_entries)
        # Airflow's UI API (``/ui/...``, e.g. connection form metadata) lives beside /api/v2.
        self._root = base_url.rstrip("/")
        self._client = httpx2.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v2",
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            transport=transport,
            **tls.httpx_kwargs("airflow", transport=transport, asynchronous=True),
        )

    async def __aenter__(self) -> AirflowGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise AirflowError(502, f"cannot reach Airflow: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = str(resp.json().get("detail", resp.text))
            except ValueError:
                detail = resp.text
            raise AirflowError(resp.status_code, detail)
        return resp.json() if resp.content else None

    async def _paged(self, url: str, key: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Every entry of a paged Airflow list (up to ``limit`` or ``max_entries``). Pages are
        followed by what the server actually returned: Airflow clamps ``limit`` to its own
        maximum_page_limit, so asking for a large page does not return everything."""
        cap = min(limit or self.max_entries, self.max_entries)
        items: list[dict[str, Any]] = []
        total: int | None = None
        while len(items) < cap:
            size = min(self.page_size, cap - len(items))
            data = await self._request("GET", url, params={"limit": size, "offset": len(items)})
            page = list(data.get(key, [])) if isinstance(data, dict) else []
            # An empty page, or the same page again (a server or proxy that ignores
            # ``offset``), ends the list.
            if not page or (items and page == items[-len(page) :]):
                break
            items.extend(page)
            if isinstance(data.get("total_entries"), int):
                total = data["total_entries"]
                if len(items) >= total:
                    break
            elif len(page) < size:
                break  # no total: a short page is the last one
        if limit is None and len(items) >= cap and (total is None or total > cap):
            log.warning("%s: stopped after %d entries (server.airflow_list_max_entries)", url, cap)
        return items[:cap]

    async def version(self) -> dict[str, Any]:
        return dict(await self._request("GET", "/version"))

    async def server_version(self) -> tuple[int, int, int] | None:
        """The server's Airflow version (once per gateway, i.e. per request); ``None`` when
        it cannot be read."""
        if not self._server_version_read:
            self._server_version_read = True
            try:
                self._server_version = parse_version(str((await self.version()).get("version")))
            except AirflowError:
                self._server_version = None
        return self._server_version

    async def latest_log_try(self, ti: TaskInstanceInfo) -> int:
        """The newest try of ``ti`` with a log, by the server's try_number semantics."""
        server = await self.server_version() if ti.state == "up_for_retry" else None
        return log_try_number(ti, server)

    async def connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo.model_validate(c)
            for c in await self._paged("/connections", "connections")
        ]

    async def connection(self, conn_id: str) -> ConnectionInfo | None:
        """One connection's public fields (Airflow redacts secrets); ``None`` when it does
        not exist. A 403 (the caller may not read connections) is raised."""
        try:
            data = await self._request("GET", f"/connections/{_seg(conn_id)}")
        except AirflowError as exc:
            if exc.status == 404:
                return None
            raise
        return ConnectionInfo.model_validate(data)

    async def connection_hook_meta(self) -> list[ConnectionHookMeta]:
        """Connection form metadata per conn_type (relabelled/hidden standard fields, typed
        extra fields), from Airflow's UI API. Airflow versions without it (404/405) yield an
        empty list, so callers fall back to a generic form."""
        try:
            data = await self._request("GET", f"{self._root}/ui/connections/hook_meta")
        except AirflowError as exc:
            if exc.status in (404, 405):
                return []
            raise
        if not isinstance(data, list):
            return []
        parsed = (ConnectionHookMeta.parse(item) for item in data)
        metas = {m.connection_type: m for m in parsed if m is not None}
        return sorted(metas.values(), key=lambda m: m.connection_type)

    async def create_connection(self, draft: ConnectionDraft) -> ConnectionInfo:
        try:
            data = await self._request("POST", "/connections", json=draft.payload())
        except AirflowError as exc:
            if exc.status == 409:
                raise AirflowError(
                    409, f"connection {draft.connection_id!r} already exists in Airflow"
                ) from exc
            raise
        return ConnectionInfo.model_validate(data)

    async def test_connection(self, draft: ConnectionDraft) -> ConnectionTestResult:
        """Airflow tests it with the provider hook (needs ``[core] test_connection=Enabled``)."""
        try:
            data = await self._request("POST", "/connections/test", json=draft.payload())
        except AirflowError as exc:
            if exc.status == 403 and "disabled" in exc.detail.lower():
                return ConnectionTestResult(
                    status=False,
                    message="Testing connections is disabled in Airflow "
                    "([core] test_connection = Enabled turns it on).",
                )
            raise
        return ConnectionTestResult.model_validate(data)

    async def pools(self) -> list[PoolInfo]:
        return [PoolInfo.model_validate(p) for p in await self._paged("/pools", "pools")]

    async def variables(self) -> list[VariableInfo]:
        # values are deliberately dropped: the builder only needs keys.
        return [
            VariableInfo.model_validate(v) for v in await self._paged("/variables", "variables")
        ]

    async def import_errors(self, filename_contains: str | None = None) -> list[ImportErrorInfo]:
        errors = [
            ImportErrorInfo.model_validate(e)
            for e in await self._paged("/importErrors", "import_errors")
        ]
        if filename_contains:
            errors = [e for e in errors if e.filename and filename_contains in e.filename]
        return errors

    async def dag_status(self, dag_id: str) -> DagStatus:
        try:
            details = await self._request("GET", f"/dags/{_seg(dag_id)}/details")
        except AirflowError as exc:
            if exc.status == 404:
                return DagStatus(dag_id=dag_id, exists=False)
            raise
        runs = await self._request(
            "GET", f"/dags/{_seg(dag_id)}/dagRuns", params={"limit": 5, "order_by": "-run_after"}
        )
        return DagStatus(
            dag_id=dag_id,
            exists=True,
            is_paused=details.get("is_paused"),
            file_token=details.get("file_token"),
            fileloc=details.get("fileloc"),
            last_parsed_time=details.get("last_parsed_time"),
            scheduling_state=details.get("scheduling_state"),
            latest_runs=[
                {
                    k: r.get(k)
                    for k in (
                        "dag_run_id",
                        "state",
                        "run_after",
                        "logical_date",
                        "start_date",
                        "end_date",
                    )
                }
                for r in runs.get("dag_runs", [])
            ],
        )

    async def reparse(self, file_token: str) -> None:
        await self._request("PUT", f"/parseDagFile/{_seg(file_token)}")

    # ----------------------------------------------------------------- runs & ops
    async def dag_runs(self, dag_id: str, limit: int = 25) -> list[DagRunInfo]:
        data = await self._request(
            "GET",
            f"/dags/{_seg(dag_id)}/dagRuns",
            params={"limit": limit, "order_by": "-run_after"},
        )
        return [DagRunInfo.model_validate(r) for r in data.get("dag_runs", [])]

    async def task_instances(self, dag_id: str, run_id: str) -> list[TaskInstanceInfo]:
        data = await self._request(
            "GET",
            f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}/taskInstances",
            params={"limit": self.page_size},
        )
        return [TaskInstanceInfo.model_validate(t) for t in data.get("task_instances", [])]

    async def task_log(
        self, dag_id: str, run_id: str, task_id: str, try_number: int, map_index: int = -1
    ) -> str:
        data = await self._request(
            "GET",
            f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}/taskInstances/{_seg(task_id)}"
            f"/logs/{int(try_number)}",
            params={"map_index": map_index, "full_content": "true"},
            headers={"Accept": "application/json"},
        )
        content = data.get("content", []) if isinstance(data, dict) else []
        lines: list[str] = []
        for item in content:
            if isinstance(item, dict):
                ts = item.get("timestamp", "")
                level = item.get("level", "")
                lines.append(
                    " ".join(x for x in (ts, level.upper(), str(item.get("event", ""))) if x)
                )
            else:
                lines.append(str(item))
        return "\n".join(lines)

    async def dag_run(self, dag_id: str, run_id: str) -> DagRunInfo | None:
        try:
            data = await self._request("GET", f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}")
        except AirflowError as exc:
            if exc.status == 404:
                return None
            raise
        return DagRunInfo.model_validate(data)

    async def xcom_entries(
        self, dag_id: str, run_id: str, task_id: str, map_index: int = -1
    ) -> list[XComInfo]:
        base = f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}/taskInstances/{_seg(task_id)}"
        data = await self._request(
            "GET", f"{base}/xcomEntries", params={"map_index": map_index, "limit": 100}
        )
        entries: list[dict[str, Any]] = list(data.get("xcom_entries", []))
        # The list endpoint carries no values: fetch them concurrently (bounded), not
        # one round-trip after another.
        limit = asyncio.Semaphore(self.fetch_concurrency)

        async def fetch(entry: dict[str, Any]) -> XComInfo:
            async with limit:
                value = await self._request(
                    "GET",
                    f"{base}/xcomEntries/{_seg(str(entry['key']))}",
                    params={
                        "map_index": entry.get("map_index", map_index),
                        "stringify": "false",
                        "deserialize": "true",
                    },
                )
            return XComInfo(
                key=entry["key"],
                map_index=entry.get("map_index", -1),
                value=value.get("value") if isinstance(value, dict) else value,
            )

        return list(await asyncio.gather(*(fetch(e) for e in entries)))

    async def dag_source(self, dag_id: str) -> str:
        data = await self._request(
            "GET", f"/dagSources/{_seg(dag_id)}", headers={"Accept": "application/json"}
        )
        if isinstance(data, dict):
            return str(data.get("content", ""))
        return str(data)

    async def dag_versions(self, dag_id: str) -> list[DagVersionInfo]:
        data = await self._request(
            "GET",
            f"/dags/{_seg(dag_id)}/dagVersions",
            params={"limit": 100, "order_by": "-version_number"},
        )
        return [DagVersionInfo.model_validate(v) for v in data.get("dag_versions", [])]

    async def clear_run(
        self,
        dag_id: str,
        run_id: str,
        *,
        only_failed: bool = False,
        dry_run: bool = False,
        only_new: bool = False,
        run_on_latest_version: bool | None = None,
        keep_task_state: bool = False,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Clear a run. Optional fields are only sent when set: Airflow's request bodies are
        strict, so a field an older version does not know (``only_new`` < 3.2,
        ``run_on_latest_version`` < 3.1) must not appear in the body.

        ``keep_task_state`` (Airflow >= 3.4) exists only on ``clearTaskInstances``: that
        endpoint is used for the run's task instances instead of ``dagRuns/{id}/clear``
        (it has no ``only_new``)."""
        body: dict[str, Any] = {"dry_run": dry_run, "only_failed": only_failed}
        if run_on_latest_version is not None:
            body["run_on_latest_version"] = run_on_latest_version
        if note:
            body["note"] = note
        if keep_task_state:
            if only_new:
                raise AirflowError(422, "keep_task_state cannot be combined with only_new")
            body.update(dag_run_id=run_id, keep_task_state=True, reset_dag_runs=True)
            path = f"/dags/{_seg(dag_id)}/clearTaskInstances"
        else:
            if only_new:
                if only_failed:
                    raise AirflowError(422, "only_new and only_failed are mutually exclusive")
                body["only_new"] = True
            path = f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}/clear"
        return dict(await self._request("POST", path, json=body) or {})

    async def clear_task_instances(
        self,
        dag_id: str,
        run_id: str,
        task_id: str,
        *,
        map_index: int = -1,
        only_failed: bool = False,
        include_downstream: bool = False,
        include_upstream: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Clear one task instance of a run (``clearTaskInstances``, Airflow >= 3.0); the run
        goes back to queued so the scheduler picks the cleared tasks up. ``dry_run`` returns
        the task instances that would be cleared without changing anything."""
        task: str | list[Any] = [task_id, map_index] if map_index >= 0 else task_id
        body: dict[str, Any] = {
            "dry_run": dry_run,
            "dag_run_id": run_id,
            "task_ids": [task],
            "only_failed": only_failed,
            "include_downstream": include_downstream,
            "include_upstream": include_upstream,
            "reset_dag_runs": True,
        }
        return dict(
            await self._request("POST", f"/dags/{_seg(dag_id)}/clearTaskInstances", json=body) or {}
        )

    async def set_task_instance_state(
        self, dag_id: str, run_id: str, task_id: str, state: str, *, map_index: int = -1
    ) -> dict[str, Any]:
        """Mark one task instance ``success`` / ``failed`` (like Airflow's UI)."""
        path = f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}/taskInstances/{_seg(task_id)}"
        if map_index >= 0:
            path += f"/{int(map_index)}"
        data = await self._request(
            "PATCH", path, params={"update_mask": "new_state"}, json={"new_state": state}
        )
        return dict(data or {}) if isinstance(data, dict) else {"task_instances": data}

    async def set_dag_run_state(self, dag_id: str, run_id: str, state: str) -> DagRunInfo:
        """Mark a run ``success`` / ``failed`` / ``queued``."""
        data = await self._request(
            "PATCH",
            f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}",
            params={"update_mask": "state"},
            json={"state": state},
        )
        return DagRunInfo.model_validate(data)

    async def backfill(
        self,
        dag_id: str,
        from_date: str,
        to_date: str,
        *,
        reprocess_behavior: str = "none",
        max_active_runs: int = 10,
        run_backwards: bool = False,
        dry_run: bool = False,
        dag_run_conf: dict[str, Any] | None = None,
        run_on_latest_version: bool | None = None,
        drain_dag: bool = False,
    ) -> dict[str, Any]:
        """``run_on_latest_version`` (>= 3.1) and ``drain_dag`` (>= 3.4) are only sent when
        set (strict bodies); ``drain_dag`` is never sent to the dry-run endpoint."""
        body: dict[str, Any] = {
            "dag_id": dag_id,
            "from_date": from_date,
            "to_date": to_date,
            "reprocess_behavior": reprocess_behavior,
            "max_active_runs": max_active_runs,
            "run_backwards": run_backwards,
        }
        if dag_run_conf:
            body["dag_run_conf"] = dag_run_conf
        if run_on_latest_version is not None:
            body["run_on_latest_version"] = run_on_latest_version
        if drain_dag and not dry_run:
            body["drain_dag"] = True
        path = "/backfills/dry_run" if dry_run else "/backfills"
        return dict(await self._request("POST", path, json=body) or {})

    async def create_asset_event(
        self, asset_id: int, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return dict(
            await self._request(
                "POST", "/assets/events", json={"asset_id": asset_id, "extra": extra or {}}
            )
            or {}
        )

    async def materialize_asset(self, asset_id: int) -> dict[str, Any]:
        """Trigger the one DAG that produces the asset (Airflow >= 3.0; 404 when no DAG
        produces it, 409 when several do)."""
        return dict(
            await self._request("POST", f"/assets/{int(asset_id)}/materialize", json={}) or {}
        )

    async def assets(self, name_pattern: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 200}
        if name_pattern:
            params["name_pattern"] = name_pattern
        data = await self._request("GET", "/assets", params=params)
        return [
            {k: a.get(k) for k in ("id", "name", "uri", "group")} for a in data.get("assets", [])
        ]

    async def connection_metadata(self, conn_id: str) -> ConnectionMetadata | None:
        """Type, host, schema, port and extra field NAMES of a connection (no secrets)."""
        try:
            data = await self._request("GET", f"/connections/{_seg(conn_id)}")
        except AirflowError as exc:
            if exc.status == 404:
                return None
            raise
        extra_keys: list[str] = []
        raw = data.get("extra") if isinstance(data, dict) else None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                extra_keys = sorted(str(k) for k in parsed)
        elif isinstance(raw, dict):
            extra_keys = sorted(str(k) for k in raw)
        return ConnectionMetadata(
            connection_id=str(data.get("connection_id") or conn_id),
            conn_type=data.get("conn_type"),
            host=data.get("host"),
            schema=data.get("schema"),
            port=data.get("port"),
            extra_keys=extra_keys,
            description=data.get("description"),
        )

    async def pool_states(self) -> list[PoolState]:
        return [PoolState.model_validate(p) for p in await self._paged("/pools", "pools")]

    async def providers(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/providers", params={"limit": self.page_size})
        return [
            {k: p.get(k) for k in ("package_name", "version")}
            for p in (data or {}).get("providers", [])
        ]

    async def xcom_keys(
        self, dag_id: str, run_id: str, task_id: str, map_index: int = -1
    ) -> list[dict[str, Any]]:
        """XCom keys (no values) of a task instance."""
        base = f"/dags/{_seg(dag_id)}/dagRuns/{_seg(run_id)}/taskInstances/{_seg(task_id)}"
        data = await self._request(
            "GET", f"{base}/xcomEntries", params={"map_index": map_index, "limit": 100}
        )
        return [
            {"key": e.get("key"), "map_index": e.get("map_index", -1)}
            for e in (data or {}).get("xcom_entries", [])
        ]

    async def config_section(self, section: str | None = None) -> list[dict[str, Any]]:
        """Airflow's configuration (Airflow masks sensitive values; 403 unless
        ``[api] expose_config`` allows it)."""
        params = {"section": section} if section else None
        data = await self._request(
            "GET", "/config", params=params, headers={"Accept": "application/json"}
        )
        out = []
        for sec in (data or {}).get("sections", []):
            out.append(
                {
                    "section": sec.get("name"),
                    "options": {str(o.get("key")): o.get("value") for o in sec.get("options", [])},
                }
            )
        return out

    async def dag_bundles(self) -> list[dict[str, Any]]:
        try:
            data = await self._request("GET", "/dagBundles")
        except AirflowError as exc:
            if exc.status == 404:
                return []  # not available before Airflow 3.4
            raise
        return list(data.get("dag_bundles", []))

    async def set_paused(self, dag_id: str, paused: bool) -> None:
        await self._request("PATCH", f"/dags/{_seg(dag_id)}", json={"is_paused": paused})

    async def set_scheduling_state(self, dag_id: str, state: str) -> None:
        """Airflow >= 3.4: ``active``, ``draining`` (no new scheduled runs; the DAG pauses
        once its unfinished runs finish) or ``paused``. Exactly one of ``is_paused`` /
        ``scheduling_state`` may be sent."""
        await self._request(
            "PATCH",
            f"/dags/{_seg(dag_id)}",
            params={"update_mask": "scheduling_state"},
            json={"scheduling_state": state},
        )

    async def trigger(
        self,
        dag_id: str,
        conf: dict[str, Any] | None = None,
        *,
        logical_date: str | bool | None = True,
        partition_key: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Trigger a run. ``logical_date=True`` uses "now" (like Airflow's UI) so that
        ``{{ ds }}``-style templates work; ``None`` creates a run without logical date."""
        if logical_date is True:
            logical_date = datetime.datetime.now(datetime.timezone.utc).isoformat()
        body: dict[str, Any] = {"logical_date": logical_date or None}
        if conf:
            body["conf"] = conf
        if partition_key:
            body["partition_key"] = partition_key
        if note:
            body["note"] = note
        return dict(await self._request("POST", f"/dags/{_seg(dag_id)}/dagRuns", json=body))
