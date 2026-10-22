"""Authentication/authorisation adapters for the two deployment modes."""

from __future__ import annotations

import hashlib
import inspect
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
from fastapi import Depends, HTTPException, Request
from fastapi.params import Depends as DependsParam

from autoair.net import tls

AIRFLOW_TOKEN_COOKIE = "_token"  # noqa: S105 - cookie name, not a secret

# The Airflow UI session cookie accepted as credentials (``sso.cookie_name``/``sso.enabled``;
# plugin mode always uses Airflow's ``_token``). Set by :func:`configure_session_cookie`.
_SESSION_COOKIE: dict[str, Any] = {"name": AIRFLOW_TOKEN_COOKIE, "enabled": True}


def configure_session_cookie(name: str, *, enabled: bool) -> None:
    """Which cookie carries the Airflow session (process-wide, set when the app is built)."""
    _SESSION_COOKIE["name"] = name
    _SESSION_COOKIE["enabled"] = enabled


def session_cookie_name() -> str:
    return str(_SESSION_COOKIE["name"])


def session_cookie(request: Request) -> str | None:
    """The Airflow session token from the cookie, when cookie sessions are accepted."""
    if not _SESSION_COOKIE["enabled"]:
        return None
    return request.cookies.get(session_cookie_name()) or None


def forwarded_credentials(request: Request) -> tuple[dict[str, str], dict[str, str]]:
    """Headers/cookies to forward to Airflow's REST API on behalf of the caller."""
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth
    token = session_cookie(request)
    if token:
        cookies[session_cookie_name()] = token
        headers.setdefault("Authorization", f"Bearer {token}")
    return headers, cookies


@dataclass
class AuthBackend:
    """How callers are authenticated, plus Airflow's standard permission checks.

    ``read``/``write_dag`` stay on routes about one DAG (Airflow's per-DAG access); together
    with ``admin`` they also feed the ``airflow_permissions`` IAM strategy
    (:mod:`autoair.iam`), which decides every route's AutoAir permission.
    """

    app: Sequence[DependsParam] = field(default_factory=list)
    read: Sequence[DependsParam] = field(default_factory=list)
    write_dag: Sequence[DependsParam] = field(default_factory=list)
    # Airflow's "configuration" edit permission in plugin mode (AutoAir Admin by default).
    admin: Sequence[DependsParam] = field(default_factory=list)
    name: str = "none"
    # Plugin mode: dependency returning Airflow's user (``get_user``) for role mapping and
    # custom-view checks.
    user: DependsParam | None = None
    # Plugin mode: ``user -> (method, resource) -> bool`` (Airflow's
    # ``is_authorized_custom_view``) for the ``custom_view`` IAM strategy.
    custom_view: Callable[[Any], Callable[[str, str], bool] | None] | None = None
    # Standalone: the bearer token is verified by Airflow, so its claims can be trusted.
    verifier: TokenVerifier | None = None
    # ``autoair local --token``: the AutoAir roles of whoever holds the token (no Airflow
    # to ask); the IAM strategies do not apply.
    roles: Sequence[str] | None = None


WRITE_DAG_FLAG = "autoair_can_write_dag"


def _soft(dep: DependsParam) -> DependsParam:
    """``dep`` as a dependency returning ``False`` instead of raising 403 (other errors,
    e.g. 401 for a missing login, still propagate). Sub-dependencies of ``dep`` (the
    current user...) are resolved by FastAPI exactly as for ``dep`` itself."""
    inner = dep.dependency
    if inner is None:  # pragma: no cover - Depends() without a callable
        raise ValueError("cannot wrap an empty dependency")
    signature = inspect.signature(inner, eval_str=True).replace(return_annotation=bool)

    if inspect.iscoroutinefunction(inner):

        async def async_check(*args: Any, **kwargs: Any) -> bool:
            try:
                await inner(*args, **kwargs)
            except HTTPException as exc:
                if exc.status_code == 403:
                    return False
                raise
            return True

        async_check.__signature__ = signature  # type: ignore[attr-defined]
        wrapped: DependsParam = Depends(async_check)
        return wrapped

    def check(*args: Any, **kwargs: Any) -> bool:  # sync: FastAPI runs it in a thread
        try:
            inner(*args, **kwargs)
        except HTTPException as exc:
            if exc.status_code == 403:
                return False
            raise
        return True

    check.__signature__ = signature  # type: ignore[attr-defined]
    plain: DependsParam = Depends(check)
    return plain


def permission_flag(deps: Sequence[DependsParam], attr: str = WRITE_DAG_FLAG) -> DependsParam:
    """A dependency that records on ``request.state.<attr>`` whether every one of ``deps``
    grants access, without rejecting the request. For routes where only *some* inputs
    are privileged (e.g. ``/validate?import_check=true`` executes DAG code)."""
    params = [
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)
    ]
    params += [
        inspect.Parameter(
            f"granted_{i}", inspect.Parameter.KEYWORD_ONLY, default=_soft(d), annotation=bool
        )
        for i, d in enumerate(deps)
    ]

    def flag(request: Request, **granted: bool) -> None:
        setattr(request.state, attr, all(granted.values()))

    flag.__signature__ = inspect.Signature(params, return_annotation=None)  # type: ignore[attr-defined]
    flagged: DependsParam = Depends(flag)
    return flagged


