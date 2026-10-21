"""The ingestion pipeline: source -> fetch -> parse -> chunk -> dedupe -> embed -> index.

One run (refresh, rebuild or bundle import) holds the ingestion **lease** of its index
(:class:`autoair.kb.state.KbState`), so only one replica writes to a given index; the
lease is renewed by a heartbeat thread and expires when the replica dies. Progress, errors
and the outcome are persisted in the status document every replica reads.

Incremental by construction:

* a unit (docs package at a version) whose fingerprint did not change is skipped;
* pages are fetched conditionally (ETag / Last-Modified) and re-indexed only when their
  content hash changed;
* pages that disappeared from a source are removed - but only after the source was listed
  completely (a network failure or the time limit never deletes anything);
* changing the chunk settings re-chunks the stored pages, changing the embedding model
  re-embeds them, without fetching again.
"""

from __future__ import annotations

import concurrent.futures
import email.utils
import hashlib
import json
import logging
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from autoair.kb.chunk import chunk_page
from autoair.kb.config import KbSourceConfig
from autoair.kb.fetch import FetchError, HttpFetcher
from autoair.kb.models import (
    Chunk,
    KbError,
    KbProgress,
    KbRunStatus,
    PageDoc,
    PageRecord,
    Section,
    SourceRun,
    page_id,
)
from autoair.kb.parse import parse
from autoair.kb.sources import Loaded, PageRef, PlanContext, SourceError, Unit, plan_units
from autoair.kb.state import now_iso
from autoair.kb.stores.base import StoreError
from autoair.secret_mask import mask

if TYPE_CHECKING:
    from autoair.kb.service import KnowledgeBase

log = logging.getLogger("autoair.kb")

Mode = Literal["refresh", "rebuild", "import"]
#: Seconds between status writes (progress) of a run.
_STATUS_EVERY = 2.0
#: Consecutive embedding failures after which a run stops embedding (BM25 still indexed).
_EMBED_FAILURES = 3


class AlreadyRunning(RuntimeError):
    """Another replica (or run) holds the ingestion lease of this index."""


class Cancelled(Exception):
    pass


def encode_body(sections: Sequence[Section]) -> bytes:
    data = [s.model_dump(exclude_defaults=True) for s in sections]
    return zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 6)


def decode_body(body: bytes) -> list[Section]:
    if not body:
        return []
    return [Section.model_validate(s) for s in json.loads(zlib.decompress(body))]


def _http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def chunk_config_key(kb: KnowledgeBase) -> str:
    return hashlib.sha256(kb.kb.chunk.model_dump_json().encode()).hexdigest()[:16]


@dataclass
class _Run:
    mode: Mode
    deadline: float
    budget: int
    status: KbRunStatus
    last_write: float = 0.0
    errors: list[KbError] = field(default_factory=list)
    embed_failures: int = 0
    stopped: str | None = None


