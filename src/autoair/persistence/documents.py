"""Document repositories: small keyed blobs with an optimistic version (etag).

One protocol, two implementations:

* :class:`FileDocumentStore` - one file per key on an fsspec URL (``<root>/<key><suffix>``);
  read-modify-write cycles hold a process lock plus, on local disks, an ``flock`` so several
  processes on one host cannot lose updates (object stores: last writer wins; use a SQL
  backend for several replicas);
* :class:`~autoair.persistence.sql.SqlDocumentStore` - rows of ``<prefix>documents`` with an
  integer version (compare-and-swap ``UPDATE … WHERE version = :seen``).
"""

from __future__ import annotations

import builtins
import contextlib
import datetime
import hashlib
import re
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

import fsspec

from autoair._fs import fs_write_bytes

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,254}$")


class DocumentConflict(Exception):
    """The document changed since it was read (optimistic concurrency)."""


class _Any:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ANY"


#: ``expected_version`` default: write unconditionally.
ANY: Any = _Any()


@dataclass(frozen=True)
class StoredDocument:
    key: str
    data: bytes
    version: str
    updated_at: str | None = None

    def text(self) -> str:
        return self.data.decode("utf-8")


def check_key(key: str) -> str:
    if not KEY_RE.fullmatch(key) or ".." in key:
        raise ValueError(f"invalid document key {key!r}")
    return key


@runtime_checkable
class DocumentStore(Protocol):
    """Keyed documents of one collection."""

    def get(self, key: str) -> StoredDocument | None: ...

    def put(self, key: str, data: bytes, *, expected_version: Any = ANY) -> StoredDocument:
        """Write ``key``. ``expected_version``: :data:`ANY` (unconditional), ``None`` (must
        not exist yet) or a version returned by :meth:`get` (must be unchanged); otherwise
        :class:`DocumentConflict`."""
        ...

    def put_if_absent(self, key: str, data: bytes) -> StoredDocument:
        """Create ``key`` unless it exists; returns whichever document is stored."""
        ...

    def delete(self, key: str) -> bool: ...

    def keys(self) -> builtins.list[str]: ...

    def update(self, key: str, fn: Callable[[bytes | None], bytes | None]) -> bytes | None:
        """Atomic read-modify-write: ``fn(current)`` returns the new content (``None``
        deletes); retried on a concurrent change. Returns what was stored."""
        ...


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:20]


def _is_local(fs: Any) -> bool:
    protocol = fs.protocol if isinstance(fs.protocol, (tuple, list)) else (fs.protocol,)
    return "file" in protocol or "local" in protocol


class FileDocumentStore:
    """Documents as files ``<root>/<key><suffix>`` on any fsspec URL."""

    _LOCKS: ClassVar[dict[str, threading.RLock]] = {}
    _LOCKS_GUARD: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self, url: str, *, suffix: str = ".json", storage_options: dict[str, Any] | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.fs: Any
        self.fs, root = fsspec.core.url_to_fs(self.url, **(storage_options or {}))
        self.root = str(root).rstrip("/")
        self.suffix = suffix
        with self._LOCKS_GUARD:  # one lock per folder, shared by every store on it
            self._lock = self._LOCKS.setdefault(self.root, threading.RLock())

    def path(self, key: str) -> str:
        return f"{self.root}/{check_key(key)}{self.suffix}"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            if not _is_local(self.fs) or sys.platform == "win32":
                yield
                return
            import fcntl

            self.fs.makedirs(self.root, exist_ok=True)
            with open(f"{self.root}/.autoair.lock", "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _mtime(self, path: str) -> str | None:
        try:
            info = self.fs.info(path)
        except (FileNotFoundError, OSError):
            return None
        value = info.get("mtime") or info.get("LastModified") or info.get("created")
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()
        if isinstance(value, datetime.datetime):
            return value.isoformat()
        return str(value) if value else None

    def get(self, key: str) -> StoredDocument | None:
        path = self.path(key)
        try:
            with self.fs.open(path, "rb") as fh:
                data = bytes(fh.read())
        except FileNotFoundError:
            return None
        except IsADirectoryError:
            return None
        return StoredDocument(key, data, _etag(data), self._mtime(path))

    def _write(self, key: str, data: bytes) -> StoredDocument:
        path = self.path(key)
        fs_write_bytes(self.fs, path, data)
        return StoredDocument(key, data, _etag(data), _now())

    def _check(self, key: str, expected_version: Any) -> None:
        if expected_version is ANY:
            return
        current = self.get(key)
        seen = current.version if current is not None else None
        if seen != expected_version:
            raise DocumentConflict(f"{key} changed (expected {expected_version}, found {seen})")

    def put(self, key: str, data: bytes, *, expected_version: Any = ANY) -> StoredDocument:
        with self._locked():
            self._check(key, expected_version)
            return self._write(key, data)

    def put_if_absent(self, key: str, data: bytes) -> StoredDocument:
        with self._locked():
            current = self.get(key)
            return current if current is not None else self._write(key, data)

    def delete(self, key: str) -> bool:
        path = self.path(key)
        with self._locked():
            try:
                if not self.fs.exists(path):
                    return False
                self.fs.rm(path)
            except FileNotFoundError:
                return False
        return True

    def keys(self) -> builtins.list[str]:
        try:
            paths = self.fs.glob(f"{self.root}/*{self.suffix}")
        except (FileNotFoundError, OSError):
            return []
        out: list[str] = []
        for path in sorted(paths):
            name = str(path).rsplit("/", 1)[-1]
            key = name[: -len(self.suffix)] if self.suffix else name
            if KEY_RE.fullmatch(key):
                out.append(key)
        return out

    def update(self, key: str, fn: Callable[[bytes | None], bytes | None]) -> bytes | None:
        with self._locked():
            current = self.get(key)
            new = fn(current.data if current is not None else None)
            if new is None:
                if current is not None:
                    self.fs.rm(self.path(key))
                return None
            self._write(key, new)
            return new


__all__ = [
    "ANY",
    "DocumentConflict",
    "DocumentStore",
    "FileDocumentStore",
    "StoredDocument",
    "check_key",
]
