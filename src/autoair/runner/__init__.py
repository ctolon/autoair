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
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

from autoair._subprocess import user_code_env

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


class TaskTestResult(BaseModel):
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


class DagTestResult(BaseModel):
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


def _run_child(
    request: dict[str, Any], source: str, timeout: float, max_log: int
) -> tuple[dict[str, Any] | None, str, float, bool]:
    """Returns ``(result | None, log, wall_seconds, timed_out)``."""
    with tempfile.TemporaryDirectory(prefix="autoair-run-") as tmp:
        tmp_path = Path(tmp)
        dag_file = tmp_path / "dags" / f"{request['dag_id']}.py"
        dag_file.parent.mkdir()
        dag_file.write_text(source, encoding="utf-8")
        req_file = tmp_path / "request.json"
        res_file = tmp_path / "result.json"
        log_file = tmp_path / "output.log"
        req_file.write_text(json.dumps({**request, "source_file": str(dag_file)}), "utf-8")
        env = user_code_env({"PYTHONUNBUFFERED": "1"})  # no AutoAir secrets for DAG code
        start = time.perf_counter()
        timed_out = False
        with log_file.open("wb") as log_fh:
            proc = subprocess.Popen(
                [sys.executable, "-m", "autoair.runner._child", str(req_file), str(res_file)],
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
) -> TaskTestResult:
    """Execute ``task_id`` of ``dag_id`` defined in ``source`` (no metadata DB needed).

    ``mock_xcoms`` maps upstream task ids to the value ``xcom_pull`` returns for them
    (their return value); use ``"<task_id>:<key>"`` for other XCom keys.
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
    result, log, wall, timed_out = _run_child(request, source, timeout, max_log)
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
    return TaskTestResult.model_validate(data)


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
) -> DagTestResult:
    """Run every task of ``dag_id`` (see module docs for ``mode``)."""
    request = {
        "kind": "dag",
        "dag_id": dag_id,
        "conf": conf or {},
        "mode": mode,
        "logical_date": logical_date,
        "max_value_chars": max_value,
    }
    result, log, wall, timed_out = _run_child(request, source, timeout, max_log)
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
    return DagTestResult(
        state=str(result.get("state", "error")),
        mode=result.get("mode"),
        duration=round(wall, 3),
        tasks=tasks,
        log=log,
        error=result.get("error"),
    )


__all__ = [
    "DagTaskOutcome",
    "DagTestMode",
    "DagTestResult",
    "TaskTestResult",
    "run_dag_test",
    "run_task_test",
]
