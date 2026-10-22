"""Deploy target setup (where DAGs go): discovery, test, save — without a restart."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.deps import GatewayDep, ServicesDep
from autoair.deploy_setup import (
    DeployCandidate,
    PolicyError,
    apply_policy,
    candidate_mode,
    discover_candidates,
    restore_secrets,
    scrub_secrets,
    unreferenced_secrets,
)
from autoair.gateway import AirflowError
from autoair.iam.deps import PrincipalDep
from autoair.storage import StorageError, TargetStatus
from autoair.storage.config import StorageSettings, TargetKindName, build_target
from autoair.storage.credentials import user_conn_id
from autoair.storage.request_token import RequestTokenInfo, root_settings
from autoair.storage.request_token import info as request_token_info

read_router = APIRouter(prefix="/deploy-target", tags=["deploy-target"])
admin_router = APIRouter(prefix="/deploy-target", tags=["deploy-target"])


class DeployTargetPolicy(BaseModel):
    """Administrator policy (configuration only; the dialog cannot change it)."""

    allowed_targets: list[TargetKindName] = Field(
        description="Target kinds offered and accepted (storage.allowed_targets)."
    )
    forced_conn_id: str | None = Field(
        default=None, description="Git connection every deploy uses (storage.git.forced_conn_id)."
    )
    credentials_mode: Literal["shared", "per_user"] = "shared"
    user_conn_id_template: str = "git_{username}"
    user_conn_id_pattern: str | None = None
    user_credentials_fallback: bool = False
    commit_author: Literal["auto", "service", "user"] = "auto"
    request_token: Literal["off", "optional", "required"] = "optional"  # noqa: S105 - a mode
    request_token_overrides_forced: bool = False
    request_token_allow_insecure: bool = False


class DeployTargetInfo(BaseModel):
    settings: StorageSettings
    source: str
    status: TargetStatus | None = None
    error: str | None = None
    candidates: list[DeployCandidate]
    policy: DeployTargetPolicy | None = None
    request_token: RequestTokenInfo | None = Field(
        default=None,
        description="Git targets: whether deploys take a token typed by the user (header "
        "X-AutoAir-Git-Token) and which token the git host needs.",
    )


class GitCredentialChoice(BaseModel):
    """The caller's git credentials for deploys (per_user mode)."""

    mode: Literal["shared", "per_user"]
    forced_conn_id: str | None = None
    conn_id: str | None = Field(
        default=None, description="per_user: the template's connection id for this user."
    )
    conn_exists: bool | None = Field(
        default=None,
        description="per_user: Airflow lets this user read conn_id (null: not checked).",
    )
    pattern: str | None = None
    choices: list[str] = Field(
        default_factory=list,
        description="per_user: readable connections matching user_conn_id_pattern.",
    )
    fallback: bool = False
    error: str | None = None
    request_token: RequestTokenInfo | None = Field(
        default=None,
        description="Whether deploys of this target take a token typed by the user "
        "(off/optional/required) and the token the git host needs (null: not a git target).",
    )


def _policy(services: ServicesDep) -> DeployTargetPolicy:
    configured = services.configured_storage or services.settings.storage
    git = configured.git
    return DeployTargetPolicy(
        allowed_targets=list(configured.allowed_targets),
        forced_conn_id=git.forced_conn_id,
        credentials_mode=git.credentials_mode,
        user_conn_id_template=git.user_conn_id_template,
        user_conn_id_pattern=git.user_conn_id_pattern,
        user_credentials_fallback=git.user_credentials_fallback,
        commit_author=git.commit_author,
        request_token=git.request_token,
        request_token_overrides_forced=git.request_token_overrides_forced,
        request_token_allow_insecure=git.request_token_allow_insecure,
    )


def _enforce(services: ServicesDep, body: StorageSettings) -> StorageSettings:
    try:
        return apply_policy(body, services.configured_storage or services.settings.storage)
    except PolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class DeployTargetTest(BaseModel):
    ok: bool
    message: str
    status: TargetStatus | None = None


def _info(services: ServicesDep) -> DeployTargetInfo:
    status: TargetStatus | None = None
    error: str | None = None
    try:
        status = services.ensure_target().status()
    except Exception as exc:  # unreachable remote, missing credentials…
        error = str(exc)
    else:
        if status.details.get("unavailable"):  # could not be built at start (retried)
            error = str(status.details.get("error"))
    policy = _policy(services)
    # The manual object storage option starts from the active object target, else the
    # configured one (its masked credentials are restored from configuration on save).
    current = services.settings.storage
    prefill = current if current.mode == "object" else services.configured_storage
    candidates = [
        c
        for c in discover_candidates(services.settings.dags_folder, prefill)
        if candidate_mode(c) in policy.allowed_targets
    ]
    return DeployTargetInfo(
        settings=scrub_secrets(services.settings.storage),
        source=services.storage_source,
        status=status,
        error=error,
        candidates=candidates,
        policy=policy,
        request_token=request_token_info(
            services.settings.storage.git, root_settings(services), services.settings.storage.mode
        ),
    )


