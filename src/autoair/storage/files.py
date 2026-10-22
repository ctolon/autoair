"""Deploy-target logic shared by file-like backends (local folder, object storage).

The backend only has to implement :class:`BlobStore` (read / write / remove / list on
``/``-separated paths relative to the target root); ownership checks, optimistic
locking, ejection detection and history snapshots live in :class:`FileTarget`.
"""

from __future__ import annotations

import builtins
import contextlib
import datetime
import json
import logging
from typing import Protocol

from pydantic import ValidationError

from autoair.loader import SPEC_SUFFIX
from autoair.spec import DagSpec
from autoair.storage.base import (
    DEFAULT_HISTORY_LIMIT,
    HISTORY_DIR,
    STATE_DIR,
    ConflictError,
    DagState,
    ExistingFile,
    HandWrittenError,
    HistoryEntry,
    NotFoundError,
    SaveResult,
    SpecSummary,
    StorageError,
    StoredSpec,
    TargetKind,
    TargetStatus,
    VersionInfo,
    check_dag_id,
    check_files,
    check_overwrite,
    check_version,
    history_id,
    is_owned,
    main_file,
    parse_state,
    serialize_spec,
    sha256_text,
    source_sha,
    spec_etag,
    spec_filename,
    summary_of,
    utcnow,
)

log = logging.getLogger(__name__)


class BlobStore(Protocol):
    """Minimal file API over ``/``-separated paths relative to the target root."""

    def read(self, rel: str) -> str | None: ...
    def write(self, rel: str, content: str) -> None: ...
    def remove(self, rel: str) -> None: ...
    def listdir(self, rel_dir: str) -> list[str]: ...
    def mtime(self, rel: str) -> datetime.datetime | None: ...
    def location(self, rel: str = "") -> str: ...
    def writable(self) -> bool: ...
    def lock(self) -> contextlib.AbstractContextManager[None]: ...


