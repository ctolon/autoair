"""Unauthenticated servers (--no-auth, token-less `autoair local`): loopback binds only,
Host/Origin checked against DNS rebinding, anonymous callers get Viewer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from autoair.net.loopback import LoopbackGuard, is_loopback, public_hosts, refuse_public_bind


def guarded(**kwargs: Any) -> TestClient:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(LoopbackGuard, **kwargs)
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("app.localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("10.0.0.5", False),
        ("localhost.evil.example", False),
        ("example.com", False),
    ],
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback
    assert (refuse_public_bind(host, "x") is None) is loopback


def test_guard_checks_host_and_origin() -> None:
    c = guarded()
    assert c.get("/ping").status_code == 200
    for host in ("localhost", "localhost:1", "127.0.0.1:8765", "[::1]:8765"):
        assert c.get("/ping", headers={"Host": host}).status_code == 200, host
    for host in ("rebind.attacker.example", "127.0.0.1.nip.io:8765", "", "[::1"):
        assert c.get("/ping", headers={"Host": host}).status_code == 403, host
    assert c.get("/ping", headers={"Origin": "http://localhost:5173"}).status_code == 200
    for origin in ("http://evil.example", "null", "file://", "chrome-extension://x"):
        assert c.get("/ping", headers={"Origin": origin}).status_code == 403, origin


def test_guard_allows_configured_public_hosts() -> None:
    assert public_hosts("https://Builder.Example.com/autoair", None, "not a url") == [
        "builder.example.com"
    ]
    c = guarded(extra_hosts=["builder.example.com"])
    assert c.get("/ping", headers={"Host": "builder.example.com"}).status_code == 200
    assert c.get("/ping", headers={"Origin": "https://builder.example.com"}).status_code == 200
    assert c.get("/ping", headers={"Host": "other.example.com"}).status_code == 403


def test_guard_closes_foreign_websockets() -> None:
    app = FastAPI()

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_text("hi")
        await socket.close()

    app.add_middleware(LoopbackGuard)
    c = TestClient(app, base_url="http://127.0.0.1")
    with c.websocket_connect("/ws", headers={"Host": "127.0.0.1:8765"}) as s:
        assert s.receive_text() == "hi"
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        c.websocket_connect(
            "/ws", headers={"Host": "127.0.0.1:8765", "Origin": "http://evil.example"}
        ) as s,
    ):
        s.receive_text()


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--no-auth", "--host", "0.0.0.0", "--port", "1"],
        ["mcp-serve", "--http", "--no-auth", "--host", "0.0.0.0"],
        ["mcp", "--http", "--host", "0.0.0.0"],
        ["ai-serve", "--no-auth", "--host", "192.0.2.1"],
    ],
)
def test_cli_refuses_no_auth_on_a_public_bind(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from autoair.cli import app

    started: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(a))
    result = CliRunner().invoke(app, argv)
    if "ai-serve" in argv and "not installed" in result.output:
        pytest.skip("autoair[ai] is not installed")
    assert result.exit_code == 2, result.output
    assert "refusing to serve" in result.output
    assert started == []


def test_serve_no_auth_is_viewer_and_guarded(tmp_path: Path) -> None:
    from autoair.server import ServeOptions, build_app, settings_for

    options = ServeOptions(
        overrides={
            "dags_folder": str(tmp_path),
            "subfolder": "",
            "ai": {"enabled": False},
            "iam": {"anonymous_roles": ["Viewer"]},  # the default (conftest opts tests out)
        },
        no_auth=True,
    )
    c = TestClient(
        build_app(options, settings_for(options)),
        base_url="http://localhost:8765",
        raise_server_exceptions=False,
    )
    me = c.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["roles"] == ["Viewer"]
    assert "spec:write" not in me.json()["permissions"]
    assert c.get("/api/me", headers={"Host": "attacker.example"}).status_code == 403
    # authenticated serve has no Host check (it runs behind proxies under any name)
    authed = ServeOptions(overrides=options.overrides)
    c2 = TestClient(build_app(authed, settings_for(authed)), raise_server_exceptions=False)
    assert c2.get("/api/health", headers={"Host": "autoair.example.com"}).status_code == 200


def _rpc(c: TestClient, method: str, params: dict[str, Any], **headers: str) -> Any:
    return c.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Accept": "application/json, text/event-stream", **headers},
    )


def test_mcp_http_is_anonymous_viewer_and_guarded(tmp_path: Path) -> None:
    """`autoair mcp --http` (no authentication): callers get iam.anonymous_roles (Viewer by
    default) and requests naming a foreign Host are refused (DNS rebinding)."""
    pytest.importorskip("fastmcp")
    from autoair.api.app import build_services
    from autoair.auth import AuthBackend
    from autoair.mcp.http import create_local_http_app
    from autoair.settings import Settings

    services = build_services(
        Settings(
            dags_folder=tmp_path,
            subfolder="",
            ai={"enabled": False},
            iam={"anonymous_roles": ["Viewer"]},  # the default (conftest opts tests out)
        ),
        AuthBackend(),
    )
    c = TestClient(create_local_http_app(services), base_url="http://127.0.0.1:8766")
    listed = _rpc(c, "tools/list", {})
    assert listed.status_code == 200, listed.text
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert "list_dags" in names
    assert {"save_dag_spec", "delete_dag_spec"}.isdisjoint(names)
    spec = {"dag_id": "anon_mcp", "nodes": [], "edges": []}
    called = _rpc(c, "tools/call", {"name": "save_dag_spec", "arguments": {"spec": spec}})
    result = called.json()["result"]
    assert result["isError"]
    assert "missing AutoAir permission" in result["content"][0]["text"]
    assert not list(tmp_path.rglob("anon_mcp*"))
    bare = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
        follow_redirects=False,
    )
    assert bare.status_code == 200  # served at /mcp as well (no redirect)
    assert _rpc(c, "tools/list", {}, Host="rebind.attacker.example").status_code == 403
    assert _rpc(c, "tools/list", {}, Origin="http://evil.example").status_code == 403


def test_autoair_mcp_entry_point_refuses_a_public_http_bind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("fastmcp")
    import uvicorn

    from autoair.mcp.server import main

    started: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(a))
    with pytest.raises(SystemExit) as exited:
        main(["--http", "--host", "0.0.0.0"])
    assert exited.value.code == 2
    assert "refusing to serve" in capsys.readouterr().err
    assert started == []