class Ingestor:
    """Runs one ingestion of a :class:`~autoair.kb.service.KnowledgeBase`."""

    def __init__(self, kb: KnowledgeBase, *, fetcher: HttpFetcher | None = None) -> None:
        self.kb = kb
        self.store = kb.store
        self.state = kb.state
        self.settings = kb.kb
        self.fetcher = fetcher
        self.owner = kb.state.new_owner()
        self._run: _Run | None = None
        self._stop_renew = threading.Event()

    # -- lease ----------------------------------------------------------------------------

    def _renew_loop(self) -> None:
        ttl = self.settings.lock_ttl_seconds
        while not self._stop_renew.wait(ttl / 3):
            try:
                self.state.acquire(self.owner, ttl)
            except Exception:
                log.warning("cannot renew the knowledge-base lease", exc_info=True)

    def run(
        self,
        mode: Mode = "refresh",
        sources: Sequence[str] | None = None,
        *,
        work: Callable[[Ingestor], None] | None = None,
    ) -> KbRunStatus:
        """Refresh / rebuild the selected sources (all enabled ones by default), or run
        ``work`` (bundle import) under the same lease and status handling."""
        if not self.state.acquire(self.owner, self.settings.lock_ttl_seconds):
            raise AlreadyRunning(
                f"a knowledge-base refresh is already running ({self.state.holder() or '?'})"
            )
        renew = threading.Thread(target=self._renew_loop, name="autoair-kb-lease", daemon=True)
        renew.start()
        started = time.monotonic()
        run_id = self.owner.rsplit(":", 1)[-1]

        def begin(s: KbRunStatus) -> None:
            s.state, s.run_id, s.mode, s.owner = "running", run_id, mode, self.owner
            s.started_at, s.finished_at, s.outcome = now_iso(), None, None
            s.cancel_requested = False
            s.progress = KbProgress(phase="starting")
            s.sources, s.errors = [], []

        status = self.state.update_status(begin)
        self._run = _Run(
            mode=mode,
            deadline=started + self.settings.fetch.max_run_seconds,
            budget=self.settings.fetch.max_pages,
            status=status,
        )
        outcome: Literal["ok", "partial", "failed", "cancelled"] = "ok"
        try:
            self.store.open()
            if self.fetcher is None:
                self.fetcher = self.kb.new_fetcher()
            if mode == "rebuild":
                if sources:
                    for sid in sources:
                        self.store.clear(sid)
                else:
                    self.store.clear()
            self._prepare_vectors()
            self._rechunk_if_needed()
            if work is not None:
                work(self)
            else:
                for source, reason in self.kb.runnable_sources(sources):
                    self._source(source, reason)
            self._fill_vectors()
            # One refresh per run (not per page) for stores that batch writes.
            self.store.flush()
            runs = self._run.status.sources
            bad = [r for r in runs if r.state in ("partial", "failed")]
            if self._run.stopped == "cancelled":
                outcome = "cancelled"
            elif bad or self._run.stopped:
                only_failures = all(r.state == "failed" for r in bad)
                outcome = (
                    "failed"
                    if bad and only_failures and not any(r.state == "ok" for r in runs)
                    else "partial"
                )
        except Cancelled:
            outcome = "cancelled"
        except (StoreError, OSError, ValueError) as exc:
            outcome = "failed"
            self._error(None, None, f"{type(exc).__name__}: {exc}")
            log.warning("knowledge-base %s failed: %s", mode, mask(str(exc)))
        finally:
            self._stop_renew.set()
            if self.fetcher is not None:
                self.fetcher.close()
            if outcome != "ok":
                # Pages fetched before a cancel or an error are still written.
                try:
                    self.store.flush()
                except (StoreError, OSError) as exc:
                    outcome = "failed"
                    self._error(None, None, f"{type(exc).__name__}: {exc}")
                    log.warning("knowledge-base flush failed: %s", mask(str(exc)))
            final = outcome

            def finish(s: KbRunStatus) -> None:
                assert self._run is not None
                s.state = "failed" if final == "failed" else "idle"
                s.outcome = final
                s.finished_at = s.last_refresh = now_iso()
                if final == "ok":
                    s.last_success = s.finished_at
                s.progress = KbProgress(phase="done")
                s.sources = self._run.status.sources
                s.errors = self._run.errors[-self.settings.max_errors :]
                s.cancel_requested = False
                s.embedding_model = self.kb.embedding_model

            status = self.state.update_status(finish)
            self.state.release(self.owner)
        return status

    # -- status ---------------------------------------------------------------------------

    def _error(
        self, source: str | None, url: str | None, message: str, level: str = "error"
    ) -> None:
        assert self._run is not None
        if len(self._run.errors) < self.settings.max_errors:
            self._run.errors.append(
                KbError.model_validate(
                    {
                        "level": level,
                        "source": source,
                        "url": url,
                        "message": mask(message)[:500],
                        "at": now_iso(),
                    }
                )
            )

    def warn(self, message: str, source: str | None) -> None:
        self._error(source, None, message, "warning")

    def progress(self, *, force: bool = False, **fields: Any) -> None:
        run = self._run
        assert run is not None
        for key, value in fields.items():
            setattr(run.status.progress, key, value)
        now = time.monotonic()
        if now >= run.deadline and run.stopped is None:
            run.stopped = "time limit"
            self._error(None, None, "the time limit (kb.fetch.max_run_seconds) was reached")
        if not force and now - run.last_write < _STATUS_EVERY:
            return
        run.last_write = now
        snapshot = run.status.progress.model_copy()
        sources = list(run.status.sources)
        errors = run.errors[-self.settings.max_errors :]
        cancel: list[bool] = []

        def write(s: KbRunStatus) -> None:
            s.progress, s.sources, s.errors = snapshot, sources, errors
            cancel.append(s.cancel_requested)

        try:
            self.state.update_status(write)
        except Exception:
            log.debug("cannot write the knowledge-base status", exc_info=True)
            return
        if cancel and cancel[0]:
            run.stopped = "cancelled"
            raise Cancelled

    def _should_stop(self) -> bool:
        run = self._run
        assert run is not None
        if run.budget <= 0 and run.stopped is None:
            run.stopped = "page limit"
            self._error(None, None, "the page limit (kb.fetch.max_pages) was reached")
        return run.stopped is not None

    # -- embeddings -----------------------------------------------------------------------

    def _prepare_vectors(self) -> None:
        model = self.kb.embedding_model
        if self.store.vector_model() != model:
            self.progress(phase="resetting vectors", force=True)
            self.store.reset_vectors(model)

    def embed(self, chunks: Sequence[Chunk]) -> list[list[float]] | None:
        run = self._run
        embedder = self.kb.embedder
        if embedder is None or not chunks or (run and run.embed_failures >= _EMBED_FAILURES):
            return None
        try:
            vectors = self.kb.embed_texts([f"{c.title}\n{c.heading}\n{c.text}" for c in chunks])
        except Exception as exc:
            if run is not None:
                run.embed_failures += 1
                if run.embed_failures == 1:
                    self._error(None, None, f"embeddings failed (keyword index kept): {exc}")
            log.warning("knowledge-base embeddings failed", exc_info=True)
            return None
        if run is not None:
            run.embed_failures = 0
        return vectors

    def _fill_vectors(self) -> None:
        if self.kb.embedder is None:
            return
        batch = max(1, self.kb.embed_batch)
        while True:
            if self._run is not None and (self._run.stopped or self._run.embed_failures):
                return
            missing = self.store.missing_vectors(batch)
            if not missing:
                return
            self.progress(phase="embedding", unit=None)
            vectors = self.embed(missing)
            if vectors is None:
                return
            self.store.put_vectors({c.id: v for c, v in zip(missing, vectors, strict=True)})

    # -- writing --------------------------------------------------------------------------

    def write_page(self, page: PageDoc) -> int:
        """Chunk, embed and store a page; returns the number of chunks."""
        chunks = chunk_page(page, self.settings.chunk)
        vectors = self.embed(chunks)
        meta = PageRecord.model_validate(
            {**page.model_dump(exclude={"sections"}), "chunks": len(chunks)}
        )
        self.store.put_page(
            meta, encode_body(page.sections), chunks, vectors, self.kb.embedding_model
        )
        return len(chunks)

    def _rechunk_if_needed(self) -> None:
        key = chunk_config_key(self.kb)
        if self.store.get_meta("chunk_config") == key and not self.store.get_meta("rechunk"):
            return
        if self.store.get_meta("chunk_config") is not None or self.store.get_meta("rechunk"):
            self.progress(phase="re-chunking", force=True)
            for record, body in self.store.iter_bodies():
                page = PageDoc.model_validate(
                    {**record.model_dump(exclude={"chunks"}), "sections": decode_body(body)}
                )
                self.write_page(page)
                self.progress()
        self.store.set_meta("chunk_config", key)
        self.store.set_meta("rechunk", None)

    # -- sources --------------------------------------------------------------------------

    def _source(self, source: KbSourceConfig, reason: str | None) -> None:
        run = self._run
        assert run is not None
        result = SourceRun(source=source.id)
        run.status.sources.append(result)
        if reason is not None:
            result.state, result.detail = "skipped", reason
            return
        if self._should_stop():
            result.state, result.detail = "skipped", run.stopped
            return
        self.progress(source=source.id, unit=None, phase="listing", done=0, total=None, force=True)
        if source.kind == "bundle":
            from autoair.kb.bundle import import_bundle_source

            try:
                import_bundle_source(self, source, result)
            except (FetchError, ValueError, OSError) as exc:
                result.state, result.detail = "failed", str(exc)[:300]
                self._error(source.id, source.url, str(exc))
            return
        existing = self.store.pages(source.id)
        seen: set[str] = set()
        complete = True
        # What selects pages inside a unit: a change re-lists the unit's pages.
        scope = json.dumps(
            [
                source.include,
                source.exclude,
                source.format,
                source.layout,
                self.settings.fetch.exclude_pages,
                self.settings.fetch.include_api_reference,
                self.settings.fetch.max_pages_per_package,
            ],
            sort_keys=True,
        )
        components, versions = self.kb.source_scope(source)
        ctx = PlanContext(
            settings=self.settings,
            fetcher=self.fetcher or self.kb.new_fetcher(),
            cache_dir=self.kb.base_dir,
            warn=self.warn,
        )
        try:
            for unit in plan_units(source, ctx, components=components, versions=versions):
                result.units += 1
                if unit.note:
                    self._error(source.id, None, unit.note, "info")
                if self._should_stop():
                    complete = False
                    break
                if not self._unit(unit, existing, seen, result, scope):
                    complete = False
        except SourceError as exc:
            complete = False
            result.state, result.detail = "failed", str(exc)[:300]
            self._error(source.id, None, f"cannot list the source: {exc}")
        except Cancelled:
            result.state = "partial"
            raise
        if complete and run.stopped is None:
            removed = [pid for pid in existing if pid not in seen]
            if removed:
                result.pages_removed = self.store.delete_pages(removed)
        elif result.state == "ok":
            result.state = "partial"
            result.detail = result.detail or run.stopped or "some pages failed"
        self.progress(force=True)

    def _unit(
        self,
        unit: Unit,
        existing: dict[str, PageRecord],
        seen: set[str],
        result: SourceRun,
        scope: str = "",
    ) -> bool:
        run = self._run
        assert run is not None
        fingerprint = (
            hashlib.sha256(f"{unit.fingerprint}\0{scope}".encode()).hexdigest()
            if unit.fingerprint
            else None
        )
        previous = self.store.get_meta(f"unit:{unit.key}")
        if run.mode != "rebuild" and fingerprint and previous == fingerprint:
            ids = [pid for pid, rec in existing.items() if rec.unit == unit.key]
            seen.update(ids)
            result.pages_seen += len(ids)
            result.pages_unchanged += len(ids)
            return True
        refs = unit.pages[: max(0, run.budget)]
        if len(refs) < len(unit.pages):
            self._should_stop()
        run.budget -= len(refs)
        self.progress(unit=unit.key, phase="fetching", done=0, total=len(refs), force=True)
        ok = len(refs) == len(unit.pages)
        workers = self.settings.fetch.concurrency
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="autoair-kb-fetch"
        ) as pool:
            pending: dict[concurrent.futures.Future[Any], tuple[PageRef, str]] = {}
            queue = iter(refs)
            done = 0

            def submit() -> bool:
                ref = next(queue, None)
                if ref is None:
                    return False
                pid = page_id(unit.source, ref.url)
                prior = existing.get(pid)
                conditional = prior is not None and run.mode != "rebuild"
                etag = prior.etag if conditional and prior is not None else None
                modified = prior.last_modified if conditional and prior is not None else None
                pending[pool.submit(self._load, ref, etag, modified)] = (ref, pid)
                return True

            for _ in range(workers * 2):
                if not submit():
                    break
            while pending:
                finished, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in finished:
                    ref, pid = pending.pop(future)
                    seen.add(pid)
                    result.pages_seen += 1
                    done += 1
                    try:
                        loaded, title, sections = future.result()
                        self._store_page(
                            unit, ref, pid, existing.get(pid), loaded, title, sections, result
                        )
                    except (FetchError, FileNotFoundError, StoreError, OSError, ValueError) as exc:
                        if isinstance(exc, StoreError):
                            raise
                        ok = False
                        result.pages_failed += 1
                        self._error(unit.source, ref.url, str(exc) or type(exc).__name__)
                    if run.stopped is None:
                        submit()
                    self.progress(done=done)
                if run.stopped is not None:
                    for future in pending:
                        future.cancel()
                    ok = False
                    break
        if ok and fingerprint:
            self.store.set_meta(f"unit:{unit.key}", fingerprint)
        return ok

    def _load(
        self, ref: PageRef, etag: str | None, modified: str | None
    ) -> tuple[Loaded, str, list[Section]]:
        loaded = ref.load(etag, modified)
        if loaded.not_modified or loaded.doc is not None:
            return loaded, ref.title, []
        title, sections = parse(loaded.content, loaded.fmt, title=ref.title, resolve=loaded.resolve)
        return loaded, title, sections

    def _store_page(
        self,
        unit: Unit,
        ref: PageRef,
        pid: str,
        prior: PageRecord | None,
        loaded: Loaded,
        title: str,
        sections: list[Section],
        result: SourceRun,
    ) -> None:
        fetched = now_iso()
        if loaded.not_modified and prior is not None:
            self.store.touch_page(
                prior.model_copy(update={"fetched_at": fetched, "etag": loaded.etag or prior.etag})
            )
            result.pages_unchanged += 1
            return
        content_hash = hashlib.sha256(loaded.content.encode("utf-8", "replace")).hexdigest()
        if (
            prior is not None
            and prior.content_hash == content_hash
            and self._run is not None
            and self._run.mode != "rebuild"
        ):
            self.store.touch_page(
                prior.model_copy(
                    update={
                        "fetched_at": fetched,
                        "etag": loaded.etag,
                        "last_modified": loaded.last_modified,
                    }
                )
            )
            result.pages_unchanged += 1
            return
        page = PageDoc(
            id=pid,
            source=unit.source,
            unit=unit.key,
            component=unit.component,
            package=unit.package,
            version=unit.version,
            url=ref.url,
            title=title or ref.title,
            updated=_http_date(loaded.last_modified),
            content_hash=content_hash,
            etag=loaded.etag,
            last_modified=loaded.last_modified,
            fetched_at=fetched,
            sections=sections,
        )
        result.chunks_written += self.write_page(page)
        result.pages_changed += 1


def remove_missing(store: Any, source: str, keep: Iterable[str]) -> int:
    keep_set = set(keep)
    removed = [pid for pid in store.pages(source) if pid not in keep_set]
    return int(store.delete_pages(removed)) if removed else 0


__all__ = [
    "AlreadyRunning",
    "Cancelled",
    "Ingestor",
    "chunk_config_key",
    "decode_body",
    "encode_body",
    "remove_missing",
]
