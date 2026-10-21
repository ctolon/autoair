"""AutoAir permissions (fine-grained actions), built-in roles and their Airflow mapping.

A *permission* is one action such as ``spec:write`` or ``deploy``; a *role* is a named set of
permissions. Routes depend on permissions only (:func:`autoair.iam.deps.require`); how a user
gets them is decided by the configured strategies (:mod:`autoair.iam.resolver`).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

# --------------------------------------------------------------------------- permissions
BUILDER_USE: Final = "builder:use"
SPEC_READ: Final = "spec:read"
AUDIT_READ: Final = "audit:read"
AIRFLOW_READ: Final = "airflow:read"
SPEC_WRITE: Final = "spec:write"
SPEC_DELETE: Final = "spec:delete"
CODE_EXECUTE: Final = "code:execute"
CODE_RAW: Final = "code:raw"
RUN_TEST: Final = "run:test"
CONNECTION_WRITE: Final = "connection:write"
CATALOG_REFRESH: Final = "catalog:refresh"
AI_USE: Final = "ai:use"
REVIEW: Final = "review"
DEPLOY: Final = "deploy"
DEPLOY_DIRECT_PUSH: Final = "deploy:direct_push"
DEPLOY_OVERWRITE_EJECTED: Final = "deploy:overwrite_ejected"
PROMOTE: Final = "promote"
RUN_TRIGGER: Final = "run:trigger"
RUN_MANAGE: Final = "run:manage"
TEMPLATE_WRITE: Final = "template:write"
ICON_WRITE: Final = "icon:write"
ADMIN_DEPLOY_TARGET: Final = "admin:deploy_target"
ADMIN_CONFIG: Final = "admin:config"
ADMIN_PANEL: Final = "admin:panel"
AI_ADMIN: Final = "ai:admin"


@dataclass(frozen=True)
class PermissionInfo:
    name: str
    description: str
    # How the permission is asked of Airflow's auth manager (``is_authorized_custom_view``):
    # FAB shows it as "<can_read|can_create|can_edit|can_delete> on <resource>".
    method: str
    resource: str


PERMISSIONS: Final[tuple[PermissionInfo, ...]] = (
    PermissionInfo(
        BUILDER_USE,
        "Use the builder: operator catalog, code preview, validation, diff, templates, icons.",
        "GET",
        "Builder",
    ),
    PermissionInfo(SPEC_READ, "Read stored specs, their history and versions.", "GET", "Specs"),
    PermissionInfo(AUDIT_READ, "Read the audit trail of a DAG.", "GET", "Audit Log"),
    PermissionInfo(
        AIRFLOW_READ,
        "Read Airflow objects through AutoAir (runs, logs, pools, connections list…); "
        "Airflow still enforces its own permissions on every proxied call.",
        "GET",
        "Airflow Resources",
    ),
    PermissionInfo(
        CONNECTION_WRITE,
        "Create and test Airflow connections from the builder (Airflow enforces too).",
        "POST",
        "Airflow Resources",
    ),
    PermissionInfo(
        SPEC_WRITE,
        "Save specs; in git PR mode a save opens/updates a pull request.",
        "PUT",
        "Specs",
    ),
    PermissionInfo(SPEC_DELETE, "Delete stored specs and their DAG files.", "DELETE", "Specs"),
    PermissionInfo(
        CODE_EXECUTE,
        "Run DAG code on the AutoAir server: import checks, Python DAG imports.",
        "POST",
        "Code Execution",
    ),
    PermissionInfo(
        CODE_RAW,
        "Write raw code DAGs: hand-written DAG files deployed as written (arbitrary Python; "
        "also needs features.python_code).",
        "PUT",
        "Raw Code",
    ),
    PermissionInfo(RUN_TEST, "Test a task or a whole DAG from the builder.", "POST", "Test Runs"),
    PermissionInfo(CATALOG_REFRESH, "Rebuild the operator catalog.", "POST", "Catalog"),
    PermissionInfo(AI_USE, "Use the AI assistant (generate, explain, review, fix…).", "POST", "AI"),
    PermissionInfo(
        REVIEW, "Review and approve a spec version (four-eyes deploys).", "POST", "Reviews"
    ),
    PermissionInfo(
        DEPLOY,
        "Write DAG files that go live (local/object targets, git push mode, direct pushes).",
        "POST",
        "Deploy",
    ),
    PermissionInfo(
        DEPLOY_DIRECT_PUSH,
        "Git PR mode: push directly to the target branch instead of opening a pull request.",
        "POST",
        "Direct Push",
    ),
    PermissionInfo(
        DEPLOY_OVERWRITE_EJECTED,
        "Overwrite a generated DAG file that was changed outside AutoAir (ejected) with a "
        "regenerated one, discarding those changes (preserve.ejected_files=overwrite; needs "
        "an explicit confirmation per deploy).",
        "POST",
        "Overwrite Ejected Files",
    ),
    PermissionInfo(PROMOTE, "Promote a DAG from one environment to another.", "POST", "Promote"),
    PermissionInfo(RUN_TRIGGER, "Trigger DAG runs and create asset events.", "POST", "DAG Runs"),
    PermissionInfo(RUN_MANAGE, "Pause/unpause, clear and backfill DAG runs.", "PUT", "DAG Runs"),
    PermissionInfo(
        TEMPLATE_WRITE, "Save/delete templates, install marketplace entries.", "POST", "Templates"
    ),
    PermissionInfo(ICON_WRITE, "Upload/delete icons and set default icons.", "POST", "Icons"),
    PermissionInfo(
        ADMIN_DEPLOY_TARGET, "Change the server-wide deploy target.", "PUT", "Deploy Target"
    ),
    PermissionInfo(ADMIN_CONFIG, "Change AutoAir's runtime configuration.", "PUT", "Configuration"),
    PermissionInfo(
        ADMIN_PANEL,
        "Open the admin panel: audit browser, users, AI usage, database and caches.",
        "GET",
        "Admin Panel",
    ),
    PermissionInfo(
        AI_ADMIN,
        "Administer AI: knowledge base, prompts, AI audit log, privileged AI tools.",
        "PUT",
        "AI Settings",
    ),
)

ALL_PERMISSIONS: Final[frozenset[str]] = frozenset(p.name for p in PERMISSIONS)
#: Permissions a wildcard (``*``, ``deploy:*``) never grants.
EXPLICIT_ONLY: Final[frozenset[str]] = frozenset({DEPLOY_DIRECT_PUSH, DEPLOY_OVERWRITE_EJECTED})
PERMISSION_INFO: Final[Mapping[str, PermissionInfo]] = {p.name: p for p in PERMISSIONS}

# --------------------------------------------------------------------------- roles
AUTHENTICATED: Final = "Authenticated"
VIEWER: Final = "Viewer"
AUTHOR: Final = "Author"
REVIEWER: Final = "Reviewer"
DEPLOYER: Final = "Deployer"
CODE_DEPLOYER: Final = "CodeDeployer"
OPERATOR: Final = "Operator"
TEMPLATE_MANAGER: Final = "TemplateManager"
ADMIN: Final = "Admin"

_VIEW = (BUILDER_USE, SPEC_READ, AUDIT_READ, AIRFLOW_READ)

# ``deploy:direct_push`` is deliberately in no built-in role: it is granted to the roles
# listed in ``storage.git.direct_push_roles`` (default: Admin), see :class:`Principal`.
BUILTIN_ROLES: Final[Mapping[str, tuple[str, ...]]] = {
    # Granted to every signed-in Airflow user (Airflow itself still enforces its
    # permissions on these proxied/neutral calls).
    AUTHENTICATED: (BUILDER_USE, AIRFLOW_READ, AI_USE, CONNECTION_WRITE, RUN_TRIGGER, RUN_MANAGE),
    VIEWER: _VIEW,
    AUTHOR: (
        *_VIEW,
        SPEC_WRITE,
        CODE_EXECUTE,
        CODE_RAW,
        RUN_TEST,
        AI_USE,
        CONNECTION_WRITE,
        CATALOG_REFRESH,
    ),
    REVIEWER: (*_VIEW, REVIEW, AI_USE),
    DEPLOYER: (*_VIEW, SPEC_WRITE, SPEC_DELETE, DEPLOY, PROMOTE, RUN_TRIGGER),
    # Deploying DAGs that keep their hand-written Python file: the round trip runs the
    # file (code:execute) and the first deploy writes the imported file (code:raw).
    CODE_DEPLOYER: (
        *_VIEW,
        SPEC_WRITE,
        SPEC_DELETE,
        DEPLOY,
        PROMOTE,
        RUN_TRIGGER,
        CODE_EXECUTE,
        CODE_RAW,
    ),
    OPERATOR: (*_VIEW, RUN_TRIGGER, RUN_MANAGE),
    TEMPLATE_MANAGER: (*_VIEW, TEMPLATE_WRITE, ICON_WRITE),
    ADMIN: tuple(sorted(ALL_PERMISSIONS - {DEPLOY_DIRECT_PUSH})),
}

ROLE_DESCRIPTIONS: Final[Mapping[str, str]] = {
    AUTHENTICATED: "Baseline of every signed-in user in the 'airflow_permissions' strategy.",
    VIEWER: "Read specs, catalog, code preview, history and audit.",
    AUTHOR: "Create and edit specs and raw code DAGs, validate, test runs, AI assist; "
    "proposes changes (PRs).",
    REVIEWER: "Read everything, approve spec versions, AI review.",
    DEPLOYER: "Deploy live, delete, promote between environments, trigger runs.",
    CODE_DEPLOYER: "Deployer, plus deploying DAGs that keep their hand-written Python "
    "file (imported or edited in the code mode): code:execute and code:raw.",
    OPERATOR: "Trigger, backfill, clear, pause/unpause runs.",
    TEMPLATE_MANAGER: "Curate templates, marketplace installs and icons.",
    ADMIN: "Everything, including the deploy target and configuration.",
}


def is_pattern(name: str) -> bool:
    return any(ch in name for ch in "*?[")


def expand(patterns: Iterable[str]) -> set[str]:
    """Permission names matching ``patterns`` (exact names or fnmatch wildcards such as
    ``spec:*`` or ``*``). Unknown exact names are kept (forward compatible).

    ``deploy:direct_push`` and ``deploy:overwrite_ejected`` never come from a wildcard: they
    must be named explicitly (they bypass a review / discard hand-made changes)."""
    out: set[str] = set()
    for pattern in patterns:
        if is_pattern(pattern):
            out.update(
                p
                for p in ALL_PERMISSIONS
                if p not in EXPLICIT_ONLY and fnmatch.fnmatchcase(p, pattern)
            )
        else:
            out.add(pattern)
    return out


def unknown_permissions(patterns: Iterable[str]) -> list[str]:
    """Patterns that are neither a known permission nor a wildcard matching one."""
    out: list[str] = []
    for p in patterns:
        if is_pattern(p):
            if not any(fnmatch.fnmatchcase(k, p) for k in ALL_PERMISSIONS):
                out.append(p)
        elif p not in ALL_PERMISSIONS:
            out.append(p)
    return out


def role_table(custom: Mapping[str, list[str]] | None = None) -> dict[str, set[str]]:
    """Built-in roles, overridden/extended by ``iam.roles``."""
    table = {name: set(perms) for name, perms in BUILTIN_ROLES.items()}
    for name, patterns in (custom or {}).items():
        table[name] = expand(patterns)
    return table


def permissions_of(roles: Iterable[str], table: Mapping[str, set[str]]) -> set[str]:
    out: set[str] = set()
    for role in roles:
        out |= table.get(role, set())
    return out


def canonical_role(name: str, table: Mapping[str, set[str]]) -> str | None:
    """``name`` as a known role (case-insensitive), or ``None``."""
    if name in table:
        return name
    folded = name.casefold()
    for known in table:
        if known.casefold() == folded:
            return known
    return None


@dataclass
class Principal:
    """The caller, as seen by AutoAir's authorization."""

    user: str | None
    roles: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)
    auth: str = "none"
    mode: str = "standalone"
    strategies: list[str] = field(default_factory=list)
    # Roles that may unlock ``deploy:direct_push`` through ``direct_push_roles``. Roles
    # inferred without any real check (standalone fallback, auth disabled) are not.
    direct_push_eligible: list[str] = field(default_factory=list)
    # ``deploy:direct_push`` granted explicitly by a trusted source (a custom role named in a
    # verified grant, an Airflow custom view); never for anonymous callers.
    explicit_direct_push: bool = False
    authenticated: bool = True
    # From the auth manager's user (FAB first/last name, email) or the token's claims; used
    # to label commits, pull requests and audit records (autoair.governance).
    display_name: str | None = None
    email: str | None = None

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def can_direct_push(self, direct_push_roles: Iterable[str]) -> bool:
        if not self.authenticated:  # anonymous (authentication disabled)
            return False
        if self.explicit_direct_push:
            return True
        allowed = {r.casefold() for r in direct_push_roles}
        return any(r.casefold() in allowed for r in self.direct_push_eligible)
