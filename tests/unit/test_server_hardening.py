"""AutoAir's own server processes make themselves non-dumpable at start-up (DAG code they
import in child processes runs as the same user); the Airflow plugin does not."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from autoair.validation import sandbox


@pytest.fixture
def switches(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Process name -> the ``enabled`` (server.harden_process) it was hardened with."""
    seen: dict[str, bool] = {}

    def fake(name: str, *, enabled: bool = True) -> bool:
        seen[name] = enabled
        return enabled

    monkeypatch.setattr(sandbox, "harden_server_process", fake)
    return seen


@pytest.fixture
def calls(switches: dict[str, bool], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names of the processes hardened (server.harden_process at its default, on)."""
    seen: list[str] = []
    fake = sandbox.harden_server_process

    def record(name: str, *, enabled: bool = True) -> bool:
        assert enabled, name
        seen.append(name)
        return fake(name, enabled=enabled)

    monkeypatch.setattr(sandbox, "harden_server_process", record)
    return seen


def test_serve_and_local_harden_before_uvicorn_starts(
    calls: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn

    from autoair.server import ServeOptions, run
    from autoair.settings import Settings

    monkeypatch.setattr("autoair.logging_setup.configure_logging", lambda *_a, **_k: None)
    order: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: order.append(f"uvicorn after {calls}"))
    monkeypatch.setattr("autoair.server.build_app", lambda *_a, **_k: object())
    settings = Settings(dags_folder=tmp_path)  # type: ignore[call-arg]
    run(ServeOptions(), settings, host="127.0.0.1", port=1)
    run(ServeOptions(local=True), settings, host="127.0.0.1", port=1)
    assert calls == ["server", "local"]
    assert order == ["uvicorn after ['server']", "uvicorn after ['server', 'local']"]


def test_uvicorn_worker_processes_harden_themselves(
    calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoair import server

    monkeypatch.setattr("autoair.logging_setup.configure_logging", lambda *_a, **_k: None)
    monkeypatch.setenv(server.OPTIONS_ENV, server.ServeOptions().to_env())
    monkeypatch.setattr(server, "build_app", lambda *_a, **_k: object())
    server.app_factory()
    assert calls == ["server worker"]


def test_ai_serve_hardens(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pydantic_ai")
    import uvicorn
    from typer.testing import CliRunner

    from autoair.cli import app

    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: None)
    monkeypatch.setattr("autoair.ai.service.build_ai_service", lambda **_k: object())
    result = CliRunner().invoke(app, ["ai-serve", "--port", "1"])
    assert result.exit_code == 0, result.output
    assert calls == ["ai-serve"]


def test_mcp_serve_http_hardens(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastmcp")
    import uvicorn
    from fastapi import FastAPI
    from typer.testing import CliRunner

    from autoair.cli import app

    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: None)
    monkeypatch.setattr("autoair.api.app.build_services", lambda *_a, **_k: object())
    monkeypatch.setattr("autoair.mcp.http.create_mcp_asgi_app", lambda *_a, **_k: FastAPI())
    result = CliRunner().invoke(app, ["mcp-serve", "--http", "--port", "1"])
    assert result.exit_code == 0, result.output
    assert calls == ["mcp-serve"]


def test_the_plugin_does_not_harden_airflows_process() -> None:
    """The plugin runs inside Airflow's api-server: changing that process is not ours."""
    import inspect

    import autoair.plugin

    source = inspect.getsource(autoair.plugin)
    assert "harden_server_process" not in source
    assert "make_non_dumpable" not in source


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_harden_server_process_sets_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        sandbox, "_prctl", lambda option, value: seen.append((option, value)) or True
    )
    assert sandbox.harden_server_process("server")
    assert seen == [(sandbox._PR_SET_DUMPABLE, 0)]


def test_harden_process_off_is_respected_by_serve_and_workers(
    switches: dict[str, bool], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn

    from autoair import server
    from autoair.settings import Settings

    monkeypatch.setattr("autoair.logging_setup.configure_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "build_app", lambda *_a, **_k: object())
    off = Settings(dags_folder=tmp_path, server={"harden_process": False})  # type: ignore[call-arg]
    server.run(server.ServeOptions(), off, host="127.0.0.1", port=1)
    monkeypatch.setenv(server.OPTIONS_ENV, server.ServeOptions().to_env())
    monkeypatch.setenv("AUTOAIR_SERVER__HARDEN_PROCESS", "false")
    server.app_factory()
    assert switches == {"server": False, "server worker": False}


def test_harden_process_off_is_respected_by_mcp_and_ai_serve(
    switches: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastmcp")
    pytest.importorskip("pydantic_ai")
    import uvicorn
    from fastapi import FastAPI
    from typer.testing import CliRunner

    from autoair.cli import app

    monkeypatch.setenv("AUTOAIR_SERVER__HARDEN_PROCESS", "false")
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: None)
    monkeypatch.setattr("autoair.api.app.build_services", lambda *_a, **_k: object())
    monkeypatch.setattr("autoair.mcp.http.create_mcp_asgi_app", lambda *_a, **_k: FastAPI())
    monkeypatch.setattr("autoair.ai.service.build_ai_service", lambda **_k: object())
    runner = CliRunner()
    assert runner.invoke(app, ["mcp-serve", "--http", "--port", "1"]).exit_code == 0
    assert runner.invoke(app, ["ai-serve", "--port", "1"]).exit_code == 0
    assert switches == {"mcp-serve": False, "ai-serve": False}


def test_harden_server_process_off_leaves_the_process_dumpable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(sandbox, "_prctl", lambda *a: seen.append(a) or True)
    assert sandbox.harden_server_process("server", enabled=False) is False
    assert seen == []
    assert "server.harden_process=false" in caplog.text


def test_harden_process_defaults_on() -> None:
    from autoair.sections import ServerSettings

    assert ServerSettings().harden_process is True
