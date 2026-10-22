"""Test-run a generated task / DAG in an isolated subprocess.

:func:`run_task_test` executes one task **without the
Airflow metadata DB**: the generated file is imported with ``DagBag``, the task's
templates are rendered against a synthetic context (``logical_date``, ``ds``,
``params``, ``dag_run.conf``, a fake ``ti`` whose ``xcom_pull`` returns
``mock_xcoms``) and ``execute(context)`` is called the way the
Task SDK runner does. Mapped tasks run their first map index (``map_index=0``).

:func:`run_dag_test` runs all tasks in topological order in memory (XComs passed between
tasks, simplified trigger rules / branching), or uses ``dag.test()`` (``mode="db"``;
writes a DAG run to the metadata DB) — ``mode="auto"`` tries the DB first.

Everything runs in a fresh interpreter in its own process group with a hard timeout;
stdout/stderr and logging output are captured (truncated) into the result.

Where the code runs:

* with ``validator.url`` set (and ``validator.test_runs``), on the validator service
  (``POST /v1/run``, :func:`autoair.validation.remote.run_remote`): no DAG code runs on this
  host. When the validator cannot be asked, ``validator.fail_mode=local`` runs the test here;
  ``open`` and ``closed`` both fail it, because "open" means skipping a check, and a test run
  that is skipped has nothing to show. ``mode="db"`` always runs here (the validator has no
  metadata DB);
* else in a local child sandboxed with the ``run`` profile of
  :mod:`autoair.validation.sandbox`: its own PID + user + mount namespaces when the kernel
  allows (``runner.process_isolation``), ``PR_SET_NO_NEW_PRIVS``, no core dumps,
  non-dumpable and an audit hook that denies reading other processes' ``/proc`` entries and
  other runs' folders (``runner.audit_hook``). Unlike import checks, subprocesses, network
  and writes stay allowed: task code needs them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar, get_args

from pydantic import BaseModel, Field, PrivateAttr, computed_field

from autoair._subprocess import user_code_env

if TYPE_CHECKING:
    from autoair.sections import RunnerSettings, ValidatorSettings
    from autoair.validation.sandbox import WorkerSandbox

log = logging.getLogger(__name__)

TaskState = Literal[
    "success",
    "failed",
    "skipped",
    "upstream_failed",
    "deferred",
    "timeout",
    "import_error",
    "not_found",
    "error",
]
DagTestMode = Literal["memory", "db", "auto"]
DEFAULT_MAX_LOG = 64_000  # default of AUTOAIR_RUNNER__MAX_LOG_CHARS
DEFAULT_MAX_VALUE = 4_000  # default of AUTOAIR_RUNNER__MAX_VALUE_CHARS


class _WhereRan(BaseModel):
    # where the code ran: local, validator, or none (audit records, the result's ``where``)
    _ran_on: str = PrivateAttr(default="local")
    # the child ran in its own PID namespace (None: not known)
    _isolated: bool | None = PrivateAttr(default=None)

    @computed_field(  # type: ignore[prop-decorator]
        description="Where the code ran: local (this server), validator (the validator "
        "service) or none (it did not run)."
    )
    @property
    def where(self) -> str:
        return self._ran_on

    @computed_field(  # type: ignore[prop-decorator]
        description="Whether the run had its own PID namespace (runner.process_isolation); "
        "false: it ran with the audit hook only and could see other processes (not fully "
        "isolated), null: not known."
    )
    @property
    def process_isolated(self) -> bool | None:
        return self._isolated


class TaskTestResult(_WhereRan):
    task_id: str | None = None
    state: TaskState
    duration: float = Field(default=0.0, description="Seconds spent executing the task.")
    log: str = Field(default="", description="Captured stdout/stderr/logging (truncated).")
    return_value_repr: str | None = None
    return_value: Any = Field(default=None, description="Return value when JSON-serialisable.")
    error: str | None = None
    mapped: bool = Field(default=False, description="Mapped task; ran map_index 0 only.")
    skipped_downstream: list[str] = []
    xcom_pushes: dict[str, str] = Field(default_factory=dict)


class DagTaskOutcome(BaseModel):
    task_id: str
    state: str
    duration: float = 0.0
    return_value_repr: str | None = None
    error: str | None = None


class DagTestResult(_WhereRan):
    state: str
    mode: Literal["memory", "db"] | None = None
    duration: float = 0.0
    tasks: list[DagTaskOutcome] = []
    log: str = ""
    error: str | None = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"… [{len(text) - limit} characters truncated] …\n" + text[-limit:]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(Exception):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(5)


# ``dag.test()`` (mode db/auto) writes a DAG run through Airflow's ORM inside the child, so
# the DAG code has a metadata DB session either way: it gets the DB configuration (and the
# Fernet key to read connections). Memory mode and task tests get neither. Never the real
# JWT secret, which would let DAG code mint Airflow API tokens: dag.test() talks to an
# in-process execution API, so a random per-run secret does.
_DB_MODE_ENV = ("AIRFLOW__DATABASE__", "AIRFLOW__CORE__FERNET_KEY")


def runs_root() -> str:
    """The folder every local test run's scratch folder lives in (``autoair-runs-<uid>``
    in the temporary directory): the run profile of the audit hook denies a run reading the
    folders of the others. A private ``mkdtemp`` folder when that path is not a directory
    owned by this user (another user created it first)."""
    base = os.path.join(tempfile.gettempdir(), f"autoair-runs-{os.getuid()}")
    try:
        os.mkdir(base, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return tempfile.mkdtemp(prefix="autoair-runs-")
    try:
        info = os.lstat(base)
    except OSError:
        return tempfile.mkdtemp(prefix="autoair-runs-")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return tempfile.mkdtemp(prefix="autoair-runs-")
    return base


# Files in AIRFLOW_HOME holding Airflow's secrets (besides airflow.cfg / AIRFLOW_CONFIG).
_AIRFLOW_SECRET_FILES = (
    "airflow.db",
    "webserver_config.py",
    "simple_auth_manager_passwords.json.generated",
    "unittests.cfg",
)
# Option names whose values are secrets or fetch one (Airflow's _cmd / _secret indirection).
_SECRET_OPTION = re.compile(r"(_cmd|_secret|password|passwd|fernet_key|secret_key|_conn)$", re.I)


def _airflow_home() -> str:
    return os.environ.get("AIRFLOW_HOME") or os.path.expanduser("~/airflow")


def _airflow_config_file() -> str:
    return os.environ.get("AIRFLOW_CONFIG") or os.path.join(_airflow_home(), "airflow.cfg")


def _sensitive_options() -> set[tuple[str, str]]:
    try:
        from airflow.configuration import conf

        return {(sec.lower(), key.lower()) for sec, key in conf.sensitive_config_values}
    except Exception:  # Airflow missing or misconfigured: the name pattern still applies
        return set()


def sanitized_airflow_config(src: str, dst: str) -> None:
    """Copy ``src`` (an airflow.cfg) to ``dst`` without the options that hold secrets:
    Airflow's own ``sensitive_config_values`` (Fernet key, JWT and API secrets, the metadata
    DB URL, broker URLs...) and every option named like a secret or a ``_cmd`` / ``_secret``
    indirection. The rest of the configuration (time zone, provider settings) still applies
    to the test run. An unreadable or invalid ``src`` gives an empty file."""
    import configparser

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    with contextlib.suppress(OSError, configparser.Error, UnicodeDecodeError):
        parser.read(src, encoding="utf-8")
    sensitive = _sensitive_options()
    for section in parser.sections():
        for key in list(parser[section]):
            if (section.lower(), key.lower()) in sensitive or _SECRET_OPTION.search(key):
                parser.remove_option(section, key)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        parser.write(fh)


def _airflow_dag_folders() -> list[str]:
    folders = [os.path.join(_airflow_home(), "dags"), os.path.join(_airflow_home(), "plugins")]
    for env in ("AIRFLOW__CORE__DAGS_FOLDER", "AIRFLOW__CORE__PLUGINS_FOLDER"):
        if os.environ.get(env):
            folders.append(os.environ[env])
    with contextlib.suppress(Exception):  # Airflow missing or misconfigured
        from airflow.configuration import conf

        folders += [conf.get("core", "dags_folder"), conf.get("core", "plugins_folder")]
    return folders


def _airflow_folder_env() -> dict[str, str]:
    """Airflow's DAG and plugins folders of this process as ``AIRFLOW__CORE__*`` variables:
    a child with a private ``AIRFLOW_HOME`` would otherwise derive them from that folder
    when airflow.cfg does not name them, and DAG files that import helper modules from the
    DAG folder (on ``sys.path`` in Airflow) would stop importing."""
    env: dict[str, str] = {}
    with contextlib.suppress(Exception):  # Airflow missing or misconfigured
        from airflow.configuration import conf

        for option, name in (
            ("dags_folder", "AIRFLOW__CORE__DAGS_FOLDER"),
            ("plugins_folder", "AIRFLOW__CORE__PLUGINS_FOLDER"),
        ):
            value = os.environ.get(name) or conf.get("core", option, fallback="")
            if value:
                env[name] = os.fspath(value)
    return env


def private_airflow_home(folder: str | os.PathLike[str]) -> dict[str, str]:
    """Create ``<folder>/airflow-home`` holding a copy of airflow.cfg without its secrets
    (:func:`sanitized_airflow_config`); returns the variables that point a child at it:
    the default SQLite metadata DB and the secret files there are not the real ones, while
    Airflow's DAG and plugins folders stay where they are."""
    home = Path(folder) / "airflow-home"
    home.mkdir(exist_ok=True)
    sanitized_airflow_config(_airflow_config_file(), str(home / "airflow.cfg"))
    return {
        **_airflow_folder_env(),
        "AIRFLOW_HOME": str(home),
        "AIRFLOW_CONFIG": str(home / "airflow.cfg"),
    }


