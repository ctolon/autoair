"""``/api/admin/mcp-servers``: external MCP servers the agents may use (admin panel).

Listing needs ``admin:panel`` (the router's dependency); creating, replacing, deleting and
testing a server need ``admin:config``. Servers from the configuration are shown read-only;
servers stored here are HTTP/SSE only (never a command) and their header values are kept
encrypted and never returned.
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Request

from autoair.ai.assistants.mcp_client import (
    McpError,
    McpServerInput,
    McpServerView,
    McpToolInfo,
    list_server_tools,
    server_by_name,
    server_store_for,
    server_views,
)
from autoair.ai.config import ai_settings_of
from autoair.api.deps import ServicesDep
from autoair.audit import request_user
from autoair.iam import model as P
from autoair.iam.deps import require

router = APIRouter(prefix="/admin", tags=["admin"])

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _check_name(name: str, services: ServicesDep) -> None:
    if not _NAME.fullmatch(name):
        raise HTTPException(
            status_code=422, detail="name: lowercase letters, digits, '-' and '_' (max 40)"
        )
    configured = {s.name for s in ai_settings_of(services.settings).mcp_client.servers}
    if name in configured:
        raise HTTPException(
            status_code=409, detail=f"{name!r} is defined in the configuration (read-only)"
        )


@router.get("/mcp-servers", operation_id="adminMcpServers")
async def list_mcp_servers(services: ServicesDep) -> list[McpServerView]:
    """Every external MCP server (configuration and admin panel), secrets hidden."""
    views: list[McpServerView] = await asyncio.to_thread(server_views, services)
    return views


@router.put(
    "/mcp-servers/{name}",
    operation_id="adminPutMcpServer",
    dependencies=[require(P.ADMIN_CONFIG)],
)
async def put_mcp_server(
    name: str, body: McpServerInput, request: Request, services: ServicesDep
) -> McpServerView:
    _check_name(name, services)
    store = server_store_for(services)
    try:
        data = await asyncio.to_thread(store.put, name, body, request_user(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(
        services.audit.record,
        "ai_mcp_server_put",
        user=request_user(request),
        server=name,
        url=body.url,
        tools=body.tools,
        headers=sorted(data["headers"]),
        enabled=body.enabled,
    )
    return next(v for v in await asyncio.to_thread(server_views, services) if v.name == name)


@router.delete(
    "/mcp-servers/{name}",
    operation_id="adminDeleteMcpServer",
    dependencies=[require(P.ADMIN_CONFIG)],
)
async def delete_mcp_server(name: str, request: Request, services: ServicesDep) -> McpServerView:
    _check_name(name, services)
    views = await asyncio.to_thread(server_views, services)
    view = next((v for v in views if v.name == name), None)
    if view is None:
        raise HTTPException(status_code=404, detail="no such MCP server")
    await asyncio.to_thread(server_store_for(services).delete, name)
    await asyncio.to_thread(
        services.audit.record, "ai_mcp_server_delete", user=request_user(request), server=name
    )
    return view


@router.post(
    "/mcp-servers/{name}/test",
    operation_id="adminTestMcpServer",
    dependencies=[require(P.ADMIN_CONFIG)],
)
async def test_mcp_server(name: str, services: ServicesDep) -> list[McpToolInfo]:
    """Connect and list the server's tools (with whether the allow-list permits each)."""
    try:
        server = await asyncio.to_thread(server_by_name, services, name)
        tools: list[McpToolInfo] = await list_server_tools(services, server)
    except McpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return tools


__all__ = ["router"]
