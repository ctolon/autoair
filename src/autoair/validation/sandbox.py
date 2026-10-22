"""Limits for processes that run untrusted DAG code: the DagBag workers of the validator
service (profile ``check``) and test-run children (profile ``run``, local runner and the
validator's ``/v1/run``).

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

The two profiles differ in what the audit hook denies, because a test run executes task
code that legitimately starts processes and opens connections:

* ``check`` (an import check only parses the file): everything below;
* ``run`` (a task executes): only the read rules (other processes' ``/proc/<pid>/...``
  entries, other runs' scratch folders). No subprocess, socket, ctypes, signal or write
  denials: BashOperator, HTTP calls and temporary files must keep working. Resource limits,
  ``PR_SET_NO_NEW_PRIVS`` and non-dumpable apply to both. A run can also get a fresh PID +
  user + mount namespace with its own ``/proc`` (:func:`run_in_process_namespace`,
  ``process_isolation``), which hides every process outside the run, the server that
  started it included, from code the audit hook does not see (a ``cat`` in a BashOperator).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Any, Literal

# Linux constants (linux/sched.h, linux/prctl.h, linux/mount.h, linux/securebits.h)
_CLONE_NEWNS = 0x00020000
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWPID = 0x20000000
_CLONE_NEWNET = 0x40000000
_PR_SET_DUMPABLE = 4
_PR_SET_SECUREBITS = 28
_PR_SET_NO_NEW_PRIVS = 38
_SECBIT_NOROOT = 1 << 0
_SECBIT_NOROOT_LOCKED = 1 << 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_RDONLY = 1
_MS_REMOUNT = 32
_MS_BIND = 0x1000
_MS_REC = 0x4000
_MS_PRIVATE = 1 << 18
# statvfs flags equal to their MS_* mount flags (nosuid, nodev, noexec, noatime,
# nodiratime, relatime): kept on a read-only remount, which may not clear locked flags
_ST_LOCKED = 2 | 4 | 8 | 1024 | 2048 | 4096
_MIB = 1024 * 1024
# Exit code of the namespace helper when it could not set the namespaces up.
_NS_SETUP_FAILED = 86

SandboxProfile = Literal["check", "run"]


@dataclass(frozen=True)
class WorkerSandbox:
    """What a worker (``profile="check"``) or a test-run child (``profile="run"``) applies to
    itself (JSON-encoded on its command line)."""

    memory_limit_mb: int = 0
    cpu_limit_seconds: int = 0
    file_size_limit_mb: int = 0
    open_files_limit: int = 0
    process_limit: int = 0
    audit_hook: bool = True
    network_isolation: str = "auto"
    scratch_root: str = ""  # every worker's scratch folder lives below it
    scratch_dir: str = ""  # this worker's own folder (TMPDIR)
    profile: SandboxProfile = "check"  # which audit-hook rules apply (module docs)
    process_isolation: str = "off"  # runs: auto / required / off (run_in_process_namespace)
    private_home: bool = False  # runs: HOME / AIRFLOW_HOME below the run's scratch folder
    # runs on the AutoAir host: a copy of airflow.cfg without its secrets as AIRFLOW_CONFIG
    sanitize_airflow_config: bool = False
    # runs: files and folders that may be neither read nor written (airflow.cfg, airflow.db)
    deny_paths: tuple[str, ...] = ()
    # runs: folders that may be read but not written (the DAG folders Airflow parses)
    read_only_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerSandbox:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for name in ("deny_paths", "read_only_paths"):
            if name in known:
                known[name] = tuple(str(x) for x in known[name] or ())
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


def _write_strict(path: str, text: str) -> None:
    # The kernel reports a refused id map when the file is closed, not on write().
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, text.encode("ascii"))
    finally:
        os.close(fd)


def _mask_scratch(libc: Any, root: str, keep: str) -> bool:  # pragma: no cover - forked
    """Hide every folder below ``root`` but ``keep`` (an empty tmpfs over ``root``, ``keep``
    bound back): other runs' and check workers' folders are then out of reach even for a
    subprocess, which the audit hook does not see."""
    fd = os.open(keep, os.O_PATH | os.O_DIRECTORY)  # stays valid under the tmpfs
    try:
        if libc.mount(b"tmpfs", root.encode(), b"tmpfs", _MS_NOSUID | _MS_NODEV, b"mode=700") != 0:
            return False
        os.makedirs(keep, mode=0o700, exist_ok=True)
        source = f"/proc/self/fd/{fd}".encode()
        return int(libc.mount(source, keep.encode(), None, _MS_BIND | _MS_REC, None)) == 0
    finally:
        os.close(fd)


def _hide(libc: Any, path: str) -> bool:  # pragma: no cover - forked
    """``/dev/null`` over a file, an empty tmpfs over a folder."""
    if os.path.isdir(path):
        flags = _MS_NOSUID | _MS_NODEV
        return int(libc.mount(b"tmpfs", path.encode(), b"tmpfs", flags, b"mode=700")) == 0
    return int(libc.mount(b"/dev/null", path.encode(), None, _MS_BIND, None)) == 0


def _read_only(libc: Any, path: str) -> bool:  # pragma: no cover - forked
    """Bind ``path`` onto itself and remount it read-only, keeping the flags the kernel
    locks for a less privileged namespace (nosuid, nodev, noexec, atime)."""
    if libc.mount(path.encode(), path.encode(), None, _MS_BIND | _MS_REC, None) != 0:
        return False
    keep = os.statvfs(path).f_flag & _ST_LOCKED
    flags = _MS_REMOUNT | _MS_BIND | _MS_RDONLY | keep
    return int(libc.mount(None, path.encode(), None, flags, None)) == 0


def _namespace_child(  # pragma: no cover - forked
    argv: list[str],
    started_fd: int,
    mask_root: str = "",
    keep_dir: str = "",
    deny: tuple[str, ...] = (),
    read_only: tuple[str, ...] = (),
) -> None:
    """In the forked helper: new user + PID + mount namespaces, then fork the process that
    becomes PID 1 there, hide the other scratch folders below ``mask_root``, mount a fresh
    ``/proc`` and ``exec`` ``argv``. Never returns."""
    uid, gid = os.getuid(), os.getgid()
    try:
        libc = _libc()
        if libc.unshare(_CLONE_NEWUSER | _CLONE_NEWPID | _CLONE_NEWNS) != 0:
            os._exit(_NS_SETUP_FAILED)
        with contextlib.suppress(OSError):
            _write_strict("/proc/self/setgroups", "deny")
        _write_strict("/proc/self/gid_map", f"{gid} {gid} 1")
        _write_strict("/proc/self/uid_map", f"{uid} {uid} 1")
        pid = os.fork()
    except BaseException:
        os._exit(_NS_SETUP_FAILED)
    if pid == 0:
        try:
            # Private mounts: the new /proc must not propagate back to the host.
            if libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None) != 0:
                os._exit(_NS_SETUP_FAILED)
            if mask_root and keep_dir and not _mask_scratch(libc, mask_root, keep_dir):
                os._exit(_NS_SETUP_FAILED)
            for path in deny:
                if os.path.exists(path) and not _hide(libc, path):
                    os._exit(_NS_SETUP_FAILED)
            for path in read_only:
                # the audit hook still refuses Python writes there when this fails
                if os.path.isdir(path) and not _read_only(libc, path):
                    os.write(2, f"[autoair] could not make {path} read-only\n".encode())
            # A /proc of the new PID namespace: only this run's processes are listed.
            flags = _MS_NOSUID | _MS_NODEV | _MS_NOEXEC
            if libc.mount(b"proc", b"/proc", b"proc", flags, None) != 0:
                os._exit(_NS_SETUP_FAILED)
            # This process holds every capability of its user namespace until exec, enough
            # to unmount the new /proc again. exec drops them for a non-root uid; NOROOT
            # (locked) makes it drop them for uid 0 too.
            if not _prctl(_PR_SET_SECUREBITS, _SECBIT_NOROOT | _SECBIT_NOROOT_LOCKED):
                os._exit(_NS_SETUP_FAILED)
            os.write(started_fd, b"1")
            # argv is AutoAir's own child command line (sys.executable -m ...), no shell
            os.execv(argv[0], argv)  # noqa: S606 - started_fd is close-on-exec
        except BaseException as exc:
            # stderr is the run's log; the parent sees no "1" and reports the fallback
            with contextlib.suppress(OSError):
                os.write(2, f"[autoair] namespace set-up failed: {exc!r}\n".encode())
        os._exit(_NS_SETUP_FAILED)
    os.close(started_fd)
    try:
        _done, status = os.waitpid(pid, 0)
    except BaseException:
        os._exit(_NS_SETUP_FAILED)
    code = os.waitstatus_to_exitcode(status)
    if code < 0:  # pass a kill on (SIGKILL by the timeout, SIGXCPU...)
        import signal

        with contextlib.suppress(OSError, ValueError):
            signal.signal(-code, signal.SIG_DFL)
            os.kill(os.getpid(), -code)
        os._exit(128 - code)
    os._exit(code)


def run_in_process_namespace(
    argv: list[str],
    *,
    mask_root: str = "",
    keep_dir: str = "",
    deny: tuple[str, ...] = (),
    read_only: tuple[str, ...] = (),
) -> int | None:
    """Run ``argv`` (``argv[0]`` an absolute executable) as PID 1 of new user + PID + mount
    namespaces with its own ``/proc``: no process outside the run is visible, so DAG code
    cannot read the environment or memory of the server that started it, whichever tool it
    uses. The uid and gid stay the same; capabilities are dropped at ``exec``. With
    ``mask_root``, every folder below it except ``keep_dir`` is hidden (:func:`_mask_scratch`);
    ``deny`` paths are hidden, ``read_only`` folders mounted read-only.

    Returns the exit code (negative: killed by that signal) once ``argv`` ran, or ``None``
    when the kernel or container does not allow the namespaces (unprivileged user
    namespaces off, AppArmor's ``apparmor_restrict_unprivileged_userns``, seccomp) and
    nothing ran. Call it only from a single-threaded process."""
    if not sys.platform.startswith("linux"):
        return None
    read_fd, write_fd = os.pipe()  # close-on-exec: EOF tells "exec happened or failed"
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        return None
    if pid == 0:  # pragma: no cover - forked
        os.close(read_fd)
        _namespace_child(argv, write_fd, mask_root, keep_dir, deny, read_only)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as fh:
        started = fh.read(1) == b"1"
    _done, status = os.waitpid(pid, 0)
    if not started:
        return None
    return os.waitstatus_to_exitcode(status)


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

# Denied in import checks only (profile "check"): a test run executes task code, which
# legitimately starts processes (BashOperator), opens connections (HTTP hooks), loads native
# libraries and sends signals to its own children.
_CHECK_DENIED_EVENTS = frozenset(
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
# Events whose path arguments change the file system: allowed below the scratch folder only
# in an import check; anywhere but another worker's folder or /proc entry in a test run.
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
# Events that read a path: denied for other workers' folders and other processes' /proc
# (both profiles).
_READ_EVENTS = frozenset({"os.listdir", "os.scandir", "os.chdir", "os.getxattr", "os.listxattr"})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class _Guard:
    def __init__(self, cfg: WorkerSandbox) -> None:
        self.scratch = os.path.realpath(cfg.scratch_dir) if cfg.scratch_dir else ""
        self.root = os.path.realpath(cfg.scratch_root) if cfg.scratch_root else ""
        self.check = cfg.profile != "run"
        self.deny = [os.path.realpath(d) for d in cfg.deny_paths]
        self.read_only = [os.path.realpath(d) for d in cfg.read_only_paths]
        self.what = "an AutoAir import check" if self.check else "an AutoAir test run"
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
            # the current pid, not the one at install time: a forked child reads its own
            if head.isdigit() and head != str(os.getpid()):
                return False
        if any(self._inside(real, d) for d in self.deny):
            return False
        other_worker = self._inside(real, self.root) and not self._inside(real, self.scratch)
        return not other_worker

    def _writable(self, real: str) -> bool:
        if not self.check:
            # a run writes where its task writes, but not into the DAG folders Airflow
            # parses (a planted file would skip review and validation); _readable applies
            return not any(self._inside(real, d) for d in self.read_only)
        return self._inside(real, self.scratch) or real in ("/dev/null", os.devnull)

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        if self.check and event in _CHECK_DENIED_EVENTS:
            raise PermissionError(f"{event} is not allowed during {self.what}")
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
                    f"writing {os.fspath(path)!r} is not allowed during {self.what}"
                )
            if not self._readable(real):
                raise PermissionError(
                    f"reading {os.fspath(path)!r} is not allowed during {self.what}"
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
                    raise PermissionError(f"{event} is not allowed during {self.what}")
        finally:
            self.local.busy = False


def install_audit_hook(cfg: WorkerSandbox) -> None:
    """Install the guard of ``cfg.profile`` for the rest of this process's life (audit hooks
    cannot be removed)."""
    sys.addaudithook(_Guard(cfg))


__all__ = [
    "SandboxError",
    "SandboxProfile",
    "WorkerSandbox",
    "apply_limits",
    "harden_server_process",
    "install_audit_hook",
    "isolate_network",
    "make_non_dumpable",
    "run_in_process_namespace",
]
