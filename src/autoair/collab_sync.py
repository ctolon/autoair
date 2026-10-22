"""Share live-editing rooms between AutoAir replicas (``collab.sync``).

By default (``memory``) a room lives in the process its clients are connected to, so
several replicas need sticky sessions (every editor of a DAG on the same replica). A
:class:`RoomSync` removes that constraint:

* **fan-out** - every Yjs update and awareness (presence, cursors) message a replica
  receives from its clients is published on a :class:`Bus`; the other replicas apply it to
  their copy of the room and forward it to their clients. Buses: PostgreSQL
  ``LISTEN/NOTIFY`` (+ a relay table), a polled table on SQLite, Redis pub/sub, or an
  in-process hub (tests, several apps in one process);
* **join** - a replica opening a room it does not hold asks the others for the room's state
  (``sync-request``) and waits up to ``collab.join_timeout`` before serving the client, so a
  newcomer never sees an empty room and re-seeds it from a stale spec;
* **snapshots** - changed rooms are saved (merged, never overwritten) to the database every
  ``collab.snapshot_seconds`` and when they close; a snapshot younger than
  ``collab.snapshot_ttl_seconds`` is restored when the room opens again (e.g. after a rolling
  restart). Older snapshots are ignored: the stored spec is the source of truth.

Every relayed message is signed (HMAC-SHA256 with a key derived from the collaboration
ticket key, :func:`bus_key`): a message written to the relay table or Redis channel by
anyone else (a database or Redis user without AutoAir's key) is dropped, so it cannot
inject edits into a room.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import hmac
import importlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from autoair.collab import CollabSettings, room_of

if TYPE_CHECKING:
    from pycrdt.websocket import WebsocketServer, YRoom

    from autoair.persistence.db import Database
    from autoair.persistence.documents import DocumentStore

log = logging.getLogger("autoair.collab")

AWARENESS = 1  # y-protocols message type (pycrdt.YMessageType.AWARENESS)
GRACE_SECONDS = 10.0  # relay rows may commit out of id order within this window
PRUNE_EVERY = 60.0
MAC_SIZE = 32  # bytes of an HMAC-SHA256 signature


@dataclass(frozen=True)
class BusMessage:
    room: str
    origin: str
    kind: str  # update | awareness | sync-request
    payload: bytes


Deliver = Callable[[BusMessage], Coroutine[Any, Any, None]]


class Bus(Protocol):
    origin: str

    async def start(self, deliver: Deliver) -> None: ...
    async def publish(self, room: str, kind: str, payload: bytes) -> None: ...
    async def stop(self) -> None: ...


# --------------------------------------------------------------------------- buses


class LocalHub:
    """In-process fan-out between several :class:`LocalBus` (apps in one process, tests)."""

    def __init__(self) -> None:
        self.buses: list[LocalBus] = []


class LocalBus:
    def __init__(self, hub: LocalHub) -> None:
        self.hub = hub
        self.origin = uuid.uuid4().hex
        self._deliver: Deliver | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self, deliver: Deliver) -> None:
        self._deliver = deliver
        self._loop = asyncio.get_running_loop()
        self.hub.buses.append(self)

    async def publish(self, room: str, kind: str, payload: bytes) -> None:
        msg = BusMessage(room, self.origin, kind, payload)
        for bus in list(self.hub.buses):
            if bus is self or bus._deliver is None or bus._loop is None:
                continue
            # Each app may run its own event loop (thread): deliver on the receiver's loop.
            asyncio.run_coroutine_threadsafe(bus._deliver(msg), bus._loop)

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.hub.buses.remove(self)


def bus_key(secret: bytes) -> bytes:
    """The signing key of relayed messages, derived from the ticket key (the same on every
    replica) so that a ticket can never pass as a message signature or the reverse."""
    return hmac.new(secret, b"autoair-collab-bus", hashlib.sha256).digest()


def sign(key: bytes, room: str, origin: str, kind: str, payload: bytes) -> bytes:
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in (room.encode(), origin.encode(), kind.encode(), payload):
        mac.update(len(part).to_bytes(8, "big"))  # length-prefixed: no ambiguous joins
        mac.update(part)
    return mac.digest()


def _authentic(key: bytes, msg: BusMessage, signature: bytes) -> bool:
    expected = sign(key, msg.room, msg.origin, msg.kind, msg.payload)
    if hmac.compare_digest(signature, expected):
        return True
    log.warning(
        "collab: dropped a relayed message with a missing or wrong signature (room %s); "
        "every replica needs the same AUTOAIR_COLLAB_SECRET",
        msg.room[:64],
    )
    return False


def _encode(msg: BusMessage, key: bytes) -> str:
    return json.dumps(
        {
            "room": msg.room,
            "origin": msg.origin,
            "kind": msg.kind,
            "payload": base64.b64encode(msg.payload).decode(),
            "sig": base64.b64encode(
                sign(key, msg.room, msg.origin, msg.kind, msg.payload)
            ).decode(),
        }
    )


def _decode(raw: str | bytes, key: bytes) -> BusMessage | None:
    try:
        data = json.loads(raw)
        msg = BusMessage(
            str(data["room"]),
            str(data["origin"]),
            str(data["kind"]),
            base64.b64decode(data["payload"]),
        )
        signature = base64.b64decode(data.get("sig") or "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return msg if _authentic(key, msg, signature) else None


class RedisBus:
    """Redis pub/sub (``collab.redis_url``, ``pip install 'autoair[collab-redis]'``)."""

    def __init__(self, url: str, channel: str, *, key: bytes) -> None:
        self.url = url
        self.channel = channel
        self.key = key
        self.origin = uuid.uuid4().hex
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, deliver: Deliver) -> None:
        redis = importlib.import_module("redis.asyncio")  # optional: autoair[collab-redis]
        self._client = redis.from_url(self.url)
        pubsub = self._client.pubsub()
        await pubsub.subscribe(self.channel)

        async def run() -> None:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                msg = _decode(message.get("data") or b"", self.key)
                if msg is not None and msg.origin != self.origin:
                    try:
                        await deliver(msg)
                    except Exception:
                        log.warning("collab: cannot apply a relayed message", exc_info=True)

        self._task = asyncio.get_running_loop().create_task(run())

    async def publish(self, room: str, kind: str, payload: bytes) -> None:
        await self._client.publish(
            self.channel, _encode(BusMessage(room, self.origin, kind, payload), self.key)
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()


class DatabaseBus:
    """Relay table ``<prefix>collab_updates`` polled every ``poll_seconds``; on PostgreSQL a
    ``NOTIFY`` wakes the readers at once (the poll is then only a fallback)."""

    def __init__(
        self, db: Database, *, poll_seconds: float, retention_seconds: float, key: bytes
    ) -> None:
        from autoair.persistence.sql import SqlCollabLog

        self.db = db
        self.key = key
        self.relay = SqlCollabLog(db)
        self.origin = uuid.uuid4().hex
        self.poll = poll_seconds * (20 if db.dialect == "postgresql" else 1)
        self.retention = retention_seconds
        self.channel = f"{db.prefix}collab"
        self._watermark = 0
        self._seen: set[int] = set()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._last_prune = 0.0

    async def start(self, deliver: Deliver) -> None:
        self._watermark = await asyncio.to_thread(self.relay.last_id)
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._run(deliver)))
        if self.db.dialect == "postgresql":
            self._tasks.append(loop.create_task(self._listen()))

    def _publish_sync(self, room: str, kind: str, payload: bytes) -> None:
        # The signature is stored in front of the payload (no schema change).
        signed = sign(self.key, room[:255], self.origin[:64], kind, payload) + payload
        self.relay.append(room, self.origin, kind, signed)
        if self.db.dialect == "postgresql":
            from sqlalchemy import text

            with self.db.begin() as conn:
                conn.execute(text("SELECT pg_notify(:c, :p)"), {"c": self.channel, "p": room})

    async def publish(self, room: str, kind: str, payload: bytes) -> None:
        await asyncio.to_thread(self._publish_sync, room, kind, payload)

    async def _run(self, deliver: Deliver) -> None:
        while True:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.poll)
            self._wake.clear()
            try:
                await self._poll(deliver)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("collab: reading the relay table failed", exc_info=True)
                await asyncio.sleep(min(5.0, self.poll * 4))

    async def _poll(self, deliver: Deliver) -> None:
        rows = await asyncio.to_thread(self.relay.after, self._watermark)
        now = time.time()
        settled = self._watermark
        for row_id, room, origin, kind, payload, created_at in rows:
            if created_at < now - GRACE_SECONDS:
                settled = max(settled, row_id)
            if row_id in self._seen:
                continue
            self._seen.add(row_id)
            if origin != self.origin:
                msg = BusMessage(room, origin, kind, payload[MAC_SIZE:])
                if not _authentic(self.key, msg, payload[:MAC_SIZE]):
                    continue
                try:
                    await deliver(msg)
                except Exception:
                    log.warning("collab: cannot apply a relayed message", exc_info=True)
        # Rows older than the grace window are final: forget them, read only newer ones.
        self._watermark = settled
        self._seen = {i for i in self._seen if i > settled}
        if now - self._last_prune > PRUNE_EVERY:
            self._last_prune = now
            await asyncio.to_thread(self.relay.prune, self.retention)

    async def _listen(self) -> None:
        import psycopg
        from sqlalchemy.engine import make_url

        url = make_url(self.db.url).set(drivername="postgresql")
        kwargs: dict[str, Any] = dict(self.db.settings.connect_args)
        if self.db.settings.ssl_mode:
            kwargs.setdefault("sslmode", self.db.settings.ssl_mode)
        if self.db.settings.ssl_root_cert:
            kwargs.setdefault("sslrootcert", self.db.settings.ssl_root_cert)
        delay = 1.0
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    url.render_as_string(hide_password=False), autocommit=True, **kwargs
                )
                async with conn:
                    await conn.execute(f'LISTEN "{self.channel}"')
                    delay = 1.0
                    self._wake.set()  # catch up on what was missed while disconnected
                    async for _ in conn.notifies():
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("collab: LISTEN connection lost; retrying", exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()


# --------------------------------------------------------------------------- rooms


def _fresh(updated_at: str | None, ttl: float) -> bool:
    if not updated_at:
        return False
    try:
        then = datetime.datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()
    return age <= ttl


class RoomSync:
    """Keeps the rooms of one :class:`~pycrdt.websocket.WebsocketServer` in sync with the
    other replicas (``bus``) and persists their snapshots (``snapshots``)."""

    def __init__(
        self,
        settings: CollabSettings,
        *,
        bus: Bus | None,
        snapshots: DocumentStore | None = None,
        holders: DocumentStore | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.snapshots = snapshots
        self.holders = holders
        self.origin = bus.origin if bus is not None else uuid.uuid4().hex
        self.server: WebsocketServer | None = None
        self._names: dict[str, str] = {}  # dag_id -> server room name (path)
        self._subs: dict[str, Any] = {}
        self._dirty: set[str] = set()
        self._waiting: dict[str, asyncio.Event] = {}
        self._remote = False
        self._outbox: asyncio.Queue[tuple[str, str, bytes]] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- lifecycle -------------------------------------------------------------------------

    async def start(self, server: WebsocketServer) -> None:
        self.server = server
        self._outbox = asyncio.Queue()
        loop = asyncio.get_running_loop()
        if self.bus is not None:
            await self.bus.start(self._receive)
            self._spawn(loop, self._publisher())
        if self.snapshots is not None or self.holders is not None:
            self._spawn(loop, self._snapshotter())

    def _spawn(self, loop: asyncio.AbstractEventLoop, coro: Any) -> None:
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.bus is not None:
            await self.bus.stop()

    # -- rooms -----------------------------------------------------------------------------

    @staticmethod
    def dag_of(name: str) -> str:
        return room_of({"path": name}) or name.strip("/").rsplit("/", 1)[-1]

    async def prepare(self, name: str, room: YRoom) -> None:
        """A room was just created here: restore it and catch up with the other replicas
        before its first client is served."""
        dag = self.dag_of(name)
        self._names[dag] = name
        self._subs[dag] = room.ydoc.observe(lambda event: self._local_update(dag, event))
        room.on_message = lambda message: self._client_message(dag, message)
        if self.snapshots is not None:
            try:
                found = await asyncio.to_thread(self.snapshots.get, dag)
            except Exception:
                log.warning("collab: cannot read the snapshot of %s", dag, exc_info=True)
                found = None
            if found is not None and _fresh(found.updated_at, self.settings.snapshot_ttl_seconds):
                self._apply(room, found.data)
        if self.bus is None or self.settings.join_timeout <= 0:
            return
        if self.holders is not None and not await self._others_hold(dag):
            return  # nobody else has the room open: nothing to wait for
        event = self._waiting[dag] = asyncio.Event()
        try:
            await self.bus.publish(dag, "sync-request", b"")
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), self.settings.join_timeout)
        finally:
            self._waiting.pop(dag, None)

    async def closed(self, name: str, room: YRoom) -> None:
        """The last local client left: save the room and forget it."""
        dag = self.dag_of(name)
        sub = self._subs.pop(dag, None)
        if sub is not None:
            with contextlib.suppress(Exception):
                room.ydoc.unobserve(sub)
        if self.snapshots is not None:
            await self._save(dag, room)
        self._dirty.discard(dag)
        self._names.pop(dag, None)
        if self.holders is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._mark_holder, dag, False)

    def _apply(self, room: YRoom, update: bytes) -> None:
        self._remote = True
        try:
            room.ydoc.apply_update(update)
        finally:
            self._remote = False

    def _local_update(self, dag: str, event: Any) -> None:
        if self._remote:
            return
        self._dirty.add(dag)
        if self._outbox is not None and self.bus is not None:
            self._outbox.put_nowait((dag, "update", bytes(event.update)))

    def _client_message(self, dag: str, message: bytes) -> bool:
        if message and message[0] == AWARENESS and self._outbox is not None and self.bus:
            self._outbox.put_nowait((dag, "awareness", bytes(message)))
        return False  # processed normally as well

    async def _publisher(self) -> None:
        assert self._outbox is not None
        assert self.bus is not None
        while True:
            dag, kind, payload = await self._outbox.get()
            try:
                await self.bus.publish(dag, kind, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("collab: cannot publish to the other replicas", exc_info=True)

    def _room(self, dag: str) -> YRoom | None:
        name = self._names.get(dag)
        if name is None or self.server is None:
            return None
        return self.server.rooms.get(name)

    async def _receive(self, msg: BusMessage) -> None:
        room = self._room(msg.room)
        if room is None:
            return
        if msg.kind == "sync-request":
            if msg.room not in self._waiting and self.bus is not None:
                await self.bus.publish(msg.room, "update", room.ydoc.get_update())
            return
        if msg.kind == "update":
            self._apply(room, msg.payload)
            waiting = self._waiting.get(msg.room)
            if waiting is not None:
                waiting.set()
        elif msg.kind == "awareness":
            for client in list(room.clients):
                task = asyncio.get_running_loop().create_task(client.send(msg.payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    # -- persistence -----------------------------------------------------------------------

    async def _save(self, dag: str, room: YRoom) -> None:
        from pycrdt import merge_updates

        assert self.snapshots is not None
        state = room.ydoc.get_update()

        def merge(old: bytes | None) -> bytes:
            return merge_updates(old, state) if old else state

        try:
            await asyncio.to_thread(self.snapshots.update, dag, merge)
        except Exception:
            log.warning("collab: cannot save the snapshot of %s", dag, exc_info=True)

    def _mark_holder(self, dag: str, holding: bool) -> None:
        assert self.holders is not None
        now = time.time()
        ttl = self.settings.snapshot_seconds * 3

        def change(raw: bytes | None) -> bytes | None:
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            live = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > now - ttl}
            if holding:
                live[self.origin] = now
            else:
                live.pop(self.origin, None)
            return json.dumps(live).encode() if live else None

        self.holders.update(dag, change)

    async def _others_hold(self, dag: str) -> bool:
        assert self.holders is not None
        try:
            found = await asyncio.to_thread(self.holders.get, dag)
        except Exception:
            return True  # unknown: wait to be safe
        await asyncio.to_thread(self._mark_holder, dag, True)
        if found is None:
            return False
        try:
            data = json.loads(found.data)
        except ValueError:
            return True
        ttl = self.settings.snapshot_seconds * 3
        now = time.time()
        return any(
            k != self.origin and isinstance(v, (int, float)) and v > now - ttl
            for k, v in data.items()
        )

    async def _snapshotter(self) -> None:
        while True:
            await asyncio.sleep(self.settings.snapshot_seconds)
            for dag in list(self._names):
                room = self._room(dag)
                if room is None:
                    continue
                if self.holders is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self._mark_holder, dag, True)
                if self.snapshots is not None and dag in self._dirty:
                    self._dirty.discard(dag)
                    await self._save(dag, room)


def shared_server(
    sync: RoomSync, *, exception_handler: Callable[[Exception, logging.Logger], bool]
) -> WebsocketServer:
    """A :class:`~pycrdt.websocket.WebsocketServer` whose rooms ``sync`` keeps shared."""
    from pycrdt.websocket import WebsocketServer

    class SharedWebsocketServer(WebsocketServer):
        def __init__(self) -> None:
            super().__init__(auto_clean_rooms=True, exception_handler=exception_handler)
            self._ready: dict[str, asyncio.Event] = {}

        async def get_room(self, name: str) -> YRoom:
            creator = name not in self.rooms and name not in self._ready
            if creator:
                self._ready[name] = asyncio.Event()
            ready = self._ready.get(name)
            room = await super().get_room(name)
            if creator and ready is not None:
                try:
                    await sync.prepare(name, room)
                finally:
                    ready.set()
            elif ready is not None:
                await ready.wait()
            return room

        async def delete_room(self, *, name: str | None = None, room: YRoom | None = None) -> None:
            if name is None and room is not None:
                name = self.get_room_name(room)
            if name is not None:
                target = room or self.rooms.get(name)
                self._ready.pop(name, None)
                if target is not None:
                    await sync.closed(name, target)
            await super().delete_room(name=name, room=None if name is not None else room)

    return SharedWebsocketServer()


def build_sync(settings: Any, db: Database | None, *, secret: bytes) -> RoomSync | None:
    """The :class:`RoomSync` for ``settings.collab`` (``None``: rooms stay in memory);
    relayed messages are signed with a key derived from ``secret`` (the ticket key)."""
    cfg: CollabSettings = getattr(settings, "collab", None) or CollabSettings()
    mode = cfg.sync
    if mode == "auto":
        mode = "redis" if cfg.redis_url else "database" if db is not None else "memory"
    if mode == "memory":
        return None
    snapshots = holders = None
    if db is not None:
        from autoair.persistence.sql import SqlDocumentStore

        snapshots = SqlDocumentStore(db, "collab_rooms")
        holders = SqlDocumentStore(db, "collab_holders")
    bus: Bus
    if mode == "redis":
        if cfg.redis_url is None:
            raise ValueError("collab.sync=redis needs collab.redis_url")
        bus = RedisBus(cfg.redis_url.get_secret_value(), cfg.redis_channel, key=bus_key(secret))
    else:
        if db is None:
            raise ValueError("collab.sync=database needs a SQL database.backend")
        bus = DatabaseBus(
            db,
            poll_seconds=cfg.poll_seconds,
            retention_seconds=cfg.retention_seconds,
            key=bus_key(secret),
        )
    return RoomSync(cfg, bus=bus, snapshots=snapshots, holders=holders)


__all__ = [
    "Bus",
    "BusMessage",
    "DatabaseBus",
    "LocalBus",
    "LocalHub",
    "RedisBus",
    "RoomSync",
    "build_sync",
    "bus_key",
    "shared_server",
]
