"""Sign-in with the Airflow UI session (see docs/sso.md).

Airflow 3 keeps its UI session in the ``_token`` cookie: a JWT, ``HttpOnly``,
``SameSite=Lax``, scoped to Airflow's *host* (no ``Domain`` attribute) and path. Browsers send
host cookies regardless of the port, so

* **plugin mode** (same origin) and a standalone AutoAir on the **same host** (another port,
  or a path behind the same reverse proxy) receive it on every request: AutoAir verifies it
  against Airflow like a bearer token and the user is signed in without doing anything;
* on a **sibling sub-domain** the browser only sends it when the proxy in front of Airflow
  widens the cookie's scope (``proxy_cookie_domain`` / ``Domain=.example.com``);
* on **another site** no cookie can be shared: the password sign-in (proxied to Airflow's
  ``/auth/token``) or a pasted token remain.

Routes (all public: they establish the session):

``GET /auth/config``     what the sign-in page offers (password form, "Sign in with
                         Airflow", branding);
``GET /auth/session``    the current session (cookie or bearer), verified with Airflow;
``POST /auth/refresh``   asks Airflow to refresh a cookie session (Airflow's JWT refresh
                         middleware re-issues the cookie for auth managers that support it)
                         and relays the new cookie;
``GET /auth/sso/login``  redirects to Airflow's login page (with a return URL when Airflow
                         accepts it as safe: same host, under its base path).

The token never reaches JavaScript: the cookie stays ``HttpOnly`` and every API call carries
it; state-changing calls authenticated by the cookie must send the ``X-AutoAir-CSRF`` header
(:func:`csrf_middleware`).
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx2
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from autoair.auth import session_cookie, session_cookie_name
from autoair.governance import identity_from_claims
from autoair.iam.resolver import claim_user, jwt_claims
from autoair.net import tls

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

CSRF_HEADER = "X-AutoAir-CSRF"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class Branding(BaseModel):
    title: str = "AutoAir"
    logo_url: str | None = None
    accent_color: str | None = None
    banner_text: str | None = None
    banner_color: str = "orange"
    default_language: str | None = None
    default_timezone: str | None = None
    date_locale: str | None = None


class SsoInfo(BaseModel):
    enabled: bool = Field(description="The Airflow UI session cookie is accepted.")
    cookie_name: str
    airflow_login_url: str | None = Field(
        default=None, description="'Sign in with Airflow' (AutoAir route redirecting there)."
    )
    refresh_margin_seconds: int
    session_check_seconds: int
    propagate_logout: bool


class AuthConfig(BaseModel):
    mode: str
    auth: str = Field(description="airflow (plugin), bearer, static (local token) or none.")
    login_enabled: bool
    sso: SsoInfo
    ui: Branding


class SessionInfo(BaseModel):
    authenticated: bool
    source: Literal["cookie", "bearer", "none"] = "none"
    user: str | None = None
    display_name: str | None = None
    expires_at: str | None = None
    expires_in: float | None = Field(default=None, description="Seconds until expiry.")
    verified: bool = Field(
        default=False, description="Airflow confirmed the token (standalone verifier)."
    )
    refreshable: bool = Field(
        default=False,
        description="POST /auth/refresh can renew it (cookie sessions; bearer tokens from "
        "the password sign-in cannot be renewed without signing in again).",
    )


def _services(request: Request) -> Any:
    return request.app.state.services


def _settings(request: Request) -> Any:
    return _services(request).settings


def branding(settings: Any) -> Branding:
    ui = settings.ui
    return Branding(**ui.model_dump())


def _airflow_public(settings: Any) -> str:
    return str(settings.sso.airflow_public_url or settings.airflow_url).rstrip("/")


@router.get("/config", operation_id="getAuthConfig")
async def auth_config(request: Request) -> AuthConfig:
    """What the sign-in page offers (no authentication needed)."""
    services = _services(request)
    settings = services.settings
    sso = settings.sso
    login = getattr(services, "login", None)
    cookie_on = settings.mode == "plugin" or (sso.enabled and settings.mode == "standalone")
    root = request.scope.get("root_path", "")
    return AuthConfig(
        mode=settings.mode,
        auth=services.auth.name,
        login_enabled=login is not None and login.settings.login_enabled,
        sso=SsoInfo(
            enabled=cookie_on,
            cookie_name=session_cookie_name(),
            airflow_login_url=f"{root}/auth/sso/login" if cookie_on else None,
            refresh_margin_seconds=sso.refresh_margin_seconds,
            session_check_seconds=sso.session_check_seconds,
            propagate_logout=sso.propagate_logout,
        ),
        ui=branding(settings),
    )


def _expiry(token: str) -> tuple[str | None, float | None]:
    exp = jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return None, None
    at = datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).isoformat()
    return at, float(exp) - time.time()


def _describe(token: str, source: Literal["cookie", "bearer"], verified: bool) -> SessionInfo:
    claims = jwt_claims(token)
    at, remaining = _expiry(token)
    who = identity_from_claims(claims, claim_user(claims))
    return SessionInfo(
        authenticated=True,
        source=source,
        user=who.username,
        display_name=who.display_name,
        expires_at=at,
        expires_in=remaining,
        verified=verified,
        refreshable=source == "cookie",
    )


@router.get("/session", operation_id="getAuthSession")
async def auth_session(request: Request, response: Response) -> SessionInfo:
    """The caller's Airflow session: a bearer token, else the Airflow UI cookie, verified
    against Airflow (standalone). 401 when there is none or it is no longer valid — e.g.
    after signing out of Airflow (the UI then signs AutoAir out too)."""
    response.headers["Cache-Control"] = "no-store"
    services = _services(request)
    if services.auth.name == "none":  # authentication disabled (--no-auth / local)
        return SessionInfo(authenticated=True, source="none", verified=True)
    header = request.headers.get("authorization", "")
    cookie = session_cookie(request)
    source: Literal["cookie", "bearer"] = "bearer" if header else "cookie"
    token = header.removeprefix("Bearer ").strip() if header else (cookie or "")
    if not token:
        raise HTTPException(status_code=401, detail="no Airflow session")
    if services.auth.name == "static":
        return SessionInfo(authenticated=True, source="bearer", verified=True)
    _at, remaining = _expiry(token)
    if remaining is not None and remaining <= 0:
        raise HTTPException(status_code=401, detail="Airflow token expired")
    verifier = getattr(services.auth, "verifier", None)
    if verifier is not None:
        await verifier(request)  # 401 when Airflow rejects it
    return _describe(token, source, verifier is not None)


def _cookie_secure(request: Request, settings: Any) -> bool:
    choice = settings.server.cookie_secure
    if choice == "always":
        return True
    if choice == "never":
        return False
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto.split(",")[0].strip() == "https"


def set_session_cookie(
    response: Response, request: Request, value: str, *, max_age: int | None
) -> None:
    settings = _settings(request)
    response.set_cookie(
        session_cookie_name(),
        value,
        path="/",
        httponly=True,
        secure=_cookie_secure(request, settings),
        samesite=settings.server.cookie_samesite,
        max_age=max_age,
    )


@router.post(
    "/refresh",
    operation_id="refreshAuthSession",
    responses={401: {"description": "The Airflow session has ended (cookie cleared)."}},
)
async def refresh_session(request: Request, response: Response) -> SessionInfo:
    """Renew a cookie session through Airflow: any request carrying the cookie lets
    Airflow's JWT refresh middleware re-issue it (auth managers with refreshable sessions,
    e.g. Keycloak); the new cookie is passed on. Bearer tokens cannot be renewed (409)."""
    response.headers["Cache-Control"] = "no-store"
    settings = _settings(request)
    cookie = session_cookie(request)
    if request.headers.get("authorization") or not cookie:
        raise HTTPException(
            status_code=409, detail="only Airflow UI sessions can be refreshed; sign in again"
        )
    name = session_cookie_name()
    services = _services(request)
    transport = getattr(services, "sso_transport", None)
    try:
        async with httpx2.AsyncClient(
            timeout=settings.auth_timeout,
            transport=transport,
            follow_redirects=False,
            **tls.httpx_kwargs("airflow", transport=transport, asynchronous=True),
        ) as client:
            resp = await client.get(
                f"{settings.airflow_url.rstrip('/')}/api/v2/version",
                headers={"Authorization": f"Bearer {cookie}", "Cookie": f"{name}={cookie}"},
            )
    except httpx2.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"cannot reach Airflow: {exc}") from exc
    renewed = resp.cookies.get(name)
    if resp.status_code in (401, 403) or renewed == "":
        ended = JSONResponse({"detail": "the Airflow session has ended"}, status_code=401)
        set_session_cookie(ended, request, "", max_age=0)
        return ended  # type: ignore[return-value]
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Airflow answered {resp.status_code}")
    token = cookie
    if renewed:
        token = renewed
        _at, remaining = _expiry(renewed)
        set_session_cookie(response, request, renewed, max_age=None)
        verifier = getattr(services.auth, "verifier", None)
        if verifier is not None:
            verifier.forget(f"Bearer {cookie}")
        log.debug("Airflow session refreshed (expires in %ss)", remaining)
    return _describe(token, "cookie", True)


def _safe_next(value: str | None) -> str:
    """Only same-site relative paths (no scheme, no ``//host``, no backslashes)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _accepted_by_airflow(return_url: str, airflow: str) -> bool:
    """Airflow's ``is_safe_url``: same scheme+host+port as its base URL, under its path."""
    target, base = urlsplit(return_url), urlsplit(airflow)
    if (target.scheme, target.netloc) != (base.scheme, base.netloc):
        return False
    base_path = base.path.rstrip("/")
    return not base_path or target.path == base_path or target.path.startswith(base_path + "/")


@router.get("/sso/login", operation_id="ssoLogin", include_in_schema=False)
async def sso_login(request: Request, next: str | None = None) -> RedirectResponse:
    """Send the browser to Airflow's login page. Airflow redirects back after signing in
    only to URLs on its own host (``is_safe_url``); otherwise the user lands in Airflow and
    the AutoAir tab (which polls ``/auth/session``) picks the session up once the cookie is
    visible to it."""
    settings = _settings(request)
    if settings.mode == "standalone" and not settings.sso.enabled:
        raise HTTPException(status_code=404, detail="sign-in with Airflow is disabled")
    airflow = _airflow_public(settings)
    path = _safe_next(next)
    public = settings.server.public_url
    params: dict[str, str] = {}
    if public:
        origin = urlsplit(public)
        return_url = f"{origin.scheme}://{origin.netloc}{path}"
        if _accepted_by_airflow(return_url, airflow):
            params["next"] = return_url
    url = f"{airflow}/auth/login" + (f"?{urlencode(params)}" if params else "")
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- CSRF
class CsrfMiddleware:
    """State-changing requests authenticated only by the session cookie need the
    ``X-AutoAir-CSRF`` header (a cross-site form or ``fetch`` cannot set it without a CORS
    preflight AutoAir refuses) and, when the browser sends one, a same-origin or allowed
    ``Origin``. ``SameSite=Lax`` alone still lets sibling sites of the registrable domain (or
    other ports of the same host) post with the cookie."""

    def __init__(self, app: ASGIApp, *, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed = {o.rstrip("/") for o in allowed_origins if o != "*"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        problem = self._problem(scope, headers)
        if problem is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": problem}, status_code=403)
        await response(scope, receive, send)

    def _problem(self, scope: Scope, headers: dict[str, str]) -> str | None:
        if headers.get("authorization"):
            return None  # bearer tokens are not sent automatically by browsers
        cookies = headers.get("cookie", "")
        name = session_cookie_name()
        if not any(part.strip().startswith(f"{name}=") for part in cookies.split(";")):
            return None
        if not headers.get(CSRF_HEADER.lower()):
            return f"missing {CSRF_HEADER} header (cookie-authenticated request)"
        origin = headers.get("origin")
        if origin and origin != "null":
            host = headers.get("x-forwarded-host") or headers.get("host", "")
            proto = headers.get("x-forwarded-proto") or scope.get("scheme", "http")
            same = f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"
            if origin.rstrip("/") != same and origin.rstrip("/") not in self.allowed:
                return "cross-origin request refused"
        return None


__all__ = [
    "CSRF_HEADER",
    "AuthConfig",
    "Branding",
    "CsrfMiddleware",
    "SessionInfo",
    "auth_config",
    "branding",
    "router",
]
