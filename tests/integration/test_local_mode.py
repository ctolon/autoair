"""`autoair local`: the builder without an Airflow server."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from autoair.server import ServeOptions, build_app, local_preset, settings_for

SPEC: dict[str, Any] = {
    "dag_id": "laptop",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


TOKEN = "autoair-local-test"


def local_client(tmp_path: Path, **extra: Any) -> TestClient:
    """`autoair local` as started by the CLI: with its per-run token, sent by the client."""
    overrides = local_preset(dags_folder=tmp_path / "dags")
    overrides["ai"] = {"enabled": False}
    overrides["import_check_on_deploy"] = False
    overrides["local"] = {"token": TOKEN}
    overrides.update(extra)
    options = ServeOptions(overrides=overrides, local=True)
    client = TestClient(build_app(options, settings_for(options)), raise_server_exceptions=False)
    if overrides["local"].get("token") == TOKEN:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


def tokenless_client(tmp_path: Path, base_url: str = "http://127.0.0.1:8765", **extra: Any):
    """`autoair local --no-token`."""
    overrides = local_preset(dags_folder=tmp_path / "dags")
    overrides.update(ai={"enabled": False}, import_check_on_deploy=False, **extra)
    options = ServeOptions(overrides=overrides, local=True)
    return TestClient(
        build_app(options, settings_for(options)), base_url=base_url, raise_server_exceptions=False
    )


def test_local_preset_switches_airflow_features_off(tmp_path: Path) -> None:
    c = local_client(tmp_path)
    meta = c.get("/api/meta")
    assert meta.status_code == 200, meta.text
    body = meta.json()
    assert body["mode"] == "local"
    assert body["auth"] == "static"
    assert body["features"]["runs"] is False
    assert body["features"]["test_runs"] is False
    assert body["features"]["airflow"] is False
    assert body["storage"]["kind"] == "local"
    # the static catalog works without Airflow running
    operators = c.get("/api/catalog/operators")
    assert operators.status_code == 200
    assert any("EmptyOperator" in o["fqn"] for o in operators.json())
    # routes that need an Airflow server are off
    assert c.get("/api/airflow/pools").status_code == 404
    # no Airflow session cookie / sign-in in local mode
    cfg = c.get("/api/auth/config").json()
    assert cfg["sso"]["enabled"] is False
    assert cfg["login_enabled"] is False


def test_local_deploy_writes_the_folder(tmp_path: Path) -> None:
    c = local_client(tmp_path)
    resp = c.put("/api/specs/laptop", json={"spec": SPEC})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deployed"] is True
    assert (tmp_path / "dags" / "laptop.py").exists()
    # version message carries the deploy context
    audit = c.get("/api/specs/laptop/audit").json()
    assert audit[0]["action"] == "deploy"
    assert audit[0]["details"]["validation"]["ok"] is True


def test_local_token(tmp_path: Path) -> None:
    c = local_client(tmp_path, local={"token": "letmein"})
    assert c.get("/api/meta").status_code == 401
    assert c.get("/api/meta", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = c.get("/api/meta", headers={"Authorization": "Bearer letmein"})
    assert ok.status_code == 200
    assert ok.json()["auth"] == "static"
    assert c.get("/api/auth/config").json()["auth"] == "static"
    # the token holder is the local user: Admin by default (local.roles), no direct push
    me = c.get("/api/me", headers={"Authorization": "Bearer letmein"}).json()
    assert me["roles"] == ["Admin"]
    assert "admin:config" in me["permissions"]
    assert "deploy:direct_push" not in me["permissions"]


def test_local_roles_are_configurable(tmp_path: Path) -> None:
    c = local_client(tmp_path, local={"token": TOKEN, "roles": ["Viewer"]})
    me = c.get("/api/me").json()
    assert me["roles"] == ["Viewer"]
    assert c.put("/api/specs/laptop", json={"spec": SPEC}).status_code == 403


def test_tokenless_local_is_read_only_and_loopback_only(tmp_path: Path) -> None:
    c = tokenless_client(tmp_path, iam={"anonymous_roles": ["Viewer"]})  # the default
    me = c.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["roles"] == ["Viewer"]  # iam.anonymous_roles
    assert c.put("/api/specs/laptop", json={"spec": SPEC}).status_code == 403
    # DNS rebinding / cross-site pages: only loopback Host and Origin
    assert c.get("/api/meta", headers={"Host": "evil.example:8765"}).status_code == 403
    assert c.get("/api/meta", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/meta", headers={"Origin": "null"}).status_code == 403
    for host in ("localhost:8765", "127.0.0.1", "[::1]:8765"):
        assert c.get("/api/meta", headers={"Host": host}).status_code == 200, host
    assert c.get("/api/meta", headers={"Origin": "http://localhost:5173"}).status_code == 200
    # an explicit opt-in restores editing for anonymous visitors
    admin = tokenless_client(tmp_path, iam={"anonymous_roles": ["Admin"]})
    assert admin.put("/api/specs/laptop", json={"spec": SPEC}).status_code == 200


def test_tokenless_local_allows_the_public_url_host(tmp_path: Path) -> None:
    c = tokenless_client(
        tmp_path,
        base_url="https://builder.example.com",
        server={"public_url": "https://builder.example.com/autoair"},
    )
    assert c.get("/api/meta").status_code == 200
    assert c.get("/api/meta", headers={"Host": "other.example.com"}).status_code == 403


def test_token_protected_local_skips_the_host_check(tmp_path: Path) -> None:
    # behind a token any Host works (e.g. a reverse proxy); the token is the protection
    assert local_client(tmp_path).get("/api/meta").status_code == 200


def test_cli_refuses_public_bind_without_token() -> None:
    from autoair.cli import app

    result = CliRunner().invoke(app, ["local", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "without --token" in result.output
    result = CliRunner().invoke(app, ["local", "--host", "0.0.0.0", "--no-token"])
    assert result.exit_code == 2
    result = CliRunner().invoke(app, ["local", "--token", "x", "--no-token"])
    assert result.exit_code == 2


def test_cli_generates_a_per_run_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import autoair.server
    from autoair.cli import app

    seen: dict[str, Any] = {}

    def fake_run(options: ServeOptions, settings: Any, **kwargs: Any) -> None:
        seen.update(options=options, settings=settings, **kwargs)

    monkeypatch.setattr(autoair.server, "run", fake_run)
    monkeypatch.delenv("AUTOAIR_LOCAL__TOKEN", raising=False)
    result = CliRunner().invoke(app, ["local", "--dags-folder", str(tmp_path), "--open"])
    assert result.exit_code == 0, result.output
    token = seen["settings"].local.token.get_secret_value()
    assert token.startswith("autoair-local-")
    assert len(token) > 40
    assert seen["ui_fragment"] == f"#token={token}"
    assert seen["open_browser"] is True
    assert f"/ui/#token={token}" in result.output
    # worker processes rebuild the app from the options: same token
    assert seen["options"].overrides["local"]["token"] == token

    seen.clear()
    monkeypatch.delenv("AUTOAIR_IAM__ANONYMOUS_ROLES")  # the default (Viewer)
    result = CliRunner().invoke(app, ["local", "--dags-folder", str(tmp_path), "--no-token"])
    assert result.exit_code == 0, result.output
    assert seen["settings"].local.token is None
    assert seen["ui_fragment"] == ""
    assert "Viewer" in result.output


def _free_port() -> int:
    for port in range(8830, 8840):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 8830-8839")


def test_autoair_local_starts_and_serves(tmp_path: Path) -> None:
    """Starts the real server (`autoair local`) and loads /api/meta + the catalog."""
    port = _free_port()
    env = {
        **os.environ,
        "AUTOAIR_AI__ENABLED": "false",
        "XDG_CACHE_HOME": str(tmp_path),
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("AUTOAIR_CONFIG_FILE", None)
    env.pop("AUTOAIR_LOCAL__TOKEN", None)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from autoair.cli import app; app()",
            "local",
            "--port",
            str(port),
            "--dags-folder",
            str(tmp_path / "dags"),
            "--no-open",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        token = ""
        assert proc.stdout is not None
        for _ in range(20):  # the per-run sign-in link is printed before the server starts
            line = proc.stdout.readline().decode()
            if "#token=" in line:
                token = line.strip().rsplit("#token=", 1)[1]
                break
        assert token.startswith("autoair-local-")
        auth = {"Authorization": f"Bearer {token}"}
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout else ""
                pytest.fail(f"autoair local exited: {out[-2000:]}")
            try:
                if httpx2.get(f"{base}/api/health", timeout=2).status_code == 200:
                    break
            except httpx2.HTTPError:
                time.sleep(0.5)
        assert httpx2.get(f"{base}/api/meta", timeout=30).status_code == 401
        meta = httpx2.get(f"{base}/api/meta", headers=auth, timeout=30)
        assert meta.status_code == 200, meta.text
        assert meta.json()["mode"] == "local"
        catalog = httpx2.get(f"{base}/api/catalog/operators", headers=auth, timeout=120)
        assert catalog.status_code == 200
        assert len(catalog.json()) > 0
        page = httpx2.get(f"{base}/api/health", timeout=10)
        assert page.headers["x-content-type-options"] == "nosniff"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
