"""Repository contract: every state repository behaves the same on files, SQLite and
PostgreSQL (``AUTOAIR_TEST_POSTGRES_URL``), including under concurrent writers."""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from autoair.ai.audit_log import AiAuditLog, AiAuditRecord, FileAiAuditRepository
from autoair.ai.config import AiAuditSettings
from autoair.ai.memory import FileMemoryRepository, MemoryError, MemoryItem, MemoryStore
from autoair.ai.usage import Usage
from autoair.audit import AuditLog, AuditRecord, FileAuditRepository
from autoair.iam.login import LoginLimiter
from autoair.iam.proposals import Proposal, ProposalStore, ReviewRecord
from autoair.persistence import migrations
from autoair.persistence.config import DatabaseSettings
from autoair.persistence.db import (
    Database,
    DatabaseError,
    database_for,
    postgres_driver,
    resolve_url,
)
from autoair.persistence.documents import (
    ANY,
    DocumentConflict,
    FileDocumentStore,
)
from autoair.persistence.protocols import (
    AiAuditRepository,
    AuditRepository,
    DocumentStore,
    MemoryRepository,
    ProposalRepository,
)
from autoair.persistence.sql import (
    SqlAiAuditRepository,
    SqlAuditRepository,
    SqlDocumentStore,
    SqlLoginLimiter,
    SqlMemoryRepository,
    SqlProposalStore,
)
from autoair.spec import DagSpec
from tests.db_backends import BACKENDS, SQL_BACKENDS, database_settings, drop_tables


class _Settings(SimpleNamespace):
    pass


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    settings = _Settings(
        database=DatabaseSettings(**database_settings(request.param, tmp_path)),
        state_dir=tmp_path / "state",
    )
    db = database_for(settings)
    yield SimpleNamespace(kind=request.param, db=db, tmp=tmp_path, settings=settings)
    drop_tables(settings)


