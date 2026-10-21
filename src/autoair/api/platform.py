"""Server-side platform concerns of the API/app: request limits, rate limits, security
headers, CSRF, CORS and the Airflow session cookie. ``create_api``/``create_app`` call
:func:`install_api` / :func:`install_app`; everything is driven by settings
(``server.*``, ``limits.*``, ``sso.*``, ``cors_*``)."""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autoair.auth import configure_session_cookie

if TYPE_CHECKING:
    from autoair.api.app import Services

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- rate limits
def rate_group(method: str, path: str) -> str | None:
    """The ``limits.rate_limits`` group of an API route (``path`` is the route template
    inside the API app, e.g. ``/specs/{dag_id}``)."""
    if method == "POST" and path == "/validate":
        return "validate"
    if method == "POST" and path.startswith("/interchange/") and path.endswith("/import"):
        return "import"
    if method == "POST" and path.startswith("/ai/"):
        return "ai"
    if method == "POST" and path.startswith("/runner/"):
        return "test_runs"
    if method == "POST" and path == "/catalog/refresh":
        return "catalog_refresh"
    if path == "/specs/{dag_id}" and method in ("PUT", "DELETE"):
        return "deploy"
    if method == "POST" and (
        path == "/specs/{dag_id}/proposals/{etag}/deploy" or path == "/vcs/{dag_id}/promote"
    ):
        return "deploy"
    return None


class RateLimiter:
    """Sliding-window request counter per (user, group), in memory (per process)."""

    def __init__(self, window: float, max_keys: int) -> None:
        self.window = window
        self.max_keys = max(1, max_keys)
        self._hits: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: tuple[str, str], limit: int) -> float | None:
        """Count a request; the seconds to wait when ``limit`` is already reached."""
        if limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
                while len(self._hits) > self.max_keys:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= limit:
                return hits[0] + self.window - now
            hits.append(now)
        return None


def effective_limit(limits: Any, group: str, roles: list[str]) -> int:
    """The most generous limit among the caller's roles (0 = unlimited wins)."""
    base = int(limits.rate_limits.get(group, 0))
    candidates = [
        int(limits.role_rate_limits[r][group])
        if r in limits.role_rate_limits and group in limits.role_rate_limits[r]
        else base
        for r in roles
    ] or [base]
    if any(c <= 0 for c in candidates):
        return 0
    return max(candidates)


async def rate_limit(request: Request) -> None:
    """Router dependency: 429 (with Retry-After) above ``limits.rate_limits``."""
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:
        return
    limits = services.settings.limits
    if not limits.rate_limits_enabled:
        return
    route = request.scope.get("route")
    group = rate_group(request.method, getattr(route, "path", request.url.path))
    if group is None:
        return
    from autoair.iam.deps import STATE_ATTR

    principal = getattr(request.state, STATE_ATTR, None)
    roles = list(getattr(principal, "roles", []) or [])
    limit = effective_limit(limits, group, roles)
    user = getattr(principal, "user", None)
    who = f"user:{user}" if user else f"ip:{request.client.host if request.client else '?'}"
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = request.app.state.rate_limiter = RateLimiter(
            limits.window_seconds, limits.max_tracked_users
        )
    wait = limiter.hit((who, group), limit)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit for {group} reached ({limit} per {limits.window_seconds:g}s)",
            headers={"Retry-After": str(int(wait) + 1)},
        )