def airflow_auth() -> AuthBackend:
    """Reuse Airflow's auth manager (plugin mode). Plugin apps are NOT protected by
    Airflow automatically, so every route depends on ``get_user``."""
    from airflow.api_fastapi.core_api.security import (
        get_user,
        requires_access_configuration,
        requires_access_dag,
    )

    from autoair.iam.resolver import plugin_custom_view

    return AuthBackend(
        app=[Depends(get_user)],
        read=[Depends(requires_access_dag("GET"))],
        write_dag=[Depends(requires_access_dag("PUT"))],
        admin=[Depends(requires_access_configuration("PUT"))],
        name="airflow",
        user=Depends(get_user),
        custom_view=plugin_custom_view,
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


async def _require_token(request: Request) -> None:
    if "Authorization" not in forwarded_credentials(request)[0]:
        raise _unauthorized("Airflow bearer token required")


class TokenVerifier:
    """Checks a bearer token against Airflow (``GET /api/v2/dags?limit=1``).

    Routes that only touch local state (spec files, codegen) never reach Airflow, so
    without this check any string would be accepted as a token. Successful checks are
    cached briefly (keyed by a hash of the header) to avoid a round-trip per request.
    """

    def __init__(
        self,
        airflow_url: str,
        *,
        ttl: float = 60,
        timeout: float = 10,
        transport: httpx2.AsyncBaseTransport | None = None,
        max_entries: int = 4096,
    ) -> None:
        self._url = airflow_url.rstrip("/") + "/api/v2/dags"
        self._max_entries = max(1, max_entries)
        self._ttl = ttl
        self._timeout = timeout
        self._transport = transport
        # LRU of verified tokens: the least recently used one is evicted when full.
        self._valid_until: OrderedDict[str, float] = OrderedDict()

    async def __call__(self, request: Request) -> None:
        auth = forwarded_credentials(request)[0].get("Authorization")
        if not auth:
            raise _unauthorized("Airflow bearer token required")
        key = _token_key(auth)
        now = time.monotonic()
        expires_in = _seconds_to_expiry(auth)
        if expires_in is not None and expires_in <= 0:
            self._valid_until.pop(key, None)
            raise _unauthorized("Airflow token expired")
        if self._valid_until.get(key, 0) > now:
            self._valid_until.move_to_end(key)
            return
        try:
            options = tls.httpx_kwargs("airflow", transport=self._transport, asynchronous=True)
            async with httpx2.AsyncClient(
                timeout=self._timeout, transport=self._transport, **options
            ) as c:
                resp = await c.get(self._url, params={"limit": 1}, headers={"Authorization": auth})
        except httpx2.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"cannot reach Airflow: {exc}") from exc
        if resp.status_code in (401, 403):
            raise _unauthorized("Airflow rejected the token")
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502, detail=f"Airflow auth check failed ({resp.status_code})"
            )
        ttl = self._ttl if expires_in is None else min(self._ttl, expires_in)
        self._valid_until[key] = now + ttl
        self._valid_until.move_to_end(key)
        while len(self._valid_until) > self._max_entries:
            self._valid_until.popitem(last=False)

    def forget(self, authorization: str) -> None:
        """Stop trusting a token (logout) before its cache entry expires."""
        self._valid_until.pop(_token_key(authorization), None)


def _token_key(authorization: str) -> str:
    return hashlib.sha256(authorization.encode()).hexdigest()


def _seconds_to_expiry(authorization: str) -> float | None:
    """Seconds until the JWT's ``exp`` (unverified: only used to stop trusting it early)."""
    from autoair.iam.resolver import jwt_claims

    exp = jwt_claims(authorization).get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return float(exp) - time.time()


def bearer_passthrough(
    verifier: TokenVerifier | None = None, *, required: bool = True
) -> AuthBackend:
    """Standalone mode: the caller's Airflow token is verified (when a ``verifier`` is
    given) and forwarded, so Airflow enforces RBAC for every proxied call."""
    if not required:
        return AuthBackend(name="none")
    if verifier is None:
        return AuthBackend(app=[Depends(_require_token)], name="bearer")

    # A plain function (not the callable instance): older FastAPI releases cannot resolve
    # postponed annotations on ``__call__``.
    async def verify(request: Request) -> None:
        await verifier(request)

    return AuthBackend(app=[Depends(verify)], name="bearer", verifier=verifier)


def static_token(token: str, *, roles: Sequence[str] = ("Admin",)) -> AuthBackend:
    """``autoair local`` (``--token`` or the per-run token): one shared access token (Bearer
    header), compared in constant time, whose holder gets ``roles``. No Airflow is involved."""
    import hmac

    expected = token.encode()

    async def verify(request: Request) -> None:
        header = request.headers.get("authorization", "")
        given = header.removeprefix("Bearer ").strip().encode()
        if not header or not hmac.compare_digest(given, expected):
            raise _unauthorized("access token required (autoair local --token)")

    return AuthBackend(app=[Depends(verify)], name="static", roles=list(roles))


AuthFactory = Callable[[], AuthBackend]
