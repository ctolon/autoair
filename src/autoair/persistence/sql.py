"""SQL implementations of the repositories (SQLite / PostgreSQL through SQLAlchemy Core).

Multi-replica rules followed here:

* read-modify-write of a document is a compare-and-swap on its ``version`` (retried), never
  a blind overwrite: no lost updates;
* idempotency comes from unique constraints (the same review verdict, the same memory text,
  a proposal per (dag_id, etag)) with ``ON CONFLICT`` upserts;
* leader-only maintenance (AI audit retention) takes a non-blocking advisory lock on
  PostgreSQL, so exactly one replica prunes.
"""

from __future__ import annotations

import builtins
import datetime
import hashlib
import json
import logging
import os
import socket
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import Table, and_, case, delete, func, insert, select, update

from autoair.iam.login import LoginLimiter
from autoair.persistence.db import Database
from autoair.persistence.documents import ANY, DocumentConflict, StoredDocument, check_key

if TYPE_CHECKING:
    from autoair.ai.audit_log import AiAuditRecord
    from autoair.ai.memory import MemoryItem
    from autoair.audit import AuditRecord
    from autoair.iam.proposals import Proposal, ReviewRecord

log = logging.getLogger("autoair.persistence")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _upsert(db: Database, table: Table) -> Any:
    """``INSERT`` supporting ``on_conflict_do_*`` for the configured dialect."""
    if db.dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(table)
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    return sqlite_insert(table)


def _returning(conn: Any, kind: str) -> bool:
    """Whether ``INSERT``/``UPDATE ... RETURNING`` compiles on this connection.

    SQLAlchemy 2 sets ``insert_returning`` / ``update_returning`` per dialect (SQLite from
    3.35). SQLAlchemy 1.4, the version Airflow 3.0 pins, has neither attribute and supports
    RETURNING only on PostgreSQL."""
    return bool(getattr(conn.dialect, f"{kind}_returning", conn.dialect.name == "postgresql"))


def _insert_if_absent(conn: Any, stmt: Any, column: Any) -> bool:
    """Run an ``INSERT ... ON CONFLICT DO NOTHING``; ``True`` when it inserted the row."""
    if _returning(conn, "insert"):
        # RETURNING, not rowcount: drivers disagree on rowcount for ON CONFLICT DO NOTHING.
        return conn.execute(stmt.returning(column)).first() is not None
    # Only SQLite on SQLAlchemy 1.4 gets here; sqlite3 reports 0/1 changed rows exactly.
    return bool(conn.execute(stmt).rowcount == 1)


# --------------------------------------------------------------------------- documents


