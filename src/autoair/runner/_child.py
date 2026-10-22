"""Executed in an isolated interpreter by :mod:`autoair.runner` (never imported by the API).

``python -m autoair.runner._child <request.json> <result.json> [<sandbox.json>]``

With a sandbox (a :class:`~autoair.validation.sandbox.WorkerSandbox` of profile ``run``)
the child first re-executes itself as PID 1 of fresh PID + user + mount namespaces when
``process_isolation`` asks for it (and the kernel allows it), then applies the resource
limits, ``PR_SET_NO_NEW_PRIVS``, non-dumpable and the run profile of the audit hook before
it imports the DAG file.

Runs one task (or a whole DAG) **without the metadata DB**: the task is taken from a
``DagBag`` import of the generated file, its templates are rendered against a
synthetic context (``logical_date`` / ``ds`` / ``params`` / ``dag_run.conf`` / a fake
``ti`` whose ``xcom_pull`` serves mocked or in-memory XComs) and ``execute(context)``
is called the way the Task SDK runner does it (pre/post execute hooks,
``ExecutorSafeguard`` sentinel, ``set_current_context``).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import traceback
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

RETURN_KEY = "return_value"


def _safe_json(value: Any) -> tuple[bool, Any]:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False, None
    return True, value


# Characters of a value's repr in results (AUTOAIR_RUNNER__MAX_VALUE_CHARS, set by main()).
_REPR_LIMIT = 4000


def _repr(value: Any, limit: int | None = None) -> str:
    limit = _REPR_LIMIT if limit is None else limit
    try:
        text = repr(value)
    except Exception as exc:  # pragma: no cover - exotic objects
        text = f"<unrepresentable {type(value).__name__}: {exc}>"
    return text if len(text) <= limit else text[:limit] + "…"


class FakeTaskInstance:
    """The subset of ``RuntimeTaskInstance`` operators commonly touch."""

    def __init__(
        self,
        task: Any,
        run_id: str,
        store: dict[tuple[str, str], Any],
        map_index: int = -1,
    ) -> None:
        self.task = task
        self.task_id = task.task_id
        self.dag_id = task.dag_id
        self.run_id = run_id
        self.map_index = map_index
        self.try_number = 1
        self.max_tries = 0
        self.hostname = "autoair-test"
        self.log_url = ""
        self.state = "running"
        self.start_date: Any = None
        self.end_date: Any = None
        self.rendered_map_index: str | None = None
        self.is_trigger_log_context = False
        self._store = store
        self.pushed: dict[str, Any] = {}

    def xcom_pull(
        self,
        task_ids: str | Iterable[str] | None = None,
        dag_id: str | None = None,
        key: str = RETURN_KEY,
        include_prior_dates: bool = False,
        *,
        map_indexes: Any = None,
        default: Any = None,
        run_id: str | None = None,
    ) -> Any:
        if type(default).__name__ == "ArgNotSet":
            default = None
        if task_ids is None:
            task_ids = self.task_id
        if isinstance(task_ids, str):
            return self._store.get((task_ids, key), default)
        return [self._store.get((t, key), default) for t in task_ids]

    def xcom_push(self, key: str, value: Any) -> None:
        self.pushed[key] = value
        self._store[(self.task_id, key)] = value

    def get_template_context(self) -> Any:  # pragma: no cover - rarely used
        return {}


def _context(
    dag: Any, task: Any, ti: FakeTaskInstance, conf: dict[str, Any], logical_date: Any
) -> dict[str, Any]:
    from airflow.sdk.definitions.param import process_params
    from airflow.sdk.execution_time.context import (
        ConnectionAccessor,
        InletEventsAccessors,
        MacrosAccessor,
        OutletEventAccessors,
        VariableAccessor,
    )

    dag_run = SimpleNamespace(
        dag_id=dag.dag_id,
        run_id=ti.run_id,
        conf=conf,
        logical_date=logical_date,
        data_interval_start=logical_date,
        data_interval_end=logical_date,
        run_after=logical_date,
        start_date=logical_date,
        end_date=None,
        run_type="manual",
        state="running",
        clear_number=0,
        triggered_by="test",
        consumed_asset_events=[],
        partition_key=None,
        partition_date=None,
    )
    ds = logical_date.strftime("%Y-%m-%d")
    ts = logical_date.isoformat()
    return {
        "dag": dag,
        "task": task,
        "ti": ti,
        "task_instance": ti,
        "run_id": ti.run_id,
        "dag_run": dag_run,
        "inlets": list(getattr(task, "inlets", []) or []),
        "outlets": list(getattr(task, "outlets", []) or []),
        "outlet_events": OutletEventAccessors(),
        "inlet_events": InletEventsAccessors(list(getattr(task, "inlets", []) or [])),
        "macros": MacrosAccessor(),
        "params": process_params(dag, task, conf, suppress_exception=False),
        "var": {
            "json": VariableAccessor(deserialize_json=True),
            "value": VariableAccessor(deserialize_json=False),
        },
        "conn": ConnectionAccessor(),
        "map_index_template": getattr(task, "map_index_template", None),
        "logical_date": logical_date,
        "ds": ds,
        "ds_nodash": ds.replace("-", ""),
        "ts": ts,
        "ts_nodash": logical_date.strftime("%Y%m%dT%H%M%S"),
        "ts_nodash_with_tz": ts.replace("-", "").replace(":", ""),
        "data_interval_start": logical_date,
        "data_interval_end": logical_date,
        "task_instance_key_str": f"{dag.dag_id}__{task.task_id}__{ds.replace('-', '')}",
        "task_reschedule_count": 0,
        "triggering_asset_events": {},
        "test_mode": True,
    }


def run_task(
    dag: Any,
    task: Any,
    conf: dict[str, Any],
    store: dict[tuple[str, str], Any],
    logical_date: Any,
) -> dict[str, Any]:
    """Render + execute one task in-process; never raises."""
    from airflow.sdk.bases.operator import BaseOperator, ExecutorSafeguard

    try:  # Airflow >= 3.2
        from airflow.sdk.exceptions import (
            AirflowSkipException,
            DownstreamTasksSkipped,
            TaskDeferred,
        )
    except ImportError:  # Airflow 3.0 / 3.1
        from airflow.exceptions import (
            AirflowSkipException,
            DownstreamTasksSkipped,
            TaskDeferred,
        )
    from airflow.sdk.execution_time.context import (
        context_to_airflow_vars,
        set_current_context,
    )

    out: dict[str, Any] = {"task_id": task.task_id, "skipped_downstream": []}
    start = time.perf_counter()
    mapped = bool(getattr(task, "is_mapped", False))
    ti = FakeTaskInstance(task, f"autoair_test__{int(time.time())}", store, 0 if mapped else -1)
    try:
        if not mapped:
            task = task.prepare_for_execution()
            ti.task = task
        context = _context(dag, task, ti, conf, logical_date)
        task.render_template_fields(context, dag.get_template_env())
        task = ti.task if mapped else task  # MappedOperator swaps in the unmapped task
        context["task"] = task
        os.environ.update(context_to_airflow_vars(context, in_env_var_format=True))
        ExecutorSafeguard.test_mode = True
        with set_current_context(context):
            if getattr(task.pre_execute, "__func__", None) is not BaseOperator.pre_execute:
                task.pre_execute(context=context)
            ctx = contextvars.copy_context()
            ctx.run(ExecutorSafeguard.tracker.set, task)
            result = ctx.run(task.execute, context=context)
            if getattr(task.post_execute, "__func__", None) is not BaseOperator.post_execute:
                task.post_execute(context=context, result=result)
        if getattr(task, "do_xcom_push", True) and result is not None:
            ti.xcom_push(RETURN_KEY, result)
        ok, value = _safe_json(result)
        out.update(
            state="success",
            return_value_repr=None if result is None else _repr(result),
            return_value=value if ok else None,
        )
    except DownstreamTasksSkipped as exc:
        # Entries are task ids, or (task_id, map_index) pairs for operator-based branches.
        skipped = [t[0] if isinstance(t, (list, tuple)) else t for t in exc.tasks]
        out.update(state="success", skipped_downstream=[str(t) for t in skipped])
    except AirflowSkipException as exc:
        out.update(state="skipped", error=str(exc) or None)
    except TaskDeferred as exc:
        out.update(
            state="deferred",
            error=f"task deferred to trigger {type(exc.trigger).__name__}; deferrable "
            "execution is not supported in test runs (set deferrable=False)",
        )
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        out.update(state="failed", error="".join(traceback.format_exception(exc)[-30:]))
    out["xcom_pushes"] = {
        k: _repr(v, max(100, _REPR_LIMIT // 4)) for k, v in ti.pushed.items() if k != RETURN_KEY
    }
    out["mapped"] = mapped
    out["duration"] = round(time.perf_counter() - start, 4)
    return out


def _load_bag(source_file: str) -> Any:
    import inspect

    from autoair._compat import dagbag_class

    cls = dagbag_class()
    kwargs: dict[str, object] = {"dag_folder": source_file}
    if "include_examples" in inspect.signature(cls.__init__).parameters:
        kwargs["include_examples"] = False
    return cls(**kwargs)


def _load_dag_and_bag(source_file: str, dag_id: str) -> tuple[Any, str | None, Any]:
    bag = _load_bag(source_file)
    if bag.import_errors:
        return None, "\n".join(str(e) for e in bag.import_errors.values()), bag
    dag = bag.dags.get(dag_id)
    if dag is None:
        return None, f"DAG {dag_id!r} not found (found: {sorted(bag.dags)})", bag
    return dag, None, bag


def _store_from_mocks(mocks: dict[str, Any]) -> dict[tuple[str, str], Any]:
    store: dict[tuple[str, str], Any] = {}
    for k, v in (mocks or {}).items():
        task_id, sep, key = k.partition(":")
        store[(task_id, key if sep else RETURN_KEY)] = v
    return store


_FAILED = {"failed", "upstream_failed"}


def _should_run(rule: str, upstream: list[str]) -> str | None:
    """Simplified trigger rule evaluation; returns a state to set instead of running."""
    if not upstream:
        return None
    failed = sum(s in _FAILED for s in upstream)
    skipped = sum(s == "skipped" for s in upstream)
    success = sum(s == "success" for s in upstream)
    rule = str(getattr(rule, "value", rule))
    if rule in ("always", "all_done", "all_done_setup_success"):
        return None
    if rule == "all_success":
        return "upstream_failed" if failed else ("skipped" if skipped else None)
    if rule == "all_failed":
        return None if failed == len(upstream) else "skipped"
    if rule == "one_success":
        return None if success else ("upstream_failed" if failed else "skipped")
    if rule == "one_failed":
        return None if failed else "skipped"
    if rule == "one_done":
        return None if (success or failed) else "skipped"
    if rule in ("none_failed", "none_failed_min_one_success"):
        if failed:
            return "upstream_failed"
        if rule == "none_failed_min_one_success" and not success:
            return "skipped"
        return None
    if rule == "none_skipped":
        return "skipped" if skipped else None
    if rule == "all_skipped":
        return None if skipped == len(upstream) else "skipped"
    return None


def run_dag_in_memory(dag: Any, conf: dict[str, Any], logical_date: Any) -> dict[str, Any]:
    store: dict[tuple[str, str], Any] = {}
    states: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    forced_skip: set[str] = set()
    for task in dag.topological_sort():
        upstream = [states.get(u, "success") for u in task.upstream_task_ids]
        verdict = "skipped" if task.task_id in forced_skip else None
        verdict = verdict or _should_run(task.trigger_rule, upstream)
        if verdict:
            states[task.task_id] = verdict
            results.append({"task_id": task.task_id, "state": verdict, "duration": 0.0})
            continue
        res = run_task(dag, task, conf, store, logical_date)
        states[task.task_id] = res["state"]
        forced_skip.update(res.get("skipped_downstream") or [])
        if res.get("mapped") and (task.task_id, RETURN_KEY) in store:
            store[(task.task_id, RETURN_KEY)] = [store[(task.task_id, RETURN_KEY)]]
        results.append(res)
    failed = any(s in _FAILED for s in states.values())
    return {"state": "failed" if failed else "success", "tasks": results, "mode": "memory"}


def _serialize_to_db(bag: Any, dag_id: str) -> None:
    """Write the DAG under test to the metadata DB when it is not there yet.

    Since Airflow 3.1 ``dag.test()`` creates its DAG run from the *serialized* DAG and looks
    for it by parsing the configured DAG bundles; the generated file lives in the run's
    scratch folder, outside every bundle, so the run failed with "the dag is not
    serialized". Changing the bundle configuration for the child is no way out:
    ``dag.test()`` reconciles the configured bundles into the metadata DB and would mark the
    real ones inactive. The DAG is written under the first configured bundle instead (the
    one ``dag.test()`` walks first; the DAG processor later marks it stale because its file
    is not in that bundle). A DAG that is already serialized (deployed under the same id)
    is left alone, the way ``dag.test()`` itself treats it. Airflow 3.0 needs nothing:
    its ``dag.test()`` builds the run from the in-memory DAG."""
    try:  # Airflow >= 3.2
        from airflow.dag_processing.dagbag import sync_bag_to_db
    except ImportError:
        try:  # Airflow 3.1
            from airflow.models.dagbag import sync_bag_to_db
        except ImportError:  # Airflow 3.0
            return
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.models.dag_version import DagVersion

    if DagVersion.get_version(dag_id):
        return
    manager = DagBundlesManager()
    manager.sync_bundles_to_db()
    bundles = list(manager.get_all_dag_bundles())
    if not bundles:
        return  # dag.test() reports the missing serialized DAG itself
    # the bag holds the generated file only: no DAG of the real bundle is rewritten
    sync_bag_to_db(bag, bundles[0].name, None)


def _json_content_type_for_execution_api() -> None:
    """Airflow 3.0's Task SDK client sends JSON bodies without a ``Content-Type`` header,
    which newer FastAPI releases reject (422 "Input should be a valid dictionary"): every
    task of a ``dag.test()`` run failed on ``PATCH /task-instances/{id}/run``. Airflow 3.1+
    sets the header itself; there the hook is left alone."""
    import inspect

    try:
        from airflow.sdk.api import client as sdk_client
    except ImportError:  # pragma: no cover - Task SDK missing
        return
    try:
        source = inspect.getsource(sdk_client.Client.request)
    except (OSError, TypeError, AttributeError):
        source = ""
    original = getattr(sdk_client, "add_correlation_id", None)
    if "content-type" in source.lower() or original is None:
        return

    def add_correlation_id(request: Any) -> None:
        original(request)
        try:
            body = request.content
        except Exception:  # a streamed body: leave it as it is
            return
        if body and "content-type" not in request.headers:
            request.headers["content-type"] = "application/json"

    sdk_client.add_correlation_id = add_correlation_id


def _keep_execution_api_running(timeout: float = 30.0) -> None:
    """Up to Airflow 3.2 the in-process execution API (what ``dag.test()`` talks to) starts
    its lifespan on a background loop without waiting for it, and the lifespan lives only as
    long as the ``InProcessExecutionAPI`` object, which the Task SDK drops right after taking
    its ``transport``. Once that object is collected the lifespan exits and recent ``svcs``
    releases remove the services registry from the app, so tasks failed with "'State'
    object has no attribute 'svcs_registry'" (always on 3.2, by timing on 3.0 / 3.1).
    Airflow 3.3+ waits for the start and ties the lifespan to the transport itself; there
    nothing is changed. Elsewhere the transport keeps its API object alive and is handed
    out once the registry exists."""
    import inspect

    try:
        from airflow.api_fastapi.execution_api.app import InProcessExecutionAPI
        from airflow.sdk.execution_time import supervisor
    except ImportError:  # pragma: no cover - API server code missing
        return
    original = getattr(supervisor, "in_process_api_server", None)
    try:
        source = inspect.getsource(InProcessExecutionAPI)
    except (OSError, TypeError):
        return
    if original is None or ".result()" in source or "start_lifespan" not in source:
        return

    def in_process_api_server() -> Any:
        api = original()
        api.transport  # noqa: B018 - starts the lifespan
        _KEEP_ALIVE.append(api)  # the lifespan's exit stack lives on the API object
        deadline = time.monotonic() + timeout
        while getattr(api.app.state, "svcs_registry", None) is None:
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
        return api

    supervisor.in_process_api_server = in_process_api_server


# In-process execution API objects of a db-mode run (see _keep_execution_api_running).
_KEEP_ALIVE: list[Any] = []


def run_dag_with_db(dag: Any, conf: dict[str, Any], bag: Any = None) -> dict[str, Any]:
    _json_content_type_for_execution_api()
    _keep_execution_api_running()
    if bag is not None:
        _serialize_to_db(bag, dag.dag_id)
    try:
        dr = dag.test(run_conf=conf or None)
    finally:
        # unwind the kept lifespans while the interpreter still runs (no shutdown noise)
        import gc

        _KEEP_ALIVE.clear()
        gc.collect()
    tasks: list[dict[str, Any]] = []
    try:
        tis = dr.get_task_instances()
    except Exception:
        tis = []
    for ti in tis:
        tasks.append(
            {
                "task_id": ti.task_id,
                "state": str(getattr(ti.state, "value", ti.state)),
                "duration": float(ti.duration or 0.0),
            }
        )
    state = str(getattr(dr.state, "value", dr.state))
    return {"state": state, "tasks": tasks, "mode": "db"}


def main(req_path: str, res_path: str, *, process_isolated: bool | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    global _REPR_LIMIT
    with open(req_path, encoding="utf-8") as fh:
        req = json.load(fh)
    _REPR_LIMIT = int(req.get("max_value_chars") or _REPR_LIMIT)
    started = time.perf_counter()
    result: dict[str, Any]
    try:
        import pendulum

        dag, error, bag = _load_dag_and_bag(req["source_file"], req["dag_id"])
        if dag is None:
            result = {"state": "import_error", "error": error}
        else:
            logical_date = (
                pendulum.parse(req["logical_date"])
                if req.get("logical_date")
                else pendulum.now("UTC").replace(microsecond=0)
            )
            conf = req.get("conf") or {}
            if req["kind"] == "task":
                if req["task_id"] not in dag.task_dict:
                    result = {
                        "state": "not_found",
                        "error": f"task {req['task_id']!r} not in DAG {req['dag_id']!r}",
                    }
                else:
                    task = dag.get_task(req["task_id"])
                    store = _store_from_mocks(req.get("mock_xcoms") or {})
                    result = run_task(dag, task, conf, store, logical_date)
            else:
                mode = req.get("mode", "memory")
                if mode in ("db", "auto"):
                    try:
                        result = run_dag_with_db(dag, conf, bag)
                    except Exception as exc:
                        if mode == "db":
                            raise
                        print(
                            f"dag.test() unavailable ({exc!r}); running in memory", file=sys.stderr
                        )
                        result = run_dag_in_memory(dag, conf, logical_date)
                else:
                    result = run_dag_in_memory(dag, conf, logical_date)
    except BaseException as exc:
        result = {"state": "failed", "error": "".join(traceback.format_exception(exc)[-30:])}
    result["total_duration"] = round(time.perf_counter() - started, 4)
    result["process_isolated"] = process_isolated
    sys.stdout.flush()
    sys.stderr.flush()
    with open(res_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, default=_repr)


def _write_error(res_path: str, error: str) -> None:
    with open(res_path, "w", encoding="utf-8") as fh:
        json.dump({"state": "error", "error": error}, fh)


def entry(argv: list[str]) -> int:  # pragma: no cover - subprocess
    """Command-line entry: sandbox (when given), then :func:`main`. Returns the exit code."""
    req_path, res_path = argv[0], argv[1]
    raw = json.loads(argv[2]) if len(argv) > 2 and argv[2] else None
    isolated: bool | None = None
    if raw is not None:
        isolated = bool(raw.get("inside_namespace"))
        from autoair.validation.sandbox import (
            SandboxError,
            WorkerSandbox,
            apply_limits,
            install_audit_hook,
            run_in_process_namespace,
        )

        cfg = WorkerSandbox.from_dict(raw)
        if cfg.process_isolation != "off" and not raw.get("inside_namespace"):
            inner = json.dumps({**raw, "inside_namespace": True})
            code = run_in_process_namespace(
                [sys.executable, "-m", "autoair.runner._child", req_path, res_path, inner],
                mask_root=cfg.scratch_root,
                keep_dir=cfg.scratch_dir,
                deny=cfg.deny_paths,
                read_only=cfg.read_only_paths,
            )
            if code is not None:
                return code
            if cfg.process_isolation == "required":
                _write_error(
                    res_path,
                    "process isolation is required (runner.process_isolation or "
                    "validator.run_process_isolation) but this kernel or container does not "
                    "allow unprivileged user and PID namespaces; the test did not run. Run "
                    "test runs on the validator service (validator.url), or set "
                    "runner.process_isolation=auto to run them with the audit hook only",
                )
                return 0
            print(
                "[autoair] process isolation is not available on this host (unprivileged "
                "user namespaces are off or restricted): the run is not hidden from other "
                "processes (not fully isolated; configure validator.url for isolated test "
                "runs)",
                file=sys.stderr,
                flush=True,
            )
        try:
            apply_limits(cfg)
        except SandboxError as exc:
            _write_error(res_path, f"{exc}; the test did not run")
            return 0
        if cfg.audit_hook:
            install_audit_hook(cfg)
    main(req_path, res_path, process_isolated=isolated)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(entry(sys.argv[1:]))
