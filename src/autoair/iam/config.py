"""IAM settings (``AUTOAIR_IAM__*``): how Airflow identities become AutoAir permissions."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from autoair.iam.model import BUILTIN_ROLES, unknown_permissions

log = logging.getLogger(__name__)

Strategy = Literal["airflow_permissions", "role_mapping", "custom_view"]


def _default_strategies() -> list[Strategy]:
    return ["airflow_permissions"]


def _default_role_mapping() -> dict[str, list[str]]:
    # Airflow's stock roles (FAB: Admin/Op/User/Viewer, SimpleAuthManager: ADMIN/OP/USER/VIEWER;
    # matched case-insensitively). "Public" maps to nothing.
    return {
        "Admin": ["Admin"],
        "Op": ["Author", "Reviewer", "Deployer", "Operator", "TemplateManager"],
        "User": ["Author", "Operator"],
        "Viewer": ["Viewer"],
    }


class AirflowPermissionRoles(BaseModel):
    """``airflow_permissions`` strategy: AutoAir roles granted for each standard Airflow
    permission the user holds (checked against the request's ``dag_id`` when there is one)."""

    model_config = ConfigDict(extra="forbid")

    authenticated: list[str] = Field(
        default_factory=lambda: ["Authenticated"],
        description="Roles of every signed-in user (Airflow still enforces proxied calls).",
    )
    dag_read: list[str] = Field(
        default_factory=lambda: ["Viewer"], description="Roles granted by DAG read (GET)."
    )
    dag_edit: list[str] = Field(
        default_factory=lambda: ["Author", "Reviewer", "Deployer", "Operator", "TemplateManager"],
        description="Roles granted by DAG edit (PUT).",
    )
    configuration_edit: list[str] = Field(
        default_factory=lambda: ["Admin"],
        description="Roles granted by configuration edit (PUT); plugin mode only — standalone "
        "cannot check it and never infers direct-push rights from it.",
    )


class IamSettings(BaseModel):
    """Authorization: AutoAir roles/permissions and how they come from Airflow."""

    model_config = ConfigDict(extra="forbid")

    strategies: list[Strategy] = Field(
        default_factory=_default_strategies,
        description="How permissions are derived; the union of all listed strategies applies. "
        "'airflow_permissions': from Airflow's DAG/configuration permissions (default); "
        "'role_mapping': Airflow role names -> AutoAir roles (FAB roles, "
        "SimpleAuthManager role, or the token's role claim standalone); 'custom_view': each "
        "AutoAir permission is an Airflow custom-view permission ('can_create on AutoAir "
        "Deploy'…) granted in Airflow (FAB, Keycloak, AWS auth managers; plugin mode).",
    )
    airflow_permission_roles: AirflowPermissionRoles = Field(
        default_factory=AirflowPermissionRoles,
        description="'airflow_permissions' strategy: roles per standard Airflow permission.",
    )
    role_mapping: dict[str, list[str]] = Field(
        default_factory=_default_role_mapping,
        description="'role_mapping' strategy: Airflow role name (case-insensitive) -> AutoAir "
        'roles, e.g. {"data-eng": ["Author", "Deployer"]}.',
    )
    role_prefix: str = Field(
        default="AutoAir ",
        description="'role_mapping' strategy: an Airflow role named '<prefix><AutoAir role>' "
        "(e.g. 'AutoAir Deployer') grants that AutoAir role; empty disables this.",
    )
    roles_claims: list[str] = Field(
        default_factory=lambda: ["role", "roles"],
        description="Standalone 'role_mapping': JWT claims holding the Airflow role(s). Only "
        "used when Airflow verified the token (the default; not with --no-auth).",
    )
    roles: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Custom roles or overrides of built-in ones: name -> permissions "
        '(wildcards allowed), e.g. {"Auditor": ["spec:read", "audit:read"]}.',
    )
    default_roles: list[str] = Field(
        default_factory=list, description="Roles every signed-in user gets, whatever the strategy."
    )
    anonymous_roles: list[str] = Field(
        default_factory=lambda: ["Admin"],
        description="Roles when authentication is disabled (autoair serve --no-auth); such "
        "users never get direct-push rights through storage.git.direct_push_roles.",
    )
    custom_view_prefix: str = Field(
        default="AutoAir",
        description="'custom_view' strategy: Airflow resource names are '<prefix> <resource>' "
        "(e.g. 'AutoAir Deploy').",
    )
    direct_push_roles: list[str] = Field(
        default_factory=lambda: ["Admin"],
        description="AutoAir roles holding 'deploy:direct_push' (push straight to the branch "
        "in git PR mode); a deploy target's storage.git.direct_push_roles overrides it when "
        "set. Empty: nobody, except users granted the permission explicitly.",
    )
    force_pr_without_direct_push: bool = Field(
        default=False,
        description="Git push mode: users without 'deploy:direct_push' have their deploys "
        "opened as pull requests instead of pushed.",
    )
    require_approval: bool = Field(
        default=False,
        description="Four-eyes rule: a deploy that goes live needs an approval of that exact "
        "spec version by another user holding 'review' (pull requests are exempt). A deploy "
        "without one is stored as a proposal (HTTP 202) that a reviewer approves and anyone "
        "holding 'deploy' then deploys.",
    )
    proposals_url: str | None = Field(
        default=None,
        description="Where deploy proposals and their reviews are kept (fsspec URL; default "
        "<state>/proposals). Use shared storage when several replicas serve the API.",
    )
    proposal_ttl_seconds: float = Field(
        default=7 * 24 * 3600,
        gt=0,
        description="Deploy proposals (iam.require_approval) older than this are discarded.",
    )
    import_check_requires_code_execute: bool = Field(
        default=True,
        description="A deploy runs the import check (executes the DAG file in a sandboxed "
        "subprocess on the AutoAir server) only for callers holding 'code:execute'; for others "
        "it is skipped and reported as an info problem (static validation still applies).",
    )
    custom_view_cache_seconds: float = Field(
        default=30,
        ge=0,
        description="'custom_view' strategy: how long the auth manager's answers are cached "
        "per user and token (0 disables the cache).",
    )
    audit_denials: bool = Field(
        default=True, description="Record permission denials in the audit trail."
    )
    login_enabled: bool = Field(
        default=True,
        description="Standalone: POST /api/auth/login exchanges an Airflow username/password "
        "for an Airflow access token.",
    )
    login_max_failures: int = Field(
        default=5,
        ge=1,
        description="Standalone login: failed attempts per client IP and per username within "
        "login_window_seconds before further attempts are refused (429).",
    )
    login_max_tracked_keys: int = Field(
        default=10_000,
        ge=1,
        description="Standalone login: hard cap of client IPs/usernames tracked by the rate "
        "limiter (least recently used ones are forgotten first).",
    )
    login_window_seconds: float = Field(
        default=300, gt=0, description="Standalone login: rate-limit window (s)."
    )
    login_timeout: float = Field(
        default=10, gt=0, description="Standalone login: timeout (s) of Airflow's token call."
    )

    @model_validator(mode="after")
    def _check_names(self) -> IamSettings:
        for role, patterns in self.roles.items():
            unknown = unknown_permissions(patterns)
            if unknown:
                raise ValueError(f"iam.roles[{role!r}]: unknown permission(s) {unknown}")
        known = {r.casefold() for r in (*BUILTIN_ROLES, *self.roles)}
        referenced: dict[str, list[str]] = {
            "iam.default_roles": self.default_roles,
            "iam.anonymous_roles": self.anonymous_roles,
            "iam.direct_push_roles": self.direct_push_roles,
        }
        for name, roles in self.role_mapping.items():
            referenced[f"iam.role_mapping[{name!r}]"] = roles
        for check in AirflowPermissionRoles.model_fields:
            referenced[f"iam.airflow_permission_roles.{check}"] = getattr(
                self.airflow_permission_roles, check
            )
        for where, roles in referenced.items():
            missing = [r for r in roles if r.casefold() not in known]
            if missing:
                log.warning("%s: unknown AutoAir role(s) %s (they grant nothing)", where, missing)
        return self