class SqlDocumentStore:
    """:class:`~autoair.persistence.documents.DocumentStore` on ``<prefix>documents``."""

    def __init__(self, db: Database, collection: str) -> None:
        self.db = db
        self.collection = collection
        self.table = db.tables.documents

    def _where(self, key: str) -> Any:
        c = self.table.c
        return and_(c.collection == self.collection, c.key == check_key(key))

    def get(self, key: str) -> StoredDocument | None:
        c = self.table.c
        with self.db.connect() as conn:
            row = conn.execute(
                select(c.data, c.version, c.updated_at).where(self._where(key))
            ).first()
        if row is None:
            return None
        return StoredDocument(key, bytes(row.data), str(row.version), row.updated_at)

    def _insert(self, conn: Any, key: str, data: bytes) -> bool:
        stmt = (
            _upsert(self.db, self.table)
            .values(
                collection=self.collection,
                key=key,
                data=data,
                version=1,
                updated_at=_now_iso(),
            )
            .on_conflict_do_nothing()
        )
        return _insert_if_absent(conn, stmt, self.table.c.key)

    def put(self, key: str, data: bytes, *, expected_version: Any = ANY) -> StoredDocument:
        check_key(key)
        c = self.table.c
        now = _now_iso()
        with self.db.begin() as conn:
            if expected_version is None:
                if not self._insert(conn, key, data):
                    raise DocumentConflict(f"{key} already exists")
                return StoredDocument(key, data, "1", now)
            where = self._where(key)
            if expected_version is not ANY:
                where = and_(where, c.version == int(expected_version))
            stmt = (
                update(self.table)
                .where(where)
                .values(data=data, version=c.version + 1, updated_at=now)
            )
            version: Any = None
            if _returning(conn, "update"):
                result = conn.execute(stmt.returning(c.version)).first()
                version = None if result is None else result.version
            elif conn.execute(stmt).rowcount:
                # SQLite holds the database write lock (BEGIN IMMEDIATE) until commit, so
                # the row read back is the one just written.
                version = conn.execute(select(c.version).where(self._where(key))).scalar()
            if version is not None:
                return StoredDocument(key, data, str(version), now)
            if expected_version is not ANY:
                raise DocumentConflict(f"{key} changed (expected version {expected_version})")
            if self._insert(conn, key, data):
                return StoredDocument(key, data, "1", now)
        # Lost an insert race: the other writer's row exists now, overwrite it.
        return self.put(key, data)

    def put_if_absent(self, key: str, data: bytes) -> StoredDocument:
        check_key(key)
        with self.db.begin() as conn:
            if self._insert(conn, key, data):
                return StoredDocument(key, data, "1", _now_iso())
        found = self.get(key)
        assert found is not None
        return found

    def delete(self, key: str) -> bool:
        with self.db.begin() as conn:
            return bool(conn.execute(delete(self.table).where(self._where(key))).rowcount)

    def keys(self) -> builtins.list[str]:
        c = self.table.c
        with self.db.connect() as conn:
            rows = conn.execute(
                select(c.key).where(c.collection == self.collection).order_by(c.key)
            ).scalars()
            return [str(k) for k in rows]

    def items(self) -> builtins.list[StoredDocument]:
        c = self.table.c
        with self.db.connect() as conn:
            rows = conn.execute(
                select(c.key, c.data, c.version, c.updated_at)
                .where(c.collection == self.collection)
                .order_by(c.key)
            ).all()
        return [StoredDocument(r.key, bytes(r.data), str(r.version), r.updated_at) for r in rows]

    def update(self, key: str, fn: Callable[[bytes | None], bytes | None]) -> bytes | None:
        """Read-modify-write in one transaction holding a per-document lock (PostgreSQL
        advisory lock, SQLite write lock): concurrent replicas queue, nothing is lost."""
        c = self.table.c
        check_key(key)
        with self.db.begin() as conn:
            self.db.lock(conn, f"doc:{self.collection}:{key}")
            row = conn.execute(select(c.data).where(self._where(key))).first()
            current = bytes(row.data) if row is not None else None
            new = fn(current)
            if new is None:
                if row is not None:
                    conn.execute(delete(self.table).where(self._where(key)))
                return None
            if new == current:
                return new
            now = _now_iso()
            if row is not None:
                conn.execute(
                    update(self.table)
                    .where(self._where(key))
                    .values(data=new, version=c.version + 1, updated_at=now)
                )
            else:
                conn.execute(
                    insert(self.table).values(
                        collection=self.collection, key=key, data=new, version=1, updated_at=now
                    )
                )
            return new

    def stats(self) -> tuple[int, int]:
        """(documents, bytes) of this collection."""
        c = self.table.c
        with self.db.connect() as conn:
            row = conn.execute(
                select(func.count(), func.coalesce(func.sum(func.length(c.data)), 0)).where(
                    c.collection == self.collection
                )
            ).first()
        return (int(row[0]), int(row[1])) if row else (0, 0)


# --------------------------------------------------------------------------- audit


class SqlAuditRepository:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.table = db.tables.audit

    def append(self, record: AuditRecord) -> None:
        with self.db.begin() as conn:
            conn.execute(
                insert(self.table).values(
                    timestamp=record.timestamp,
                    action=record.action[:100],
                    user=record.user,
                    dag_id=record.dag_id,
                    details=json.dumps(record.details, default=str),
                )
            )

    def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        dag_id: str | None = None,
        user: str | None = None,
        action: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> builtins.list[AuditRecord]:
        from autoair.audit import AuditRecord

        c = self.table.c
        stmt = select(self.table)
        if dag_id is not None:
            stmt = stmt.where(c.dag_id == dag_id)
        if user is not None:
            stmt = stmt.where(c.user == user)
        if action is not None:
            stmt = stmt.where(c.action == action)
        if since is not None:
            stmt = stmt.where(c.timestamp >= since)
        if until is not None:
            stmt = stmt.where(c.timestamp < until)
        stmt = stmt.order_by(c.timestamp.desc(), c.id.desc()).limit(limit).offset(offset)
        with self.db.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        out = []
        for r in rows:
            try:
                details = json.loads(r["details"]) if r["details"] else {}
            except ValueError:
                details = {}
            out.append(
                AuditRecord(
                    timestamp=r["timestamp"],
                    action=r["action"],
                    user=r["user"],
                    dag_id=r["dag_id"],
                    details=details if isinstance(details, dict) else {},
                )
            )
        return out

    def count(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(self.table)).scalar() or 0)


