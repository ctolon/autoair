"""Collaboration hardening: tickets are bound to the user and room and used once, live
sessions end after ``collab.session_seconds`` (the reconnect asks for a new ticket, which
re-checks DAG edit permission), and messages relayed between replicas are signed."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

pytest.importorskip("pycrdt.websocket")
websockets = pytest.importorskip("websockets")

from autoair.api.app import build_services, create_app  # noqa: E402
from autoair.auth import AuthBackend  # noqa: E402
from autoair.collab import TicketLedger, issue_ticket, verify_ticket  # noqa: E402
from autoair.collab_sync import BusMessage, DatabaseBus, RedisBus, bus_key  # noqa: E402
from autoair.iam.deps import current_principal  # noqa: E402
from autoair.iam.model import ALL_PERMISSIONS, Principal  # noqa: E402
from autoair.persistence import FileDocumentStore  # noqa: E402
from autoair.settings import Settings  # noqa: E402

pytestmark = [
    pytest.mark.airflow,
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


def _serve(app: Any) -> tuple[Any, threading.Thread, int]:
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, thread, port


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    tmp = tmp_path_factory.mktemp("collab-sec")
    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp,
        subfolder="",
        ai={"enabled": False},
        features={"mcp": False},
        collab_secret="sec-secret",
        collab={"session_seconds": 1},
    )
    app = create_app(build_services(settings, AuthBackend()))
    api = next(r.app for r in app.routes if getattr(r, "path", None) == "/api")

    async def alice() -> Principal:
        return Principal(user="alice", roles=["Admin"], permissions=set(ALL_PERMISSIONS))

    api.dependency_overrides[current_principal] = alice
    srv, thread, port = _serve(app)
    yield f"ws://127.0.0.1:{port}", f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def _ticket(http: str, dag: str = "sec_dag") -> str:
    resp = httpx2.post(f"{http}/api/collab/{dag}/ticket")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["ticket"])


def test_ticket_is_bound_to_the_user_and_room(server: tuple[str, str]) -> None:
    claims = verify_ticket(b"sec-secret", _ticket(server[1]), "sec_dag")
    assert claims is not None
    assert claims["u"] == "alice"
    assert claims["d"] == "sec_dag"
    assert claims["e"] - time.time() <= 60  # short-lived by default


def test_ticket_is_single_use(server: tuple[str, str]) -> None:
    ws, http = server
    ticket = _ticket(http)

    async def connect() -> None:
        async with websockets.connect(f"{ws}/collab/sec_dag?ticket={ticket}") as conn:
            await conn.send(b"\x01\x00")  # any frame: the handshake succeeded

    asyncio.run(connect())
    with pytest.raises(websockets.InvalidStatus) as err:
        asyncio.run(connect())
    assert err.value.response.status_code == 403


def test_live_session_ends_after_session_seconds(server: tuple[str, str]) -> None:
    ws, http = server

    async def wait_closed() -> int | None:
        async with websockets.connect(f"{ws}/collab/sec_dag?ticket={_ticket(http)}") as conn:
            started = time.monotonic()
            await asyncio.wait_for(conn.wait_closed(), 10)
            assert time.monotonic() - started < 5
            return conn.close_code

    assert asyncio.run(wait_closed()) == 4001


def test_ticket_ledger_claims_once_and_prunes(tmp_path: Path) -> None:
    store = FileDocumentStore(str(tmp_path / "tickets"))
    ledger = TicketLedger(store)
    claims = verify_ticket(b"k", issue_ticket(b"k", "d", user="u", ttl=30), "d")
    assert claims is not None
    assert ledger.claim(claims) is True
    assert ledger.claim(claims) is False
    assert TicketLedger(store).claim(claims) is False  # another replica sees the claim
    assert ledger.claim({"d": "d", "e": int(time.time()) + 30}) is False  # no nonce
    store.put("tickets-1", b"[]")  # a bucket long expired
    ledger._next_prune = 0
    ledger.claim(verify_ticket(b"k", issue_ticket(b"k", "d"), "d") or {})
    remaining = store.keys()
    assert "tickets-1" not in remaining


# --------------------------------------------------------------------------- bus signing


def test_database_bus_drops_unsigned_and_forged_rows(tmp_path: Path) -> None:
    from autoair.persistence.db import database_for
    from autoair.persistence.sql import SqlCollabLog
    from tests.db_backends import drop_tables

    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path,
        subfolder="",
        database={"backend": "sqlite", "url": f"sqlite:///{tmp_path / 'bus.db'}"},
    )
    db = database_for(settings)
    assert db is not None
    key = bus_key(b"bus-secret")

    async def scenario() -> list[BusMessage]:
        a = DatabaseBus(db, poll_seconds=0.05, retention_seconds=60, key=key)
        b = DatabaseBus(db, poll_seconds=0.05, retention_seconds=60, key=key)
        got: list[BusMessage] = []

        async def collect(msg: BusMessage) -> None:
            got.append(msg)

        async def ignore(msg: BusMessage) -> None:
            return None

        await a.start(ignore)
        await b.start(collect)
        log = SqlCollabLog(db)
        await asyncio.to_thread(log.append, "room", "intruder", "update", b"\x00" * 40)
        forged = DatabaseBus(db, poll_seconds=1, retention_seconds=60, key=bus_key(b"wrong"))
        await forged.publish("room", "update", b"\x02")
        await a.publish("room", "update", b"\x01\x02")
        deadline = time.monotonic() + 5
        while not got and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        await a.stop()
        await b.stop()
        return got

    try:
        got = asyncio.run(scenario())
    finally:
        drop_tables(settings)
    assert [(m.room, m.kind, m.payload) for m in got] == [("room", "update", b"\x01\x02")]


def test_redis_bus_drops_unsigned_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    queues: list[asyncio.Queue[Any]] = []

    class FakePubSub:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[Any] = asyncio.Queue()

        async def subscribe(self, channel: str) -> None:
            queues.append(self.queue)

        async def listen(self) -> Any:
            while True:
                yield await self.queue.get()

    class FakeRedis:
        def pubsub(self) -> FakePubSub:
            return FakePubSub()

        async def publish(self, channel: str, data: str) -> None:
            for q in queues:
                q.put_nowait({"type": "message", "data": data.encode()})

        async def aclose(self) -> None:
            return None

    fake = types.ModuleType("redis.asyncio")
    fake.from_url = lambda url: FakeRedis()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", types.ModuleType("redis"))
    monkeypatch.setitem(sys.modules, "redis.asyncio", fake)
    key = bus_key(b"s")

    async def scenario() -> list[BusMessage]:
        a = RedisBus("redis://x", "c", key=key)
        b = RedisBus("redis://x", "c", key=key)
        got: list[BusMessage] = []

        async def collect(msg: BusMessage) -> None:
            got.append(msg)

        await a.start(collect)
        await b.start(collect)
        unsigned = json.dumps({"room": "r", "origin": "evil", "kind": "update", "payload": "AA=="})
        for q in queues:
            q.put_nowait({"type": "message", "data": unsigned.encode()})
        other = RedisBus("redis://x", "c", key=bus_key(b"other"))
        await other.start(collect)
        await other.publish("r", "update", b"\x09")
        await a.publish("r", "update", b"\x07")
        await asyncio.sleep(0.1)
        await a.stop()
        await b.stop()
        await other.stop()
        return got

    got = asyncio.run(scenario())
    # b got a's message; the unsigned and the wrongly signed ones were dropped everywhere
    assert [(m.origin == "evil", m.payload) for m in got] == [(False, b"\x07")]
