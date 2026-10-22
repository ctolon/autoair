"""FastAPI dependencies: resolve the caller's :class:`Principal` once per request and
enforce AutoAir permissions with :func:`require`.

``current_principal`` is a placeholder that :func:`install` overrides per application
(``app.dependency_overrides``), because how a principal is resolved depends on the auth
backend (plugin: Airflow's ``get_user`` + auth manager; standalone: the verified token).
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.params import Depends as DependsParam

from autoair.auth import _soft, forwarded_credentials
from autoair.governance import identity_from_claims, identity_from_user, merge_identity
from autoair.iam.config import IamSettings
from autoair.iam.model import CODE_EXECUTE, CODE_RAW, Principal
from autoair.iam.resolver import (
    CustomViewCheck,
    Identity,
    airflow_user_name,
    airflow_user_roles,
    claim_roles,
    claim_user,
    jwt_claims,
    resolve,
)

if TYPE_CHECKING:
    from autoair.api.app import Services

log = logging.getLogger(__name__)
STATE_ATTR = "autoair_principal"


async def current_principal(request: Request) -> Principal:
    """The caller's principal (overridden by :func:`install`)."""
    principal: Principal | None = getattr(request.state, STATE_ATTR, None)
    if principal is None:  # pragma: no cover - install() always overrides this
        raise HTTPException(status_code=500, detail="AutoAir IAM is not configured")
    return principal


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def iam_enabled(services: Any) -> bool:
    """``features.iam``: AutoAir's own authorization layer is on (the default)."""
    settings = getattr(services, "settings", None)
    feature = getattr(settings, "feature", None)
    if not callable(feature):
        return True
    try:
        return bool(feature("iam"))
    except ValueError:  # settings stand-ins (tests, embedders) without the flag
        return True


def iam_without_roles(configured: IamSettings) -> IamSettings:
    """The IAM settings in force when ``features.iam`` is off: AutoAir adds no roles of its
    own. Every signed-in user gets the ``Authenticated`` baseline plus the
    roles Airflow's DAG read/edit and configuration edit permissions grant (the built-in
    ``airflow_permissions`` defaults); no role mapping, custom roles or custom views, no
    four-eyes approvals, and nobody may bypass a git target's mode (no direct pushes, no
    forced pull requests). Sign-in, auditing, the anonymous (``--no-auth``) roles and
    ``airflow_permission_roles.unverified`` (standalone) keep their configured values."""
    from autoair.iam.config import AirflowPermissionRoles

    return configured.model_copy(
        update={
            "strategies": ["airflow_permissions"],
            # What an uncheckable permission grants (standalone) stays configurable: the
            # default is Viewer, and nothing else could grant more with IAM off.
            "airflow_permission_roles": AirflowPermissionRoles(
                unverified=list(configured.airflow_permission_roles.unverified)
            ),
            "role_mapping": {},
            "role_prefix": "",
            "roles": {},
            "default_roles": [],
            "direct_push_roles": [],
            "force_pr_without_direct_push": False,
            "require_approval": False,
        }
    )


def iam_settings(services: Any) -> IamSettings:
    """The IAM settings in force (``iam.*``, or :func:`iam_without_roles` with features.iam off)."""
    iam = getattr(getattr(services, "settings", None), "iam", None)
    configured = iam if isinstance(iam, IamSettings) else IamSettings()
    return configured if iam_enabled(services) else iam_without_roles(configured)


def direct_push_roles(services: Services, team: str | None = None) -> list[str]:
    """Roles that may push directly to the deploy target used for ``team``:
    ``storage.git.direct_push_roles`` when set (``[]``: nobody), else
    ``iam.direct_push_roles``. The single source of this rule (deploy route, ``/api/me``).
    With features.iam off nobody may: the target's mode decides."""
    if not iam_enabled(services):
        return []
    settings = services.settings
    storage = settings.team_storage.get(team, settings.storage) if team else settings.storage
    roles = getattr(storage.git, "direct_push_roles", None)
    if roles is not None:
        return list(roles)
    return list(iam_settings(services).direct_push_roles)


class CustomViewCache:
    """Answers of the auth manager's ``is_authorized_custom_view`` per (user, token), kept
    for ``ttl`` seconds: the ``custom_view`` strategy asks one question per AutoAir
    permission, which would otherwise be ~20 synchronous checks on every request."""

    def __init__(self, ttl: float, max_entries: int = 1024) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], tuple[float, dict[tuple[str, str], bool]]]
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def wrap(self, key: tuple[str, str], check: CustomViewCheck) -> CustomViewCheck:
        if self.ttl <= 0:
            return check
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] <= now:
                entry = (now + self.ttl, {})
                self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        answers = entry[1]

        def cached(method: str, resource: str) -> bool:
            q = (method, resource)
            if q not in answers:
                answers[q] = check(method, resource)  # exceptions are not cached
            return answers[q]

        return cached


