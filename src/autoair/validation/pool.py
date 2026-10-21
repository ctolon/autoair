"""A small pool of warm, long-lived DagBag worker processes.

Importing Airflow costs ~1-2 s per interpreter, which dominated the one-shot
subprocess import check. Each worker here pre-imports ``airflow.sdk`` and the
``DagBag`` class once, then serves checks sent over a pipe:

* per-check timeout — a worker that does not answer in time is killed and replaced;
* crash isolation — a dead worker is replaced and the check reported as failed;
* recycling — a worker is replaced after ``max_tasks_per_child`` checks (DAG modules
  leak into ``sys.modules``; user code may mutate global state);
* lazy start, thread-safe :meth:`DagBagPool.submit`, :meth:`DagBagPool.shutdown`.

Workers are fresh interpreters started with :mod:`subprocess` (never ``fork`` a
threaded server process) and talk to the parent through a
:class:`multiprocessing.connection.Connection` over a socket pair. Unlike
``multiprocessing``'s ``spawn``/``forkserver`` this does not re-import the parent's
``__main__`` (uvicorn, pytest, ``python -``), which is both slow and fragile.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autoair._subprocess import user_code_env

if TYPE_CHECKING:
    from autoair.validation.dagbag_check import ImportCheckResult

DEFAULT_SIZE = 2
DEFAULT_MAX_TASKS = 50
DEFAULT_START_TIMEOUT = 120.0


# --------------------------------------------------------------------------- worker side


def bag_summary(dag_file: str) -> dict[str, Any]:
    """Import ``dag_file`` with Airflow's DagBag; JSON-able summary."""
    import inspect

    from autoair._compat import dagbag_class

    cls = dagbag_class()
    kwargs: dict[str, object] = {"dag_folder": dag_file}
    if "include_examples" in inspect.signature(cls.__init__).parameters:  # removed in 3.3
        kwargs["include_examples"] = False
    bag = cls(**kwargs)
    return {
        "dag_ids": sorted(bag.dags),
        "task_ids": {d: sorted(t.task_id for t in dag.tasks) for d, dag in bag.dags.items()},
        "import_errors": [str(e) for e in bag.import_errors.values()],
    }


def _worker_entry(fd: int) -> None:  # pragma: no cover - subprocess
    _worker_main(Connection(fd))


def _worker_main(conn: Connection) -> None:  # pragma: no cover - subprocess
    import logging

    logging.disable(logging.WARNING)
    try:
        import airflow.sdk  # noqa: F401 - warm import

        from autoair._compat import dagbag_class

        dagbag_class()
    except Exception as exc:
        conn.send(("ready", f"{type(exc).__name__}: {exc}"))
    else:
        conn.send(("ready", None))
    baseline = set(sys.modules)
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        if msg is None:
            return
        kind, payload = msg
        try:
            if kind == "source":
                source, filename = payload
                with tempfile.TemporaryDirectory(prefix="autoair-check-") as tmp:
                    path = Path(tmp) / filename
                    path.write_text(source, encoding="utf-8")
                    result: dict[str, Any] = bag_summary(str(path))
            else:
                result = bag_summary(str(payload))
            reply: tuple[str, Any] = ("ok", result)
        except BaseException as exc:
            reply = ("error", f"{type(exc).__name__}: {exc}")
        # drop DAG modules imported by this check (DagBag names them unusual_prefix_*)
        for name in [m for m in sys.modules if m not in baseline]:
            if name.startswith("unusual_prefix_") or "autoair-check-" in str(
                getattr(sys.modules.get(name), "__file__", "") or ""
            ):
                sys.modules.pop(name, None)
        try:
            conn.send(reply)
        except (OSError, BrokenPipeError):
            return


# --------------------------------------------------------------------------- parent side