@pytest.fixture(params=SQL_BACKENDS)
def sql(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    settings = _Settings(
        database=DatabaseSettings(**database_settings(request.param, tmp_path)),
        state_dir=tmp_path / "state",
    )
    db = database_for(settings)
    yield db
    drop_tables(settings)


def documents(b: Any, collection: str = "things", suffix: str = ".json") -> DocumentStore:
    if b.db is None:
        return FileDocumentStore(str(b.tmp / collection), suffix=suffix)
    return SqlDocumentStore(b.db, collection)


# --------------------------------------------------------------------------- documents


def test_document_crud_and_versions(backend: Any) -> None:
    store = documents(backend)
    assert isinstance(store, DocumentStore)
    assert store.get("a") is None
    assert store.keys() == []
    first = store.put("a", b'{"v": 1}')
    got = store.get("a")
    assert got is not None
    assert got.data == b'{"v": 1}'
    assert got.version == first.version
    assert got.updated_at
    second = store.put("a", b'{"v": 2}', expected_version=got.version)
    assert second.version != got.version
    with pytest.raises(DocumentConflict):
        store.put("a", b'{"v": 3}', expected_version=got.version)  # stale version
    with pytest.raises(DocumentConflict):
        store.put("a", b"x", expected_version=None)  # must not exist
    assert store.put("b", b"new", expected_version=None).data == b"new"
    assert store.put_if_absent("b", b"other").data == b"new"
    assert store.keys() == ["a", "b"]
    deleted = store.delete("a")
    assert deleted is True
    deleted = store.delete("a")
    assert deleted is False
    assert store.keys() == ["b"]
    with pytest.raises(ValueError, match="invalid document key"):
        store.get("../escape")


def test_document_update_deletes_with_none(backend: Any) -> None:
    store = documents(backend)
    store.put("k", b"1")
    assert store.update("k", lambda old: None) is None
    assert store.get("k") is None
    assert store.update("k", lambda old: (old or b"") + b"x") == b"x"


def test_concurrent_updates_lose_nothing(backend: Any) -> None:
    """Read-modify-write from many threads (several replicas on SQL): every increment lands."""
    store = documents(backend)
    workers, rounds = 6, 15

    def bump(raw: bytes | None) -> bytes:
        count = json.loads(raw)["n"] if raw else 0
        return json.dumps({"n": count + 1}).encode()

    def work() -> None:
        # A separate store object per worker, like separate replicas.
        own = documents(backend)
        for _ in range(rounds):
            own.update("counter", bump)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    found = store.get("counter")
    assert found is not None
    assert json.loads(found.data)["n"] == workers * rounds


def test_binary_documents(backend: Any) -> None:
    store = documents(backend, "icons", suffix=".png")
    png = bytes(range(256)) * 4
    store.put("logo", png)
    found = store.get("logo")
    assert found is not None
    assert found.data == png
    assert ANY is not None


# --------------------------------------------------------------------------- audit


def audit_repo(b: Any) -> AuditRepository:
    return FileAuditRepository(str(b.tmp / "audit")) if b.db is None else SqlAuditRepository(b.db)


def test_audit_append_and_filters(backend: Any) -> None:
    repo = audit_repo(backend)
    assert isinstance(repo, AuditRepository)
    log = AuditLog(repository=repo)
    base = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
    for i in range(12):
        repo.append(
            AuditRecord(
                timestamp=(base + datetime.timedelta(days=i % 3, minutes=i)).isoformat(),
                action="deploy" if i % 2 else "delete",
                user=f"u{i % 3}",
                dag_id=f"dag{i % 4}",
                details={"i": i},
            )
        )
    log.record("template_save", user="alice", template="t1")
    everything = repo.query(limit=100)
    assert len(everything) == 13
    assert [r.timestamp for r in everything] == sorted(
        (r.timestamp for r in everything), reverse=True
    )
    assert everything[0].action == "template_save"
    assert {r.user for r in repo.query(user="u1", limit=100)} == {"u1"}
    assert {r.action for r in repo.query(action="deploy", limit=100)} == {"deploy"}
    assert {r.dag_id for r in repo.query(dag_id="dag2", limit=100)} == {"dag2"}
    day2 = repo.query(
        since="2026-10-02T00:00:00+00:00", until="2026-10-03T00:00:00+00:00", limit=99
    )
    assert day2
    assert all(r.timestamp.startswith("2026-10-02") for r in day2)
    page1 = repo.query(limit=5)
    page2 = repo.query(limit=5, offset=5)
    assert [r.timestamp for r in page1 + page2] == [r.timestamp for r in everything[:10]]
    assert log.tail(3, dag_id="dag1")[0].dag_id == "dag1"


# --------------------------------------------------------------------------- AI audit


def ai_repo(b: Any) -> AiAuditRepository:
    if b.db is None:
        return FileAiAuditRepository(str(b.tmp / "ai-audit"))
    return SqlAiAuditRepository(b.db)


def _ai(
    ts: str, user: str, model: str, tokens: int, cost: float | None, outcome: str = "ok"
) -> Any:
    return AiAuditRecord(
        timestamp=ts,
        user=user,
        feature="explain",
        provider="anthropic",
        model=model,
        usage=Usage(requests=1, input_tokens=tokens, output_tokens=tokens // 2, cost_usd=cost),
        outcome=outcome,
    )


def test_ai_audit_recent_and_retention(backend: Any) -> None:
    repo = ai_repo(backend)
    assert isinstance(repo, AiAuditRepository)
    log = AiAuditLog(
        AiAuditSettings(retention_days=7), str(backend.tmp / "unused"), repository=repo
    )
    today = datetime.date(2026, 10, 15)
    for offset in (0, 1, 3, 10, 30):
        day = today - datetime.timedelta(days=offset)
        rec = _ai(f"{day.isoformat()}T10:00:00+00:00", f"user{offset}", "m", 100, 0.01)
        repo.append(rec, rec.model_dump_json(exclude_none=True))
    assert len(log.recent(100)) == 5
    assert [r.user for r in log.recent(100, user="user3")] == ["user3"]
    removed = log.prune(today.isoformat())
    assert sorted(Path(p).name for p in removed) == ["2026-09-15", "2026-10-05"]
    assert {r.user for r in log.recent(100)} == {"user0", "user1", "user3"}
    since = log.recent(100, None, "2026-10-14T00:00:00+00:00")
    assert {r.user for r in since} == {"user0", "user1"}


def test_ai_audit_usage_on_sql(sql: Database) -> None:
    from autoair.admin import aggregate_usage, usage_rows

    repo = SqlAiAuditRepository(sql)
    records = [
        _ai("2026-10-15T10:00:00+00:00", "alice", "opus", 1000, 0.5),
        _ai("2026-10-15T11:00:00+00:00", "alice", "opus", 500, 0.25),
        _ai("2026-10-15T12:00:00+00:00", "bob", "haiku", 10, None, outcome="error"),
    ]
    for r in records:
        repo.append(r, r.model_dump_json())
    sql_rows = {(r.user, r.model): r for r in usage_rows(repo.usage(None))}
    py_rows = {(r.user, r.model): r for r in aggregate_usage(records)}
    assert sql_rows.keys() == py_rows.keys()
    for key, row in py_rows.items():
        other = sql_rows[key]
        assert (other.requests, other.input_tokens, other.output_tokens, other.errors) == (
            row.requests,
            row.input_tokens,
            row.output_tokens,
            row.errors,
        )
        assert (other.cost_usd or 0) == pytest.approx(row.cost_usd or 0)
    assert sql_rows[("alice", "opus")].cost_usd == pytest.approx(0.75)


def test_ai_audit_retention_is_leader_only_on_postgres(sql: Database) -> None:
    """Only the replica holding the advisory lock prunes (SQLite: the write lock)."""
    repo = SqlAiAuditRepository(sql)
    old = _ai("2026-01-01T10:00:00+00:00", "x", "m", 1, None)
    repo.append(old, old.model_dump_json())
    if sql.dialect == "postgresql":
        with sql.begin() as conn:
            sql.lock(conn, repo.LOCK)  # another replica holds the lock
            assert repo.prune(retention_days=7, max_total_mb=0, today="2026-10-15") == []
    assert repo.prune(retention_days=7, max_total_mb=0, today="2026-10-15") == ["2026-01-01"]


# --------------------------------------------------------------------------- AI memory


def memory_repo(b: Any) -> MemoryRepository:
    if b.db is None:
        return FileMemoryRepository(str(b.tmp / "mem"))
    return SqlMemoryRepository(b.db)


def _item(text: str, scope: str = "global", owner: str | None = None) -> MemoryItem:
    import uuid

    return MemoryItem(
        id=uuid.uuid4().hex[:12],
        scope=scope,  # type: ignore[arg-type]
        owner=owner,
        text=text,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )


def test_memory_add_is_idempotent_and_bounded(backend: Any) -> None:
    repo = memory_repo(backend)
    assert isinstance(repo, MemoryRepository)
    first = repo.add(_item("owner is data-eng@"), max_items=3)
    again = repo.add(_item("owner is data-eng@"), max_items=3)
    assert again.id == first.id
    repo.add(_item("owner is data-eng@", "team", "core"), max_items=3)  # another scope
    repo.add(_item("prefer deferrable sensors"), max_items=3)
    with pytest.raises(MemoryError, match="full"):
        repo.add(_item("one too many"), max_items=3)
    assert len(repo.all()) == 3
    deleted = repo.delete(first.id)
    assert deleted is True
    deleted = repo.delete(first.id)
    assert deleted is False
    store = MemoryStore(repository=repo)
    items, total = store.list(visible_to=("core", None))
    assert total == 2
    assert {i.scope for i in items} == {"global", "team"}


def test_memory_concurrent_adds_of_the_same_text(backend: Any) -> None:
    repo = memory_repo(backend)
    ids: list[str] = []

    def work() -> None:
        ids.append(memory_repo(backend).add(_item("same text"), max_items=100).id)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1
    assert len(repo.all()) == 1


# --------------------------------------------------------------------------- proposals


def proposal_repo(b: Any, ttl: float = 3600) -> ProposalRepository:
    if b.db is None:
        return ProposalStore(str(b.tmp / "proposals"), ttl=ttl)
    return SqlProposalStore(b.db, ttl=ttl)


def _proposal(etag: str, ts: str | None = None) -> Proposal:
    spec = DagSpec.model_validate({"dag_id": "etl", "nodes": [], "edges": []})
    return Proposal(
        dag_id="etl",
        etag=etag,
        spec=spec,
        author="alice",
        timestamp=ts or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        message="please",
    )


def test_proposals_and_reviews(backend: Any) -> None:
    store = proposal_repo(backend)
    assert isinstance(store, ProposalRepository)
    store.save(_proposal("v1"))
    store.save(_proposal("v1"))  # saving again is idempotent
    store.save(_proposal("v2"))
    assert sorted(p.etag for p in store.pending("etl")) == ["v1", "v2"]
    got = store.get("etl", "v1")
    assert got is not None
    assert got.message == "please"
    assert store.get("etl", "nope") is None
    now = datetime.datetime.now(datetime.timezone.utc)
    store.add_review(
        "etl", ReviewRecord(timestamp=now.isoformat(), user="bob", etag="v1", verdict="approve")
    )
    assert store.approvers("etl", "v1", exclude="alice") == ["bob"]
    assert store.approvers("etl", "v1", exclude="bob") == []
    later = (now + datetime.timedelta(seconds=1)).isoformat()
    store.add_review(
        "etl",
        ReviewRecord(timestamp=later, user="bob", etag="v1", verdict="request_changes"),
    )
    assert store.approvers("etl", "v1", exclude="alice") == []  # latest verdict wins
    assert [r.verdict for r in store.reviews("etl", "v1")] == ["request_changes", "approve"]
    deleted = store.delete("etl", "v1")
    assert deleted is True
    deleted = store.delete("etl", "v1")
    assert deleted is False
    assert [p.etag for p in store.pending("etl")] == ["v2"]


def test_repeated_approval_is_idempotent_on_sql(sql: Database) -> None:
    store = SqlProposalStore(sql, ttl=3600)
    now = datetime.datetime.now(datetime.timezone.utc)
    for i in range(3):  # a double-click / retried request
        ts = (now + datetime.timedelta(milliseconds=i)).isoformat()
        store.add_review(
            "etl", ReviewRecord(timestamp=ts, user="bob", etag="v1", verdict="approve")
        )
    reviews = store.reviews("etl")
    assert len(reviews) == 1
    assert reviews[0].timestamp == (now + datetime.timedelta(milliseconds=2)).isoformat()


def test_expired_proposals_are_ignored(backend: Any) -> None:
    store = proposal_repo(backend, ttl=60)
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    store.save(_proposal("old", ts=old))
    assert store.get("etl", "old") is None
    assert store.pending("etl") == []


# --------------------------------------------------------------------------- login limiter


def test_shared_login_limiter_holds_across_replicas(sql: Database) -> None:
    a = SqlLoginLimiter(sql, max_failures=3, window=60)
    b = SqlLoginLimiter(sql, max_failures=3, window=60)  # another replica
    assert isinstance(a, LoginLimiter)
    keys = ["ip:1.2.3.4", "user:x"]
    first = a.reserve(keys)
    assert isinstance(first, tuple)
    b.failed(keys)
    second = b.reserve(keys)
    assert isinstance(second, tuple)
    blocked = a.reserve(keys)
    assert isinstance(blocked, float)
    assert blocked > 0
    assert (b.retry_after(keys) or 0) > 0
    a.refund(second)  # an inconclusive attempt is given back
    assert isinstance(b.reserve(keys), tuple)
    a.succeeded("user:x")
    a.succeeded("ip:1.2.3.4")
    assert b.retry_after(keys) is None
    assert len(a) == 0


# --------------------------------------------------------------------------- database


def test_migrations_are_idempotent(sql: Database) -> None:
    assert migrations.current(sql) == migrations.HEAD
    assert migrations.upgrade(sql) == []
    assert migrations.pending(sql) == []
    assert [h["version"] for h in migrations.history(sql)] == [
        m.version for m in migrations.MIGRATIONS
    ]
    with pytest.raises(migrations.MigrationError):
        migrations.upgrade(sql, migrations.HEAD + 1)


def test_shared_secret_is_created_once(sql: Database) -> None:
    first = sql.shared_secret("k")
    assert len(first) == 32
    assert sql.shared_secret("k") == first


def test_sqlite_without_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLAlchemy 1.4 (Airflow 3.0) cannot compile RETURNING for SQLite: the rowcount and
    read-back path must keep the same contract (CAS versions, insert-if-absent, memory)."""
    from autoair.persistence import sql as sql_module

    monkeypatch.setattr(sql_module, "_returning", lambda _conn, _kind: False)
    settings = _Settings(
        database=DatabaseSettings(**database_settings("sqlite", tmp_path)),
        state_dir=tmp_path / "state",
    )
    b = SimpleNamespace(kind="sqlite", db=database_for(settings), tmp=tmp_path, settings=settings)
    try:
        test_document_crud_and_versions(b)
        test_concurrent_updates_lose_nothing(b)
        test_memory_add_is_idempotent_and_bounded(b)
        test_shared_secret_is_created_once(b.db)
    finally:
        drop_tables(settings)


def test_url_resolution_and_masking(tmp_path: Path) -> None:
    pg = DatabaseSettings(
        backend="postgresql", url="postgresql://autoair@db:5432/autoair", password="s3cret"
    )
    url = resolve_url(pg)
    assert url.startswith(f"{postgres_driver()}://autoair:s3cret@db")
    from autoair.persistence.db import masked_url

    assert "s3cret" not in masked_url(url)
    with pytest.raises(DatabaseError, match="sqlite URL"):
        resolve_url(DatabaseSettings(backend="postgresql", url="sqlite:///x.db"))
    with pytest.raises(DatabaseError, match=r"database\.url"):
        resolve_url(DatabaseSettings(backend="postgresql"))
    default = resolve_url(DatabaseSettings(backend="sqlite"), tmp_path / "a.db")
    assert default == f"sqlite:///{tmp_path / 'a.db'}"


def test_postgres_driver_follows_the_installed_sqlalchemy() -> None:
    import sqlalchemy
    from sqlalchemy.engine import make_url

    from autoair.persistence.db import with_postgres_driver

    sa2 = int(sqlalchemy.__version__.split(".")[0]) >= 2
    # SQLAlchemy 1.4 (Airflow 3.0) has no psycopg 3 dialect: the documented
    # postgresql+psycopg:// URL falls back to psycopg2 there.
    expected = "postgresql+psycopg" if sa2 else "postgresql+psycopg2"
    assert postgres_driver() == expected
    for raw in ("postgresql://h/db", "postgres://h/db", "postgresql+psycopg://h/db"):
        assert with_postgres_driver(make_url(raw)).drivername == expected
    # An explicitly chosen other driver is kept.
    assert with_postgres_driver(make_url("postgresql+pg8000://h/db")).drivername == (
        "postgresql+pg8000"
    )


def test_migrate_on_start_off_refuses_an_old_schema(tmp_path: Path) -> None:
    settings = _Settings(
        database=DatabaseSettings(
            backend="sqlite", url=f"sqlite:///{tmp_path / 'x.db'}", migrate_on_start=False
        ),
        state_dir=tmp_path,
    )
    with pytest.raises(DatabaseError, match="autoair db upgrade"):
        database_for(settings)
    migrations.upgrade(Database(settings.database))
    assert database_for(settings) is not None
    drop_tables(settings)
