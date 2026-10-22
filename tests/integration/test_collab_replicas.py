"""Co-editing across replicas: two (three) AutoAir apps sharing one SQLite / PostgreSQL
database serve the same room; edits made on one replica reach clients of the other, a
replica joining late catches up, and room snapshots survive the replicas."""

from __future__ import annotations

import asyncio
import contextlib
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
pytest.importorskip("sqlalchemy")
websockets = pytest.importorskip("websockets")

from pycrdt import Doc, Map, Provider  # noqa: E402

from autoair.api.app import build_services, create_app  # noqa: E402
from autoair.auth import AuthBackend  # noqa: E402
from autoair.collab import CollabSettings  # noqa: E402
from autoair.collab_sync import (  # noqa: E402
    BusMessage,
    LocalBus,
    LocalHub,
    RedisBus,
    RoomSync,
    build_sync,
)
from autoair.settings import Settings  # noqa: E402
from tests.db_backends import PG_URL, SQL_BACKENDS, database_settings, drop_tables  # noqa: E402
from tests.integration.test_collab import WsChannel  # noqa: E402

pytestmark = [
    pytest.mark.airflow,
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

ROOM = "/collab/shared_dag"


class Server:
    def __init__(self, settings: Settings) -> None:
        import uvicorn

        self.services = build_services(settings, AuthBackend())
        app = create_app(self.services)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.ws = f"ws://127.0.0.1:{port}"
        self.http = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)

    def ticket(self) -> str:
        resp = httpx2.post(f"{self.http}/api/collab/shared_dag/ticket")
        assert resp.status_code == 200, resp.text
        return str(resp.json()["ticket"])

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(params=SQL_BACKENDS)
def cluster(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        features={"mcp": False},
        collab_secret="cluster-secret",
        iam={"anonymous_roles": ["Admin"]},  # AuthBackend(): anonymous callers edit
        database=database_settings(request.param, tmp_path),
        collab={
            "poll_seconds": 0.05,
            "snapshot_seconds": 0.2,
            "join_timeout": 3,
            "snapshot_ttl_seconds": 60,
        },
    )
    servers: list[Server] = []

    def start() -> Server:
        server = Server(settings)
        servers.append(server)
        return server

    yield types.SimpleNamespace(start=start, settings=settings, kind=request.param)
    for server in servers:
        server.stop()
    drop_tables(settings)


