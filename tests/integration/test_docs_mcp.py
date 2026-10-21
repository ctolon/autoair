"""The AutoAir documentation MCP tools: on the main server, and ``autoair docs-mcp`` over
stdio and HTTP (no Airflow, no AI extra, no knowledge-base store)."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.optional import importorskip_mcp

importorskip_mcp()

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

from autoair.api.app import build_services  # noqa: E402
from autoair.auth import bearer_passthrough  # noqa: E402
from autoair.docs import mcp as docs_mcp  # noqa: E402
from autoair.docs.mcp import DOCS_TOOLS, create_docs_mcp_server  # noqa: E402
from autoair.mcp.iam import TOOL_FEATURES, TOOL_PERMISSIONS  # noqa: E402
from autoair.mcp.server import create_mcp_server  # noqa: E402
from autoair.settings import Settings  # noqa: E402

SITE = "https://ctolon.github.io/autoair/docs/"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _exercise(client: Client[Any]) -> dict[str, Any]:
    tools = {t.name for t in await client.list_tools()}
    found = await client.call_tool(
        "search_autoair_docs", {"query": "install the Airflow plugin", "limit": 3}
    )
    hit = found.structured_content["hits"][0]
    page = await client.call_tool("read_autoair_doc", {"page": hit["url"], "max_chars": 2000})
    listing = await client.call_tool("list_autoair_docs", {"section": "ai"})
    return {
        "tools": tools,
        "hits": found.structured_content["hits"],
        "page": page.structured_content,
        "listing": listing.structured_content,
    }


def _check(result: dict[str, Any]) -> None:
    assert len(result["hits"]) <= 3
    assert result["hits"][0]["url"].startswith(SITE)
    assert result["page"]["content"].startswith("#")
    assert len(result["page"]["content"]) <= 2000
    assert result["listing"]["pages"]
    assert all(p["section"] == "ai" for p in result["listing"]["pages"])


def test_docs_tools_are_classified_without_permissions_behind_their_feature() -> None:
    for name in DOCS_TOOLS:
        assert TOOL_PERMISSIONS[name] == ()
        assert TOOL_FEATURES[name] == ("autoair_docs",)


def test_docs_only_server_lists_exactly_the_docs_tools_and_answers() -> None:
    async def go() -> dict[str, Any]:
        async with Client(create_docs_mcp_server()) as client:
            return await _exercise(client)

    result = _run(go())
    assert result["tools"] == set(DOCS_TOOLS)
    _check(result)


def test_read_refuses_paths_outside_the_packaged_docs() -> None:
    async def go() -> list[str]:
        out = []
        async with Client(create_docs_mcp_server()) as client:
            for ref in ("../../etc/passwd", "file:///etc/passwd", "src/autoair/settings.py"):
                result = await client.call_tool(
                    "read_autoair_doc", {"page": ref}, raise_on_error=False
                )
                assert result.is_error
                out.append(result.content[0].text)  # type: ignore[union-attr]
            bad = await client.call_tool(
                "read_autoair_doc", {"page": "ai/mcp", "max_chars": 10**7}, raise_on_error=False
            )
            assert bad.is_error  # outside the schema bounds
        return out

    for text in _run(go()):
        assert "no AutoAir documentation page" in text or "not an AutoAir" in text
        assert "root:" not in text


def test_main_server_has_the_docs_tools_and_the_feature_switch_hides_them(tmp_path: Path) -> None:
    def tools(**features: bool) -> set[str]:
        services = build_services(
            Settings(dags_folder=tmp_path, subfolder="", ai={"enabled": False}, features=features),
            bearer_passthrough(None),
        )

        async def go() -> set[str]:
            async with Client(create_mcp_server(services)) as client:
                names = {t.name for t in await client.list_tools()}
                if features.get("autoair_docs") is False:
                    refused = await client.call_tool(
                        "search_autoair_docs", {"query": "x"}, raise_on_error=False
                    )
                    assert refused.is_error
                    assert "autoair_docs" in refused.content[0].text  # type: ignore[union-attr]
                return names

        return _run(go())

    assert set(DOCS_TOOLS) <= tools()
    assert not set(DOCS_TOOLS) & tools(autoair_docs=False)
    # The documentation tools need neither the AI layer nor the knowledge base.
    assert set(DOCS_TOOLS) <= tools(ai=False, kb=False)


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONUNBUFFERED": "1"}


def _command() -> list[str]:
    """``autoair docs-mcp`` through the console script when it is installed."""
    script = Path(sys.executable).with_name("autoair")
    if script.exists():
        return [str(script), "docs-mcp"]
    return [sys.executable, "-m", "autoair.docs.mcp"]


def test_docs_mcp_over_stdio() -> None:
    command = _command()
    transport = StdioTransport(command=command[0], args=command[1:], env=_env())

    async def go() -> dict[str, Any]:
        async with Client(transport) as client:
            return await _exercise(client)

    result = _run(go())
    assert result["tools"] == set(DOCS_TOOLS)
    _check(result)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_docs_mcp_over_http_on_a_free_port() -> None:
    port = _free_port()
    proc = subprocess.Popen(
        [*_command(), "--http", "--port", str(port)],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert proc.poll() is None, proc.stdout.read().decode() if proc.stdout else ""
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)

        async def go() -> dict[str, Any]:
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                return await _exercise(client)

        result = _run(go())
        assert result["tools"] == set(DOCS_TOOLS)
        _check(result)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_docs_mcp_without_fastmcp_names_the_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import autoair.mcp.availability as availability

    monkeypatch.setattr(availability, "mcp_runtime_available", lambda: False)
    with pytest.raises(SystemExit) as exc:
        docs_mcp.main([])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "autoair[mcp]" in err
    assert "uvx --from 'autoair[mcp]' autoair docs-mcp" in err


def test_docs_mcp_help_shows_agent_setup(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        docs_mcp.main(["--help"])
    out = capsys.readouterr().out
    assert "claude mcp add" in out
    assert "--http" in out
