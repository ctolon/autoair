"""Real-time collaborative editing (Yjs CRDT over WebSockets, ``pip install autoair[collab]``).

Each DAG is a room (``/collab/<dag_id>``). The server relays Yjs sync/awareness messages
between clients (pycrdt-websocket); the document state lives in memory while anyone is
connected — the stored spec stays the source of truth and is written by deploys.

Authentication: the browser first asks the regular (authenticated) HTTP API for a
short-lived *ticket* — ``POST /api/collab/<dag_id>/ticket``, which requires permission to
edit that DAG — and opens the WebSocket with ``?ticket=``. The ticket is an HMAC over
(dag_id, user, expiry, nonce), so the WebSocket needs no access to the auth manager, works
with every Airflow auth manager (and with tokens kept in cookies, local storage or not at
all), and no long-lived JWT ever appears in a URL. A ticket opens one connection: its nonce
is claimed in a shared document store (:class:`TicketLedger`), so a ticket that leaked (logs,
proxy, browser history) cannot be replayed. A connection is closed after
``collab.session_seconds``; the browser then asks for a new ticket, which re-checks the edit
permission, so a revoked permission ends live editing within that interval.

Plugin apps get no lifespan inside Airflow, so the room server starts lazily on the first
connection.

Several replicas (``collab.sync``, see :mod:`autoair.collab_sync`): by default rooms live in
one process (put a sticky load balancer in front of several replicas). With a SQL database or
``collab.redis_url`` the replicas share rooms: Yjs updates and presence are fanned out
(PostgreSQL ``LISTEN/NOTIFY``, a polled table on SQLite, or Redis pub/sub), a replica opening
a room asks the others for its state, and room snapshots are persisted. Tickets validate on
any replica: their key comes from settings / Airflow / the database, never per process.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs

from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from autoair.collab_sync import RoomSync
    from autoair.persistence.db import Database
    from autoair.persistence.documents import DocumentStore

log = logging.getLogger(__name__)

ROOM_RE = re.compile(r"^/?(?:.*/)?collab/([A-Za-z0-9_.-]{1,250})/?$")
MAX_ROOMS = 500
TICKET_TTL = 60  # seconds; only checked when a connection is opened
SESSION_EXPIRED = 4001  # WebSocket close code: reconnect with a fresh ticket
TICKET_BUCKET = 60  # seconds of ticket expiries per ledger document

ASGIApp = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


CollabSyncMode = Literal["auto", "memory", "database", "redis"]


class CollabSettings(BaseModel):
    """How live-editing rooms are shared between replicas (``AUTOAIR_COLLAB__*``)."""

    model_config = ConfigDict(extra="forbid")

    sync: CollabSyncMode = Field(
        default="auto",
        description="memory: rooms live in one process (several replicas need sticky "
        "sessions); database: share rooms through the SQL database (PostgreSQL "
        "LISTEN/NOTIFY, SQLite polling); redis: Redis pub/sub (collab.redis_url); auto: "
        "redis when redis_url is set, else database with a SQL backend, else memory.",
    )
    redis_url: SecretStr | None = Field(
        default=None,
        description="Redis URL for pub/sub fan-out, e.g. redis://redis:6379/0 "
        "(pip install 'autoair[collab-redis]').",
    )
    redis_channel: str = Field(
        default="autoair:collab", min_length=1, description="Redis pub/sub channel."
    )
    poll_seconds: float = Field(
        default=0.25,
        gt=0,
        le=10,
        description="Database fan-out: how often a replica reads new messages (PostgreSQL "
        "is woken by NOTIFY and polls only as a fallback, 20x slower).",
    )
    join_timeout: float = Field(
        default=1.5,
        ge=0,
        le=30,
        description="Seconds a replica opening a room waits for the others to send its "
        "current state (shared sync only).",
    )
    snapshot_seconds: float = Field(
        default=5,
        gt=0,
        description="Shared sync: how often a changed room is saved to the database.",
    )
    snapshot_ttl_seconds: float = Field(
        default=300,
        ge=0,
        description="A saved room is restored only when it changed within this many "
        "seconds (older ones are stale: the stored spec is the source of truth).",
    )
    retention_seconds: float = Field(
        default=300,
        ge=10,
        description="Database fan-out: how long relayed messages are kept before pruning.",
    )
    session_seconds: float = Field(
        default=300,
        ge=1,
        description="A live-editing connection is closed after this many seconds; the "
        "browser reconnects with a new ticket, which re-checks DAG edit permission, so a "
        "revoked permission ends co-editing within this interval. Lower is stricter (one "
        "ticket request per user and interval).",
    )


def available() -> bool:
    try:
        import pycrdt.websocket  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- tickets


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def collab_secret(
    configured: str | None = None, *, fallback: Callable[[], bytes] | None = None
) -> bytes:
    """HMAC key for tickets, the same on every replica: configured, else Airflow's JWT
    secret (shared by all api-server replicas), else ``fallback`` (a key kept in the SQL
    database or in the state folder), else a per-process random key (single replica only)."""
    if configured:
        return configured.encode()
    try:
        from airflow.configuration import conf

        jwt_secret = conf.get("api_auth", "jwt_secret", fallback=None)
    except Exception:
        jwt_secret = None
    if jwt_secret:
        return hashlib.sha256(b"autoair-collab:" + jwt_secret.encode()).digest()
    if fallback is not None:
        try:
            return hashlib.sha256(b"autoair-collab:" + fallback()).digest()
        except Exception:
            log.warning("collab: cannot read the shared ticket key", exc_info=True)
    log.info("collab: no AUTOAIR_COLLAB_SECRET / Airflow jwt_secret; using a per-process key")
    return secrets.token_bytes(32)


def key_file(path: Path) -> bytes:
    """A random key stored in ``path`` (created once, mode 0600): shared by every process
    using the same state folder."""
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process won the race
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def issue_ticket(secret: bytes, dag_id: str, user: str | None = None, ttl: int = TICKET_TTL) -> str:
    claims = {"d": dag_id, "u": user, "e": int(time.time()) + ttl, "j": secrets.token_urlsafe(16)}
    payload = _b64(json.dumps(claims).encode())
    signature = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_ticket(secret: bytes, ticket: str | None, dag_id: str) -> dict[str, Any] | None:
    """The ticket's claims when it is authentic, unexpired and for ``dag_id``."""
    if not ticket or ticket.count(".") != 1:
        return None
    payload, signature = ticket.split(".")
    expected = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_unb64(payload))
    except ValueError:
        return None
    if not isinstance(claims, dict) or claims.get("d") != dag_id:
        return None
    if int(claims.get("e", 0)) < time.time():
        return None
    return claims