def local_check_sandbox(
    cfg: RunnerSettings | None = None,
    *,
    read_only: tuple[str | os.PathLike[str], ...] = (),
) -> WorkerSandbox:
    """The sandbox of the local warm DagBag pool (import checks without a validator): the
    protections of :func:`local_sandbox` that cannot break parsing a DAG file, i.e. the run
    profile of the audit hook when ``runner.audit_hook`` is on (no reading other processes'
    ``/proc/<pid>`` entries, other workers' and runs' scratch folders or Airflow's secret
    files; no writes into the DAG folders), non-dumpable, and a private ``AIRFLOW_HOME``
    with a secret-free copy of airflow.cfg. No PID namespace: pool workers are long-lived
    and talk to the server over an inherited socket."""
    return replace(local_sandbox(cfg, read_only=read_only), process_isolation="off")


def local_sandbox(
    cfg: RunnerSettings | None = None,
    *,
    read_only: tuple[str | os.PathLike[str], ...] = (),
) -> WorkerSandbox:
    """The sandbox of a local test-run child (``runner.*``): run profile, no resource limits
    besides no core dumps, network allowed; Airflow's secret files denied and a copy of
    airflow.cfg without secrets; the DAG folders (Airflow's and ``read_only``, e.g. AutoAir's
    output folder) read-only; ``runner.process_isolation`` (``auto`` by default)."""
    from autoair.sections import RunnerSettings, active
    from autoair.validation.sandbox import WorkerSandbox

    cfg = cfg or active(RunnerSettings)
    isolation = cfg.process_isolation or "auto"
    home = _airflow_home()
    deny = [_airflow_config_file(), *(os.path.join(home, f) for f in _AIRFLOW_SECRET_FILES)]
    folders = [*_airflow_dag_folders(), *(os.fspath(p) for p in read_only)]
    return WorkerSandbox(
        profile="run",
        audit_hook=cfg.audit_hook,
        network_isolation="off",
        process_isolation=isolation,
        scratch_root=runs_root(),
        sanitize_airflow_config=True,
        deny_paths=tuple(sorted({os.path.realpath(p) for p in deny if os.path.isfile(p)})),
        read_only_paths=tuple(sorted({os.path.realpath(p) for p in folders if os.path.isdir(p)})),
    )


