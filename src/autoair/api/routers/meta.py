from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair import __version__
from autoair._compat import airflow_version_str, supports_react_apps
from autoair.api.app import Services
from autoair.api.deps import ServicesDep
from autoair.api.routers.runs import db_mode_unavailable
from autoair.iam import model as P
from autoair.iam.deps import PrincipalDep, direct_push_roles, iam_enabled, iam_settings
from autoair.iam.model import Principal
from autoair.iam.proposals import write_mode
from autoair.iam.sso import Branding, branding
from autoair.storage import TargetStatus
from autoair.validation import policy_from_settings


class PolicyInfo(BaseModel):
    allow_python_code: bool
    allow_privileged_operators: bool
    require_owner: bool
    require_description: bool
    min_retries: int | None
    required_tags: list[str]
    forbidden_operators: list[str]
    max_tasks: int | None
    dag_id_prefix: str | None
    python_ref_allow: list[str]
    dag_id_patterns: list[str] = Field(
        default_factory=list, description="Regexes every dag_id must fullmatch (policy)."
    )
    task_id_patterns: list[str] = Field(
        default_factory=list, description="Regexes every task_id must fullmatch (policy)."
    )


class DeployRights(BaseModel):
    """What a deploy of the default target does for this user."""

    git_mode: Literal["pr", "push"] | None = Field(
        description="Git deploy mode of the default target (null: not a git target)."
    )
    can_save: bool = Field(description="May save at all (spec:write).")
    can_deploy_live: bool = Field(description="May write changes that go live (deploy).")
    direct_push: bool = Field(
        description="May send direct_push=true (push to the branch, bypassing a PR)."
    )
    default_opens_pr: bool = Field(
        description="A plain deploy (direct_push omitted) opens/updates a pull request."
    )
    require_approval: bool = Field(
        description="Live deploys need another reviewer's approval of the version."
    )
    git_token: Literal["off", "optional", "required"] | None = Field(
        default=None,
        description="Git targets: deploys take a token typed by the user "
        "(storage.git.request_token after the kill switch and forced-connection rule; "
        "required: the quick deploy opens the deploy dialog). Null: not a git target.",
    )


class TestRunInfo(BaseModel):
    """Where test runs execute and whether ``mode=db`` is available to the caller."""

    on_validator: bool = Field(
        description="Task and DAG test runs (mode memory) run on the validator service "
        "(validator.url and validator.test_runs), not on this host."
    )
    db_mode_unavailable: Literal["disabled", "admin_only"] | None = Field(
        default=None,
        description="Why mode=db is not available: disabled (runner.allow_db_mode is off) or "
        "admin_only (it needs admin:config). Null: the caller may use it. mode=auto runs in "
        "memory whenever this is set.",
    )


class MeInfo(BaseModel):
    user: str | None
    display_name: str | None = Field(
        default=None, description="From the auth manager / token (first/last name, name)."
    )
    email: str | None = None
    roles: list[str]
    permissions: list[str]
    auth: str = Field(description="airflow (plugin), bearer (standalone token) or none.")
    mode: str
    strategies: list[str]
    login_enabled: bool = Field(description="Standalone password sign-in is available.")
    iam_enabled: bool = Field(
        default=True,
        description="AutoAir's own authorization is on (features.iam). false: roles come only "
        "from Airflow's DAG/configuration permissions; no approvals or direct-push roles.",
    )
    deploy: DeployRights
    role_permissions: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Permissions granted by each of the caller's roles (role chip tooltips).",
    )


class MetaInfo(BaseModel):
    autoair_version: str
    airflow_version: str | None
    target_airflow_version: str | None
    mode: str
    auth: str
    allow_python_code: bool
    allow_privileged_operators: bool
    import_check_on_deploy: bool
    output_dir: str
    supports_react_apps: bool
    storage: TargetStatus
    environments: list[str]
    default_environment: str | None
    team: str | None
    ai_enabled: bool
    ai_model: str | None
    ai_unavailable_reason: str | None = Field(
        default=None,
        description="Why AI is off although features.ai is on (e.g. the autoair[ai] extra is "
        "not installed).",
    )
    mcp_enabled: bool
    mcp_unavailable_reason: str | None = Field(
        default=None,
        description="Why MCP is off although features.mcp is on (e.g. the autoair[mcp] extra "
        "is not installed, or cannot be next to Airflow 3.1).",
    )
    collab_enabled: bool
    features: dict[str, bool] = Field(
        default_factory=dict,
        description="Effective feature flags (AUTOAIR_FEATURES__*, runtime overrides and "
        "policy applied): hide what is false.",
    )
    expose_config: Literal["false", "true", "non-sensitive-only"] = Field(
        default="false", description="Whether GET /config is available (admins only)."
    )
    policy: PolicyInfo
    iam: MeInfo | None = Field(default=None, description="The caller (same as GET /me).")
    ui: Branding = Field(
        default_factory=Branding,
        description="UI branding and defaults (ui.*): title, logo, accent, banner, language, "
        "time zone.",
    )
    public_url: str | None = Field(default=None, description="server.public_url.")
    airflow_public_url: str | None = Field(
        default=None,
        description="Airflow as browsers and API clients reach it (sso.airflow_public_url, "
        "else airflow_url): base of the trigger-endpoint snippets.",
    )
    allow_force_overwrite: bool = Field(
        default=False,
        description="preserve.allow_force_overwrite: the overwrite (force) choice for a DAG "
        "file changed outside AutoAir or the hand-written file AutoAir keeps is offered "
        "(still with 'deploy:overwrite_ejected'). false: hide it.",
    )
    secrets_backend: bool = Field(
        default=False,
        description="airflow_secrets_backend: connections/variables may come from a secrets "
        "backend, so ids missing from Airflow's metastore are not flagged.",
    )
    test_runs: TestRunInfo | None = Field(
        default=None, description="Test runs: where they execute, whether mode=db is offered."
    )


