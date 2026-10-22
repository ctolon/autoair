"""A small pool of warm, long-lived DagBag worker processes.

Importing Airflow costs ~1-2 s per interpreter, which dominated the one-shot
subprocess import check. Each worker here pre-imports ``airflow.sdk`` and the
``DagBag`` class once, then serves checks sent over a pipe:

* per-check timeout — a worker that does not answer in time is killed and replaced;
* crash isolation — a dead worker is replaced and the check reported as failed;
* recycling — a worker is replaced after ``max_tasks_per_child`` checks (DAG modules
  leak into ``sys.modules``; user code may mutate global state);
* lazy start, thread-safe :meth:`DagBagPool.submit`, :meth:`DagBagPool.shutdown`;
* optional :class:`~autoair.validation.sandbox.WorkerSandbox` limits: the validator
  service's (resource limits, network namespace, the check profile of the audit hook, a
  private scratch folder and HOME), or those of the process's own pool
  (:func:`local_pool_sandbox`: the run profile of the audit hook, non-dumpable, a private
  AIRFLOW_HOME with a secret-free airflow.cfg).

Workers are fresh interpreters started with :mod:`subprocess` (never ``fork`` a
threaded server process) in their own process group, and talk to the parent through a
:class:`multiprocessing.connection.Connection` over a socket pair. Unlike
``multiprocessing``'s ``spawn``/``forkserver`` this does not re-import the parent's
``__main__`` (uvicorn, pytest, ``python -``), which is both slow and fragile.

Messages are JSON, never pickles: the worker runs user code, which can write to its end
of the pipe, and unpickling what it wrote would execute code in the parent.
"""

from __future__ import annotations

import atexit
import contextlib
import inspect
import json
import os
import queue
import secrets
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from multiprocessing.connection import Connection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autoair._subprocess import user_code_env

if TYPE_CHECKING:
    from autoair.validation.dagbag_check import ImportCheckResult
    from autoair.validation.sandbox import WorkerSandbox

DEFAULT_SIZE = 2
DEFAULT_MAX_TASKS = 50
DEFAULT_START_TIMEOUT = 120.0
# Largest reply accepted from a worker (dag ids, task ids, import error tracebacks).
MAX_REPLY_BYTES = 16 * 1024 * 1024


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


def _send(conn: Connection, message: dict[str, Any]) -> None:
    conn.send_bytes(json.dumps(message, default=str).encode())


_WARM_DAG = (
    'from airflow.sdk import DAG\n\nwith DAG("autoair_pool_warmup", schedule=None):\n    pass\n'
)


def _warm_parse(cls: Any) -> None:  # pragma: no cover - subprocess
    folder = tempfile.mkdtemp(prefix="warm-")
    path = os.path.join(folder, "autoair_pool_warmup.py")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_WARM_DAG)
        kwargs: dict[str, Any] = {"dag_folder": path}
        if "include_examples" in inspect.signature(cls.__init__).parameters:
            kwargs["include_examples"] = False
        cls(**kwargs)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _worker_entry(fd: int, sandbox: str | None = None) -> None:  # pragma: no cover - subprocess
    _worker_main(Connection(fd), json.loads(sandbox) if sandbox else None)


