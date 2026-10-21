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


def local_client(tmp_path: Path, **extra: Any) -> TestClient:
    overrides = local_preset(dags_folder=tmp_path / "dags")
    overrides["ai"] = {"enabled": False}
    overrides["import_check_on_deploy"] = False
    overrides.update(extra)
    options = ServeOptions(overrides=overrides, local=True)
    return TestClient(build_app(options, settings_for(options)), raise_server_exceptions=False)


def test_local_preset_switches_airflow_features_off(tmp_path: Path) -> None:
    c = local_client(tmp_path)
    meta = c.get("/api/meta")
    assert meta.status_code == 200, meta.text
    body = meta.json()
    assert body["mode"] == "local"
    assert body["auth"] == "none"
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


def test_cli_refuses_public_bind_without_token() -> None:
    from autoair.cli import app

    result = CliRunner().invoke(app, ["local", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "without --token" in result.output


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
    env = {**os.environ, "AUTOAIR_AI__ENABLED": "false", "XDG_CACHE_HOME": str(tmp_path)}
    env.pop("AUTOAIR_CONFIG_FILE", None)
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
        meta = httpx2.get(f"{base}/api/meta", timeout=30)
        assert meta.status_code == 200, meta.text
        assert meta.json()["mode"] == "local"
        catalog = httpx2.get(f"{base}/api/catalog/operators", timeout=120)
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