def _run_child(
    request: dict[str, Any],
    source: str,
    timeout: float,
    max_log: int,
    *,
    with_db: bool = False,
    sandbox: WorkerSandbox | None = None,
    env_base: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str, float, bool]:
    """Returns ``(result | None, log, wall_seconds, timed_out)``. ``sandbox``: the run
    profile the child applies (default :func:`local_sandbox`); ``env_base``: the environment
    the scrubbed child environment is derived from (default ``os.environ``)."""
    sandbox = sandbox or local_sandbox()
    with tempfile.TemporaryDirectory(prefix="run-", dir=sandbox.scratch_root or None) as tmp:
        tmp_path = Path(tmp)
        dag_file = tmp_path / "dags" / f"{request['dag_id']}.py"
        dag_file.parent.mkdir()
        dag_file.write_text(source, encoding="utf-8")
        req_file = tmp_path / "request.json"
        res_file = tmp_path / "result.json"
        log_file = tmp_path / "output.log"
        req_file.write_text(json.dumps({**request, "source_file": str(dag_file)}), "utf-8")
        # No AutoAir, Airflow or cloud secrets for DAG code (DB settings only for dag.test()).
        extra = {"PYTHONUNBUFFERED": "1", "TMPDIR": tmp}
        if sandbox.private_home:
            # the validator: a run must not plant files (airflow.cfg, user site-packages)
            # that later runs would load
            home = tmp_path / "home"
            (home / "airflow").mkdir(parents=True)
            extra.update(
                HOME=str(home),
                AIRFLOW_HOME=str(home / "airflow"),
                PYTHONNOUSERSITE="1",
                PYTHONDONTWRITEBYTECODE="1",
            )
        if with_db:
            extra["AIRFLOW__API_AUTH__JWT_SECRET"] = secrets.token_urlsafe(32)
            # dag.test() gets the metadata DB by design (runner.allow_db_mode): the real
            # configuration, and the secret files stay readable
            sandbox = replace(sandbox, deny_paths=())
        elif sandbox.sanitize_airflow_config:
            # Airflow reads its secrets from airflow.cfg as well as from the environment: the
            # run gets a private AIRFLOW_HOME (so the default SQLite DB is not the real one)
            # and a copy of the configuration without them
            extra.update(private_airflow_home(tmp_path))
        elif sandbox.private_home:
            extra["AIRFLOW_CONFIG"] = os.path.join(extra["AIRFLOW_HOME"], "airflow.cfg")
        base = os.environ if env_base is None else env_base
        keep = (
            frozenset(k.upper() for k in base if k.upper().startswith(_DB_MODE_ENV))
            if with_db
            else frozenset()
        )
        env = user_code_env(extra, base=base, keep=keep)
        box = json.dumps(replace(sandbox, scratch_dir=tmp).to_dict())
        start = time.perf_counter()
        timed_out = False
        with log_file.open("wb") as log_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "autoair.runner._child",
                    str(req_file),
                    str(res_file),
                    box,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=tmp,
                env=env,
                start_new_session=True,  # own process group: kill bash children too
            )
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
        wall = time.perf_counter() - start
        log = _truncate(log_file.read_text("utf-8", errors="replace"), max_log)
        result: dict[str, Any] | None = None
        if res_file.exists():
            with contextlib.suppress(ValueError, OSError):
                result = json.loads(res_file.read_text("utf-8"))
        if result is None and not timed_out and proc.returncode not in (0, None):
            log += f"\n[runner exited with code {proc.returncode}]"
        return result, log, wall, timed_out


