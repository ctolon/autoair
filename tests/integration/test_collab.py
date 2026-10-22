"""Two Yjs clients co-editing a DAG room through the real ASGI app (uvicorn + websockets)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx2
import pytest

pytest.importorskip("pycrdt.websocket")
websockets = pytest.importorskip("websockets")

from pycrdt import Doc, Map, Provider  # noqa: E402

from autoair.api.app import build_services, create_app  # noqa: E402
from autoair.auth import AuthBackend  # noqa: E402
from autoair.collab import issue_ticket, verify_ticket  # noqa: E402
from autoair.settings import Settings  # noqa: E402

pytestmark = [
    pytest.mark.airflow,
    # pycrdt objects owned by the in-process server thread are freed on the test thread.
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


class WsChannel:
    """Adapts a ``websockets`` client connection to pycrdt's Channel protocol."""

    def __init__(self, ws: object, path: str) -> None:
        self._ws = ws
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self) -> WsChannel:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.recv()
        except websockets.ConnectionClosed:
            raise StopAsyncIteration from None

    async def send(self, message: bytes) -> None:
        await self._ws.send(message)  # type: ignore[attr-defined]

    async def recv(self) -> bytes:
        return bytes(await self._ws.recv())  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    tmp = tmp_path_factory.mktemp("collab")
    settings = Settings(
        dags_folder=tmp,
        subfolder="",
        ai={"enabled": False},
        features={"mcp": False},
        collab_secret="test-secret",
        iam={"anonymous_roles": ["Admin"]},  # AuthBackend(): anonymous callers edit
    )
    app = create_app(build_services(settings, AuthBackend()))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"ws://127.0.0.1:{port}", f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


async def _sync(url: str, tickets: tuple[str, str]) -> dict[str, str]:
    room = "/collab/collab_demo"
    async with (
        websockets.connect(f"{url}{room}?ticket={tickets[0]}") as ws_a,
        websockets.connect(f"{url}{room}?ticket={tickets[1]}") as ws_b,
    ):
        doc_a, doc_b = Doc(), Doc()
        nodes_a = doc_a.get("nodes", type=Map)
        nodes_b = doc_b.get("nodes", type=Map)
        async with Provider(doc_a, WsChannel(ws_a, room)), Provider(doc_b, WsChannel(ws_b, room)):
            nodes_a["n1"] = '{"id":"n1","task_id":"extract"}'
            for _ in range(100):
                if "n1" in nodes_b:
                    break
                await asyncio.sleep(0.05)
            nodes_b["n2"] = '{"id":"n2","task_id":"load"}'
            for _ in range(100):
                if "n2" in nodes_a:
                    break
                await asyncio.sleep(0.05)
            return {**dict(nodes_a), **{f"b:{k}": v for k, v in dict(nodes_b).items()}}


def test_two_clients_converge(server):
    ws_url, http_url = server
    tickets = []
    for _ in range(2):  # a ticket opens one connection
        resp = httpx2.post(f"{http_url}/api/collab/collab_demo/ticket")
        assert resp.status_code == 200, resp.text
        tickets.append(resp.json()["ticket"])
    result = asyncio.run(_sync(ws_url, (tickets[0], tickets[1])))
    assert result["n1"] == '{"id":"n1","task_id":"extract"}'
    assert result["n2"] == '{"id":"n2","task_id":"load"}'
    assert result["b:n1"] == result["n1"]


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?ticket=forged.ticket",
        f"?ticket={issue_ticket(b'test-secret', 'other_dag')}",
        f"?ticket={issue_ticket(b'test-secret', 'collab_demo', ttl=-1)}",
        f"?ticket={issue_ticket(b'wrong-secret', 'collab_demo')}",
    ],
    ids=["missing", "forged", "other-dag", "expired", "wrong-key"],
)
def test_connection_without_valid_ticket_is_rejected(server, query):
    async def connect() -> None:
        async with websockets.connect(f"{server[0]}/collab/collab_demo{query}") as ws:
            await ws.recv()

    with pytest.raises(websockets.InvalidStatus) as err:
        asyncio.run(connect())
    assert err.value.response.status_code == 403


def test_invalid_room_is_rejected(server):
    async def connect() -> None:
        async with websockets.connect(f"{server[0]}/collab/../etc?ticket=x") as ws:
            await ws.recv()

    with pytest.raises((websockets.InvalidStatus, websockets.ConnectionClosed)):
        asyncio.run(connect())


def test_ticket_roundtrip():
    ticket = issue_ticket(b"k", "dag_a", user="alice")
    claims = verify_ticket(b"k", ticket, "dag_a")
    assert claims is not None
    assert claims["u"] == "alice"
    assert verify_ticket(b"k", ticket, "dag_b") is None
    assert verify_ticket(b"k", ticket + "x", "dag_a") is None
    assert verify_ticket(b"k", None, "dag_a") is None
