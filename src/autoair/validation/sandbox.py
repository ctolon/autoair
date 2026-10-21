"""Limits for the DagBag worker processes of the validator service.

A worker imports untrusted DAG code. The validator container (no egress, read-only root
file system, non-root user, no credentials) is the boundary; inside it each worker also

* gets resource limits (address space, CPU seconds, file size, open files, processes, no
  core dumps) and ``PR_SET_NO_NEW_PRIVS``;
* is made non-dumpable, so other workers cannot read its ``/proc`` entries (memory,
  environment) and see the DAG source of a concurrent check;
* moves into its own empty network namespace when the kernel allows unprivileged user
  namespaces (``network_isolation``);
* installs a Python audit hook (``audit_hook``) after the warm imports. Audit hooks can be
  bypassed by code that reaches native internals, so the hook is defence in depth, not the
  boundary.

Everything here runs in the worker process (:func:`autoair.validation.pool._worker_main`).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Any

# Linux constants (linux/sched.h, linux/prctl.h)
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_MIB = 1024 * 1024


@dataclass(frozen=True)
class WorkerSandbox:
    """What a validator worker applies to itself (JSON-encoded on its command line)."""

    memory_limit_mb: int = 0
    cpu_limit_seconds: int = 0
    file_size_limit_mb: int = 0
    open_files_limit: int = 0
    process_limit: int = 0
    audit_hook: bool = True
    network_isolation: str = "auto"
    scratch_root: str = ""  # every worker's scratch folder lives below it
    scratch_dir: str = ""  # this worker's own folder (TMPDIR)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerSandbox:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class SandboxError(RuntimeError):
    """A required isolation could not be applied (the worker does not start)."""


def _libc() -> Any:
    import ctypes

    return ctypes.CDLL(None, use_errno=True)


def _prctl(option: int, value: int) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return int(_libc().prctl(option, value, 0, 0, 0)) == 0
    except (OSError, AttributeError):
        return False


def make_non_dumpable() -> bool:
    """``PR_SET_DUMPABLE=0``: ``/proc/<pid>`` of this process becomes root-owned, so other
    processes of the same user cannot read its environment or memory (Linux)."""
    return _prctl(_PR_SET_DUMPABLE, 0)


def harden_server_process(name: str, *, enabled: bool = True) -> bool:
    """Make an AutoAir server process (``serve``, ``local``, ``mcp-serve --http``,
    ``ai-serve``) non-dumpable: DAG code it imports in child processes (round-trip imports
    of source-preserving deploys, previews, local import checks) runs as the same user and
    could otherwise read the server's environment and memory through ``/proc``. Never
    called inside the Airflow plugin: that would change Airflow's own api-server process
    (there, ``validator.url`` keeps user code out of it). The flag resets on ``exec``, so
    each uvicorn worker process calls this itself. ``enabled``: ``server.harden_process``
    (off: debugging with py-spy, gdb or core dumps)."""
    import logging

    if not enabled:
        logging.getLogger("autoair.validation.sandbox").warning(
            "the %s process stays dumpable (server.harden_process=false): child processes "
            "running DAG code can read its environment",
            name,
        )
        return False
    done = make_non_dumpable()
    if not done:
        logging.getLogger("autoair.validation.sandbox").info(
            "could not make the %s process non-dumpable (not Linux?)", name
        )
    return done


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="ascii") as fh:
        fh.write(text)


def isolate_network() -> bool:
    """Move this process into new user + network namespaces (only a loopback interface,
    down: every connection fails). ``False`` when the kernel or the container forbids it."""
    if not sys.platform.startswith("linux"):
        return False
    uid, gid = os.getuid(), os.getgid()
    try:
        libc = _libc()
        if libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNET) != 0:
            return False
    except (OSError, AttributeError):
        return False
    # Keep the same uid/gid inside the namespace (user names and file owners stay as they
    # were); without the map the process would appear as the overflow user.
    with contextlib.suppress(OSError):
        _write("/proc/self/setgroups", "deny")
        _write("/proc/self/gid_map", f"{gid} {gid} 1")
    with contextlib.suppress(OSError):
        _write("/proc/self/uid_map", f"{uid} {uid} 1")
    return True


def apply_limits(cfg: WorkerSandbox) -> dict[str, Any]:
    """Resource limits, no new privileges, non-dumpable, network namespace. Returns what
    was applied (reported to the service's /ready)."""
    report: dict[str, Any] = {"network_isolated": False, "limits": []}
    try:
        import resource
    except ImportError:  # pragma: no cover - not POSIX
        resource = None  # type: ignore[assignment]
    if resource is not None:
        limits = [
            ("RLIMIT_CORE", 0),
            ("RLIMIT_AS", cfg.memory_limit_mb * _MIB),
            ("RLIMIT_CPU", cfg.cpu_limit_seconds),
            ("RLIMIT_FSIZE", cfg.file_size_limit_mb * _MIB),
            ("RLIMIT_NOFILE", cfg.open_files_limit),
            ("RLIMIT_NPROC", cfg.process_limit),
        ]
        for name, value in limits:
            which = getattr(resource, name, None)
            if which is None or (value <= 0 and name != "RLIMIT_CORE"):
                continue
            _soft, hard = resource.getrlimit(which)
            if hard != resource.RLIM_INFINITY and hard < value:
                value = hard
            # CPU: SIGXCPU at the soft limit (reported as "CPU time limit exceeded"), the
            # kernel's SIGKILL one second later if the worker ignores it.
            new_hard = value + 1 if name == "RLIMIT_CPU" else value
            if hard != resource.RLIM_INFINITY and new_hard > hard:
                new_hard = hard
            try:
                resource.setrlimit(which, (value, new_hard))
            except (ValueError, OSError):
                continue
            report["limits"].append(name)
    if cfg.file_size_limit_mb > 0:
        import signal

        # A write over RLIMIT_FSIZE then fails with EFBIG (an import error) instead of
        # killing the worker.
        with contextlib.suppress(AttributeError, ValueError, OSError):
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    # The namespace first: writing /proc/self/uid_map needs a dumpable process.
    if cfg.network_isolation != "off":
        report["network_isolated"] = isolate_network()
        if not report["network_isolated"] and cfg.network_isolation == "required":
            raise SandboxError(
                "network isolation is required (validator.network_isolation) but this "
                "kernel or container does not allow unprivileged network namespaces"
            )
    _prctl(_PR_SET_NO_NEW_PRIVS, 1)
    make_non_dumpable()
    return report


# --------------------------------------------------------------------------- audit hook

_DENIED_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.kill",
        "os.killpg",
        "signal.pthread_kill",
        "pty.spawn",
        "ctypes.dlopen",
        "ctypes.dlsym",
        "ctypes.dlsym/handle",
        "resource.setrlimit",
        "resource.prlimit",
        "socket.connect",
        "socket.bind",
        "socket.sendto",
        "socket.sendmsg",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
        "webbrowser.open",
    }
)
# Events whose path arguments change the file system: allowed below the scratch folder only.
_WRITE_EVENTS = frozenset(
    {
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.mkdir",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "os.chflags",
        "os.setxattr",
        "os.removexattr",
        "shutil.rmtree",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
    }
)
# Events that read a path: denied for other workers' folders and other processes' /proc.
_READ_EVENTS = frozenset({"os.listdir", "os.scandir", "os.chdir", "os.getxattr", "os.listxattr"})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class _Guard:
    def __init__(self, cfg: WorkerSandbox) -> None:
        self.scratch = os.path.realpath(cfg.scratch_dir) if cfg.scratch_dir else ""
        self.root = os.path.realpath(cfg.scratch_root) if cfg.scratch_root else ""
        self.pid = str(os.getpid())
        self.local = threading.local()

    def _real(self, path: Any) -> str | None:
        if isinstance(path, int):
            return None  # an already open descriptor
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, (str, os.PathLike)):
            return None
        try:
            return os.path.realpath(os.fspath(path))
        except (OSError, ValueError, TypeError):
            return None

    def _inside(self, real: str, folder: str) -> bool:
        return bool(folder) and (real == folder or real.startswith(folder + os.sep))

    def _readable(self, real: str) -> bool:
        if real.startswith("/proc/"):
            head = real[len("/proc/") :].split("/", 1)[0]
            if head.isdigit() and head != self.pid:
                return False
        other_worker = self._inside(real, self.root) and not self._inside(real, self.scratch)
        return not other_worker

    def _writable(self, real: str) -> bool:
        return self._inside(real, self.scratch) or real in ("/dev/null", os.devnull)

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        if event in _DENIED_EVENTS:
            raise PermissionError(f"{event} is not allowed during an AutoAir import check")
        if event == "open":
            self._check_open(args)
        elif event in _WRITE_EVENTS:
            self._check_paths(event, args, write=True)
        elif event in _READ_EVENTS:
            self._check_paths(event, args, write=False)

    def _check_open(self, args: tuple[Any, ...]) -> None:
        if getattr(self.local, "busy", False) or not args:
            return
        self.local.busy = True
        try:
            path = args[0]
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
            )
            real = self._real(path)
            if real is None:
                return
            if write and not self._writable(real):
                raise PermissionError(
                    f"writing {os.fspath(path)!r} is not allowed during an AutoAir import check"
                )
            if not self._readable(real):
                raise PermissionError(
                    f"reading {os.fspath(path)!r} is not allowed during an AutoAir import check"
                )
        finally:
            self.local.busy = False

    def _check_paths(self, event: str, args: tuple[Any, ...], *, write: bool) -> None:
        if getattr(self.local, "busy", False):
            return
        self.local.busy = True
        try:
            for arg in args:
                if not isinstance(arg, (str, bytes, os.PathLike)):
                    continue
                real = self._real(arg)
                if real is None:
                    continue
                if (write and not self._writable(real)) or not self._readable(real):
                    raise PermissionError(f"{event} is not allowed during an AutoAir import check")
        finally:
            self.local.busy = False


def install_audit_hook(cfg: WorkerSandbox) -> None:
    """Install the guard for the rest of this process's life (audit hooks cannot be
    removed)."""
    sys.addaudithook(_Guard(cfg))


__all__ = [
    "SandboxError",
    "WorkerSandbox",
    "apply_limits",
    "harden_server_process",
    "install_audit_hook",
    "isolate_network",
    "make_non_dumpable",
]
