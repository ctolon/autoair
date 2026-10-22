"""Object storage deploy target (S3, GCS, Azure Blob, ... via fsspec).

Pairs with Airflow's ``S3DagBundle`` / object-storage bundles. Each object is written whole
(one PUT; a temporary file renamed into place on local file systems), the spec first and
the DAG file after it, and a failed save restores what it already wrote.

Plain object stores have no multi-object transaction, so writers of one DAG are serialised
by a lease in AutoAir's own state backend (``locks``: a document with compare-and-swap,
shared by every replica on a SQL backend): the base-etag check, the ownership checks and
the writes all happen while holding it, and a second writer that read the same etag gets a
conflict. Without ``locks`` the lease is process-local.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import fsspec
from fsspec.core import url_to_fs

from autoair.storage.base import DEFAULT_HISTORY_LIMIT, ConflictError, StorageError, TargetStatus
from autoair.storage.files import FileTarget

if TYPE_CHECKING:
    from autoair.persistence import DocumentStore

log = logging.getLogger(__name__)

DEFAULT_LOCK_SECONDS = 300.0  # default of AUTOAIR_STORAGE__OBJECT_LOCK_SECONDS
DEFAULT_LOCK_WAIT_SECONDS = 30.0  # default of AUTOAIR_STORAGE__OBJECT_LOCK_WAIT_SECONDS
_LOCK_POLL_SECONDS = 0.05


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
        data = content.encode("utf-8")
        if "file" in _protocols(self.fs):
            # A local (or mounted) file system writes in place: a reader (the dag-processor)
            # could see half a file, so write a temporary file and rename it over the target.
            tmp = f"{parent}/.{path.rsplit('/', 1)[-1]}.{uuid.uuid4().hex}.tmp"
            try:
                self.fs.pipe_file(tmp, data)
                self.fs.mv(tmp, path)
            except BaseException:
                with contextlib.suppress(Exception):
                    self.fs.rm_file(tmp)
                raise
            return
        # Object stores replace an object whole in one PUT: readers see the old or the new.
        self.fs.pipe_file(path, data)

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


def _expires(data: bytes) -> float:
    """Expiry (epoch seconds) of a lease document; an unreadable one counts as expired."""
    try:
        return float(json.loads(data).get("expires", 0))
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _protocols(fs: fsspec.AbstractFileSystem) -> tuple[str, ...]:
    protocol = fs.protocol
    return (protocol,) if isinstance(protocol, str) else tuple(protocol)


class ObjectStorageTarget(FileTarget):
    """Deploy target on ``s3://bucket/prefix``, ``gs://...``, ``memory://...`` etc.

    ``locks``: AutoAir's document store for the per-DAG write lease (see the module docs);
    ``lock_seconds`` is how long a lease lasts (a replica that died while holding it blocks
    the DAG at most that long), ``lock_wait_seconds`` how long a writer waits for one."""

    def __init__(
        self,
        url: str,
        storage_options: dict[str, Any] | None = None,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        locks: DocumentStore | None = None,
        lock_seconds: float = DEFAULT_LOCK_SECONDS,
        lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> None:
        self.object_store = FsspecStore(url, storage_options)
        super().__init__(self.object_store, kind="object", history_limit=history_limit)
        self.locks = locks
        self.lock_seconds = lock_seconds
        self.lock_wait_seconds = lock_wait_seconds
        # The URL as configured, so every replica derives the same lease key.
        self._lock_scope = hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:24]
        # (key, claim, version) of the lease this target holds; guarded by the store's lock.
        self._held: tuple[str, dict[str, Any], str] | None = None

    def _still_writing(self) -> None:
        """Renew the lease (compare-and-swap on the version claimed): a writer whose lease
        expired and was taken over stops before its next write instead of interleaving."""
        if self.locks is None or self._held is None:
            return
        from autoair.persistence import DocumentConflict

        key, claim, version = self._held
        claim = {**claim, "expires": time.time() + self.lock_seconds}
        try:
            doc = self.locks.put(key, json.dumps(claim).encode(), expected_version=version)
        except DocumentConflict:
            raise ConflictError(
                None,
                f"the write lease of DAG {claim.get('dag_id')!r} expired and another replica "
                "took it over; nothing was saved, reload and retry",
            ) from None
        self._held = (key, claim, doc.version)

    def _write_lock(self, dag_id: str) -> contextlib.AbstractContextManager[None]:
        return self._locked(dag_id)

    @contextlib.contextmanager
    def _locked(self, dag_id: str) -> Iterator[None]:
        with self.object_store.lock(), self._lease(dag_id):
            yield

    @contextlib.contextmanager
    def _lease(self, dag_id: str) -> Iterator[None]:
        """Hold the write lease of ``dag_id`` in :attr:`locks` (compare-and-swap)."""
        if self.locks is None:
            yield
            return
        from autoair.persistence import DocumentConflict

        key = f"object-deploy:{self._lock_scope}:{hashlib.sha256(dag_id.encode()).hexdigest()}"
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + self.lock_wait_seconds
        held = None
        claim: dict[str, Any] = {}
        while held is None:
            doc = self.locks.get(key)
            if doc is None or _expires(doc.data) <= time.time():
                claim = {
                    "owner": owner,
                    "dag_id": dag_id,
                    "expires": time.time() + self.lock_seconds,
                }
                try:
                    held = self.locks.put(
                        key,
                        json.dumps(claim).encode(),
                        expected_version=None if doc is None else doc.version,
                    )
                except DocumentConflict:
                    continue  # another replica claimed it first: look again
                break
            if time.monotonic() >= deadline:
                raise ConflictError(
                    None,
                    f"another AutoAir replica is writing DAG {dag_id!r} to this object "
                    "storage; reload and retry",
                )
            time.sleep(_LOCK_POLL_SECONDS)
        self._held = (key, claim, held.version)
        try:
            yield
        finally:
            version = self._held[2] if self._held is not None else held.version
            self._held = None
            released = json.dumps({"owner": None, "dag_id": dag_id, "expires": 0}).encode()
            try:
                self.locks.put(key, released, expected_version=version)
            except DocumentConflict:
                log.warning(
                    "the write lease of %s expired before the write finished "
                    "(storage.object_lock_seconds too short?)",
                    dag_id,
                )
            except Exception:  # the write's own outcome matters more; the lease expires
                log.warning("cannot release the write lease of %s", dag_id, exc_info=True)

    def status(self) -> TargetStatus:
        status = super().status()
        protocol = self.object_store.fs.protocol
        status.details["protocol"] = protocol if isinstance(protocol, str) else protocol[0]
        return status
