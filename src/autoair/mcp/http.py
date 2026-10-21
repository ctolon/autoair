"""Streamable-HTTP MCP endpoint for mounting inside the AutoAir FastAPI app.

The endpoint runs the MCP transport in **stateless** mode with a short-lived session
manager per HTTP request. That needs no application lifespan, which matters because
Airflow mounts plugin FastAPI apps as sub-applications whose lifespans never run.
Each MCP request is self-contained (tools here never need server-to-client requests).

Mounting::

    from autoair.mcp.http import create_mcp_asgi_app

    app.mount("/mcp", create_mcp_asgi_app(services))  # clients use .../mcp/

The returned FastAPI sub-app applies ``services.auth.app`` dependencies to every
request (so the plugin's Airflow auth / the standalone bearer verifier protect it) and
the caller's ``Authorization`` header is forwarded to Airflow by the gateway tools.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Response
from fastmcp import FastMCP
from fastmcp.server.http import FastMCPStreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from autoair.mcp.server import create_mcp_server

if TYPE_CHECKING:
    from autoair.auth import AuthBackend


class _ASGIResponse(Response):
    """Hands the raw ASGI request to another app after FastAPI resolved dependencies."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__()
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


def stateless_mcp_asgi(
    server: FastMCP,
    *,
    json_response: bool = True,
    security: TransportSecuritySettings | None = None,
) -> ASGIApp:
    """Raw ASGI app serving ``server`` statelessly (no lifespan required).

    FastMCP's own ``http_app()`` starts its session manager in an ASGI lifespan, which
    Airflow never runs for plugin sub-apps; a per-request stateless manager does not
    need one. The FastMCP request context (``get_http_request``) is bound by FastMCP's
    low-level server, so tools still see the caller's headers.
    """
    lowlevel = server._mcp_server  # FastMCP's low-level server (no public accessor yet)
    settings = security or TransportSecuritySettings(enable_dns_rebinding_protection=False)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        manager = FastMCPStreamableHTTPSessionManager(
            app=lowlevel,
            json_response=json_response,
            stateless=True,
            security_settings=settings,
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)

    return app


def create_mcp_asgi_app(
    services: Any,
    *,
    server: FastMCP | None = None,
    auth: AuthBackend | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    json_response: bool = True,
) -> FastAPI:
    """FastAPI sub-app exposing the AutoAir MCP server over streamable HTTP.

    ``auth`` defaults to ``services.auth``. ``allowed_hosts``/``allowed_origins`` enable
    the SDK's DNS-rebinding protection (recommended when serving without auth on
    localhost); by default it is off because the app sits behind AutoAir's auth.
    """
    mcp = server or create_mcp_server(services)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts or allowed_origins),
        allowed_hosts=list(allowed_hosts or []),
        allowed_origins=list(allowed_origins or []),
    )
    handler = stateless_mcp_asgi(mcp, json_response=json_response, security=security)
    backend = auth if auth is not None else getattr(services, "auth", None)
    dependencies = list(backend.app) if backend is not None else []
    if backend is not None and getattr(services, "settings", None) is not None:
        # Resolve the caller's AutoAir principal (request.state) for the IAM middleware.
        from fastapi import Depends

        from autoair.iam.deps import principal_resolver

        dependencies.append(Depends(principal_resolver(services)))

    app = FastAPI(
        title="AutoAir MCP",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        dependencies=dependencies,
    )
    app.state.mcp_server = mcp

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def mcp_endpoint(path: str) -> Response:
        return _ASGIResponse(handler)

    return app
