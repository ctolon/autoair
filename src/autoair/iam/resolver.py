"""Turn an authenticated Airflow identity into a :class:`~autoair.iam.model.Principal`.

The three strategies (union of those configured in ``iam.strategies``):

``airflow_permissions``
    AutoAir roles follow Airflow's standard permissions: DAG read, DAG edit (for the
    request's ``dag_id`` when there is one) and configuration edit. The default.
``role_mapping``
    Airflow role names (FAB ``user.roles``, SimpleAuthManager ``user.role``, or the verified
    token's role claim in standalone mode) map to AutoAir roles.
``custom_view``
    Every AutoAir permission is asked of the auth manager as a custom view
    (``is_authorized_custom_view``), so admins grant e.g. "can_create on AutoAir Deploy"
    to Airflow roles. Plugin mode only.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from autoair.iam.config import IamSettings
from autoair.iam.model import (
    ALL_PERMISSIONS,
    DEPLOY_DIRECT_PUSH,
    PERMISSIONS,
    Principal,
    canonical_role,
    permissions_of,
    role_table,
)

log = logging.getLogger(__name__)

# (method, resource name) -> granted?
CustomViewCheck = Callable[[str, str], bool]


@dataclass
class Identity:
    """What authentication established about the caller (input of :func:`resolve`)."""

    user: str | None
    authenticated: bool = True
    # Airflow role names — only from a trusted source (auth manager user, verified token).
    airflow_roles: list[str] = field(default_factory=list)
    # ``airflow_permissions`` results: name -> (granted, really checked)
    checks: dict[str, tuple[bool, bool]] = field(default_factory=dict)
    custom_view: CustomViewCheck | None = None
    display_name: str | None = None
    email: str | None = None


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def map_airflow_roles(
    airflow_roles: Iterable[str], settings: IamSettings, table: Mapping[str, set[str]]
) -> list[str]:
    """AutoAir roles for Airflow role names (``role_mapping`` strategy)."""
    mapping = {k.casefold(): v for k, v in settings.role_mapping.items()}
    prefix = settings.role_prefix.casefold()
    out: list[str] = []
    for name in airflow_roles:
        out.extend(mapping.get(name.casefold(), []))
        if prefix and name.casefold().startswith(prefix):
            role = canonical_role(name[len(prefix) :].strip(), table)
            if role:
                out.append(role)
    return out


def custom_view_resource(settings: IamSettings, resource: str) -> str:
    prefix = settings.custom_view_prefix.strip()
    return f"{prefix} {resource}" if prefix else resource


def resolve(
    identity: Identity,
    settings: IamSettings,
    *,
    auth: str,
    mode: str,
    direct_push_roles: Iterable[str] | None = None,
) -> Principal:
    table = role_table(settings.roles)
    strategies: list[str] = list(settings.strategies)
    roles: list[str] = []
    eligible: list[str] = []
    explicit: set[str] = set()

    def grant(names: Iterable[str], *, trusted: bool) -> None:
        names = [canonical_role(n, table) or n for n in names]
        roles.extend(names)
        if trusted:
            eligible.extend(names)

    if not identity.authenticated:
        grant(settings.anonymous_roles, trusted=False)
    else:
        if "airflow_permissions" in strategies:
            apr = settings.airflow_permission_roles
            grant(apr.authenticated, trusted=True)
            for check, granted_roles in (
                ("dag_read", apr.dag_read),
                ("dag_edit", apr.dag_edit),
                ("configuration_edit", apr.configuration_edit),
            ):
                granted, verified = identity.checks.get(check, (False, False))
                if granted:
                    grant(granted_roles, trusted=verified)
        if "role_mapping" in strategies:
            grant(map_airflow_roles(identity.airflow_roles, settings, table), trusted=True)
        if "custom_view" in strategies and identity.custom_view is not None:
            for info in PERMISSIONS:
                resource = custom_view_resource(settings, info.resource)
                try:
                    if identity.custom_view(info.method, resource):
                        explicit.add(info.name)
                except Exception:  # an auth manager error never grants anything
                    log.warning("custom view check failed for %s", resource, exc_info=True)
        grant(settings.default_roles, trusted=True)

    roles = _dedupe(roles)
    eligible = _dedupe(eligible)
    permissions = permissions_of(roles, table) | explicit
    # ``deploy:direct_push`` only ever comes from trusted grants (roles from verified
    # checks/claims, custom views) of a signed-in user, never from untrusted roles.
    trusted = permissions_of(eligible, table) | explicit
    explicit_push = identity.authenticated and DEPLOY_DIRECT_PUSH in trusted
    principal = Principal(
        user=identity.user,
        roles=roles,
        permissions={p for p in permissions if p in ALL_PERMISSIONS and p != DEPLOY_DIRECT_PUSH},
        auth=auth,
        mode=mode,
        strategies=strategies,
        direct_push_eligible=eligible,
        explicit_direct_push=explicit_push,
        authenticated=identity.authenticated,
        display_name=identity.display_name,
        email=identity.email,
    )
    if direct_push_roles is None:
        direct_push_roles = settings.direct_push_roles
    if principal.can_direct_push(direct_push_roles):
        principal.permissions.add(DEPLOY_DIRECT_PUSH)
    return principal


# --------------------------------------------------------------------------- helpers
# Real JWT payloads are a few hundred bytes; anything far larger is not worth decoding
# (and a deeply nested one would exhaust the parser's recursion limit).
MAX_JWT_PAYLOAD = 16 * 1024


def jwt_claims(token: str) -> dict[str, Any]:
    """Payload of a JWT *without* verifying it (callers decide whether to trust it).

    Never raises: oversized, malformed or non-object payloads give ``{}``."""
    parts = token.removeprefix("Bearer ").strip().split(".")
    if len(parts) != 3 or len(parts[1]) > MAX_JWT_PAYLOAD:
        return {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, RecursionError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def claim_user(claims: Mapping[str, Any]) -> str | None:
    for key in ("preferred_username", "username", "name", "sub"):
        if claims.get(key):
            return str(claims[key])
    return None


def claim_roles(claims: Mapping[str, Any], names: Iterable[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        value = claims.get(name)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(str(v) for v in value if isinstance(v, (str, int)))
    return out


def airflow_user_roles(user: Any) -> list[str]:
    """Role names of an auth-manager user: FAB ``user.roles`` (objects with ``name``),
    SimpleAuthManager ``user.role`` (a string), or any ``roles`` list of strings."""
    out: list[str] = []
    try:
        role = getattr(user, "role", None)
        if isinstance(role, str) and role:
            out.append(role)
        roles = getattr(user, "roles", None)
        if roles and not isinstance(roles, str):
            for r in roles:
                name = r if isinstance(r, str) else getattr(r, "name", None)
                if name:
                    out.append(str(name))
    except Exception:  # lazy-loaded relationships on a detached user…
        log.warning("cannot read the roles of %r", user, exc_info=True)
    return out


def airflow_user_name(user: Any) -> str | None:
    try:
        name = user.get_name()
    except Exception:
        name = getattr(user, "username", None)
    return str(name) if name else None


_warned_simple = False


def plugin_custom_view(user: Any) -> CustomViewCheck | None:
    """``is_authorized_custom_view`` of Airflow's auth manager for ``user``, or ``None``
    when the auth manager cannot express custom permissions (SimpleAuthManager grants
    every custom view to every viewer, so it would grant AutoAir's admin actions)."""
    from airflow.api_fastapi.app import get_auth_manager

    global _warned_simple
    manager = get_auth_manager()
    if type(manager).__name__ == "SimpleAuthManager":
        if not _warned_simple:
            _warned_simple = True
            log.warning(
                "iam.strategies contains 'custom_view' but SimpleAuthManager authorizes every "
                "custom view for every user: ignoring it (use 'role_mapping')"
            )
        return None

    def check(method: str, resource: str) -> bool:
        return bool(
            manager.is_authorized_custom_view(method=method, resource_name=resource, user=user)
        )

    return check
