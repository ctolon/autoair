"""Local folder deploy target (an Airflow ``LocalDagBundle`` / ``dags_folder``)."""

from __future__ import annotations

import contextlib
import datetime
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from autoair.loader import render_stub
from autoair.spec import DagSpec
from autoair.storage.base import (
    DEFAULT_HISTORY_LIMIT,
    SpecSummary,
    StorageError,
    check_dag_id,
    spec_filename,
)
from autoair.storage.files import FileTarget

# Generated DAG files must be readable by the dag-processor / workers (often another user).
DEFAULT_FILE_MODE = 0o644


class LocalStore:
    """:class:`~autoair.storage.files.BlobStore` on a directory with atomic writes."""

    def __init__(
        self,
        root: Path,
        *,
        file_mode: int = DEFAULT_FILE_MODE,
        dir_mode: int | None = None,
        temp_dir: Path | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.temp_dir = temp_dir.expanduser() if temp_dir is not None else None
        self._lock = threading.RLock()

    def path(self, rel: str) -> Path:
        path = (self.root / rel).resolve() if rel else self.root
        if path != self.root and self.root not in path.parents:
            raise StorageError("path escapes the DAG folder")
        return path

    def read(self, rel: str) -> str | None:
        path = self.path(rel)
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None

    def write(self, rel: str, content: str) -> None:
        path = self.path(rel)
        self._mkdir(path.parent)
        _atomic_write(path, content, mode=self.file_mode, temp_dir=self.temp_dir)

    def _mkdir(self, directory: Path) -> None:
        if self.dir_mode is None:
            directory.mkdir(parents=True, exist_ok=True)
            return
        missing = []
        probe = directory
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        for d in reversed(missing):
            d.mkdir(exist_ok=True)
            os.chmod(d, self.dir_mode)  # explicit: mkdir's mode is masked by the umask

    def remove(self, rel: str) -> None:
        self.path(rel).unlink(missing_ok=True)

    def listdir(self, rel_dir: str) -> list[str]:
        directory = self.path(rel_dir)
        if not directory.is_dir():
            return []
        return [p.name for p in directory.iterdir() if p.is_file()]

    def mtime(self, rel: str) -> datetime.datetime | None:
        try:
            stamp = self.path(rel).stat().st_mtime
        except FileNotFoundError:
            return None
        return datetime.datetime.fromtimestamp(stamp, tz=datetime.timezone.utc)

    def location(self, rel: str = "") -> str:
        return str(self.path(rel))

    def writable(self) -> bool:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return os.access(probe, os.W_OK)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Serialise writers in this process and (on POSIX) across processes."""
        with self._lock:
            if sys.platform == "win32":  # pragma: no cover
                yield
                return
            import fcntl

            lock_path = self.path(".autoair/lock")
            self._mkdir(lock_path.parent)
            with lock_path.open("a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)


class LocalFolderTarget(FileTarget):
    """Writes ``<dag_id>.autoair.json`` plus the generated DAG file(s) into ``root``.

    History snapshots live in ``<root>/.autoair/history/<dag_id>/`` and per-DAG
    bookkeeping (hashes of the generated files) in ``<root>/.autoair/state/``.
    Files not created by AutoAir are never overwritten or deleted.
    """

    def __init__(
        self,
        root: Path,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        file_mode: int = DEFAULT_FILE_MODE,
        dir_mode: int | None = None,
        temp_dir: Path | None = None,
    ) -> None:
        self.local = LocalStore(root, file_mode=file_mode, dir_mode=dir_mode, temp_dir=temp_dir)
        super().__init__(self.local, kind="local", history_limit=history_limit)

    @property
    def root(self) -> Path:
        return self.local.root


class FileSystemRepository:
    """Minimal ``save(spec, python_source)`` facade over a local folder.

    * python mode: ``<dag_id>.py`` is the generated, formatted DAG source.
    * loader mode: ``<dag_id>.py`` is a tiny stub calling :func:`autoair.loader.load_dag`.
    """

    def __init__(self, root: Path, *, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self.target = LocalFolderTarget(root, history_limit=history_limit)
        self.root = self.target.root

    def _path(self, dag_id: str, suffix: str) -> Path:
        check_dag_id(dag_id)
        path = (self.root / f"{dag_id}{suffix}").resolve()
        if path.parent != self.root:
            raise StorageError("path escapes the DAG folder")
        return path

    def spec_path(self, dag_id: str) -> Path:
        return self._path(dag_id, spec_filename(dag_id).removeprefix(dag_id))

    def dag_path(self, dag_id: str) -> Path:
        return self._path(dag_id, ".py")

    def list(self) -> list[SpecSummary]:
        return self.target.list()

    def get(self, dag_id: str) -> DagSpec | None:
        stored = self.target.get(dag_id)
        return stored.spec if stored else None

    def save(self, spec: DagSpec, python_source: str | None) -> Path:
        if spec.output_mode == "python" and python_source is None:
            raise StorageError("python output mode requires generated source")
        content = python_source if spec.output_mode == "python" else render_stub(spec.dag_id)
        assert content is not None
        self.target.save(spec, {f"{spec.dag_id}.py": content})
        return self.dag_path(spec.dag_id)

    def delete(self, dag_id: str) -> bool:
        return self.target.delete(dag_id)


def _atomic_write(
    path: Path, content: str, *, mode: int = DEFAULT_FILE_MODE, temp_dir: Path | None = None
) -> None:
    """Write a temporary file and rename it over ``path`` (readers never see half a file).
    ``temp_dir`` must be on the same file system as ``path`` for the rename to be atomic."""
    directory = temp_dir if temp_dir is not None else path.parent
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