# --------------------------------------------------------------------------- AI audit


class SqlAiAuditRepository:
    LOCK = "ai-audit-retention"

    def __init__(self, db: Database) -> None:
        self.db = db
        self.table = db.tables.ai_audit

    def append(self, record: AiAuditRecord, line: str) -> None:
        usage = record.usage
        with self.db.begin() as conn:
            conn.execute(
                insert(self.table).values(
                    timestamp=record.timestamp,
                    user=record.user,
                    feature=record.feature[:100],
                    provider=record.provider,
                    model=record.model,
                    outcome=(record.outcome or "ok")[:50],
                    input_tokens=usage.input_tokens
                    + usage.cache_read_tokens
                    + usage.cache_write_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd,
                    record=line,
                )
            )

    def recent(
        self, limit: int = 100, user: str | None = None, since: str | None = None
    ) -> builtins.list[AiAuditRecord]:
        from autoair.ai.audit_log import AiAuditRecord

        c = self.table.c
        stmt = select(c.record)
        if user is not None:
            stmt = stmt.where(c.user == user)
        if since is not None:
            stmt = stmt.where(c.timestamp >= since)
        stmt = stmt.order_by(c.timestamp.desc(), c.id.desc()).limit(limit)
        with self.db.connect() as conn:
            lines = conn.execute(stmt).scalars().all()
        out = []
        for line in lines:
            try:
                out.append(AiAuditRecord.model_validate_json(line))
            except ValueError:
                continue
        return out

    def prune(self, *, retention_days: int, max_total_mb: int, today: str) -> builtins.list[str]:
        """Delete whole days older than ``retention_days`` and, beyond ``max_total_mb``, the
        oldest days (never ``today``). Only the replica holding the advisory lock prunes."""
        c = self.table.c
        removed: list[str] = []
        with self.db.begin() as conn:
            if not self.db.try_lock(conn, self.LOCK):
                return []  # another replica is pruning right now
            if retention_days > 0:
                cutoff = (
                    datetime.date.fromisoformat(today) - datetime.timedelta(days=retention_days)
                ).isoformat()
                days = conn.execute(
                    select(func.substr(c.timestamp, 1, 10).label("day"))
                    .where(c.timestamp < cutoff)
                    .group_by("day")
                ).scalars()
                removed.extend(str(d) for d in days)
                conn.execute(delete(self.table).where(c.timestamp < cutoff))
            limit = max_total_mb * 1024 * 1024
            if limit > 0:
                sizes = conn.execute(
                    select(
                        func.substr(c.timestamp, 1, 10).label("day"),
                        func.sum(func.length(c.record)).label("size"),
                    )
                    .group_by("day")
                    .order_by("day")
                ).all()
                total = sum(int(s.size or 0) for s in sizes)
                for row in sizes[:-1]:  # never the newest day
                    if total <= limit or row.day >= today:
                        break
                    conn.execute(delete(self.table).where(c.timestamp.like(f"{row.day}%")))
                    removed.append(str(row.day))
                    total -= int(row.size or 0)
        return removed

    def usage(self, since: str | None = None) -> builtins.list[dict[str, Any]]:
        """Requests, tokens and cost per (user, model) since ``since``."""
        c = self.table.c
        stmt = select(
            c.user,
            c.provider,
            c.model,
            func.count().label("requests"),
            func.coalesce(func.sum(c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(c.output_tokens), 0).label("output_tokens"),
            func.sum(c.cost_usd).label("cost_usd"),
            func.sum(case((c.outcome != "ok", 1), else_=0)).label("errors"),
        ).group_by(c.user, c.provider, c.model)
        if since is not None:
            stmt = stmt.where(c.timestamp >= since)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    def count(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(self.table)).scalar() or 0)