def _worker_main(  # pragma: no cover - subprocess
    conn: Connection, sandbox: dict[str, Any] | None = None
) -> None:
    import logging

    logging.disable(logging.WARNING)
    cfg: WorkerSandbox | None = None
    report: dict[str, Any] = {}
    try:
        if sandbox is not None:
            from autoair.validation.sandbox import WorkerSandbox, apply_limits

            cfg = WorkerSandbox.from_dict(sandbox)
            if cfg.scratch_dir:
                tempfile.tempdir = cfg.scratch_dir
            report = apply_limits(cfg)
        import airflow.sdk  # noqa: F401 - warm import

        from autoair._compat import dagbag_class

        dagbag_class()
        if cfg is not None and cfg.audit_hook:
            # Parse one trivial DAG before the guard goes in: DagBag imports most of its
            # machinery lazily on the first file (Airflow 3.0: airflow.models.dag, croniter's
            # platform.architecture() subprocess, dill's ctypes lookups), which the guard
            # would otherwise refuse and DagBag would swallow as "no DAG found".
            _warm_parse(dagbag_class())
            from autoair.validation.sandbox import install_audit_hook

            install_audit_hook(cfg)
            report["audit_hook"] = True
    except Exception as exc:
        _send(conn, {"tag": "ready", "error": f"{type(exc).__name__}: {exc}", "sandbox": report})
    else:
        _send(conn, {"tag": "ready", "error": None, "sandbox": report})
    baseline = set(sys.modules)
    while True:
        try:
            raw = conn.recv_bytes()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        kind = msg.get("kind") if isinstance(msg, dict) else None
        if kind not in ("source", "file"):
            return
        try:
            if kind == "source":
                with tempfile.TemporaryDirectory(prefix="autoair-check-") as tmp:
                    # a bare file name: never a path out of the temporary folder
                    name = os.path.basename(str(msg["filename"])) or "dag.py"
                    path = Path(tmp) / name
                    path.write_text(str(msg["source"]), encoding="utf-8")
                    result: dict[str, Any] = bag_summary(str(path))
            else:
                result = bag_summary(str(msg["path"]))
            reply: dict[str, Any] = {"tag": "ok", "result": result}
        except BaseException as exc:
            reply = {"tag": "error", "error": f"{type(exc).__name__}: {exc}"}
        reply["nonce"] = msg.get("nonce")
        # drop DAG modules imported by this check (DagBag names them unusual_prefix_*)
        for name in [m for m in sys.modules if m not in baseline]:
            if name.startswith("unusual_prefix_") or "autoair-check-" in str(
                getattr(sys.modules.get(name), "__file__", "") or ""
            ):
                sys.modules.pop(name, None)
        try:
            _send(conn, reply)
        except (OSError, BrokenPipeError):
            return


# --------------------------------------------------------------------------- parent side


def _read_exact(fd: int, size: int, deadline: float) -> bytes:
    """``size`` bytes from ``fd`` before ``deadline`` (monotonic); ``TimeoutError`` when
    they do not arrive in time, ``EOFError`` when the worker closed its end."""
    data = bytearray()
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while len(data) < size:
            left = deadline - time.monotonic()
            if left <= 0 or not sel.select(left):
                raise TimeoutError("no complete reply from the DagBag worker in time")
            chunk = os.read(fd, min(size - len(data), 1 << 20))
            if not chunk:
                raise EOFError("the DagBag worker closed the connection")
            data += chunk
    return bytes(data)


def _recv_frame(conn: Connection, deadline: float) -> bytes:
    """One message in :class:`~multiprocessing.connection.Connection`'s framing (a 4-byte
    big-endian length, ``-1`` followed by an 8-byte length for large messages), read with
    a deadline over the whole frame: user code in the worker can write a length header
    and stop, and ``Connection.recv_bytes`` would then block forever."""
    fd = conn.fileno()
    (size,) = struct.unpack("!i", _read_exact(fd, 4, deadline))
    if size == -1:
        (size,) = struct.unpack("!Q", _read_exact(fd, 8, deadline))
    if size < 0 or size > MAX_REPLY_BYTES:
        raise OSError("reply from the DagBag worker is too long")
    return _read_exact(fd, size, deadline)


def _recv(conn: Connection, deadline: float) -> dict[str, Any]:
    """One JSON message of a worker; ``OSError`` for anything else (too long, not JSON),
    ``TimeoutError`` when it is not complete by ``deadline`` (monotonic)."""
    raw = _recv_frame(conn, deadline)
    try:
        msg = json.loads(raw)
    except ValueError as exc:
        raise OSError("invalid reply from the DagBag worker") from exc
    if not isinstance(msg, dict):
        raise OSError("invalid reply from the DagBag worker")
    return msg


def _exit_reason(code: int | None) -> str:
    """Why a worker died, for the import error shown to the user."""
    if code is None or code >= 0:
        return f"DagBag worker crashed (exit code {code})"
    sig = -code
    if sig == getattr(signal, "SIGXCPU", None):
        return "DagBag worker crashed: CPU time limit exceeded (validator.cpu_limit_seconds)"
    if sig == signal.SIGKILL:
        return "DagBag worker crashed: killed (SIGKILL, possibly out of memory)"
    try:
        name = signal.Signals(sig).name
    except ValueError:
        name = str(sig)
    return f"DagBag worker crashed (signal {name})"