async def _wait(predicate: Any, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _client(server: Server) -> Any:
    ticket = server.ticket()
    async with websockets.connect(f"{server.ws}{ROOM}?ticket={ticket}") as ws:
        doc = Doc()
        nodes = doc.get("nodes", type=Map)
        async with Provider(doc, WsChannel(ws, ROOM)):
            yield doc, nodes, ws


def test_two_replicas_converge_and_late_joiners_catch_up(cluster: Any) -> None:
    one = cluster.start()
    two = cluster.start()
    assert one.services.database is not None

    async def scenario() -> dict[str, Any]:
        async with _client(one) as (_, nodes_a, _), _client(two) as (_, nodes_b, _):
            nodes_a["n1"] = json.dumps({"task_id": "extract"})
            await _wait(lambda: "n1" in nodes_b)
            nodes_b["n2"] = json.dumps({"task_id": "load"})
            await _wait(lambda: "n2" in nodes_a)
            # concurrent edits of the same node converge to one value everywhere
            nodes_a["n1"] = json.dumps({"task_id": "from_a"})
            nodes_b["n1"] = json.dumps({"task_id": "from_b"})
            await _wait(lambda: nodes_a.get("n1") == nodes_b.get("n1"))
            await asyncio.sleep(0.5)
            assert nodes_a.get("n1") == nodes_b.get("n1")
            # a third replica opening the room catches up before serving its client
            three = cluster.start()
            async with _client(three) as (_, nodes_c, _):
                await _wait(lambda: {"n1", "n2"} <= set(nodes_c.keys()), timeout=5)
                result = dict(nodes_c)
            return {"a": dict(nodes_a), "b": dict(nodes_b), "c": result}

    result = asyncio.run(scenario())
    assert result["a"] == result["b"] == result["c"]
    assert json.loads(result["a"]["n2"]) == {"task_id": "load"}


def test_room_snapshots_survive_the_replicas(cluster: Any) -> None:
    from autoair.persistence.db import database_for
    from autoair.persistence.sql import SqlDocumentStore

    one = cluster.start()

    async def edit() -> None:
        async with _client(one) as (_, nodes, _):
            nodes["kept"] = json.dumps({"task_id": "persisted"})
            await asyncio.sleep(0.6)  # > snapshot_seconds

    asyncio.run(edit())
    db = database_for(cluster.settings)
    assert db is not None
    snapshots = SqlDocumentStore(db, "collab_rooms")
    deadline = time.monotonic() + 5
    while snapshots.get("shared_dag") is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert snapshots.get("shared_dag") is not None
    one.stop()

    fresh = cluster.start()  # e.g. after a rolling restart

    async def reopen() -> dict[str, Any]:
        async with _client(fresh) as (_, nodes, _):
            await _wait(lambda: "kept" in nodes, timeout=5)
            return dict(nodes)

    assert json.loads(asyncio.run(reopen())["kept"]) == {"task_id": "persisted"}


def test_presence_is_relayed_between_replicas(cluster: Any) -> None:
    """Awareness (presence, cursors, selections) sent to one replica reaches clients of
    another."""
    from pycrdt import Awareness, create_awareness_message

    one = cluster.start()
    two = cluster.start()

    async def scenario() -> bool:
        ticket_b = two.ticket()
        async with (
            _client(one) as (doc_a, _, ws_a),
            websockets.connect(f"{two.ws}{ROOM}?ticket={ticket_b}") as ws_b,
        ):
            awareness = Awareness(doc_a)
            awareness.set_local_state({"user": {"name": "Alice Doe"}, "cursor": {"x": 1, "y": 2}})
            message = create_awareness_message(awareness.encode_awareness_update([doc_a.client_id]))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                await ws_a.send(message)
                try:
                    raw = await asyncio.wait_for(ws_b.recv(), 0.5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, bytes) and raw[:1] == b"\x01" and b"Alice Doe" in raw:
                    return True
            return False

    assert asyncio.run(scenario())


# --------------------------------------------------------------------------- unit level


def test_build_sync_modes(tmp_path: Path) -> None:
    from autoair.persistence.db import database_for

    plain = Settings(dags_folder=tmp_path, subfolder="")  # type: ignore[call-arg]
    assert build_sync(plain, None, secret=b"k") is None  # default: rooms stay in memory
    sqlite = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path,
        subfolder="",
        database={"backend": "sqlite", "url": f"sqlite:///{tmp_path / 's.db'}"},
    )
    db = database_for(sqlite)
    sync = build_sync(sqlite, db, secret=b"k")
    assert sync is not None
    assert type(sync.bus).__name__ == "DatabaseBus"
    forced_memory = sqlite.model_copy(update={"collab": CollabSettings(sync="memory")})
    assert build_sync(forced_memory, db, secret=b"k") is None
    redis = plain.model_copy(update={"collab": CollabSettings(redis_url="redis://r:6379/0")})
    sync = build_sync(redis, None, secret=b"k")
    assert sync is not None
    assert isinstance(sync.bus, RedisBus)
    with pytest.raises(ValueError, match="SQL"):
        build_sync(
            plain.model_copy(update={"collab": CollabSettings(sync="database")}),
            None,
            secret=b"k",
        )
    drop_tables(sqlite)


def test_local_hub_fans_out_to_other_buses() -> None:
    async def scenario() -> list[BusMessage]:
        hub = LocalHub()
        a, b, c = LocalBus(hub), LocalBus(hub), LocalBus(hub)
        got: list[BusMessage] = []

        async def collect(msg: BusMessage) -> None:
            got.append(msg)

        async def ignore(msg: BusMessage) -> None:
            raise AssertionError("a bus never receives its own messages")

        await a.start(ignore)
        await b.start(collect)
        await c.start(collect)
        await a.publish("room", "update", b"\x00\x01")
        await asyncio.sleep(0.05)
        await c.stop()
        await a.publish("room", "awareness", b"\x01")
        await asyncio.sleep(0.05)
        return got

    got = asyncio.run(scenario())
    assert [(m.kind, m.payload) for m in got] == [
        ("update", b"\x00\x01"),
        ("update", b"\x00\x01"),
        ("awareness", b"\x01"),
    ]


