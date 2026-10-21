"""Register AutoAir's permissions in the FAB auth manager (``autoair iam sync-fab``).

FAB only lets admins grant permissions that exist in its database. This creates every
AutoAir permission as a FAB permission (``can_create on AutoAir Deploy``…), and optionally
one Airflow role per built-in AutoAir role ("AutoAir Deployer"…) holding them, so admins
manage AutoAir access in Airflow's Security → Roles / Users pages.
"""

from __future__ import annotations

from typing import Any

from autoair.iam.config import IamSettings
from autoair.iam.model import PERMISSION_INFO, PERMISSIONS, role_table
from autoair.iam.resolver import custom_view_resource

# Airflow's FAB auth manager maps request methods to these action names.
FAB_ACTIONS = {"GET": "can_read", "POST": "can_create", "PUT": "can_edit", "DELETE": "can_delete"}


def fab_permission(name: str, settings: IamSettings) -> tuple[str, str]:
    """(FAB action, FAB resource) of an AutoAir permission."""
    info = PERMISSION_INFO[name]
    return FAB_ACTIONS[info.method], custom_view_resource(settings, info.resource)


def sync_fab(sm: Any, settings: IamSettings, *, create_roles: bool = False) -> list[str]:
    """Create the permissions (and roles) through FAB's security manager ``sm``; return a
    log of what was done. Idempotent: existing permissions/roles are kept, and permissions
    are only added to roles, never removed (admins may have customised them)."""
    if create_roles and not settings.role_prefix.strip():
        # Without a prefix the roles would be named "Admin", "Viewer"… — Airflow's own
        # built-in roles, which this would modify.
        raise ValueError(
            "refusing to create roles with an empty iam.role_prefix: they would modify "
            "Airflow's built-in roles (set iam.role_prefix, e.g. 'AutoAir ')"
        )
    done: list[str] = []
    perms: dict[str, Any] = {}
    for info in PERMISSIONS:
        action, resource = fab_permission(info.name, settings)
        perm = sm.get_permission(action, resource)
        if perm is None:
            perm = sm.create_permission(action, resource)
            done.append(f"created permission {action} on {resource}")
        perms[info.name] = perm
    if not create_roles:
        return done
    prefix = settings.role_prefix
    for role_name, granted in role_table(settings.roles).items():
        if role_name == "Authenticated":
            continue
        fab_name = f"{prefix}{role_name}"
        role = sm.find_role(fab_name)
        if role is None:
            role = sm.add_role(fab_name)
            done.append(f"created role {fab_name}")
        existing = {(p.action.name, p.resource.name) for p in (role.permissions or [])}
        for name in sorted(granted):
            if name not in perms:
                continue
            if fab_permission(name, settings) not in existing:
                sm.add_permission_to_role(role, perms[name])
                done.append(f"granted {name} to {fab_name}")
    return done