class TicketLedger:
    """Tickets already used, shared by every replica (single use of tickets).

    A ticket's nonce is added to the document of its expiry minute with an atomic
    read-modify-write (:meth:`DocumentStore.update`), so two replicas presented the same
    ticket at once accept it only once. Documents of minutes that have passed only hold
    expired tickets, which :func:`verify_ticket` rejects anyway: they are deleted."""

    def __init__(self, store: DocumentStore, *, prune_every: float = TICKET_BUCKET) -> None:
        self.store = store
        self.prune_every = prune_every
        self._next_prune = 0.0

    def claim(self, claims: dict[str, Any]) -> bool:
        """``True`` the first time the ticket with these (verified) claims is presented."""
        nonce = claims.get("j")
        expiry = claims.get("e")
        if not isinstance(nonce, str) or not nonce or not isinstance(expiry, int):
            return False  # tickets of older releases carry no nonce
        fresh = False

        def add(raw: bytes | None) -> bytes | None:
            nonlocal fresh
            try:
                used = json.loads(raw) if raw else []
            except ValueError:
                used = []
            if not isinstance(used, list):
                used = []
            fresh = nonce not in used
            if fresh:
                used.append(nonce)
            return json.dumps(used).encode()

        self.store.update(f"tickets-{expiry // TICKET_BUCKET}", add)
        self._prune()
        return fresh

    def _prune(self) -> None:
        now = time.time()
        if now < self._next_prune:
            return
        self._next_prune = now + self.prune_every
        current = int(now) // TICKET_BUCKET
        names: list[str] = self.store.keys()
        for key in names:
            bucket = key.removeprefix("tickets-")
            if bucket.isdigit() and int(bucket) < current:
                self.store.delete(key)


def ticket_ledger(
    settings: Any, database: Database | None, *, collection: str = "collab_tickets"
) -> TicketLedger:
    """The ledger in the SQL database when there is one, else in the state folder (shared
    by the replicas only when the state folder is). ``collection``: ``collab_tickets``
    (rooms) or ``lsp_tickets`` (language-server sockets)."""
    if database is not None:
        from autoair.persistence.sql import SqlDocumentStore

        return TicketLedger(SqlDocumentStore(database, collection))
    from autoair.persistence import FileDocumentStore

    folder = collection.replace("_", "-")
    return TicketLedger(FileDocumentStore(str(Path(settings.state_dir) / folder)))


