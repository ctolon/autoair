"""The AI service: AutoAir's AI API (and optionally MCP) as its own ASGI app.

Run it with ``autoair ai-serve`` (or ``python -m autoair.ai.service``) and point the main
server at it with ``AUTOAIR_AI__SERVICE_URL``. It needs the same configuration as the
main server (catalog, state / DAG storage for saved specs, memory, knowledge base), e.g.
the same environment and a shared volume or object-storage URLs.

Authentication:

* requests forwarded by the AutoAir proxy carry an HMAC-signed identity
  (``ai.service.shared_secret``): user and AI role are taken from it;
* direct requests (e.g. MCP clients, other services) need an Airflow bearer token that is
  verified against Airflow (``ai.service.verify_tokens``), like ``autoair serve``.
"""

from __future__ import annotations

import logging
import os
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool

from autoair import __version__
from autoair.ai.access import (
    IDENTITY_HEADER,
    SIGNATURE_HEADER,
    SharedReplayCache,
    verify_identity,
)
from autoair.ai.config import activate_ai, ai_settings_of
from autoair.iam.deps import PrincipalDep, check
from autoair.iam.deps import install as install_iam
from autoair.persistence import document_store, is_sql

log = logging.getLogger("autoair.ai.service")


def _trusted_identity(services: Any) -> Any:
    """Dependency: accept a signed identity from the proxy, else the auth backend."""
    ai = ai_settings_of(services.settings)
    secret = (
        ai.service.shared_secret.get_secret_value().encode() if ai.service.shared_secret else None
    )
    replay = None
    if is_sql(services.settings):
        replay = SharedReplayCache(
            document_store(services.settings, "ai_identity_nonces", file_url=""),
            max_entries=ai.service.replay_cache_max_entries,
        )

    async def dependency(request: Request) -> None:
        payload = request.headers.get(IDENTITY_HEADER)
        signature = request.headers.get(SIGNATURE_HEADER)
        if payload and signature:
            data = (
                await run_in_threadpool(
                    partial(
                        verify_identity,
                        secret,
                        payload,
                        signature,
                        method=request.method,
                        path=request.url.path,
                        query=request.url.query,
                        body=await request.body(),
                        replay=replay,
                    )
                )
                if secret
                else None
            )
            if data is None:
                raise HTTPException(status_code=401, detail="invalid AI service identity")
            request.state.autoair_ai_user = data.get("user")
            request.state.autoair_ai_role = data.get("role")
            return
        if not ai.service.verify_tokens:
            return
        for dep in services.auth.app:
            fn = dep.dependency
            if fn is None:
                continue
            result = fn(request)
            if hasattr(result, "__await__"):
                await result

    return dependency


def create_ai_service(services: Any) -> FastAPI:
    """ASGI app: ``/api/ai/*``, ``/health``, ``/ready`` and (``ai.service.serve_mcp``)
    ``/mcp``."""
    from autoair.ai.prompts import configure
    from autoair.api.routers import ai as ai_router
    from autoair.api.routers.ai_mount import ai_dependencies

    ai = ai_settings_of(services.settings)
    configure(ai.prompts.override_dir)
    activate_ai(services.settings)
    app = FastAPI(
        title="AutoAir AI service",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api = FastAPI(
        title="AutoAir AI API",
        version=__version__,
        separate_input_output_schemas=False,
    )
    from autoair.api.errors import install_document_errors

    install_document_errors(api)
    api.state.services = services
    api.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=6)
    # Unsigned (direct) calls get an IAM principal exactly like the main app and need
    # ``ai:use``; requests signed by the AutoAir proxy were authorised there already.
    install_iam(api, services)

    async def iam_gate(request: Request, principal: PrincipalDep) -> None:
        if getattr(request.state, "autoair_ai_role", None) is not None:
            return
        await check(request, principal, "ai:use")

    protected = APIRouter(dependencies=[Depends(_trusted_identity(services)), Depends(iam_gate)])
    ai_router.include_ai_routers(protected, services, ai_dependencies(services.auth))
    api.include_router(protected)

    @api.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "ai"}

    app.mount("/api", api)

    @app.get("/health", include_in_schema=False)
    def root_health() -> dict[str, str]:
        return {"status": "ok", "service": "ai", "version": __version__}

    @app.get("/ready", include_in_schema=False)
    def ready() -> dict[str, Any]:
        return {"ready": True, "llm": getattr(services, "llm", None) is not None}

    if ai.service.serve_mcp and getattr(services.settings.features, "mcp", True):
        try:
            from autoair.mcp.http import create_mcp_asgi_app
        except ImportError as exc:
            log.info("MCP disabled in the AI service: %s (install autoair[mcp])", exc)
        else:
            app.mount("/mcp", create_mcp_asgi_app(services))
    return app


def build_ai_service(*, no_auth: bool = False) -> FastAPI:
    """Services from the environment (like ``autoair serve``) + the AI app."""
    from autoair.api.app import build_services
    from autoair.auth import TokenVerifier, bearer_passthrough
    from autoair.settings import Settings

    settings = Settings(mode="standalone")
    verifier = TokenVerifier(
        settings.airflow_url, ttl=settings.auth_cache_ttl, timeout=settings.auth_timeout
    )
    auth = bearer_passthrough(verifier, required=not no_auth)
    return create_ai_service(build_services(settings, auth))


def main() -> None:  # pragma: no cover - CLI entry
    import uvicorn

    host = os.environ.get("AUTOAIR_AI_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("AUTOAIR_AI_SERVICE_PORT", "8790"))
    uvicorn.run(build_ai_service(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["build_ai_service", "create_ai_service"]