_R = TypeVar("_R", TaskTestResult, DagTestResult)


def _validator(remote: bool | None, cfg: ValidatorSettings | None) -> ValidatorSettings | None:
    """The validator settings when this run goes to the validator service, else ``None``."""
    if remote is False:
        return None
    from autoair.sections import ValidatorSettings, active

    cfg = cfg or active(ValidatorSettings)
    if not cfg.runs_url or not cfg.test_runs:
        return None
    return cfg


def _on_validator(
    model: type[_R], payload: dict[str, Any], timeout: float, cfg: ValidatorSettings
) -> _R | None:
    """The validator's result, or ``None`` when it is down and ``fail_mode`` is ``local``."""
    from autoair.validation.remote import ValidatorUnavailable, run_remote

    try:
        data = run_remote({**payload, "timeout": timeout}, timeout, cfg)
    except ValidatorUnavailable as exc:
        # never a second, local run of a test that may already have run on the validator
        if cfg.fail_mode == "local" and not exc.may_have_run:
            log.warning("validator unavailable (%s); the test run runs locally", exc)
            return None
        outcome = (
            "it may have run there; it is not started again"
            if exc.may_have_run
            else "the test did not run"
        )
        failed = model.model_validate(
            {
                "task_id": payload.get("task_id"),
                "state": "error",
                "error": f"the test-run service is unavailable ({exc}); {outcome} "
                f"(validator.fail_mode={cfg.fail_mode}: test runs never fall back to this "
                "host unless it is local)",
            }
        )
        failed._ran_on = "validator" if exc.may_have_run else "none"
        return failed
    fields = {k: v for k, v in data.items() if k in model.model_fields}
    if model is TaskTestResult:
        fields.setdefault("task_id", payload.get("task_id"))
        if fields.get("state") not in get_args(TaskState):
            fields["state"] = "error"
    else:
        fields.setdefault("state", "error")
    try:
        result = model.model_validate(fields)
    except ValueError:
        result = model.model_validate(
            {
                "task_id": payload.get("task_id"),
                "state": "error",
                "error": "the validator's test-run answer was invalid",
            }
        )
    result._ran_on = "validator"
    result._isolated = _isolated_flag(data)
    return result