router = APIRouter(tags=["meta"])


def _collab_available() -> bool:
    from autoair.collab import available

    return available()


@router.get("/meta", operation_id="getMeta")
async def get_meta(services: ServicesDep, principal: PrincipalDep) -> MetaInfo:
    s = services.settings
    status = await run_in_threadpool(services.target.status)
    features = s.effective_features()
    features["collab"] = features["collab"] and _collab_available()
    policy = policy_from_settings(s)
    return MetaInfo(
        autoair_version=__version__,
        airflow_version=airflow_version_str(),
        target_airflow_version=s.target_airflow_version,
        mode=s.mode,
        auth=services.auth.name,
        allow_python_code=policy.allow_python_code,
        allow_privileged_operators=s.allow_privileged_operators,
        import_check_on_deploy=s.import_check_on_deploy,
        output_dir=str(s.output_dir),
        supports_react_apps=supports_react_apps(),
        storage=status,
        environments=list(s.environments),
        default_environment=s.default_environment,
        team=s.team,
        ai_enabled=services.llm is not None and features["ai"],
        ai_model=getattr(services.llm, "model", None),
        # features.ai is kept off when the AI runtime is missing: say why.
        ai_unavailable_reason=s.feature_unavailable("ai")
        if s.configured_features()["ai"]
        else None,
        mcp_enabled=features["mcp"],
        mcp_unavailable_reason=s.feature_unavailable("mcp")
        if s.configured_features()["mcp"]
        else None,
        collab_enabled=features["collab"],
        features=features,
        expose_config=s.expose_config,
        iam=me_info(services, principal),
        ui=branding(s),
        public_url=s.server.public_url,
        airflow_public_url=str(s.sso.airflow_public_url or s.airflow_url).rstrip("/") or None,
        secrets_backend=s.airflow_secrets_backend,
        allow_force_overwrite=s.preserve.allow_force_overwrite,
        test_runs=TestRunInfo(
            on_validator=bool(s.validator.runs_url and s.validator.test_runs),
            db_mode_unavailable=db_mode_unavailable(s, principal),
        ),
        policy=PolicyInfo(
            allow_python_code=policy.allow_python_code,
            allow_privileged_operators=s.allow_privileged_operators,
            require_owner=s.require_owner,
            require_description=s.require_description,
            min_retries=s.min_retries,
            required_tags=list(s.required_tags),
            forbidden_operators=list(s.forbidden_operators),
            max_tasks=s.max_tasks,
            dag_id_prefix=s.dag_id_prefix,
            python_ref_allow=list(s.python_ref_allow),
            dag_id_patterns=list(policy.dag_id_patterns),
            task_id_patterns=list(policy.task_id_patterns),
        ),
    )


def _role_permissions(custom: dict[str, list[str]], roles: list[str]) -> dict[str, list[str]]:
    from autoair.iam.model import role_table

    table = role_table(custom)
    return {r: sorted(table[r]) for r in roles if r in table}


def me_info(services: Services, principal: Principal, team: str | None = None) -> MeInfo:
    """Deploy rights of ``principal`` on the deploy target of ``team`` (default: this
    deployment's team, i.e. the default target)."""
    from autoair.storage.git import GitTarget
    from autoair.storage.request_token import effective_mode, root_settings

    iam = iam_settings(services)
    team = team or services.settings.team
    target = services.target_for(team)
    git_mode = target.mode if isinstance(target, GitTarget) else None
    storage = (
        services.settings.team_storage.get(team, services.settings.storage)
        if team
        else services.settings.storage
    )
    git_token = (
        effective_mode(storage.git, root_settings(services)) if target.kind == "git" else None
    )
    direct = principal.can_direct_push(direct_push_roles(services, team))
    permissions = set(principal.permissions) - {P.DEPLOY_DIRECT_PUSH}
    if direct:
        permissions.add(P.DEPLOY_DIRECT_PUSH)
    opens_pr = write_mode(git_mode, direct_push=None, allowed=direct, iam=iam) == "pr"
    login = services.login
    return MeInfo(
        user=principal.user,
        display_name=principal.display_name,
        email=principal.email,
        roles=principal.roles,
        permissions=sorted(permissions),
        auth=principal.auth,
        mode=principal.mode,
        strategies=principal.strategies,
        login_enabled=login is not None and login.settings.login_enabled,
        iam_enabled=iam_enabled(services),
        role_permissions=_role_permissions(iam.roles, principal.roles),
        deploy=DeployRights(
            git_mode=git_mode,
            can_save=principal.has(P.SPEC_WRITE),
            can_deploy_live=principal.has(P.DEPLOY),
            direct_push=direct,
            default_opens_pr=opens_pr,
            require_approval=iam.require_approval,
            git_token=git_token,
        ),
    )


@router.get("/me", operation_id="getMe")
async def get_me(
    services: ServicesDep,
    principal: PrincipalDep,
    team: Annotated[
        str | None,
        Query(max_length=200, description="Deploy rights on this team's target (multi-team)."),
    ] = None,
) -> MeInfo:
    """The caller: user, AutoAir roles, effective permissions, deploy rights, auth mode."""
    return me_info(services, principal, team)