# --------------------------------------------------------------------------- body limit
class BodyLimitMiddleware:
    """413 for request bodies above ``server.max_body_bytes`` (Content-Length or
    streamed)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = False
                if too_big:
                    await self._reject(scope, receive, send)
                    return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": f"request body larger than {self.max_bytes} bytes"}, status_code=413
        )
        await response(scope, receive, send)


class _TooLarge(Exception):
    pass


# --------------------------------------------------------------------------- headers
def _spa_csp(server: Any, script_hashes: list[str] | None = None) -> str:
    if server.content_security_policy:
        return str(server.content_security_policy)
    scripts = " ".join(["'self'", *(f"'sha256-{h}'" for h in script_hashes or [])])
    ancestors = " ".join(server.frame_ancestors) or "'none'"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {scripts}",
            # Chakra/emotion and CodeMirror inject <style> elements at runtime.
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob: https:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            f"frame-ancestors {ancestors}",
        ]
    )


def script_hash(script: str) -> str:
    return base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()


def page_headers(settings: Any, inline_scripts: list[str]) -> dict[str, str]:
    """Headers of the UI page: its CSP allows exactly its inline scripts (by hash)."""
    server = settings.server
    if not server.security_headers or settings.mode == "plugin":
        return {}
    return {"Content-Security-Policy": _spa_csp(server, [script_hash(s) for s in inline_scripts])}


class SecurityHeadersMiddleware:
    """CSP, X-Content-Type-Options, Referrer-Policy, X-Frame-Options and HSTS (standalone
    server; inside Airflow the api-server owns these headers)."""

    def __init__(self, app: ASGIApp, *, settings: Any) -> None:
        self.app = app
        server = settings.server
        self.headers: list[tuple[bytes, bytes]] = [(b"x-content-type-options", b"nosniff")]
        if server.referrer_policy:
            self.headers.append((b"referrer-policy", server.referrer_policy.encode()))
        ancestors = [a.strip() for a in server.frame_ancestors]
        if not server.content_security_policy:
            if ancestors == ["'none'"]:
                self.headers.append((b"x-frame-options", b"DENY"))
            elif ancestors == ["'self'"]:
                self.headers.append((b"x-frame-options", b"SAMEORIGIN"))
        self.csp = _spa_csp(server).encode()
        self.hsts: bytes | None = None
        if server.hsts_seconds > 0:
            value = f"max-age={server.hsts_seconds}"
            if server.hsts_include_subdomains:
                value += "; includeSubDomains"
            if server.hsts_preload:
                value += "; preload"
            self.hsts = value.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https"

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for name, value in self.headers:
                    if name not in present:
                        headers.append((name, value))
                if b"content-security-policy" not in present:
                    headers.append((b"content-security-policy", self.csp))
                if self.hsts and https:
                    headers.append((b"strict-transport-security", self.hsts))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


# --------------------------------------------------------------------------- install
def install_api(api: FastAPI, services: Services) -> None:
    """Session cookie, public sign-in routes and the rate limiter of the JSON API."""
    from autoair.iam.sso import router as sso_router

    settings = services.settings
    if settings.mode == "plugin":
        configure_session_cookie("_token", enabled=True)
    else:
        configure_session_cookie(
            settings.sso.cookie_name,
            enabled=settings.sso.enabled and settings.mode == "standalone",
        )
    limits = settings.limits
    api.state.rate_limiter = RateLimiter(limits.window_seconds, limits.max_tracked_users)
    api.include_router(sso_router)  # public: it establishes the session


def install_app(app: FastAPI, services: Services) -> None:
    """Middlewares of the outer app (standalone/local server; plugin mode: limits only)."""
    from autoair.iam.sso import CsrfMiddleware

    settings = services.settings
    server = settings.server
    standalone = settings.mode != "plugin"
    cookie_sessions = settings.mode == "standalone" and settings.sso.enabled
    if cookie_sessions and settings.sso.require_csrf_header:
        app.add_middleware(CsrfMiddleware, allowed_origins=list(settings.cors_origins))
    if settings.cors_origins:
        origins = list(settings.cors_origins)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            # With a wildcard Starlette would reflect *any* Origin together with
            # Access-Control-Allow-Credentials: never send credentials cross-site then.
            allow_credentials=settings.cors_allow_credentials and "*" not in origins,
            allow_methods=list(settings.cors_allow_methods),
            allow_headers=list(settings.cors_allow_headers),
            expose_headers=list(settings.cors_expose_headers),
            max_age=settings.cors_max_age,
        )
    if standalone and server.security_headers:
        app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(BodyLimitMiddleware, max_bytes=server.max_body_bytes)


__all__ = [
    "BodyLimitMiddleware",
    "RateLimiter",
    "SecurityHeadersMiddleware",
    "effective_limit",
    "install_api",
    "install_app",
    "page_headers",
    "rate_group",
    "rate_limit",
    "script_hash",
]
