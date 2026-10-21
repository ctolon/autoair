"""The knowledge base in the agents (search_docs + citations), prompts, MCP and the
scheduler."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from autoair.kb.service import kb_context, kb_for
from tests.integration.test_iam_api import make_services


def _services(tmp_path: Path, **kb: Any) -> Any:
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "sensors.md").write_text(
        "# Deferrable operators\n\n## Triggerer\n\nDeferrable sensors release the worker slot "
        "and wait in the triggerer.\n"
    )
    config = {
        "sources": [
            {
                "id": "corp",
                "kind": "path",
                "url": str(docs),
                "layout": "files",
                "component": "core",
                "public_base_url": "https://docs.corp/airflow",
            }
        ],
        "refresh_interval_hours": 0,
        "refresh_on_start": False,
        **kb,
    }
    features = config.pop("features", {})
    services = make_services(tmp_path, kb=config, features=features)
    return services


def _indexed(tmp_path: Path, **kb: Any) -> Any:
    services = _services(tmp_path, **kb)
    assert kb_for(services).refresh().outcome == "ok"
    return services


def test_search_docs_tool_returns_citable_kb_hits(tmp_path: Path) -> None:
    from autoair.ai.assistants.sources import SourceCollector
    from autoair.ai.research.tools import research_tools

    services = _indexed(tmp_path)
    tool = next(t for t in research_tools(services) if t.name == "search_docs")
    collector = SourceCollector()
    wrapped = collector.wrap(tool)
    hits = asyncio.run(
        wrapped.handler(tool.input_model(query="triggerer worker slot", scopes=["docs"]))
    )
    assert hits
    assert hits[0]["kind"] == "kb"
    assert hits[0]["cite"] == 1
    assert hits[0]["uri"] == "https://docs.corp/airflow/sensors.md#triggerer"
    assert hits[0]["version"]
    assert hits[0]["source"] == "corp"
    citation = collector.citations[0]
    assert citation.kind == "doc"
    assert citation.ref.startswith("https://docs.corp/")
    assert "Deferrable operators" in citation.title
    # default scopes include the knowledge base next to the local scopes
    mixed = asyncio.run(tool.handler(tool.input_model(query="deferrable triggerer")))
    assert any(h.get("kind") == "kb" for h in mixed)
    # filters reach the knowledge base
    none = asyncio.run(
        tool.handler(tool.input_model(query="triggerer", scopes=["docs"], component="providers"))
    )
    assert none == []


def test_kb_off_hides_it_from_agents_and_prompts(tmp_path: Path) -> None:
    from autoair.ai.research.tools import research_tools

    services = _indexed(tmp_path)
    services.settings = services.settings.model_copy(
        update={"features": services.settings.features.model_copy(update={"kb": False})}
    )
    tool = next(t for t in research_tools(services) if t.name == "search_docs")
    assert asyncio.run(tool.handler(tool.input_model(query="triggerer", scopes=["docs"]))) == []
    assert asyncio.run(kb_context(services, "triggerer")) == ""


def test_prompt_context_carries_the_url(tmp_path: Path) -> None:
    services = _indexed(tmp_path)
    text = asyncio.run(kb_context(services, "deferrable triggerer", max_chars=2000))
    assert "URL: https://docs.corp/airflow/sensors.md#triggerer" in text
    assert "[docs 1] Deferrable operators" in text


def test_mcp_kb_tools(tmp_path: Path) -> None:
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from autoair.kb.mcp import create_kb_mcp_server
    from autoair.mcp.server import create_mcp_server

    services = _indexed(tmp_path)

    async def run(server: Any) -> tuple[set[str], Any, Any, Any]:
        async with Client(server) as c:
            names = {t.name for t in await c.list_tools()}
            search = await c.call_tool("kb_search", {"query": "triggerer", "limit": 3})
            status = await c.call_tool("kb_status", {})
            sources = await c.call_tool("kb_sources", {})
            return names, search, status, sources

    names, search, status, sources = asyncio.run(run(create_mcp_server(services)))
    assert {"kb_search", "kb_sources", "kb_status"} <= names
    hit = search.structured_content["hits"][0]
    assert hit["url"].startswith("https://docs.corp/")
    assert hit["version"]
    assert status.structured_content["store"]["pages"] == 1
    assert sources.structured_content["sources"][0]["id"] == "corp"
    names, *_ = asyncio.run(run(create_kb_mcp_server(lambda: services)))
    assert names == {"kb_search", "kb_sources", "kb_status"}


def test_mcp_kb_tools_respect_the_feature_switch(tmp_path: Path) -> None:
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from autoair.mcp.server import create_mcp_server

    services = _services(tmp_path, features={"kb": False})

    async def run() -> Any:
        async with Client(create_mcp_server(services)) as c:
            return await c.call_tool("kb_search", {"query": "x"}, raise_on_error=False)

    assert asyncio.run(run()).is_error


def test_scheduler_starts_a_due_refresh_once(tmp_path: Path) -> None:
    from autoair.kb.scheduler import check_once

    services = _services(tmp_path, refresh_interval_hours=1, refresh_on_start=True)
    kb = kb_for(services)
    assert check_once(services) is True
    kb.wait(30)
    assert kb.run_status().outcome == "ok"
    assert check_once(services) is False  # refreshed less than an hour ago
    idle = _services(tmp_path / "x", sources=[{"id": "official", "kind": "official"}])
    assert check_once(idle) is False  # nothing usable (air-gapped default)