def _test(services: ServicesDep, settings: StorageSettings) -> DeployTargetTest:
    """Build the target and talk to it (reads the base branch head / lists specs)."""
    try:
        target = build_target(settings, default_local_path=services.settings.output_dir)
        status = target.status()
        target.list()
    except (StorageError, OSError, ValueError) as exc:
        return DeployTargetTest(ok=False, message=str(exc))
    except Exception as exc:  # provider errors (HTTP, git) surface as messages
        return DeployTargetTest(ok=False, message=f"{type(exc).__name__}: {exc}")
    if not status.writable:
        return DeployTargetTest(ok=False, message="target is read-only", status=status)
    return DeployTargetTest(ok=True, message=f"connected: {status.location}", status=status)


@read_router.get("", operation_id="getDeployTarget")
async def get_deploy_target(services: ServicesDep) -> DeployTargetInfo:
    """Current deploy target and the options this Airflow offers (bundles, git checkout)."""
    return await run_in_threadpool(_info, services)


@admin_router.post("/test", operation_id="testDeployTarget")
async def test_deploy_target(body: StorageSettings, services: ServicesDep) -> DeployTargetTest:
    body = _enforce(services, body)
    body = restore_secrets(body, services.configured_storage or StorageSettings())
    return await run_in_threadpool(_test, services, body)


@admin_router.put("", operation_id="setDeployTarget")
async def set_deploy_target(body: StorageSettings, services: ServicesDep) -> DeployTargetInfo:
    """Test, persist (secrets excluded — use an Airflow connection) and switch now. The
    administrator's policy (allowed target kinds, forced git connection, credential rules)
    is enforced: disallowed kinds are refused, policy fields are taken from configuration."""
    if services.deploy_store is None:
        raise HTTPException(status_code=404, detail="deploy target setup is not available")
    body = _enforce(services, body)
    if body.git.token or body.git.github_private_key:
        raise HTTPException(
            status_code=400,
            detail="credentials are not stored here: put them in an Airflow connection and set "
            "git.conn_id",
        )
    configured = services.configured_storage or StorageSettings()
    clear = unreferenced_secrets(body, configured)
    if clear:
        raise HTTPException(
            status_code=400,
            detail="credentials are not stored here: remove storage_options "
            + ", ".join(clear)
            + " (configure them in AUTOAIR_STORAGE__STORAGE_OPTIONS or the environment; "
            "'***' keeps the configured value)",
        )
    body = restore_secrets(body, configured)  # "***" -> the configured credential
    result = await run_in_threadpool(_test, services, body)
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.message)
    await run_in_threadpool(services.deploy_store.save, body)
    git = body.git.model_copy(update={"token": None, "github_private_key": None})
    services.apply_storage(body.model_copy(update={"git": git}), source="ui")
    return await run_in_threadpool(_info, services)


@admin_router.delete("", operation_id="resetDeployTarget")
async def reset_deploy_target(services: ServicesDep) -> DeployTargetInfo:
    """Forget the target chosen in the UI and go back to AUTOAIR_STORAGE__* settings."""
    if services.deploy_store is not None:
        await run_in_threadpool(services.deploy_store.clear)
    # The target the process was configured with (env *and* CLI overrides), not a fresh
    # Settings() that would silently drop CLI options.
    configured = services.configured_storage or StorageSettings()
    services.apply_storage(configured, source="settings")
    return await run_in_threadpool(_info, services)


@read_router.get("/git-credentials", operation_id="getGitCredentials")
async def get_git_credentials(
    services: ServicesDep,
    principal: PrincipalDep,
    gateway: GatewayDep,
    team: Annotated[
        str | None,
        Query(max_length=250, description="Team whose deploy target (team_storage) applies."),
    ] = None,
) -> GitCredentialChoice:
    """Which credentials the caller's git deploys use: a token typed per deploy
    (storage.git.request_token), the forced or shared connection, or their own connection
    (per-user credentials, looked up with their token so Airflow's RBAC applies)."""
    settings = services.settings
    storage = settings.team_storage.get(team, settings.storage) if team else settings.storage
    git = storage.git
    token_info = request_token_info(git, root_settings(services), storage.mode)
    if git.forced_conn_id or git.credentials_mode != "per_user":
        return GitCredentialChoice(
            mode=git.credentials_mode, forced_conn_id=git.forced_conn_id, request_token=token_info
        )
    out = GitCredentialChoice(
        mode="per_user",
        pattern=git.user_conn_id_pattern,
        fallback=git.user_credentials_fallback,
        request_token=token_info,
    )
    if not principal.user:
        out.error = "per-user git credentials need a signed-in user"
        return out
    out.conn_id = user_conn_id(git, principal.user)
    try:
        out.conn_exists = await gateway.connection(out.conn_id) is not None
        if git.user_conn_id_pattern:
            pattern = re.compile(git.user_conn_id_pattern)
            out.choices = sorted(
                c.connection_id
                for c in await gateway.connections()
                if pattern.fullmatch(c.connection_id)
            )
    except AirflowError as exc:
        if exc.status in (401, 403):
            out.conn_exists = False
            out.error = "Airflow does not let you read connections"
        else:
            out.error = f"cannot list connections ({exc.status})"
    return out