# --------------------------------------------------------------------------- AI memory


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class SqlMemoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.table = db.tables.ai_memory

    def all(self) -> builtins.list[MemoryItem]:
        from autoair.ai.memory import MemoryItem

        c = self.table.c
        with self.db.connect() as conn:
            rows = conn.execute(select(c.item).order_by(c.created_at)).scalars().all()
        out = []
        for row in rows:
            try:
                out.append(MemoryItem.model_validate_json(row))
            except ValueError:
                continue
        return out

    def add(self, item: MemoryItem, *, max_items: int) -> MemoryItem:
        from autoair.ai.memory import MemoryError, MemoryItem

        c = self.table.c
        owner = item.owner or ""
        digest = _text_hash(item.text)
        existing_q = select(c.item).where(
            and_(c.scope == item.scope, c.owner == owner, c.text_hash == digest)
        )
        with self.db.begin() as conn:
            self.db.lock(conn, "ai-memory")  # the capacity check below is exact
            existing = conn.execute(existing_q).scalar()
            if existing is not None:
                return MemoryItem.model_validate_json(existing)
            count = conn.execute(select(func.count()).select_from(self.table)).scalar() or 0
            if count >= max_items:
                raise MemoryError(f"memory is full ({max_items} items); delete some first")
            stmt = (
                _upsert(self.db, self.table)
                .values(
                    id=item.id,
                    scope=item.scope,
                    owner=owner,
                    text_hash=digest,
                    created_at=item.created_at.isoformat(),
                    item=item.model_dump_json(),
                )
                .on_conflict_do_nothing()
            )
            inserted = _insert_if_absent(conn, stmt, self.table.c.id)
        if inserted:
            return item
        with self.db.connect() as conn:  # a concurrent add of the same text won
            existing = conn.execute(existing_q).scalar()
        if existing is None:  # pragma: no cover - id collision
            raise MemoryError("could not store the memory; try again")
        return MemoryItem.model_validate_json(existing)

    def delete(self, item_id: str) -> bool:
        with self.db.begin() as conn:
            return bool(conn.execute(delete(self.table).where(self.table.c.id == item_id)).rowcount)


# --------------------------------------------------------------------------- reviews/proposals