def _identity(
    services: Services,
    request: Request,
    user: Any,
    checks: dict[str, tuple[bool, bool]],
    cache: CustomViewCache | None = None,
) -> Identity:
    auth = services.auth
    iam = iam_settings(services)
    if auth.name == "none" and not auth.app:
        return Identity(user=None, authenticated=False)
    if auth.roles is not None:  # autoair local: the token holder's configured roles
        return Identity(user=None, fixed_roles=list(auth.roles))
    if user is not None:  # plugin mode: Airflow's auth manager user
        custom = None
        name = airflow_user_name(user)
        if "custom_view" in iam.strategies and auth.custom_view is not None:
            custom = auth.custom_view(user)
            if custom is not None and cache is not None and name:
                token = forwarded_credentials(request)[0].get("Authorization", "")
                key = (name, hashlib.sha256(token.encode()).hexdigest())
                custom = cache.wrap(key, custom)
        token = forwarded_credentials(request)[0].get("Authorization", "")
        who = merge_identity(
            identity_from_user(user, name), identity_from_claims(jwt_claims(token), name)
        )
        return Identity(
            user=name,
            airflow_roles=airflow_user_roles(user),
            checks=checks,
            custom_view=custom,
            display_name=who.display_name,
            email=who.email,
        )
    token = forwarded_credentials(request)[0].get("Authorization", "")
    claims = jwt_claims(token)
    # Only a token Airflow has verified (TokenVerifier) may carry trusted role claims.
    trusted = auth.verifier is not None
    who = identity_from_claims(claims, claim_user(claims))
    return Identity(
        user=who.username,
        airflow_roles=claim_roles(claims, iam.roles_claims) if trusted else [],
        checks=checks,
        display_name=who.display_name,
        email=who.email,
    )


def principal_resolver(services: Services) -> Any:
    """The dependency that replaces :func:`current_principal` for this application."""
    auth = services.auth
    iam = iam_settings(services)
    groups: dict[str, list[DependsParam]] = {}
    if "airflow_permissions" in iam.strategies:
        groups = {
            "dag_read": list(auth.read),
            "dag_edit": list(auth.write_dag),
            "configuration_edit": list(auth.admin),
        }
    if "airflow_permissions" in iam.strategies and not all(groups.values()):
        log.info(
            "Airflow's DAG/configuration permissions cannot be checked here (standalone "
            "mode): the 'airflow_permissions' strategy grants signed-in users only "
            "iam.airflow_permission_roles.unverified (%s); use the 'role_mapping' strategy or "
            "iam.default_roles to grant more",
            iam.airflow_permission_roles.unverified,
        )
    cache = CustomViewCache(iam.custom_view_cache_seconds)
    params = [
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)
    ]
    if auth.user is not None:
        params.append(
            inspect.Parameter(
                "airflow_user", inspect.Parameter.KEYWORD_ONLY, default=auth.user, annotation=Any
            )
        )
    for name, deps in groups.items():
        for i, dep in enumerate(deps):
            params.append(
                inspect.Parameter(
                    f"{name}__{i}",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=_soft(dep),
                    annotation=bool,
                )
            )

    def resolve_principal(request: Request, **kwargs: Any) -> Principal:
        # sync: FastAPI runs it in a thread (auth managers may query their database)
        checks: dict[str, tuple[bool, bool]] = {}
        for name, deps in groups.items():
            granted = all(kwargs[f"{name}__{i}"] for i in range(len(deps)))
            checks[name] = (granted, bool(deps))
        identity = _identity(services, request, kwargs.get("airflow_user"), checks, cache)
        principal = resolve(
            identity,
            iam,
            auth=auth.name,
            mode=services.settings.mode,
            direct_push_roles=direct_push_roles(services),
        )
        setattr(request.state, STATE_ATTR, principal)
        return principal

    resolve_principal.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        params, return_annotation=Principal
    )
    return resolve_principal


def install(api: FastAPI, services: Services) -> None:
    api.dependency_overrides[current_principal] = principal_resolver(services)


async def deny(
    request: Request, principal: Principal, permission: str, *, detail: str | None = None
) -> HTTPException:
    """Audit a denial and return the 403 to raise."""
    services: Services | None = getattr(request.app.state, "services", None)
    if services is not None and iam_settings(services).audit_denials:
        await run_in_threadpool(
            services.audit.record,
            "permission_denied",
            user=principal.user,
            dag_id=request.path_params.get("dag_id"),
            permission=permission,
            method=request.method,
            path=request.url.path,
            roles=principal.roles,
        )
    return HTTPException(
        status_code=403, detail=detail or f"missing AutoAir permission {permission!r}"
    )


async def check(
    request: Request, principal: Principal, *permissions: str, detail: str | None = None
) -> None:
    """Raise 403 (audited) unless ``principal`` holds every permission."""
    for permission in permissions:
        if not principal.has(permission):
            raise await deny(request, principal, permission, detail=detail)


def require(*permissions: str) -> DependsParam:
    """Dependency: the caller must hold every one of ``permissions``."""
    wanted = tuple(permissions)

    async def required(request: Request, principal: PrincipalDep) -> None:
        await check(request, principal, *wanted)

    required.__name__ = "require_" + "_".join(p.replace(":", "_") for p in wanted)
    dep: DependsParam = Depends(required)
    return dep


async def require_permission(request: Request, permission: str, *, action: str) -> None:
    """In-route check for inputs that are privileged only sometimes (e.g. ``/validate``
    with ``import_check=true`` executes DAG code)."""
    principal: Principal | None = getattr(request.state, STATE_ATTR, None)
    if principal is None:
        raise HTTPException(status_code=403, detail=f"{action} is not allowed")
    detail = None
    if permission == CODE_EXECUTE:
        detail = f"{action} requires DAG edit permission (AutoAir permission {permission!r})"
    elif permission == CODE_RAW:
        detail = (
            f"{action} stores hand-written Python (source.code), which needs the AutoAir "
            f"permission {permission!r}"
        )
    await check(request, principal, permission, detail=detail)