class _Worker:
    def __init__(self, silence: bool) -> None:
        parent, child = socket.socketpair()
        fd = child.fileno()
        out = subprocess.DEVNULL if silence else None
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    f"from autoair.validation.pool import _worker_entry; _worker_entry({fd})",
                ],
                pass_fds=(fd,),
                env=user_code_env(),  # workers run DAG code: no AutoAir secrets
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
            )
        finally:
            child.close()
        self.conn = Connection(parent.detach())
        self.ready = False
        self.startup_error: str | None = None
        self.tasks = 0

    def wait_ready(self, timeout: float) -> None:
        if self.ready:
            return
        if not self.conn.poll(timeout):
            raise TimeoutError(f"DagBag worker did not start within {timeout}s")
        try:
            _tag, err = self.conn.recv()
        except EOFError as exc:
            raise EOFError(
                f"DagBag worker exited during start-up (exit code {self.process.poll()})"
            ) from exc
        self.ready = True
        self.startup_error = err

    def alive(self) -> bool:
        return self.process.poll() is None

    def exitcode(self) -> int | None:
        return self.process.poll()

    def kill(self) -> None:
        with contextlib.suppress(Exception):
            self.process.kill()
        with contextlib.suppress(Exception):
            self.process.wait(5)
        with contextlib.suppress(Exception):
            self.conn.close()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.send(None)
        with contextlib.suppress(Exception):
            self.process.wait(2)
        if self.alive():
            self.kill()
        with contextlib.suppress(Exception):
            self.conn.close()


class DagBagPool:
    """Warm DagBag workers; ``submit`` is thread-safe and blocks while all are busy."""

    def __init__(
        self,
        size: int = DEFAULT_SIZE,
        *,
        max_tasks_per_child: int = DEFAULT_MAX_TASKS,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        silence_output: bool = True,
    ) -> None:
        if size < 1:
            raise ValueError("pool size must be >= 1")
        self.size = size
        self.max_tasks_per_child = max_tasks_per_child
        self.start_timeout = start_timeout
        self.silence_output = silence_output
        self._slots: queue.Queue[_Worker | None] = queue.Queue()
        self._all: set[_Worker] = set()
        self._lock = threading.Lock()
        self._closed = False
        self.started = 0  # number of worker processes ever started (diagnostics)
        for _ in range(size):
            self._slots.put(None)

    # ------------------------------------------------------------ lifecycle
    def _spawn(self) -> _Worker:
        w = _Worker(self.silence_output)
        with self._lock:
            self._all.add(w)
            self.started += 1
        return w

    def _discard(self, w: _Worker | None, *, kill: bool) -> None:
        if w is None:
            return
        if kill:
            w.kill()
        else:
            w.stop()
        with self._lock:
            self._all.discard(w)

    def warmup(self) -> None:
        """Start all workers now (they import Airflow in the background)."""
        taken: list[_Worker | None] = []
        for _ in range(self.size):
            w = self._slots.get()
            if w is None or not w.alive():
                self._discard(w, kill=True)
                w = self._spawn()
            taken.append(w)
        for w in taken:
            self._slots.put(w)

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            workers = list(self._all)
            self._all.clear()
        for w in workers:
            w.stop()

    def __enter__(self) -> DagBagPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # ------------------------------------------------------------ checks
    def submit(
        self, source: str, filename: str = "dag.py", timeout: float = 60
    ) -> ImportCheckResult:
        """Import ``source`` (written as ``filename`` in a fresh temp dir) via DagBag."""
        return self._run(("source", (source, filename)), timeout)

    def submit_file(self, path: Path, timeout: float = 60) -> ImportCheckResult:
        return self._run(("file", str(path)), timeout)

    def _run(self, msg: tuple[str, Any], timeout: float) -> ImportCheckResult:
        from autoair.validation.dagbag_check import ImportCheckResult

        if self._closed:
            raise RuntimeError("DagBagPool is shut down")
        w = self._slots.get()
        replacement: _Worker | None = None
        try:
            if w is None or not w.alive():
                self._discard(w, kill=True)
                w = self._spawn()
            try:
                w.wait_ready(self.start_timeout)
            except (TimeoutError, EOFError, OSError) as exc:
                self._discard(w, kill=True)
                w = None
                return ImportCheckResult(ok=False, import_errors=[f"DagBag worker: {exc}"])
            if w.startup_error:
                err = w.startup_error
                self._discard(w, kill=True)
                w = None
                return ImportCheckResult(ok=False, import_errors=[f"DagBag worker: {err}"])
            deadline = time.monotonic() + timeout
            current = w
            try:
                current.conn.send(msg)
                if not current.conn.poll(max(0.0, deadline - time.monotonic())):
                    self._discard(current, kill=True)
                    w = None
                    return ImportCheckResult(
                        ok=False, import_errors=[f"import timed out after {timeout}s"]
                    )
                tag, payload = current.conn.recv()
            except (EOFError, OSError, BrokenPipeError):
                with contextlib.suppress(Exception):
                    current.process.wait(1)
                code = current.exitcode()
                self._discard(current, kill=True)
                w = None
                return ImportCheckResult(
                    ok=False, import_errors=[f"DagBag worker crashed (exit code {code})"]
                )
            w.tasks += 1
            if w.tasks >= self.max_tasks_per_child:
                self._discard(w, kill=False)
                w = None
            if tag != "ok":
                return ImportCheckResult(ok=False, import_errors=[str(payload)])
            result = ImportCheckResult.model_validate(payload)
            result.ok = not result.import_errors and bool(result.dag_ids)
            return result
        finally:
            if w is None and not self._closed:
                # start the replacement right away so it warms up in the background
                with contextlib.suppress(Exception):
                    replacement = self._spawn()
            self._slots.put(w if w is not None else replacement)