def test_room_sync_applies_remote_updates_without_echo() -> None:
    """A remote update is applied to the local room but never re-published."""

    async def scenario() -> tuple[list[tuple[str, str]], dict[str, Any]]:
        hub = LocalHub()
        spy = LocalBus(hub)
        seen: list[tuple[str, str]] = []

        async def record(msg: BusMessage) -> None:
            seen.append((msg.room, msg.kind))

        await spy.start(record)
        sync = RoomSync(CollabSettings(join_timeout=0), bus=LocalBus(hub))
        room = types.SimpleNamespace(ydoc=Doc(), clients=set(), on_message=None)
        server = types.SimpleNamespace(rooms={"/collab/x": room})
        await sync.start(server)  # type: ignore[arg-type]
        await sync.prepare("/collab/x", room)  # type: ignore[arg-type]
        source = Doc()
        source.get("nodes", type=Map)["a"] = "1"
        await sync._receive(BusMessage("x", spy.origin, "update", source.get_update()))
        await asyncio.sleep(0.05)
        assert seen == []  # applying a remote update publishes nothing
        room.ydoc.get("nodes", type=Map)["b"] = "2"  # a local client edit
        await asyncio.sleep(0.05)
        await sync.stop()
        return seen, dict(room.ydoc.get("nodes", type=Map))

    seen, nodes = asyncio.run(scenario())
    assert nodes == {"a": "1", "b": "2"}
    assert seen == [("x", "update")]


def test_redis_bus_with_a_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """RedisBus speaks pub/sub through redis.asyncio (optional extra): exercised with a fake."""
    channels: dict[str, list[asyncio.Queue[Any]]] = {}

    class FakePubSub:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[Any] = asyncio.Queue()

        async def subscribe(self, channel: str) -> None:
            channels.setdefault(channel, []).append(self.queue)

        async def listen(self) -> Any:
            yield {"type": "subscribe", "data": 1}
            while True:
                yield await self.queue.get()

    class FakeRedis:
        def pubsub(self) -> FakePubSub:
            return FakePubSub()

        async def publish(self, channel: str, data: str) -> None:
            for q in channels.get(channel, []):
                q.put_nowait({"type": "message", "data": data.encode()})

        async def aclose(self) -> None:
            return None

    fake = types.ModuleType("redis.asyncio")
    fake.from_url = lambda url: FakeRedis()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", types.ModuleType("redis"))
    monkeypatch.setitem(sys.modules, "redis.asyncio", fake)

    async def scenario() -> list[BusMessage]:
        a = RedisBus("redis://x", "chan", key=b"k")
        b = RedisBus("redis://x", "chan", key=b"k")
        got: list[BusMessage] = []

        async def collect(msg: BusMessage) -> None:
            got.append(msg)

        await a.start(collect)
        await b.start(collect)
        await a.publish("room", "update", b"\xff\x00")
        await asyncio.sleep(0.05)
        await a.stop()
        await b.stop()
        return got

    got = asyncio.run(scenario())
    assert len(got) == 1  # b got it; a ignored its own message
    assert (got[0].room, got[0].kind, got[0].payload) == ("room", "update", b"\xff\x00")


@pytest.mark.postgres
@pytest.mark.skipif(not PG_URL, reason="AUTOAIR_TEST_POSTGRES_URL is not set")
def test_postgres_bus_is_woken_by_notify(tmp_path: Path) -> None:
    """With a 200 s fallback poll, delivery within seconds proves LISTEN/NOTIFY works."""
    from autoair.collab_sync import DatabaseBus
    from autoair.persistence.db import database_for

    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path, subfolder="", database=database_settings("postgresql", tmp_path)
    )
    db = database_for(settings)
    assert db is not None

    async def scenario() -> list[BusMessage]:
        a = DatabaseBus(db, poll_seconds=10, retention_seconds=60, key=b"k")
        b = DatabaseBus(db, poll_seconds=10, retention_seconds=60, key=b"k")
        got: list[BusMessage] = []

        async def collect(msg: BusMessage) -> None:
            got.append(msg)

        async def ignore(msg: BusMessage) -> None:
            return None

        await a.start(ignore)
        await b.start(collect)
        await asyncio.sleep(1.0)  # LISTEN connection established
        await a.publish("room", "update", b"\x01\x02")
        await _wait(lambda: bool(got), timeout=5)
        await a.stop()
        await b.stop()
        return got

    try:
        got = asyncio.run(scenario())
    finally:
        drop_tables(settings)
    assert [(m.room, m.payload) for m in got] == [("room", b"\x01\x02")]