class SqlProposalStore:
    """:class:`~autoair.iam.proposals.ProposalStore` on ``<prefix>proposals`` and
    ``<prefix>reviews`` (same semantics: ``ttl`` expiry, newest first)."""

    def __init__(self, db: Database, *, ttl: float) -> None:
        self.db = db
        self.ttl = ttl
        self.proposals = db.tables.proposals
        self.reviews_table = db.tables.reviews

    def _cutoff(self) -> str:
        return (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=self.ttl)
        ).isoformat()

    # proposals
    def save(self, proposal: Proposal) -> Proposal:
        from autoair.iam.proposals import check_etag
        from autoair.storage.base import check_dag_id

        check_dag_id(proposal.dag_id)
        check_etag(proposal.etag)
        data = proposal.model_dump_json(by_alias=True, exclude_none=True)
        stmt = _upsert(self.db, self.proposals).values(
            dag_id=proposal.dag_id,
            etag=proposal.etag,
            timestamp=proposal.timestamp,
            author=proposal.author,
            data=data,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["dag_id", "etag"],
            set_={"timestamp": proposal.timestamp, "author": proposal.author, "data": data},
        )
        with self.db.begin() as conn:
            conn.execute(stmt)
        return proposal

    def _rows(self, where: Any) -> builtins.list[Proposal]:
        from autoair.iam.proposals import Proposal

        c = self.proposals.c
        with self.db.connect() as conn:
            rows = (
                conn.execute(select(c.data).where(and_(where, c.timestamp >= self._cutoff())))
                .scalars()
                .all()
            )
        out = []
        for row in rows:
            try:
                out.append(Proposal.model_validate_json(row))
            except ValueError:
                continue
        return sorted(out, key=lambda p: p.timestamp, reverse=True)

    def get(self, dag_id: str, etag: str) -> Proposal | None:
        from autoair.iam.proposals import check_etag
        from autoair.storage.base import check_dag_id

        c = self.proposals.c
        found = self._rows(and_(c.dag_id == check_dag_id(dag_id), c.etag == check_etag(etag)))
        return found[0] if found else None

    def pending(self, dag_id: str) -> builtins.list[Proposal]:
        from autoair.storage.base import check_dag_id

        return self._rows(self.proposals.c.dag_id == check_dag_id(dag_id))

    def all_pending(self) -> builtins.list[Proposal]:
        return self._rows(self.proposals.c.dag_id.isnot(None))

    def delete(self, dag_id: str, etag: str) -> bool:
        c = self.proposals.c
        with self.db.begin() as conn:
            return bool(
                conn.execute(
                    delete(self.proposals).where(and_(c.dag_id == dag_id, c.etag == etag))
                ).rowcount
            )

    # reviews
    def _read_reviews(self, dag_id: str, etag: str | None = None) -> builtins.list[ReviewRecord]:
        from autoair.iam.proposals import ReviewRecord

        c = self.reviews_table.c
        where = and_(c.dag_id == dag_id, c.timestamp >= self._cutoff())
        if etag is not None:
            where = and_(where, c.etag == etag)
        with self.db.connect() as conn:
            rows = conn.execute(select(c.record, c.timestamp).where(where)).all()
        out = []
        for row in rows:
            try:
                rec = ReviewRecord.model_validate_json(row.record)
            except ValueError:
                continue
            out.append(rec.model_copy(update={"timestamp": row.timestamp}))
        return out

    def reviews(self, dag_id: str, etag: str | None = None) -> builtins.list[ReviewRecord]:
        found = self._read_reviews(dag_id, etag)
        return sorted(found, key=lambda r: r.timestamp, reverse=True)

    def all_reviews(self, limit: int = 200) -> builtins.list[tuple[str, ReviewRecord]]:
        from autoair.iam.proposals import ReviewRecord

        c = self.reviews_table.c
        with self.db.connect() as conn:
            rows = conn.execute(
                select(c.dag_id, c.record, c.timestamp)
                .where(c.timestamp >= self._cutoff())
                .order_by(c.timestamp.desc())
                .limit(limit)
            ).all()
        out = []
        for row in rows:
            try:
                rec = ReviewRecord.model_validate_json(row.record)
            except ValueError:
                continue
            out.append((row.dag_id, rec.model_copy(update={"timestamp": row.timestamp})))
        return out

    def add_review(self, dag_id: str, review: ReviewRecord) -> ReviewRecord:
        from autoair.iam.proposals import MAX_REVIEWS_PER_DAG, check_etag
        from autoair.storage.base import check_dag_id

        check_dag_id(dag_id)
        check_etag(review.etag)
        c = self.reviews_table.c
        comment_hash = _text_hash(review.comment) if review.comment else ""
        stmt = _upsert(self.db, self.reviews_table).values(
            dag_id=dag_id,
            etag=review.etag,
            user=review.user or "",
            verdict=review.verdict,
            comment_hash=comment_hash,
            timestamp=review.timestamp,
            record=review.model_dump_json(),
        )
        # The same verdict again (a double submit, a retried request) refreshes its
        # timestamp: one row per (reviewer, version, verdict, comment).
        stmt = stmt.on_conflict_do_update(
            index_elements=["dag_id", "etag", "user", "verdict", "comment_hash"],
            set_={"timestamp": review.timestamp, "record": review.model_dump_json()},
        )
        with self.db.begin() as conn:
            conn.execute(stmt)
            # Bounded per DAG like the file index (oldest dropped).
            keep = (
                select(c.id)
                .where(c.dag_id == dag_id)
                .order_by(c.timestamp.desc(), c.id.desc())
                .limit(MAX_REVIEWS_PER_DAG)
                .scalar_subquery()
            )
            conn.execute(
                delete(self.reviews_table).where(and_(c.dag_id == dag_id, c.id.notin_(keep)))
            )
        return review

    def approvers(self, dag_id: str, etag: str, *, exclude: str | None) -> builtins.list[str]:
        from autoair.iam.proposals import approvers

        return approvers(self._read_reviews(dag_id), etag, exclude=exclude)


# --------------------------------------------------------------------------- login limiter


