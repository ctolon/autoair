"""Standalone sign-in: exchange an Airflow username/password for an Airflow access token.

AutoAir never stores or logs the password: it is forwarded once to Airflow's token endpoint
(``POST {airflow_url}/auth/token``, served by SimpleAuthManager, FAB and Keycloak) and the
returned JWT goes back to the browser, which then sends it as a bearer token like a pasted
one. Failed attempts are rate-limited per client IP and per username (in memory).
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Any

import httpx2
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

from autoair.auth import forwarded_credentials, session_cookie
from autoair.iam.config import IamSettings
from autoair.iam.resolver import claim_user, jwt_claims
from autoair.net import tls

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=4096)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    user: str | None = None
    expires_at: str | None = Field(
        default=None, description="Token expiry (ISO 8601), when the token carries one."
    )


class LogoutResponse(BaseModel):
    logged_out: bool
    revoked: bool = Field(description="Airflow revoked the token (Airflow >= 3.2).")


class LoginLimiter:
    """Sliding-window counter of failed sign-ins per key (client IP, username).

    An attempt is *reserved* (counted as a failure) before Airflow is asked, under the lock,
    so concurrent requests cannot all pass the check; a successful or inconclusive attempt is
    refunded afterwards. Keys live in a bounded LRU (``max_keys``): memory stays capped under
    a spray of usernames/IPs and every operation is O(1) amortised.
    """

    def __init__(self, max_failures: int, window: float, max_keys: int = 10_000) -> None:
        self.max_failures = max_failures
        self.window = window
        self.max_keys = max(1, max_keys)
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _hits(self, key: str, now: float, *, create: bool) -> deque[float] | None:
        hits = self._failures.get(key)
        if hits is None:
            if not create:
                return None
            hits = self._failures[key] = deque()
            while len(self._failures) > self.max_keys:
                self._failures.popitem(last=False)
        else:
            self._failures.move_to_end(key)
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def _wait(self, keys: list[str], now: float) -> float:
        wait = 0.0
        for key in keys:
            hits = self._hits(key, now, create=False)
            if hits is None:
                continue
            if len(hits) >= self.max_failures:
                wait = max(wait, hits[0] + self.window - now)
            elif not hits:
                self._failures.pop(key, None)
        return wait

    def retry_after(self, keys: list[str]) -> float | None:
        with self._lock:
            return self._wait(keys, time.monotonic()) or None

    def reserve(self, keys: list[str]) -> float | tuple[float, list[str]]:
        """Count an attempt for ``keys`` now, or return the seconds to wait (> 0) when one
        of them is already at the limit. The returned reservation is passed to
        :meth:`refund` when the attempt turns out not to be a failed sign-in."""
        now = time.monotonic()
        with self._lock:
            wait = self._wait(keys, now)
            if wait > 0:
                return wait
            for key in keys:
                hits = self._hits(key, now, create=True)
                assert hits is not None
                hits.append(now)
        return now, list(keys)

    def refund(self, reservation: tuple[float, list[str]]) -> None:
        stamp, keys = reservation
        with self._lock:
            for key in keys:
                hits = self._failures.get(key)
                if hits is None:
                    continue
                with contextlib.suppress(ValueError):
                    hits.remove(stamp)
                if not hits:
                    self._failures.pop(key, None)

    def failed(self, keys: list[str]) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                hits = self._hits(key, now, create=True)
                assert hits is not None
                hits.append(now)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def __len__(self) -> int:
        return len(self._failures)


class LoginService:
    def __init__(
        self,
        airflow_url: str,
        settings: IamSettings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.airflow_url = airflow_url.rstrip("/")
        self.settings = settings
        self.transport = transport
        self.limiter = LoginLimiter(
            settings.login_max_failures,
            settings.login_window_seconds,
            settings.login_max_tracked_keys,
        )

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            timeout=self.settings.login_timeout,
            transport=self.transport,
            follow_redirects=False,
            **tls.httpx_kwargs("login", transport=self.transport, asynchronous=True),
        )


def _keys(request: Request, username: str) -> tuple[str, str]:
    ip = request.client.host if request.client else "unknown"
    user = hashlib.sha256(username.casefold().encode()).hexdigest()
    return f"ip:{ip}", f"user:{user}"


def _service(request: Request) -> LoginService:
    services: Any = request.app.state.services
    login: LoginService | None = getattr(services, "login", None)
    if login is None or not login.settings.login_enabled:
        raise HTTPException(status_code=404, detail="sign-in with a password is not enabled")
    return login


def _expires_at(token: str) -> str | None:
    exp = jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).isoformat()


@router.post("/login", operation_id="login")
async def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Sign in with an Airflow username/password (standalone mode only)."""
    service = _service(request)
    ip_key, user_key = _keys(request, body.username)
    reservation = service.limiter.reserve([ip_key, user_key])
    if not isinstance(reservation, tuple):
        raise HTTPException(
            status_code=429,
            detail="too many failed sign-in attempts; try again later",
            headers={"Retry-After": str(int(reservation) + 1)},
        )
    payload = {"username": body.username, "password": body.password.get_secret_value()}
    try:
        async with service.client() as client:
            resp = await client.post(f"{service.airflow_url}/auth/token", json=payload)
    except httpx2.HTTPError as exc:
        service.limiter.refund(reservation)  # Airflow unreachable: not a failed sign-in
        log.warning("sign-in: cannot reach Airflow (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="cannot reach Airflow") from None
    finally:
        del payload
    if resp.status_code in (400, 401, 403):  # the reserved attempt stays counted
        log.info("sign-in failed for a user from %s", ip_key.removeprefix("ip:"))
        raise HTTPException(
            status_code=401,
            detail="invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    service.limiter.refund(reservation)
    token = None
    if 200 <= resp.status_code < 300:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("access_token"), str):
            token = data["access_token"]
    if not token:
        raise HTTPException(
            status_code=502, detail=f"unexpected answer from Airflow ({resp.status_code})"
        )
    service.limiter.succeeded(user_key)
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(
        access_token=token,
        user=claim_user(jwt_claims(token)) or body.username,
        expires_at=_expires_at(token),
    )


@router.post("/logout", operation_id="logout")
async def logout(request: Request, response: Response) -> LogoutResponse:
    """Forget the caller's token (and ask Airflow to revoke it, best effort). With
    ``sso.propagate_logout`` an Airflow UI session cookie is revoked and cleared too, which
    signs the Airflow UI out where it shares the cookie (docs/sso.md)."""
    service = _service(request)
    authorization = forwarded_credentials(request)[0].get("Authorization")
    services: Any = request.app.state.services
    sso = getattr(services.settings, "sso", None)
    propagate = bool(getattr(sso, "propagate_logout", True))
    if session_cookie(request) and propagate:
        from autoair.iam.sso import set_session_cookie

        set_session_cookie(response, request, "", max_age=0)
    if not authorization or (not propagate and not request.headers.get("authorization")):
        return LogoutResponse(logged_out=True, revoked=False)  # leave Airflow's session alone
    verifier = getattr(services.auth, "verifier", None)
    if verifier is not None:
        verifier.forget(authorization)
    revoked = False
    try:
        async with service.client() as client:
            resp = await client.get(
                f"{service.airflow_url}/api/v2/auth/logout",
                headers={"Authorization": authorization},
            )
        # only a 2xx is a revocation (a redirect to a login page is not)
        revoked = 200 <= resp.status_code < 300
    except httpx2.HTTPError:
        pass
    return LogoutResponse(logged_out=True, revoked=revoked)