class _Worker:
    def __init__(
        self,
        silence: bool,
        *,
        env: Mapping[str, str] | None = None,
        sandbox: WorkerSandbox | None = None,
    ) -> None:
        self.scratch: str | None = None
        extra: dict[str, str] = {}
        sandbox_arg: str | None = None
        if sandbox is not None:
            from dataclasses import replace

            self.scratch = tempfile.mkdtemp(prefix="w-", dir=sandbox.scratch_root or None)
            sandbox = replace(sandbox, scratch_dir=self.scratch)
            sandbox_arg = json.dumps(sandbox.to_dict())
            if sandbox.sanitize_airflow_config:
                # the local pool (no validator): Airflow's configuration without its secrets
                # in a private AIRFLOW_HOME; HOME and user site-packages stay, DAG files may
                # import from them
                from autoair.runner import private_airflow_home

                try:
                    extra = {"TMPDIR": self.scratch, **private_airflow_home(self.scratch)}
                except BaseException:
                    self._remove_scratch()
                    raise
            else:
                home = os.path.join(self.scratch, "home")
                os.makedirs(os.path.join(home, "airflow"), exist_ok=True)
                # Private homes: a check must not plant files (airflow.cfg, config modules,
                # user site-packages) that a later worker would load during its warm imports.
                extra = {
                    "TMPDIR": self.scratch,
                    "HOME": home,
                    "AIRFLOW_HOME": os.path.join(home, "airflow"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                }
        parent, child = socket.socketpair()
        fd = child.fileno()
        out = subprocess.DEVNULL if silence else None
        base = dict(env) if env is not None else user_code_env()  # no AutoAir secrets
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from autoair.validation.pool import _worker_entry; "
                    f"_worker_entry({fd}, {sandbox_arg!r})",
                ],
                pass_fds=(fd,),
                env={**base, **extra},
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
                # Own process group: a kill also reaches processes the DAG code started.
                start_new_session=True,
            )
        except BaseException:
            self._remove_scratch()
            raise
        finally:
            child.close()
        self.conn = Connection(parent.detach())
        self.ready = False
        self.startup_error: str | None = None
        self.sandbox_report: dict[str, Any] = {}
        self.tasks = 0

    def wait_ready(self, timeout: float) -> None:
        if self.ready:
            return
        deadline = time.monotonic() + timeout
        try:
            msg = _recv(self.conn, deadline)
        except TimeoutError:
            raise TimeoutError(f"DagBag worker did not start within {timeout}s") from None
        except EOFError as exc:
            raise EOFError(
                f"DagBag worker exited during start-up (exit code {self.process.poll()})"
            ) from exc
        self.ready = True
        err = msg.get("error")
        self.startup_error = str(err) if err else None
        report = msg.get("sandbox")
        self.sandbox_report = report if isinstance(report, dict) else {}

    def alive(self) -> bool:
        return self.process.poll() is None

    def exitcode(self) -> int | None:
        return self.process.poll()

    def _remove_scratch(self) -> None:
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None

    def kill(self) -> None:
        with contextlib.suppress(Exception):
            os.killpg(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            self.process.kill()
        with contextlib.suppress(Exception):
            self.process.wait(5)
        with contextlib.suppress(Exception):
            self.conn.close()
        self._remove_scratch()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()  # the worker sees EOF and exits
        with contextlib.suppress(Exception):
            self.process.wait(2)
        # Whatever the DAG code left running in the group goes with it.
        with contextlib.suppress(Exception):
            os.killpg(self.process.pid, signal.SIGKILL)
        if self.alive():
            self.kill()
        self._remove_scratch()


class DagBagPool:
    """Warm DagBag workers; ``submit`` is thread-safe and blocks while all are busy."""

    def __init__(
        self,
        size: int = DEFAULT_SIZE,
        *,
        max_tasks_per_child: int = DEFAULT_MAX_TASKS,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        silence_output: bool = True,
        env: Mapping[str, str] | None = None,
        sandbox: WorkerSandbox | None = None,
    ) -> None:
        if size < 1:
            raise ValueError("pool size must be >= 1")
        self.size = size
        self.max_tasks_per_child = max_tasks_per_child
        self.start_timeout = start_timeout
        self.silence_output = silence_output
        self.env = dict(env) if env is not None else None
        self._own_root: str | None = None
        if sandbox is not None and not sandbox.scratch_root:
            from dataclasses import replace

            self._own_root = tempfile.mkdtemp(prefix="autoair-validator-")
            sandbox = replace(sandbox, scratch_root=self._own_root)
        self.sandbox = sandbox
        self._slots: queue.Queue[_Worker | None] = queue.Queue()
        self._all: set[_Worker] = set()
        self._lock = threading.Lock()
        self._closed = False
        self.started = 0  # number of worker processes ever started (diagnostics)
        self.sandbox_report: dict[str, Any] = {}  # of the last worker that started
        for _ in range(size):
            self._slots.put(None)

    # ------------------------------------------------------------ lifecycle
    def _spawn(self) -> _Worker:
        w = _Worker(self.silence_output, env=self.env, sandbox=self.sandbox)
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
        if self._own_root:
            shutil.rmtree(self._own_root, ignore_errors=True)

    def __enter__(self) -> DagBagPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # ------------------------------------------------------------ checks
    def submit(
        self, source: str, filename: str = "dag.py", timeout: float = 60
    ) -> ImportCheckResult:
        """Import ``source`` (written as ``filename`` in a fresh temp dir) via DagBag."""
        return self._run({"kind": "source", "source": source, "filename": filename}, timeout)

    def submit_file(self, path: Path, timeout: float = 60) -> ImportCheckResult:
        return self._run({"kind": "file", "path": str(path)}, timeout)

    def _run(self, msg: dict[str, Any], timeout: float) -> ImportCheckResult:
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
            self.sandbox_report = w.sandbox_report
            if w.startup_error:
                err = w.startup_error
                self._discard(w, kill=True)
                w = None
                return ImportCheckResult(ok=False, import_errors=[f"DagBag worker: {err}"])
            deadline = time.monotonic() + timeout
            current = w
            # Binds the reply to this check: a worker reused for later checks may still run
            # code of an earlier one (a thread) that writes a reply of its own.
            nonce = secrets.token_hex(16)
            try:
                current.conn.send_bytes(json.dumps({**msg, "nonce": nonce}).encode())
                reply = _recv(current.conn, deadline)
            except TimeoutError:
                # also a reply that started but never finished: the worker is replaced
                self._discard(current, kill=True)
                w = None
                return ImportCheckResult(
                    ok=False, import_errors=[f"import timed out after {timeout}s"]
                )
            except (EOFError, OSError, BrokenPipeError):
                with contextlib.suppress(Exception):
                    current.process.wait(1)
                code = current.exitcode()
                self._discard(current, kill=True)
                w = None
                return ImportCheckResult(ok=False, import_errors=[_exit_reason(code)])
            if reply.get("nonce") != nonce:
                self._discard(current, kill=True)
                w = None
                return ImportCheckResult(
                    ok=False, import_errors=["invalid reply from the DagBag worker"]
                )
            w.tasks += 1
            if w.tasks >= self.max_tasks_per_child:
                self._discard(w, kill=False)
                w = None
            if reply.get("tag") != "ok":
                return ImportCheckResult(ok=False, import_errors=[str(reply.get("error"))])
            try:
                result = ImportCheckResult.model_validate(reply.get("result"))
            except ValueError:
                return ImportCheckResult(
                    ok=False, import_errors=["invalid reply from the DagBag worker"]
                )
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
    read_only: tuple[str, ...] | None = None,
) -> None:
    """Pool settings for this process (``AUTOAIR_DAGBAG_POOL*``, see Settings); they apply
    to the next pool started (call :func:`shutdown_pool` to restart a running one).
    ``read_only``: folders besides Airflow's DAG folders that checks may read but not write
    (AutoAir's output folder)."""
    for key, value in (
        ("enabled", enabled),
        ("size", size),
        ("max_tasks", max_tasks),
        ("start_timeout", start_timeout),
        ("read_only", read_only),
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


def local_pool_sandbox() -> WorkerSandbox:
    """The sandbox of this process's own pool (:func:`autoair.runner.local_check_sandbox`,
    read with the current ``runner.*`` settings)."""
    from autoair.runner import local_check_sandbox

    return local_check_sandbox(read_only=tuple(_config.get("read_only") or ()))


def get_pool() -> DagBagPool:
    """Process-wide pool (size / recycling from ``AUTOAIR_DAGBAG_POOL_SIZE`` /
    ``AUTOAIR_DAGBAG_POOL_MAX_TASKS`` or :func:`configure_pool`), its workers sandboxed with
    :func:`local_pool_sandbox`; shut down at interpreter exit."""
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
                    sandbox=local_pool_sandbox(),
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
    "local_pool_sandbox",
    "pool_enabled",
    "shutdown_pool",
]