class SqlLoginLimiter(LoginLimiter):
    """:class:`~autoair.iam.login.LoginLimiter` with the failures in ``<prefix>
    login_failures``: the sliding window holds across every replica. Reservations are taken
    under a per-key advisory lock (PostgreSQL) / the write lock (SQLite)."""

    def __init__(self, db: Database, max_failures: int, window: float) -> None:
        super().__init__(max_failures, window)
        self.db = db
        self.table = db.tables.login_failures
        self._last_purge = 0.0

    def _window_wait(self, conn: Any, keys: builtins.list[str], now: float) -> float:
        c = self.table.c
        wait = 0.0
        for key in keys:
            rows = (
                conn.execute(
                    select(c.at).where(and_(c.key == key, c.at > now - self.window)).order_by(c.at)
                )
                .scalars()
                .all()
            )
            if len(rows) >= self.max_failures:
                wait = max(wait, float(rows[0]) + self.window - now)
        return wait

    def _purge(self, conn: Any, now: float) -> None:
        if now - self._last_purge < self.window:
            return
        self._last_purge = now
        conn.execute(delete(self.table).where(self.table.c.at <= now - self.window))

    def retry_after(self, keys: builtins.list[str]) -> float | None:
        with self.db.connect() as conn:
            return self._window_wait(conn, keys, time.time()) or None

    def reserve(self, keys: builtins.list[str]) -> float | tuple[float, builtins.list[str]]:
        now = time.time()
        with self.db.begin() as conn:
            for key in sorted(keys):
                self.db.lock(conn, f"login:{key}")
            wait = self._window_wait(conn, keys, now)
            if wait > 0:
                return wait
            for key in keys:
                conn.execute(insert(self.table).values(key=key, at=now))
            self._purge(conn, now)
        return now, list(keys)

    def refund(self, reservation: tuple[float, builtins.list[str]]) -> None:
        stamp, keys = reservation
        c = self.table.c
        with self.db.begin() as conn:
            for key in keys:
                row = conn.execute(
                    select(c.id).where(and_(c.key == key, c.at == stamp)).limit(1)
                ).scalar()
                if row is not None:
                    conn.execute(delete(self.table).where(c.id == row))

    def failed(self, keys: builtins.list[str]) -> None:
        now = time.time()
        with self.db.begin() as conn:
            for key in keys:
                conn.execute(insert(self.table).values(key=key, at=now))

    def succeeded(self, key: str) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(self.table).where(self.table.c.key == key))

    def __len__(self) -> int:
        c = self.table.c
        with self.db.connect() as conn:
            return int(
                conn.execute(
                    select(func.count(func.distinct(c.key))).where(c.at > time.time() - self.window)
                ).scalar()
                or 0
            )


# --------------------------------------------------------------------------- replicas


def replica_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def heartbeat(db: Database, *, version: str, mode: str, started_at: str) -> None:
    t = db.tables.replicas
    now = time.time()
    stmt = _upsert(db, t).values(
        id=replica_id()[:128],
        hostname=socket.gethostname()[:255],
        pid=os.getpid(),
        version=version[:50],
        mode=mode[:20],
        started_at=started_at,
        last_seen=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"], set_={"last_seen": now, "version": version[:50]}
    )
    with db.begin() as conn:
        conn.execute(stmt)


def replicas(db: Database, *, stale_after: float) -> builtins.list[dict[str, Any]]:
    """Replicas seen within ``stale_after * 10`` seconds (older rows are deleted)."""
    t = db.tables.replicas
    now = time.time()
    with db.begin() as conn:
        conn.execute(delete(t).where(t.c.last_seen < now - stale_after * 10))
        rows = conn.execute(select(t).order_by(t.c.started_at)).mappings().all()
    return [{**dict(r), "alive": now - float(r["last_seen"]) <= stale_after * 2.5} for r in rows]


# --------------------------------------------------------------------------- collab updates


class SqlCollabLog:
    """Append-only log of Yjs messages shared by the replicas (the fan-out channel of
    :class:`~autoair.collab_sync.DatabaseBus`)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.table = db.tables.collab_updates

    def append(self, room: str, origin: str, kind: str, payload: bytes) -> int:
        with self.db.begin() as conn:
            result = conn.execute(
                insert(self.table).values(
                    room=room[:255],
                    origin=origin[:64],
                    kind=kind,
                    payload=payload,
                    created_at=time.time(),
                )
            )
            return int(result.inserted_primary_key[0])

    def last_id(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute(select(func.max(self.table.c.id))).scalar() or 0)

    def after(
        self, last_id: int, *, limit: int = 1000
    ) -> builtins.list[tuple[int, str, str, str, bytes, float]]:
        """Rows with ``id > last_id``: (id, room, origin, kind, payload, created_at)."""
        c = self.table.c
        stmt = (
            select(c.id, c.room, c.origin, c.kind, c.payload, c.created_at)
            .where(c.id > last_id)
            .order_by(c.id)
            .limit(limit)
        )
        with self.db.connect() as conn:
            return [
                (int(r.id), r.room, r.origin, r.kind, bytes(r.payload), float(r.created_at))
                for r in conn.execute(stmt).all()
            ]

    def prune(self, older_than: float) -> int:
        with self.db.begin() as conn:
            return int(
                conn.execute(
                    delete(self.table).where(self.table.c.created_at < time.time() - older_than)
                ).rowcount
            )


__all__ = [
    "SqlAiAuditRepository",
    "SqlAuditRepository",
    "SqlCollabLog",
    "SqlDocumentStore",
    "SqlLoginLimiter",
    "SqlMemoryRepository",
    "SqlProposalStore",
    "heartbeat",
    "replica_id",
    "replicas",
]