def ran_on(result: TaskTestResult | DagTestResult) -> str:
    """Where the code of ``result`` ran: ``local``, ``validator`` or ``none``."""
    return result._ran_on


def process_isolated(result: TaskTestResult | DagTestResult) -> bool | None:
    """Whether a local run had its own PID namespace (``None`` when unknown)."""
    return result._isolated


def _isolated_flag(result: dict[str, Any]) -> bool | None:
    flag = result.get("process_isolated")
    return flag if isinstance(flag, bool) else None


# One warning per process about local runs without a PID namespace (log noise otherwise).
_warned_not_isolated = False


def _warn_if_not_isolated(isolated: bool | None) -> None:
    global _warned_not_isolated
    if isolated is False and not _warned_not_isolated:
        _warned_not_isolated = True
        log.warning(
            "local test runs are not fully isolated: this host does not allow unprivileged "
            "user and PID namespaces, so runs execute with the audit hook only and their "
            "subprocesses can see other processes; configure validator.url for isolated test "
            "runs (or runner.process_isolation=required to refuse such runs)"
        )


def run_task_test(
    source: str,
    dag_id: str,
    task_id: str,
    *,
    conf: dict[str, Any] | None = None,
    timeout: float = 120,
    mock_xcoms: dict[str, Any] | None = None,
    logical_date: str | None = None,
    max_log: int = DEFAULT_MAX_LOG,
    max_value: int = DEFAULT_MAX_VALUE,
    remote: bool | None = None,
    validator: ValidatorSettings | None = None,
    sandbox: WorkerSandbox | None = None,
    env_base: Mapping[str, str] | None = None,
) -> TaskTestResult:
    """Execute ``task_id`` of ``dag_id`` defined in ``source`` (no metadata DB needed).

    ``mock_xcoms`` maps upstream task ids to the value ``xcom_pull`` returns for them
    (their return value); use ``"<task_id>:<key>"`` for other XCom keys. ``remote``:
    ``None`` asks the validator service when ``validator.url`` is set (module docs),
    ``False`` runs here; ``sandbox`` / ``env_base`` as :func:`_run_child`.
    """
    request = {
        "kind": "task",
        "dag_id": dag_id,
        "task_id": task_id,
        "conf": conf or {},
        "mock_xcoms": mock_xcoms or {},
        "logical_date": logical_date,
        "max_value_chars": max_value,
    }
    remote_cfg = _validator(remote, validator)
    if remote_cfg is not None:
        answer = _on_validator(
            TaskTestResult,
            {**request, "source": source, "max_log_chars": max_log},
            timeout,
            remote_cfg,
        )
        if answer is not None:
            return answer
    result, log, wall, timed_out = _run_child(
        request, source, timeout, max_log, sandbox=sandbox, env_base=env_base
    )
    if timed_out:
        return TaskTestResult(
            task_id=task_id,
            state="timeout",
            duration=round(wall, 3),
            log=log,
            error=f"task did not finish within {timeout}s",
        )
    if result is None:
        return TaskTestResult(
            task_id=task_id,
            state="error",
            duration=round(wall, 3),
            log=log,
            error="test runner crashed (see log)",
        )
    data = {k: v for k, v in result.items() if k in TaskTestResult.model_fields}
    data.setdefault("task_id", task_id)
    data["log"] = log
    if data.get("state") not in get_args(TaskState):
        data["state"] = "error"
    task = TaskTestResult.model_validate(data)
    task._isolated = _isolated_flag(result)
    _warn_if_not_isolated(task._isolated)
    return task


