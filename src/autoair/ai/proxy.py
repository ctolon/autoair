"""Proxy ``/api/ai/*`` (and optionally ``/mcp``) from the main AutoAir server to a separate
AI service (``ai.service_url``, see :mod:`autoair.ai.service`).

* The caller is authenticated by the main server first (same dependencies as every API
  route); the proxy forwards the caller's ``Authorization`` / Airflow cookie so the AI
  service can call Airflow on the user's behalf, and - with ``ai.service.shared_secret`` -
  an HMAC-signed identity header (user + AI role) the service trusts.
* Responses are streamed through unchanged (status, body, content type), so SSE
  endpoints stream end to end; the upstream request is closed when the client goes away.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from autoair.ai.access import request_role, request_user, sign_identity
from autoair.ai.config import ai_settings_of
from autoair.net import tls

log = logging.getLogger("autoair.ai.proxy")

FORWARD_REQUEST = (
    "authorization",
    "cookie",
    "content-type",
    "accept",
    "accept-language",
    "x-request-id",
    "traceparent",
)
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)


class _Upstream:
    def __init__(self, services: Any) -> None:
        ai = ai_settings_of(services.settings)
        assert ai.service_url
        self.base = ai.service_url.rstrip("/")
        self.timeout = ai.service.timeout_seconds
        self.secret = (
            ai.service.shared_secret.get_secret_value().encode()
            if ai.service.shared_secret
            else None
        )
        #: Tests (and in-process deployments) can inject an ASGI transport.
        self.transport: httpx2.AsyncBaseTransport | None = getattr(
            services, "ai_proxy_transport", None
        )
        self._client: httpx2.AsyncClient | None = None

    def client(self) -> httpx2.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx2.AsyncClient(
                timeout=httpx2.Timeout(self.timeout, connect=10.0),
                transport=self.transport,
                follow_redirects=False,
                **tls.httpx_kwargs("ai", transport=self.transport, asynchronous=True),
            )
        return self._client

    def headers(self, request: Request) -> dict[str, str]:
        out = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST}
        out["accept-encoding"] = "identity"
        out["x-forwarded-for"] = request.client.host if request.client else ""
        return out

    def sign(self, request: Request, upstream: httpx2.Request, body: bytes) -> None:
        """Signed identity bound to exactly this upstream request (method, path, query,
        body hash, nonce); the AI service rejects replays."""
        if self.secret is None:
            return
        upstream.headers.update(
            sign_identity(
                self.secret,
                request_user(request),
                request_role(request),
                method=upstream.method,
                path=upstream.url.path,
                query=upstream.url.query.decode("ascii"),
                body=body,
            )
        )

    async def forward(self, request: Request, url: str) -> Response:
        body = await request.body()
        upstream = self.client().build_request(
            request.method,
            url,
            params=request.query_params,
            content=body,
            headers=self.headers(request),
        )
        self.sign(request, upstream, body)
        try:
            resp = await self.client().send(upstream, stream=True)
        except httpx2.HTTPError as exc:
            log.warning("AI service unreachable: %s", exc)
            raise HTTPException(status_code=502, detail="the AI service is unreachable") from exc
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}

        async def stream() -> AsyncIterator[bytes]:
            # Always release the upstream connection: Starlette skips the background task
            # when the body iterator raises (upstream failed mid-stream) and closes this
            # generator when the client disconnects.
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            except httpx2.HTTPError as exc:
                log.warning("AI service stream aborted: %s", exc)
                raise  # aborts the response, so the client sees an incomplete body
            finally:
                await resp.aclose()

        try:
            return StreamingResponse(
                stream(),
                status_code=resp.status_code,
                headers=headers,
                background=BackgroundTask(resp.aclose),
            )
        except BaseException:
            await resp.aclose()
            raise


def create_proxy_router(services: Any) -> APIRouter:
    upstream = _Upstream(services)
    router = APIRouter(tags=["ai"])

    @router.api_route(
        "/ai/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def proxy_ai(path: str, request: Request) -> Response:
        return await upstream.forward(request, f"{upstream.base}/api/ai/{path}")

    return router


def create_mcp_proxy_app(services: Any) -> FastAPI:
    """``/mcp`` forwarded to the AI service (same auth as the API)."""
    upstream = _Upstream(services)
    backend = getattr(services, "auth", None)
    app = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        dependencies=list(backend.app) if backend is not None else [],
    )

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def proxy_mcp(path: str, request: Request) -> Response:
        return await upstream.forward(request, f"{upstream.base}/mcp/{path}")

    return app


__all__ = ["create_mcp_proxy_app", "create_proxy_router"]