class FileTarget:
    """A :class:`~autoair.storage.base.DeployTarget` on top of a :class:`BlobStore`."""

    kind: TargetKind

    def __init__(
        self, store: BlobStore, *, kind: TargetKind, history_limit: int = DEFAULT_HISTORY_LIMIT
    ) -> None:
        self.store = store
        self.kind = kind
        self.history_limit = max(1, history_limit)

    # ----------------------------------------------------------------- internals
    @staticmethod
    def _state_rel(dag_id: str) -> str:
        return f"{STATE_DIR}/{check_dag_id(dag_id)}.json"

    @staticmethod
    def _history_dir(dag_id: str) -> str:
        return f"{HISTORY_DIR}/{check_dag_id(dag_id)}"

    def _read_state(self, dag_id: str) -> DagState | None:
        """The state record for reads (an unreadable one reads as missing)."""
        raw = self.store.read(self._state_rel(dag_id))
        if raw is None:
            return None
        try:
            return DagState.model_validate_json(raw)
        except ValidationError:
            log.warning("ignoring unreadable AutoAir state for %s", dag_id)
            return None

    def _state_for_write(self, dag_id: str) -> DagState | None:
        """The state record a write decides with: unreadable fails closed (it says which
        files are hand-written or hand-edited, so guessing could destroy one)."""
        rel = self._state_rel(dag_id)
        return parse_state(self.store.read(rel), self.store.location(rel), dag_id)

    def _write_lock(self, dag_id: str) -> contextlib.AbstractContextManager[None]:
        """Held while a write of ``dag_id`` reads, checks and writes."""
        return self.store.lock()

    def _still_writing(self) -> None:
        """Called before each write step; raises when this writer lost its right to write
        (an expired replica lease), which rolls the save back."""

    def _apply(self, steps: builtins.list[tuple[str, str | None]]) -> None:
        """Write (content) or remove (``None``) each path in order. When a step fails, the
        steps done so far are undone (previous content restored) and the error raised, so
        a failed deploy leaves the spec, history, DAG files and state as they were."""
        done: builtins.list[tuple[str, str | None]] = []
        try:
            for rel, content in steps:
                self._still_writing()
                done.append((rel, self.store.read(rel)))
                if content is None:
                    self.store.remove(rel)
                else:
                    self.store.write(rel, content)
        except BaseException:
            for rel, previous in reversed(done):
                try:
                    if previous is None:
                        self.store.remove(rel)
                    else:
                        self.store.write(rel, previous)
                except Exception:
                    log.exception("cannot restore %s after a failed write", rel)
            raise

    def _generated_names(self, dag_id: str, state: DagState | None) -> list[str]:
        if state is not None and state.files:
            return list(state.files)
        return [f"{dag_id}.py"]  # no state record: a file placed by hand or another tool

    def _check_overwrite(
        self,
        name: str,
        existing: str | None,
        recorded: str | None,
        new_sha: str | None,
        force: bool,
        expected: str | None = None,
        *,
        unknown: bool = False,
    ) -> None:
        check_overwrite(
            self.store.location(name),
            existing,
            recorded,
            new_sha,
            force,
            expected,
            unknown=unknown,
        )

    def current_file(self, dag_id: str, *, environment: str | None = None) -> ExistingFile | None:
        """The main DAG file now in the target (``<dag_id>.py``) and its recorded hash."""
        name = f"{check_dag_id(dag_id)}.py"
        content = self.store.read(name)
        if content is None:
            return None
        state = self._read_state(dag_id)
        return ExistingFile(
            name=name,
            content=content,
            location=self.store.location(name),
            recorded_sha=state.files.get(name) if state else None,
            source_sha=state.source_sha if state else None,
            hand_written=state.hand_written if state else False,
        )

    # ----------------------------------------------------------------- reads
    def list(self) -> list[SpecSummary]:
        out: list[SpecSummary] = []
        for name in sorted(self.store.listdir("")):
            if not name.endswith(SPEC_SUFFIX) or name.startswith("."):
                continue
            raw = self.store.read(name)
            if raw is None:
                continue
            try:
                spec = DagSpec.model_validate_json(raw)
            except Exception:
                log.warning("skipping unreadable spec %s", name, exc_info=True)
                continue
            if name != f"{spec.dag_id}{SPEC_SUFFIX}":
                log.warning("skipping %s: its dag_id is %r", name, spec.dag_id)
                continue
            try:
                state = self._read_state(spec.dag_id)
            except StorageError:
                continue
            out.append(
                summary_of(
                    spec,
                    updated_at=self.store.mtime(name) or utcnow(),
                    dag_file=self.store.location(
                        main_file(spec.dag_id, self._generated_names(spec.dag_id, state))
                    ),
                    etag=spec_etag(spec),
                )
            )
        return out

    def get(self, dag_id: str) -> StoredSpec | None:
        raw = self.store.read(spec_filename(dag_id))
        if raw is None:
            return None
        try:
            spec = DagSpec.model_validate_json(raw)
        except ValidationError as exc:
            raise StorageError(f"stored spec for {dag_id!r} is invalid: {exc}") from exc
        state = self._read_state(dag_id)
        return StoredSpec(spec=spec, etag=spec_etag(spec), version=state.version if state else None)

    def history(self, dag_id: str, limit: int = 50) -> builtins.list[VersionInfo]:
        names = sorted(
            (n for n in self.store.listdir(self._history_dir(dag_id)) if n.endswith(".json")),
            reverse=True,
        )
        out: list[VersionInfo] = []
        for name in names[: max(0, limit)]:
            entry = self._read_history(dag_id, name.removesuffix(".json"))
            if entry is not None:
                out.append(entry.info())
        return out

    def _read_history(self, dag_id: str, version: str) -> HistoryEntry | None:
        raw = self.store.read(f"{self._history_dir(dag_id)}/{check_version(version)}.json")
        if raw is None:
            return None
        try:
            return HistoryEntry.model_validate_json(raw)
        except ValidationError:
            log.warning("ignoring unreadable history entry %s/%s", dag_id, version)
            return None

    def get_version(self, dag_id: str, version: str) -> DagSpec:
        entry = self._read_history(dag_id, version)
        if entry is None:
            raise NotFoundError(f"no version {version!r} of {dag_id!r}")
        return DagSpec.model_validate(entry.spec)

    def status(self) -> TargetStatus:
        return TargetStatus(
            kind=self.kind,
            location=self.store.location(),
            writable=self.store.writable(),
            details={"history_limit": self.history_limit},
        )

    # ----------------------------------------------------------------- writes
    def save(
        self,
        spec: DagSpec,
        files: dict[str, str],
        *,
        base_etag: str | None = None,
        author: str | None = None,
        message: str | None = None,
        environment: str | None = None,
        force: bool = False,
        expected: dict[str, str] | None = None,
        hand_written: bool | None = None,
        allow_hand_written: bool = False,
    ) -> SaveResult:
        """``expected``: file name -> sha256 of the file a source-preserving deploy patched
        (written only while the file is unchanged, also when AutoAir did not create it).
        ``hand_written``: the written file is a hand-written one AutoAir keeps (``None``:
        unchanged from the last save). ``allow_hand_written``: the caller may replace or
        remove a hand-written file AutoAir keeps without ``expected`` covering it (granted
        only with ``deploy:overwrite_ejected``); the usual ownership checks still apply."""
        dag_id = check_dag_id(spec.dag_id)
        check_files(dag_id, files)
        expected = expected or {}
        with self._write_lock(dag_id):
            current = self.get(dag_id)
            if base_etag is not None and (current is None or current.etag != base_etag):
                raise ConflictError(current)
            state = self._state_for_write(dag_id)
            recorded = state.files if state else {}
            hashes = {name: sha256_text(content) for name, content in files.items()}
            # Every write path (loader/YAML output modes, raw code, promotion, MCP) ends
            # here, so the kept hand-written file is protected here and not only by the plan.
            guarded = state is not None and state.hand_written and not allow_hand_written
            for name in files:
                existing = self.store.read(name)
                if guarded and existing is not None and name not in expected:
                    raise HandWrittenError(self.store.location(name))
                self._check_overwrite(
                    name,
                    existing,
                    recorded.get(name),
                    hashes[name],
                    force,
                    expected.get(name),
                    unknown=state is None,
                )
            stale: list[str] = []
            for name in recorded:
                if name in files:
                    continue
                existing = self.store.read(name)
                if existing is None:
                    continue
                if guarded:
                    raise HandWrittenError(self.store.location(name))
                if not is_owned(existing):
                    log.warning("leaving %s in place: it is not owned by AutoAir", name)
                    continue
                self._check_overwrite(name, existing, recorded[name], None, force)
                stale.append(name)

            etag = spec_etag(spec)
            main = main_file(dag_id, files)
            keep = state.hand_written if state is not None else False
            kept = keep if hand_written is None else hand_written
            if (
                current is not None
                and state is not None
                and current.etag == etag
                and state.files == hashes
                and state.source_sha == source_sha(spec)
                and state.hand_written == kept
                and not stale
            ):
                return SaveResult(
                    path=self.store.location(main),
                    etag=etag,
                    version=state.version,
                    message=f"unchanged: {self.store.location(main)}",
                )

            now = utcnow()
            version = history_id(now, etag)
            entry = HistoryEntry(
                version=version,
                timestamp=now,
                etag=etag,
                author=author,
                message=message,
                environment=environment,
                spec=json.loads(serialize_spec(spec)),
            )
            history_json = entry.model_dump_json(indent=2)
            state_json = DagState(
                dag_id=dag_id,
                etag=etag,
                version=version,
                files=hashes,
                updated_at=now,
                author=author,
                environment=environment,
                source_sha=source_sha(spec),
                hand_written=kept,
            ).model_dump_json(indent=2)
            # Spec first (in loader mode the stub reads it at parse time), then the DAG
            # files, the history entry and the state record; all or nothing (_apply).
            self._apply(
                [
                    (spec_filename(dag_id), serialize_spec(spec)),
                    *((name, files[name]) for name in sorted(files, key=lambda n: n == main)),
                    *((name, None) for name in stale),
                    (f"{self._history_dir(dag_id)}/{version}.json", history_json),
                    (self._state_rel(dag_id), state_json),
                ]
            )
            self._prune_history(dag_id)
        location = self.store.location(main)
        return SaveResult(
            path=location, etag=etag, version=version, message=f"written to {location}"
        )

    def _prune_history(self, dag_id: str) -> None:
        directory = self._history_dir(dag_id)
        try:
            names = sorted(n for n in self.store.listdir(directory) if n.endswith(".json"))
            for name in names[: max(0, len(names) - self.history_limit)]:
                self.store.remove(f"{directory}/{name}")
        except OSError:  # the deploy itself succeeded; old entries go next time
            log.warning("cannot prune the history of %s", dag_id, exc_info=True)

    def delete(
        self,
        dag_id: str,
        *,
        base_etag: str | None = None,
        author: str | None = None,
        force: bool = False,
        allow_hand_written: bool = False,
    ) -> bool:
        """``allow_hand_written``: the caller may delete a hand-written file AutoAir keeps
        (``deploy:overwrite_ejected``); it is still deleted only while unchanged."""
        check_dag_id(dag_id)
        with self._write_lock(dag_id):
            current = self.get(dag_id)
            if current is None:
                return False
            if base_etag is not None and current.etag != base_etag:
                raise ConflictError(current)
            state = self._state_for_write(dag_id)
            recorded = state.files if state else {}
            doomed: list[str] = []
            for name in self._generated_names(dag_id, state):
                existing = self.store.read(name)
                if existing is None:
                    continue
                expected = None
                if state is not None and state.hand_written:
                    if not allow_hand_written:
                        raise HandWrittenError(self.store.location(name))
                    expected = recorded.get(name)
                self._check_overwrite(
                    name,
                    existing,
                    recorded.get(name),
                    None,
                    force,
                    expected,
                    unknown=state is None,
                )
                doomed.append(name)
            # DAG files first so Airflow never parses a stub without its spec.
            self._apply(
                [
                    *((name, None) for name in doomed),
                    (spec_filename(dag_id), None),
                    (self._state_rel(dag_id), None),
                ]
            )
            log.info("deleted DAG %s (by %s)", dag_id, author or "unknown")
        return True
