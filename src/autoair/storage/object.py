"""Object storage deploy target (S3, GCS, Azure Blob, ... via fsspec).

Pairs with Airflow's ``S3DagBundle`` / object-storage bundles. Writes are best-effort
atomic per object (spec first, DAG file last); optimistic locking is enforced inside
this process only, since plain object stores have no compare-and-swap primitive.
"""

from __future__ import annotations

import contextlib
import datetime
import threading
from collections.abc import Iterator
from typing import Any

import fsspec
from fsspec.core import url_to_fs

from autoair.storage.base import DEFAULT_HISTORY_LIMIT, StorageError, TargetStatus
from autoair.storage.files import FileTarget


class FsspecStore:
    """:class:`~autoair.storage.files.BlobStore` on an fsspec filesystem prefix."""

    def __init__(self, url: str, storage_options: dict[str, Any] | None = None) -> None:
        fs, path = url_to_fs(url, **(storage_options or {}))
        self.fs: fsspec.AbstractFileSystem = fs
        self.base = str(path).rstrip("/")
        self.url = url
        self._lock = threading.RLock()

    def full(self, rel: str) -> str:
        parts = [p for p in rel.split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise StorageError("path escapes the storage prefix")
        return "/".join([self.base, *parts]) if parts else self.base

    def read(self, rel: str) -> str | None:
        try:
            data = self.fs.cat_file(self.full(rel))
        except (FileNotFoundError, IsADirectoryError):
            return None
        return bytes(data).decode("utf-8")

    def write(self, rel: str, content: str) -> None:
        path = self.full(rel)
        parent = path.rsplit("/", 1)[0]
        with contextlib.suppress(Exception):
            self.fs.makedirs(parent, exist_ok=True)
        self.fs.pipe_file(path, content.encode("utf-8"))

    def remove(self, rel: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.fs.rm_file(self.full(rel))

    def listdir(self, rel_dir: str) -> list[str]:
        try:
            entries = self.fs.ls(self.full(rel_dir), detail=True, refresh=True)
        except (FileNotFoundError, NotADirectoryError):
            return []
        out: list[str] = []
        for entry in entries:
            if entry.get("type") != "file":
                continue
            out.append(str(entry["name"]).rstrip("/").rsplit("/", 1)[-1])
        return out

    def mtime(self, rel: str) -> datetime.datetime | None:
        try:
            info = self.fs.info(self.full(rel))
        except FileNotFoundError:
            return None
        for key in ("mtime", "LastModified", "last_modified", "updated", "created"):
            value = info.get(key)
            if isinstance(value, datetime.datetime):
                return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
            if isinstance(value, (int, float)):
                return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)
            if isinstance(value, str):
                with contextlib.suppress(ValueError):
                    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)
        return None

    def location(self, rel: str = "") -> str:
        return str(self.fs.unstrip_protocol(self.full(rel)))

    def writable(self) -> bool:
        return not getattr(self.fs, "read_only", False)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield


class ObjectStorageTarget(FileTarget):
    """Deploy target on ``s3://bucket/prefix``, ``gs://...``, ``memory://...`` etc."""

    def __init__(
        self,
        url: str,
        storage_options: dict[str, Any] | None = None,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self.object_store = FsspecStore(url, storage_options)
        super().__init__(self.object_store, kind="object", history_limit=history_limit)

    def status(self) -> TargetStatus:
        status = super().status()
        protocol = self.object_store.fs.protocol
        status.details["protocol"] = protocol if isinstance(protocol, str) else protocol[0]
        return status