# --------------------------------------------------------------------------- singleton

_pool: DagBagPool | None = None
_pool_lock = threading.Lock()
# Set by :func:`configure_pool` (from Settings); the environment is the fallback.
_config: dict[str, Any] = {}


def configure_pool(
    *,
    enabled: bool | None = None,
    size: int | None = None,
    max_tasks: int | None = None,
    start_timeout: float | None = None,
) -> None:
    """Pool settings for this process (``AUTOAIR_DAGBAG_POOL*``, see Settings); they apply
    to the next pool started (call :func:`shutdown_pool` to restart a running one)."""
    for key, value in (
        ("enabled", enabled),
        ("size", size),
        ("max_tasks", max_tasks),
        ("start_timeout", start_timeout),
    ):
        if value is not None:
            _config[key] = value


def _setting(key: str, env: str, default: Any) -> Any:
    """The ``AUTOAIR_DAGBAG_*`` variable when set (Settings reads the same variable, so
    both agree; it also lets a process override the pool at runtime), else the value from
    :func:`configure_pool`, else ``default``."""
    raw = os.environ.get(env)
    if raw is not None and raw.strip():
        return raw.strip()
    return _config.get(key, default)


def _int_setting(key: str, env: str, default: int) -> int:
    try:
        return max(1, int(_setting(key, env, default)))
    except (TypeError, ValueError):
        return default


def _float_setting(key: str, env: str, default: float) -> float:
    try:
        value = float(_setting(key, env, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def pool_enabled() -> bool:
    value = _setting("enabled", "AUTOAIR_DAGBAG_POOL", True)
    if isinstance(value, str):
        return value.lower() not in {"0", "false", "no", "off"}
    return bool(value)


def get_pool() -> DagBagPool:
    """Process-wide pool (size / recycling from ``AUTOAIR_DAGBAG_POOL_SIZE`` /
    ``AUTOAIR_DAGBAG_POOL_MAX_TASKS`` or :func:`configure_pool`); shut down at
    interpreter exit."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = DagBagPool(
                    _int_setting("size", "AUTOAIR_DAGBAG_POOL_SIZE", DEFAULT_SIZE),
                    max_tasks_per_child=_int_setting(
                        "max_tasks", "AUTOAIR_DAGBAG_POOL_MAX_TASKS", DEFAULT_MAX_TASKS
                    ),
                    start_timeout=_float_setting(
                        "start_timeout",
                        "AUTOAIR_DAGBAG_POOL_START_TIMEOUT",
                        DEFAULT_START_TIMEOUT,
                    ),
                )
                atexit.register(shutdown_pool)
                # start every worker now (non-blocking) so none cold-starts later
                _pool.warmup()
    return _pool


def shutdown_pool() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown()


__all__ = [
    "DagBagPool",
    "bag_summary",
    "configure_pool",
    "get_pool",
    "pool_enabled",
    "shutdown_pool",
]