# --------------------------------------------------------------------------- server


def _ticket(scope: dict[str, Any]) -> str | None:
    query = parse_qs(scope.get("query_string", b"").decode())
    values = query.get("ticket")
    return str(values[0]) if values else None


def room_of(scope: dict[str, Any]) -> str | None:
    match = ROOM_RE.fullmatch(scope.get("path", ""))
    return match.group(1) if match else None


def create_collab_app(
    secret: bytes | None,
    *,
    max_rooms: int = MAX_ROOMS,
    sync: RoomSync | None = None,
    tickets: TicketLedger | None = None,
    session_seconds: float | None = None,
) -> ASGIApp:
    """ASGI app serving Yjs rooms. ``secret=None`` disables authentication (tests/dev);
    ``tickets`` makes every ticket single-use; ``session_seconds`` closes connections
    (code 4001) after that long; ``sync`` shares the rooms with other replicas
    (:mod:`autoair.collab_sync`)."""
    from pycrdt.websocket import ASGIServer, WebsocketServer

    if sync is not None:
        from autoair.collab_sync import shared_server

        server = shared_server(sync, exception_handler=_log_exception)
    else:
        server = WebsocketServer(auto_clean_rooms=True, exception_handler=_log_exception)
    start_lock = asyncio.Lock()
    tasks: set[asyncio.Task[None]] = set()

    async def ensure_started() -> None:
        if server.started.is_set():
            return
        async with start_lock:
            if not server.started.is_set():
                task = asyncio.get_running_loop().create_task(server.start())
                tasks.add(task)  # keep a reference for the process lifetime
                await server.started.wait()
                if sync is not None:
                    await sync.start(server)

    async def rejected(scope: dict[str, Any]) -> bool:
        dag_id = room_of(scope)
        if dag_id is None:
            return True
        if len(server.rooms) >= max_rooms and scope.get("path") not in server.rooms:
            log.warning("collab: room limit reached")
            return True
        if secret is None:
            return False
        claims = verify_ticket(secret, _ticket(scope), dag_id)
        if claims is None:
            log.debug("collab: missing or invalid ticket for %s", dag_id)
            return True
        if tickets is not None:
            try:
                fresh = await asyncio.to_thread(tickets.claim, claims)
            except Exception:  # the ledger is unreachable: fail closed
                log.warning("collab: cannot record the ticket; refusing", exc_info=True)
                return True
            if not fresh:
                log.info("collab: ticket for %s presented again (user %s)", dag_id, claims.get("u"))
                return True
        return False

    asgi = ASGIServer(server)

    async def app(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "websocket":
            await asgi(dict(scope), receive, send)
            return
        await ensure_started()
        connect = await receive()  # websocket.connect
        if await rejected(dict(scope)):
            # Closing before accepting answers the handshake with HTTP 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        replayed = False
        deadline = None if session_seconds is None else time.monotonic() + session_seconds
        closed = False
        pending: asyncio.Task[Any] | None = None

        async def guarded_send(message: MutableMapping[str, Any]) -> None:
            if not closed:
                await send(message)

        async def replay() -> dict[str, Any]:
            nonlocal replayed, closed, pending
            if not replayed:
                replayed = True
                return dict(connect)
            if closed:
                return {"type": "websocket.disconnect", "code": SESSION_EXPIRED}
            if deadline is None:
                return dict(await receive())
            if pending is None:
                pending = asyncio.ensure_future(receive())
            remaining = deadline - time.monotonic()
            done, _ = await asyncio.wait({pending}, timeout=max(remaining, 0))
            if done:
                message, pending = pending.result(), None
                return dict(message)
            # The session is over: the client reconnects with a new ticket, which
            # re-checks its permission to edit the DAG.
            closed = True
            with contextlib.suppress(Exception):  # the client may be gone already
                await send(
                    {"type": "websocket.close", "code": SESSION_EXPIRED, "reason": "reauthenticate"}
                )
            return {"type": "websocket.disconnect", "code": SESSION_EXPIRED}

        try:
            await asgi(dict(scope), replay, guarded_send)
        finally:
            if pending is not None:
                pending.cancel()

    return app


def _log_exception(exception: Exception, logger: logging.Logger) -> bool:
    # Typically a client that went away mid-message: not actionable.
    logger.info("collab: connection ended: %s", exception)
    return True