def run_dag_test(
    source: str,
    dag_id: str,
    *,
    conf: dict[str, Any] | None = None,
    timeout: float = 300,
    mode: DagTestMode = "memory",
    logical_date: str | None = None,
    max_log: int = DEFAULT_MAX_LOG,
    max_value: int = DEFAULT_MAX_VALUE,
    remote: bool | None = None,
    validator: ValidatorSettings | None = None,
    sandbox: WorkerSandbox | None = None,
    env_base: Mapping[str, str] | None = None,
) -> DagTestResult:
    """Run every task of ``dag_id`` (see module docs for ``mode`` and ``remote``). Callers
    decide whether ``db`` / ``auto`` are allowed (``runner.allow_db_mode``, the API route);
    both run here, never on the validator."""
    request = {
        "kind": "dag",
        "dag_id": dag_id,
        "conf": conf or {},
        "mode": mode,
        "logical_date": logical_date,
        "max_value_chars": max_value,
    }
    remote_cfg = _validator(remote, validator) if mode == "memory" else None
    if remote_cfg is not None:
        answer = _on_validator(
            DagTestResult,
            {**request, "source": source, "max_log_chars": max_log},
            timeout,
            remote_cfg,
        )
        if answer is not None:
            return answer
    result, log, wall, timed_out = _run_child(
        request,
        source,
        timeout,
        max_log,
        with_db=mode in ("db", "auto"),
        sandbox=sandbox,
        env_base=env_base,
    )
    if timed_out:
        return DagTestResult(
            state="timeout",
            duration=round(wall, 3),
            log=log,
            error=f"DAG run did not finish within {timeout}s",
        )
    if result is None:
        return DagTestResult(
            state="error", duration=round(wall, 3), log=log, error="test runner crashed"
        )
    tasks = [
        DagTaskOutcome.model_validate(
            {k: v for k, v in t.items() if k in DagTaskOutcome.model_fields}
        )
        for t in result.get("tasks", [])
    ]
    dag = DagTestResult(
        state=str(result.get("state", "error")),
        mode=result.get("mode"),
        duration=round(wall, 3),
        tasks=tasks,
        log=log,
        error=result.get("error"),
    )
    dag._isolated = _isolated_flag(result)
    _warn_if_not_isolated(dag._isolated)
    return dag


__all__ = [
    "DagTaskOutcome",
    "DagTestMode",
    "DagTestResult",
    "TaskTestResult",
    "local_check_sandbox",
    "local_sandbox",
    "private_airflow_home",
    "process_isolated",
    "ran_on",
    "run_dag_test",
    "run_task_test",
    "runs_root",
]
