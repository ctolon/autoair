"""Spec CRUD + deploy (validate → render → write via the deploy target → reparse).

Permissions (autoair.iam): ``spec:read`` to read, ``audit:read`` for the audit trail,
``review`` to approve a version, ``spec:write`` to save — plus ``deploy`` when the save goes
live (anything but a pull request) and ``deploy:direct_push`` to bypass a pull request.
Airflow's per-DAG read/edit access is checked by the router dependencies (``create_api``).

Four-eyes deploys (``iam.require_approval``): a deploy that would go live without another
user's approval of that exact version is stored as a *proposal* (HTTP 202) instead; it is
reviewed (``POST /specs/{dag_id}/reviews`` with the proposal's etag) and then deployed by
anyone holding ``deploy`` (``POST /specs/{dag_id}/proposals/{etag}/deploy``). Pull requests
are exempt: on git targets in PR mode the pull request is the proposal.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.app import Services
from autoair.api.deps import ServicesDep, get_gateway, policy_of
from autoair.api.features import ensure_feature
from autoair.api.git_token import GitTokenHeader, accept_token, http_error, scrubbed_errors
from autoair.api.routers.ops import DEPLOYS
from autoair.audit import request_user
from autoair.gateway import AirflowError, AirflowGateway
from autoair.governance import (
    DeployContext,
    UserIdentity,
    ValidationSummary,
    commit_message,
    dag_link,
    reviewers_of,
)
from autoair.iam import model as P
from autoair.iam.deps import PrincipalDep, check, deny, direct_push_roles, iam_settings, require
from autoair.iam.model import Principal
from autoair.iam.proposals import (
    Proposal,
    ProposalError,
    ProposalStore,
    ReviewRecord,
    latest_verdicts,
    new_proposal,
    now_iso,
)
from autoair.persistence.protocols import ProposalRepository
from autoair.secret_mask import mask
from autoair.spec import DagSpec
from autoair.storage import (
    ConflictError,
    DeployTarget,
    EjectedError,
    NotFoundError,
    SpecSummary,
    StorageError,
    StoredSpec,
    spec_etag,
)
from autoair.storage.credentials import CredentialsError, ResolvedTarget, resolve_user_target
from autoair.storage.git import GitMode, GitTarget
from autoair.storage.request_token import (
    RequestToken,
    RequestTokenError,
    build_request_target,
    check_required,
    root_settings,
    token_user,
)
from autoair.telemetry import span
from autoair.validation import Problem, ValidationReport, validate_spec

# Split so the auth backend can guard writes with DAG-level permissions.
read_router = APIRouter(prefix="/specs", tags=["specs"])
write_router = APIRouter(prefix="/specs", tags=["specs"])


class StoredSpecResponse(BaseModel):
    spec: DagSpec
    etag: str
    version: str | None = None
    pending_branch: str | None = None
    pending_pr_url: str | None = None


class DeployRequest(BaseModel):
    spec: DagSpec
    base_etag: str | None = None
    environment: str | None = None
    message: str | None = Field(
        default=None,
        max_length=20_000,
        description="Commit message (first line: subject). AutoAir appends the mandatory "
        "context block and trailers (autoair.governance); they cannot be removed.",
    )
    pr_description: str | None = Field(
        default=None,
        max_length=60_000,
        description="Pull request description (git PR mode): the {description} placeholder "
        "of storage.git.pr_body_template; the context block is always appended.",
    )
    git_conn_id: str | None = Field(
        default=None,
        max_length=250,
        description="per_user git credentials: the user's Airflow connection to commit with "
        "(must match storage.git.user_conn_id_pattern; default: the template's).",
    )
    force: bool = False
    create_only: bool = Field(
        default=False,
        description="Only create: when the DAG already exists in the target the deploy "
        "returns status 'conflict' and writes nothing (bulk imports use it so nothing is "
        "overwritten without confirmation).",
    )
    direct_push: bool | None = Field(
        default=None,
        description="Git targets: true pushes straight to the branch even in PR mode (needs "
        "'deploy:direct_push'); false opens a pull request even in push mode; null follows "
        "the target's mode (and iam.force_pr_without_direct_push).",
    )


class ReviewRequest(BaseModel):
    etag: str = Field(
        min_length=1,
        max_length=128,
        description="Reviewed spec version (etag): a stored version or a deploy proposal.",
    )
    verdict: Literal["approve", "request_changes", "comment"] = "approve"
    comment: str | None = Field(default=None, max_length=4000)


class Review(BaseModel):
    timestamp: str
    user: str | None
    etag: str
    verdict: str
    comment: str | None = None


class ConflictInfo(BaseModel):
    current_etag: str | None = None
    current_version: str | None = None
    current_spec: DagSpec | None = None
    ejected_file: str | None = None


DeployStatus = Literal["deployed", "invalid", "conflict", "ejected", "pending_approval"]


class DeployResult(BaseModel):
    deployed: bool
    status: DeployStatus = Field(
        default="deployed",
        description="'pending_approval' (HTTP 202): stored as a deploy proposal that needs "
        "another user's approval (iam.require_approval); nothing went live.",
    )
    report: ValidationReport
    dag_file: str | None = None
    etag: str | None = None
    version: str | None = None
    pr_url: str | None = None
    branch: str | None = None
    reparse_requested: bool = False
    conflict: ConflictInfo | None = None
    proposal_etag: str | None = Field(
        default=None, description="Etag of the stored deploy proposal (pending_approval)."
    )
    message: str


class ProposalSummary(BaseModel):
    dag_id: str
    etag: str
    author: str | None = None
    timestamp: str
    expires_at: str
    message: str | None = None
    environment: str | None = None
    base_etag: str | None = None
    direct_push: bool | None = None
    approved_by: list[str] = Field(
        default_factory=list, description="Other users whose latest verdict is 'approve'."
    )
    changes_requested_by: list[str] = Field(default_factory=list)
    approved: bool = Field(description="Deployable: approved by someone else than the author.")


class ProposalDetail(ProposalSummary):
    spec: DagSpec
    current_spec: DagSpec | None = Field(
        default=None, description="The DAG's stored spec now (diff base)."
    )
    current_etag: str | None = None
    stale: bool = Field(
        default=False,
        description="The stored spec changed since the author's base version (deploying "
        "would conflict unless forced).",
    )
    reviews: list[Review] = Field(default_factory=list)


class ProposalDeployRequest(BaseModel):
    force: bool = Field(
        default=False, description="Overwrite even if the stored spec changed meanwhile."
    )
    message: str | None = Field(
        default=None, max_length=4000, description="Commit message (default: the proposal's)."
    )


class DeleteResult(BaseModel):
    deleted: bool


def _etag(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().removeprefix("W/").strip('"') or None


def _find(services: Services, dag_id: str) -> StoredSpec | None:
    for target in services.all_targets():
        stored = target.get(dag_id)
        if stored is not None:
            return stored
    return None


def _owner(services: Services, dag_id: str) -> DeployTarget:
    for target in services.all_targets():
        if target.get(dag_id) is not None:
            return target
    return services.target


def _store(services: Services) -> ProposalRepository:
    if services.proposals is None:
        iam = iam_settings(services)
        services.proposals = ProposalStore(
            iam.proposals_url or str(services.settings.state_dir / "proposals"),
            ttl=iam.proposal_ttl_seconds,
        )
    return services.proposals


def _user(request: Request, principal: Principal) -> str | None:
    return principal.user or request_user(request)


@read_router.get("", operation_id="listSpecs", dependencies=[require(P.SPEC_READ)])
async def list_specs(services: ServicesDep) -> list[SpecSummary]:
    def merged() -> list[SpecSummary]:
        seen: dict[str, SpecSummary] = {}
        for target in services.all_targets():
            for summary in target.list():
                seen.setdefault(summary.dag_id, summary)
        return sorted(seen.values(), key=lambda s: s.dag_id)

    return await run_in_threadpool(merged)


@read_router.get("/{dag_id}", operation_id="getSpec", dependencies=[require(P.SPEC_READ)])
async def get_spec(dag_id: str, services: ServicesDep, response: Response) -> StoredSpecResponse:
    try:
        stored = await run_in_threadpool(_find, services, dag_id)
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if stored is None:
        raise HTTPException(status_code=404, detail=f"no AutoAir spec for {dag_id!r}")
    response.headers["ETag"] = f'"{stored.etag}"'
    return StoredSpecResponse(
        spec=stored.spec,
        etag=stored.etag,
        version=stored.version,
        pending_branch=stored.pending_branch,
        pending_pr_url=stored.pending_pr_url,
    )


def _import_check(services: Services, principal: Principal) -> tuple[bool, Problem | None]:
    """Whether this deploy runs the import check (executes the DAG file in a sandboxed
    subprocess); when skipped for lack of ``code:execute``, the info problem saying so."""
    if not services.settings.import_check_on_deploy:
        return False, None
    if iam_settings(services).import_check_requires_code_execute and not principal.has(
        P.CODE_EXECUTE
    ):
        return False, Problem(
            severity="info",
            code="import_check_skipped",
            message="import check skipped: the deploying user lacks the AutoAir permission "
            "'code:execute' (iam.import_check_requires_code_execute); the DAG was validated "
            "statically only",
        )
    return True, None


def validate_for_write(
    services: Services,
    principal: Principal,
    effective: DagSpec,
    env: str | None = None,
    *,
    action: Literal["deploy", "promote"] = "deploy",
) -> ValidationReport:
    """The validation every write runs (deploy, promotion): static checks, policy rules and
    OPA for ``env``, plus the import check when enabled and allowed. Blocking."""
    settings = services.settings
    run_import, skipped = _import_check(services, principal)
    report = validate_spec(
        effective,
        catalog=services.catalog,
        registry=services.registry,
        policy=policy_of(services),
        import_check=run_import,
        target_version=settings.target_version_tuple(),
        import_timeout=settings.import_check_timeout,
        templates=services.templates.lock,
        environment=env,
        context={"user": principal.user, "action": action},
    )
    if skipped is not None:
        report.problems.append(skipped)
    return report


async def _validate(
    services: Services, principal: Principal, effective: DagSpec, env: str | None = None
) -> ValidationReport:
    return await run_in_threadpool(validate_for_write, services, principal, effective, env)


async def check_raw_write(request: Request, principal: Principal, spec: DagSpec) -> None:
    """A raw code DAG is arbitrary Python: writing one needs the code editor and Python code
    features (404 while off) and the ``code:raw`` permission."""
    if spec.raw_code is None:
        return
    ensure_feature(request, "code_editor")
    ensure_feature(request, "python_code")
    await check(
        request,
        principal,
        P.CODE_RAW,
        detail="raw code DAGs are arbitrary Python: saving one needs the AutoAir permission "
        "'code:raw'",
    )


def _environment(services: Services, spec: DagSpec, environment: str | None) -> str | None:
    env = environment or services.settings.default_environment
    if env and spec.environments and env not in spec.environments:
        raise HTTPException(status_code=400, detail=f"unknown environment {env!r}")
    return env


@write_router.put(
    "/{dag_id}",
    operation_id="deploySpec",
    dependencies=[require(P.SPEC_WRITE)],
    responses={
        202: {
            "model": DeployResult,
            "description": "Stored as a deploy proposal awaiting approval (status "
            "'pending_approval'); nothing went live.",
        }
    },
)
async def deploy_spec(
    dag_id: str,
    body: DeployRequest,
    request: Request,
    response: Response,
    services: ServicesDep,
    principal: PrincipalDep,
    gateway: Annotated[AirflowGateway, Depends(get_gateway)],
    git_token: GitTokenHeader,
    if_match: Annotated[str | None, Header()] = None,
) -> DeployResult:
    spec = body.spec
    if spec.dag_id != dag_id:
        raise HTTPException(status_code=400, detail="dag_id in path and body differ")
    await check_raw_write(request, principal, spec)
    env = _environment(services, spec, body.environment)
    target = services.target_for(spec.team)
    token = accept_token(
        services, request, git_token, kind=target.kind, cfg=_git_cfg(services, spec.team)
    )
    push_mode = await _push_mode(request, principal, services, target, body.direct_push, spec)
    base_etag = body.base_etag or _etag(if_match)
    if body.create_only and not base_etag:
        stored = await run_in_threadpool(_stored_etag, target, dag_id, env)
        if stored is not None:
            return DeployResult(
                deployed=False,
                status="conflict",
                report=ValidationReport(ok=True, problems=[]),
                message=f"DAG {dag_id!r} already exists; confirm to overwrite it",
                conflict=ConflictInfo(current_etag=stored),
            )
    etag = spec_etag(spec)
    user = _user(request, principal)
    approvers: list[str] = []
    if push_mode != "pr":  # goes live: not a pull request
        if iam_settings(services).require_approval:
            approvers = await run_in_threadpool(_approvers, services, dag_id, etag, user)
            if not approvers:
                return await _propose(
                    request, response, services, principal, spec, etag, env, body, base_etag
                )
        await check(request, principal, P.DEPLOY)
    result = await _deploy(
        request,
        services,
        principal,
        gateway,
        spec,
        env=env,
        target=target,
        push_mode=push_mode,
        base_etag=base_etag,
        message=body.message,
        force=body.force,
        direct_push=bool(body.direct_push and push_mode == "push"),
        audit_extra={},
        approvals=approvers,
        pr_description=body.pr_description,
        git_conn_id=body.git_conn_id,
        token=token,
    )
    if result.deployed and approvers:  # an identical pending proposal is now deployed
        await run_in_threadpool(_store(services).delete, dag_id, etag)
    return result


def _approvers(services: Services, dag_id: str, etag: str, user: str | None) -> list[str]:
    """Approvals that let ``user`` deploy version ``etag`` live: by another user than the
    author — the author of a pending proposal of that version, else the deployer."""
    store = _store(services)
    proposal = store.get(dag_id, etag)
    author = proposal.author if proposal is not None else user
    if not user:
        return []
    return store.approvers(dag_id, etag, exclude=author)


async def _propose(
    request: Request,
    response: Response,
    services: Services,
    principal: Principal,
    spec: DagSpec,
    etag: str,
    env: str | None,
    body: DeployRequest,
    base_etag: str | None,
) -> DeployResult:
    """Store ``spec`` as a deploy proposal (validated first; nothing goes live)."""
    effective = spec.with_environment(env) if env and env in spec.environments else spec
    report = await _validate(services, principal, effective, env)
    if not report.ok:
        DEPLOYS.labels("invalid").inc()
        return DeployResult(
            deployed=False,
            status="invalid",
            report=report,
            message="validation failed; no proposal was stored",
        )
    user = _user(request, principal)
    store = _store(services)
    existing = await run_in_threadpool(store.get, spec.dag_id, etag)
    if existing is not None:  # the same version is already proposed: keep its author
        response.status_code = 202
        return DeployResult(
            deployed=False,
            status="pending_approval",
            report=report,
            etag=etag,
            proposal_etag=etag,
            message=f"this version is already proposed (by {existing.author or 'unknown'}) "
            "and awaits an approval by another reviewer",
        )
    proposal = new_proposal(
        spec,
        etag,
        author=user,
        message=body.message,
        base_etag=base_etag,
        environment=env,
        force=body.force,
        direct_push=body.direct_push,
    )
    await run_in_threadpool(store.save, proposal)
    target = services.target_for(spec.team)
    context = await run_in_threadpool(
        lambda: _context(
            services,
            request,
            principal,
            spec,
            env=env,
            target=target,
            push_mode=None,
            etag_before=base_etag,
            report=report,
            description=body.pr_description,
        )
    )
    await run_in_threadpool(
        lambda: services.audit.record(
            "propose",
            user=user,
            dag_id=spec.dag_id,
            **{**context.audit(), "etag": etag, "base_etag": base_etag, "message": body.message},
        )
    )
    DEPLOYS.labels("proposed").inc()
    response.status_code = 202
    return DeployResult(
        deployed=False,
        status="pending_approval",
        report=report,
        etag=etag,
        proposal_etag=etag,
        message="this version needs an approval by another reviewer: stored as a deploy "
        "proposal; once approved, anyone holding 'deploy' can deploy it",
    )


def _identity(request: Request, principal: Principal) -> UserIdentity:
    return UserIdentity(
        username=_user(request, principal),
        display_name=principal.display_name,
        email=principal.email,
    )


def _git_cfg(services: Services, team: str | None) -> Any:
    settings = services.settings
    storage = settings.team_storage.get(team, settings.storage) if team else settings.storage
    return storage.git


async def _write_target(
    services: Services,
    gateway: AirflowGateway,
    target: DeployTarget,
    *,
    team: str | None,
    user: str | None,
    conn_id: str | None,
    token: RequestToken | None = None,
) -> ResolvedTarget:
    """The target this user's write goes through: a throw-away target authenticated with
    the token typed for this request (storage.git.request_token), else per-user git
    credentials (their own Airflow connection, checked with their token) when
    storage.git.credentials_mode is per_user, else the shared target."""
    cfg = _git_cfg(services, team)
    try:
        check_required(token, kind=target.kind, cfg=cfg, root=root_settings(services))
        if token is not None:
            writer = await run_in_threadpool(build_request_target, cfg, token)
            return ResolvedTarget(writer, "request_token")
    except RequestTokenError as exc:
        raise http_error(exc) from None
    try:
        return await resolve_user_target(
            target,
            _git_cfg(services, team),
            username=user,
            requested_conn_id=conn_id,
            check_access=gateway.connection,
            run_sync=run_in_threadpool,
        )
    except CredentialsError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _commit_as_service(services: Services, team: str | None, resolved: ResolvedTarget) -> bool:
    """storage.git.commit_author: ``service`` commits as the service identity (the user in
    trailers/Co-authored-by); ``user`` as the user; ``auto`` = user with their own
    credentials."""
    choice = _git_cfg(services, team).commit_author
    if choice == "auto":  # the user's own connection or the token they typed
        return resolved.source not in ("user", "request_token")
    return bool(choice == "service")


async def _credential_audit(resolved: ResolvedTarget) -> dict[str, Any]:
    """Which credentials a write used (audit): ``credential: request-token`` (plus the git
    host's name for the token's owner, when it tells) or the connection's source."""
    if resolved.source == "request_token":
        return {
            "credentials": resolved.source,
            "credential": "request-token",
            "credential_user": await run_in_threadpool(token_user, resolved.target),
        }
    return {
        "credentials": resolved.source,
        "credential": "connection",
        "credentials_conn_id": resolved.conn_id,
    }


def _target_label(target: DeployTarget, push_mode: GitMode | None) -> str:
    if isinstance(target, GitTarget):
        return f"{target.provider.location}@{target.base_branch}"
    root = getattr(target, "root", None) or getattr(target, "url", None)
    return f"{target.kind}:{root}" if root else target.kind


def _stored_etag(target: DeployTarget, dag_id: str, env: str | None) -> str | None:
    try:
        if isinstance(target, GitTarget):
            stored = target.get(dag_id, environment=env)
        else:
            stored = target.get(dag_id)
    except Exception:  # unreadable/unreachable: the etag is informative only
        return None
    return stored.etag if stored else None


def _reviews(services: Services, dag_id: str, etag: str, author: str | None) -> list[str]:
    try:
        return reviewers_of(_store(services).reviews(dag_id), etag, exclude=author)
    except Exception:
        return []


def _context(
    services: Services,
    request: Request,
    principal: Principal,
    spec: DagSpec,
    *,
    action: Literal["deploy", "delete", "promote"] = "deploy",
    env: str | None,
    target: DeployTarget,
    push_mode: GitMode | None,
    etag_before: str | None,
    report: ValidationReport | None,
    approvals: list[str] | None = None,
    proposed_by: str | None = None,
    description: str | None = None,
) -> DeployContext:
    etag = spec_etag(spec)
    user = _identity(request, principal)
    return DeployContext(
        action=action,
        dag_id=spec.dag_id,
        user=user,
        environment=env,
        etag_before=etag_before,
        etag_after=etag if action != "delete" else None,
        validation=ValidationSummary.of(report) if report is not None else None,
        approvals=list(approvals or []),
        reviewers=_reviews(services, spec.dag_id, etag, proposed_by or user.username),
        proposed_by=proposed_by,
        link=dag_link(
            services.settings.server.public_url, spec.dag_id, mode=services.settings.mode
        ),
        target=_target_label(target, push_mode),
        git_mode=push_mode,
        description=description,
    )


async def _deploy(
    request: Request,
    services: Services,
    principal: Principal,
    gateway: AirflowGateway,
    spec: DagSpec,
    *,
    env: str | None,
    target: DeployTarget,
    push_mode: GitMode | None,
    base_etag: str | None,
    message: str | None,
    force: bool,
    direct_push: bool,
    audit_extra: dict[str, Any],
    approvals: list[str] | None = None,
    proposed_by: str | None = None,
    pr_description: str | None = None,
    git_conn_id: str | None = None,
    token: RequestToken | None = None,
) -> DeployResult:
    """Validate, render and write ``spec`` (authorization already checked). Every write
    carries the deploy context (autoair.governance): commit/PR text and the audit record.
    ``token``: the git token typed for this request (used for this write only)."""
    dag_id = spec.dag_id
    effective = spec.with_environment(env) if env and env in spec.environments else spec
    report = await _validate(services, principal, effective, env)
    user = _user(request, principal)
    if not report.ok:
        DEPLOYS.labels("invalid").inc()
        return DeployResult(
            deployed=False,
            status="invalid",
            report=report,
            message="validation failed; nothing was written",
        )
    resolved = await _write_target(
        services, gateway, target, team=spec.team, user=user, conn_id=git_conn_id, token=token
    )
    writer = resolved.target
    before = base_etag or await run_in_threadpool(_stored_etag, target, dag_id, env)
    context = await run_in_threadpool(
        lambda: _context(
            services,
            request,
            principal,
            spec,
            env=env,
            target=writer,
            push_mode=push_mode,
            etag_before=before,
            report=report,
            approvals=approvals,
            proposed_by=proposed_by,
            description=pr_description,
        )
    )
    as_service = _commit_as_service(services, spec.team, resolved)
    try:
        with span("render", dag_id=dag_id, output_mode=spec.output_mode):
            files = await run_in_threadpool(services.render_files, spec, env)
        save_args: dict[str, Any] = {
            "base_etag": base_etag,
            "author": user,
            "message": message,
            "environment": env,
            "force": force,
        }
        if isinstance(writer, GitTarget):
            save_args["mode"] = push_mode
            save_args["context"] = context
            save_args["co_author"] = as_service
            if not as_service:
                save_args["author"] = context.user.git_author() or user
            if push_mode == "push" and writer.mode == "pr" and save_args["base_etag"]:
                save_args["base_etag"] = await run_in_threadpool(
                    _direct_push_base, writer, dag_id, env, save_args["base_etag"]
                )
        else:  # local folder / object storage: the version message carries the context
            subject = (message or "").strip() or f"AutoAir: update DAG {dag_id}"
            save_args["message"] = commit_message(subject, context, co_author=False)
        with scrubbed_errors(token):
            result = await run_in_threadpool(lambda: writer.save(spec, files, **save_args))
    except ConflictError as exc:
        DEPLOYS.labels("conflict").inc()
        cur = exc.current
        return DeployResult(
            deployed=False,
            status="conflict",
            report=report,
            message=mask(str(exc)),
            conflict=ConflictInfo(
                current_etag=cur.etag if cur else None,
                current_version=cur.version if cur else None,
                current_spec=cur.spec if cur else None,
            ),
        )
    except EjectedError as exc:
        DEPLOYS.labels("ejected").inc()
        return DeployResult(
            deployed=False,
            status="ejected",
            report=report,
            message=mask(str(exc)),
            conflict=ConflictInfo(ejected_file=str(exc.path)),
        )
    except HTTPException:
        DEPLOYS.labels("error").inc()
        raise
    except StorageError as exc:
        DEPLOYS.labels("error").inc()
        raise HTTPException(status_code=409, detail=mask(str(exc))) from exc

    DEPLOYS.labels("ok").inc()
    kind = "pull_request" if result.pr_url else ("direct_push" if direct_push else "deploy")
    details: dict[str, Any] = {
        **context.audit(),
        "etag": result.etag,
        "version": result.version,
        "target": target.kind,
        "target_location": context.target,
        "pr_url": result.pr_url,
        "branch": result.branch,
        "direct_push": direct_push,
        "write": kind,
        **(await _credential_audit(resolved)),
        "commit_author": "service" if as_service else "user",
        **audit_extra,
    }
    await run_in_threadpool(
        lambda: services.audit.record("deploy", user=user, dag_id=dag_id, **details)
    )
    reparsed = False
    msg = result.message or f"written to {result.path}"
    if target.kind == "local" and not result.pr_url and not services.settings.feature("airflow"):
        msg += "; Airflow will pick it up on the next bundle refresh"  # no Airflow server
    elif target.kind == "local" and not result.pr_url:
        try:
            status = await gateway.dag_status(dag_id)
            if status.exists and status.file_token:
                await gateway.reparse(status.file_token)
                reparsed = True
                msg += "; reparse requested"
            else:
                msg += "; Airflow will pick the new DAG up on the next bundle refresh"
        except AirflowError as exc:
            msg += f"; could not contact Airflow ({exc.detail})"
    elif result.pr_url:
        msg += f"; review and merge {result.pr_url} to release it"
    else:
        msg += "; Airflow will pick it up on the next bundle refresh"
    return DeployResult(
        deployed=True,
        report=report,
        dag_file=str(result.path),
        etag=result.etag,
        version=result.version,
        pr_url=result.pr_url,
        branch=result.branch,
        reparse_requested=reparsed,
        message=msg,
    )


class GitCredentialsInfo(BaseModel):
    mode: Literal["shared", "per_user"]
    forced_conn_id: str | None = Field(
        default=None, description="Admin-forced Airflow connection (storage.git.forced_conn_id)."
    )
    conn_id: str | None = Field(
        default=None, description="per_user: the connection this deploy would use."
    )
    pattern: str | None = Field(
        default=None, description="per_user: regex of connection ids the user may pick."
    )
    fallback: bool = Field(default=False, description="Shared credentials if the user has none.")


class DeployPreview(BaseModel):
    """What a deploy would write: the commit message and pull request texts with the
    mandatory context block (the deploy dialog shows it)."""

    target_kind: str
    git_mode: Literal["pr", "push"] | None = None
    opens_pr: bool = False
    commit_message: str
    pr_title: str | None = None
    pr_body: str | None = None
    context: str = Field(description="The mandatory context block (cannot be removed).")
    trailers: list[str] = Field(default_factory=list)
    commit_author: Literal["service", "user"] = "service"
    credentials: GitCredentialsInfo | None = None
    validation: str = Field(description="Static validation summary (no import check).")


@write_router.post(
    "/{dag_id}/deploy-preview",
    operation_id="previewDeploy",
    dependencies=[require(P.SPEC_WRITE)],
)
async def preview_deploy(
    dag_id: str,
    body: DeployRequest,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
) -> DeployPreview:
    """The commit message / pull request description a deploy of ``body`` would produce
    (nothing is written; the validation is static)."""
    spec = body.spec
    if spec.dag_id != dag_id:
        raise HTTPException(status_code=400, detail="dag_id in path and body differ")
    await check_raw_write(request, principal, spec)
    env = _environment(services, spec, body.environment)
    target = services.target_for(spec.team)
    push_mode = await _push_mode(request, principal, services, target, body.direct_push, spec)
    effective = spec.with_environment(env) if env and env in spec.environments else spec
    settings = services.settings
    report = await run_in_threadpool(
        lambda: validate_spec(
            effective,
            catalog=services.catalog,
            registry=services.registry,
            policy=policy_of(services),
            import_check=False,
            target_version=settings.target_version_tuple(),
            environment=env,
            context={"user": principal.user, "action": "deploy_preview"},
        )
    )
    user = _user(request, principal)
    cfg = _git_cfg(services, spec.team)
    credentials: GitCredentialsInfo | None = None
    source: Literal["shared", "user"] = "shared"
    if isinstance(target, GitTarget):
        conn: str | None = None
        if cfg.credentials_mode == "per_user" and not cfg.forced_conn_id and user:
            from autoair.storage.credentials import chosen_conn_id

            try:
                conn = chosen_conn_id(cfg, user, body.git_conn_id)
            except CredentialsError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            source = "user"
        credentials = GitCredentialsInfo(
            mode=cfg.credentials_mode,
            forced_conn_id=cfg.forced_conn_id,
            conn_id=conn,
            pattern=cfg.user_conn_id_pattern if cfg.credentials_mode == "per_user" else None,
            fallback=cfg.user_credentials_fallback,
        )
    as_service = _commit_as_service(services, spec.team, ResolvedTarget(target, source))
    before = body.base_etag or await run_in_threadpool(_stored_etag, target, dag_id, env)
    context = await run_in_threadpool(
        lambda: _context(
            services,
            request,
            principal,
            spec,
            env=env,
            target=target,
            push_mode=push_mode,
            etag_before=before,
            report=report,
            description=body.pr_description,
        )
    )
    summary = ValidationSummary.of(report).text()
    if isinstance(target, GitTarget):
        author = user if as_service else (context.user.git_author() or user)
        texts = target.compose(
            dag_id,
            environment=env,
            author=author,
            message=body.message,
            context=context,
            co_author=as_service,
        )
        opens_pr = push_mode == "pr"
        return DeployPreview(
            target_kind=target.kind,
            git_mode=push_mode,
            opens_pr=opens_pr,
            commit_message=texts.commit_message,
            pr_title=texts.pr_title if opens_pr else None,
            pr_body=texts.pr_body if opens_pr else None,
            context=context.block(),
            trailers=context.trailers(co_author=as_service),
            commit_author="service" if as_service else "user",
            credentials=credentials,
            validation=summary,
        )
    subject = (body.message or "").strip() or f"AutoAir: update DAG {dag_id}"
    return DeployPreview(
        target_kind=target.kind,
        commit_message=commit_message(subject, context, co_author=False),
        context=context.block(),
        trailers=context.trailers(co_author=False),
        commit_author="user",
        validation=summary,
    )


@write_router.delete("/{dag_id}", operation_id="deleteSpec", dependencies=[require(P.SPEC_DELETE)])
async def delete_spec(
    dag_id: str,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
    gateway: Annotated[AirflowGateway, Depends(get_gateway)],
    git_token: GitTokenHeader,
    if_match: Annotated[str | None, Header()] = None,
    git_conn_id: Annotated[
        str | None,
        Query(max_length=250, description="per_user git credentials: connection to use."),
    ] = None,
) -> DeleteResult:
    user = _user(request, principal)
    # Storage lookups block (files, git, object stores): keep them off the event loop.
    unreadable = False
    try:
        owner = await run_in_threadpool(_owner, services, dag_id)
        stored = await run_in_threadpool(_find, services, dag_id)
    except StorageError as exc:
        # Configured credentials that cannot read (request_token=required without a
        # connection, private repository): only the user's token can find the DAG.
        if git_token is None:
            default_cfg = _git_cfg(services, None)
            try:
                check_required(
                    None,
                    kind=services.settings.storage.mode,
                    cfg=default_cfg,
                    root=root_settings(services),
                )
            except RequestTokenError as required:
                raise http_error(required) from None
            raise HTTPException(status_code=409, detail=mask(str(exc))) from exc
        owner, stored, unreadable = services.target_for(None), None, True
    team = stored.spec.team if stored else None
    token = accept_token(
        services, request, git_token, kind=owner.kind, cfg=_git_cfg(services, team)
    )
    resolved = await _write_target(
        services, gateway, owner, team=team, user=user, conn_id=git_conn_id, token=token
    )
    writer = resolved.target
    if unreadable:
        try:
            with scrubbed_errors(token):
                stored = await run_in_threadpool(writer.get, dag_id)
        except StorageError as exc:
            raise HTTPException(status_code=409, detail=mask(str(exc))) from exc
    context: DeployContext | None = None
    if stored is not None:
        context = _context(
            services,
            request,
            principal,
            stored.spec,
            action="delete",
            env=None,
            target=writer,
            push_mode=writer.mode if isinstance(writer, GitTarget) else None,
            etag_before=stored.etag,
            report=None,
        )
    as_service = _commit_as_service(services, team, resolved)
    kwargs: dict[str, Any] = {"base_etag": _etag(if_match), "author": user}
    if isinstance(writer, GitTarget) and context is not None:
        kwargs.update(context=context, co_author=as_service)
        if not as_service:
            kwargs["author"] = context.user.git_author() or user
    try:
        with scrubbed_errors(token):
            deleted = await run_in_threadpool(lambda: writer.delete(dag_id, **kwargs))
    except NotFoundError:
        return DeleteResult(deleted=False)
    except StorageError as exc:
        raise HTTPException(status_code=409, detail=mask(str(exc))) from exc
    if deleted:
        credential = await _credential_audit(resolved)
        await run_in_threadpool(
            lambda: services.audit.record(
                "delete",
                user=user,
                dag_id=dag_id,
                **{
                    **(context.audit() if context else {}),
                    "target": owner.kind,
                    **credential,
                },
            )
        )
    return DeleteResult(deleted=deleted)


@read_router.get(
    "/{dag_id}/audit", operation_id="getSpecAudit", dependencies=[require(P.AUDIT_READ)]
)
async def spec_audit(
    dag_id: str, services: ServicesDep, limit: Annotated[int, Query(ge=1, le=1000)] = 50
) -> list[dict[str, object]]:
    records = await run_in_threadpool(services.audit.tail, limit, dag_id)
    return [r.model_dump() for r in records]


async def _push_mode(
    request: Request,
    principal: Principal,
    services: Services,
    target: DeployTarget,
    direct_push: bool | None,
    spec: DagSpec,
) -> GitMode | None:
    """``"pr"``/``"push"`` for a git target (``None``: other targets always write live).

    The request flag never escalates: ``direct_push=true`` needs ``deploy:direct_push``
    (granted to ``direct_push_roles``); ``false`` (open a PR) is always allowed.
    """
    allowed = principal.can_direct_push(direct_push_roles(services, spec.team))
    if direct_push and not allowed:
        raise await deny(
            request,
            principal,
            P.DEPLOY_DIRECT_PUSH,
            detail="pushing directly needs the AutoAir permission 'deploy:direct_push'",
        )
    if not isinstance(target, GitTarget):
        return None
    if direct_push is not None:
        return "push" if direct_push else "pr"
    if (
        target.mode == "push"
        and not allowed
        and iam_settings(services).force_pr_without_direct_push
    ):
        return "pr"
    return target.mode


def _direct_push_base(
    target: GitTarget, dag_id: str, environment: str | None, base_etag: str
) -> str | None:
    """In PR mode the editor saw the pending pull-request version; a direct push of an
    edit of *that* version supersedes the PR, so it must not conflict with the base."""
    seen = target.get(dag_id, environment=environment)
    if seen is not None and seen.pending_branch and seen.etag == base_etag:
        return None
    return base_etag


def _review(rec: ReviewRecord) -> Review:
    return Review.model_validate(rec.model_dump())


@read_router.get(
    "/{dag_id}/reviews", operation_id="listSpecReviews", dependencies=[require(P.SPEC_READ)]
)
async def list_reviews(dag_id: str, services: ServicesDep) -> list[Review]:
    try:
        records = await run_in_threadpool(_store(services).reviews, dag_id)
    except (StorageError, ProposalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_review(r) for r in records[:200]]


@read_router.post("/{dag_id}/reviews", operation_id="reviewSpec", dependencies=[require(P.REVIEW)])
async def review_spec(
    dag_id: str,
    body: ReviewRequest,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
) -> Review:
    """Approve, request changes on, or comment a spec version: a stored version or a deploy
    proposal (by its etag). The author of a proposal cannot approve it."""
    user = _user(request, principal)
    store = _store(services)
    try:
        proposal = await run_in_threadpool(store.get, dag_id, body.etag)
    except (StorageError, ProposalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if proposal is not None and body.verdict == "approve" and (not user or proposal.author == user):
        raise await deny(
            request, principal, P.REVIEW, detail="you cannot approve your own deploy proposal"
        )
    rec = ReviewRecord(
        timestamp=now_iso(),
        user=user,
        etag=body.etag,
        verdict=body.verdict,
        comment=body.comment,
    )
    await run_in_threadpool(store.add_review, dag_id, rec)
    await run_in_threadpool(
        lambda: services.audit.record(
            "review",
            user=user,
            dag_id=dag_id,
            etag=body.etag,
            verdict=body.verdict,
            comment=body.comment,
            proposal=proposal is not None,
            proposed_by=proposal.author if proposal is not None else None,
            user_name=principal.display_name,
            user_email=principal.email,
            link=dag_link(services.settings.server.public_url, dag_id, mode=services.settings.mode),
        )
    )
    return _review(rec)


# --------------------------------------------------------------------------- proposals
def _summary_fields(store: ProposalRepository, proposal: Proposal) -> dict[str, Any]:
    import datetime

    reviews = store.reviews(proposal.dag_id, proposal.etag)
    verdicts = latest_verdicts(reviews, proposal.etag)
    approved_by = sorted(u for u, v in verdicts.items() if v == "approve" and u != proposal.author)
    try:
        expires = (
            datetime.datetime.fromisoformat(proposal.timestamp)
            + datetime.timedelta(seconds=store.ttl)
        ).isoformat()
    except ValueError:
        expires = proposal.timestamp
    return {
        "dag_id": proposal.dag_id,
        "etag": proposal.etag,
        "author": proposal.author,
        "timestamp": proposal.timestamp,
        "expires_at": expires,
        "message": proposal.message,
        "environment": proposal.environment,
        "base_etag": proposal.base_etag,
        "direct_push": proposal.direct_push,
        "approved_by": approved_by,
        "changes_requested_by": sorted(u for u, v in verdicts.items() if v == "request_changes"),
        "approved": bool(approved_by),
        "_reviews": reviews,
    }


@read_router.get(
    "/{dag_id}/proposals", operation_id="listProposals", dependencies=[require(P.SPEC_READ)]
)
async def list_proposals(dag_id: str, services: ServicesDep) -> list[ProposalSummary]:
    """Pending deploy proposals of a DAG (newest first)."""
    store = _store(services)

    def load() -> list[ProposalSummary]:
        out = []
        for proposal in store.pending(dag_id):
            fields = _summary_fields(store, proposal)
            fields.pop("_reviews")
            out.append(ProposalSummary(**fields))
        return out

    try:
        return await run_in_threadpool(load)
    except (StorageError, ProposalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _load_proposal(store: ProposalRepository, dag_id: str, etag: str) -> Proposal:
    try:
        proposal = store.get(dag_id, etag)
    except (StorageError, ProposalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"no pending proposal {etag!r} for {dag_id!r}")
    return proposal


@read_router.get(
    "/{dag_id}/proposals/{etag}", operation_id="getProposal", dependencies=[require(P.SPEC_READ)]
)
async def get_proposal(dag_id: str, etag: str, services: ServicesDep) -> ProposalDetail:
    """A deploy proposal: the proposed spec, its reviews, and the stored spec (diff base)."""
    store = _store(services)

    def load() -> ProposalDetail:
        proposal = _load_proposal(store, dag_id, etag)
        fields = _summary_fields(store, proposal)
        reviews = fields.pop("_reviews")
        current = _find(services, dag_id)
        return ProposalDetail(
            **fields,
            spec=proposal.spec,
            current_spec=current.spec if current else None,
            current_etag=current.etag if current else None,
            stale=bool(current and proposal.base_etag and current.etag != proposal.base_etag),
            reviews=[_review(r) for r in reviews],
        )

    return await run_in_threadpool(load)


@write_router.post(
    "/{dag_id}/proposals/{etag}/deploy",
    operation_id="deployProposal",
    dependencies=[require(P.DEPLOY)],
)
async def deploy_proposal(
    dag_id: str,
    etag: str,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
    gateway: Annotated[AirflowGateway, Depends(get_gateway)],
    git_token: GitTokenHeader,
    body: Annotated[ProposalDeployRequest | None, Body()] = None,
) -> DeployResult:
    """Deploy an approved proposal. Anyone holding ``deploy`` may, including its author,
    once another user approved it."""
    body = body or ProposalDeployRequest()
    store = _store(services)
    proposal = await run_in_threadpool(_load_proposal, store, dag_id, etag)
    spec = proposal.spec
    if spec.dag_id != dag_id or spec_etag(spec) != proposal.etag:
        raise HTTPException(status_code=409, detail="the stored proposal is inconsistent")
    approvers = await run_in_threadpool(
        lambda: store.approvers(dag_id, proposal.etag, exclude=proposal.author)
    )
    if not approvers:
        raise await deny(
            request,
            principal,
            P.REVIEW,
            detail="this proposal needs an approval by another user than its author",
        )
    target = services.target_for(spec.team)
    token = accept_token(
        services, request, git_token, kind=target.kind, cfg=_git_cfg(services, spec.team)
    )
    push_mode = await _push_mode(request, principal, services, target, proposal.direct_push, spec)
    result = await _deploy(
        request,
        services,
        principal,
        gateway,
        spec,
        env=_environment(services, spec, proposal.environment),
        target=target,
        push_mode=push_mode,
        base_etag=proposal.base_etag,
        message=body.message or proposal.message,
        force=body.force or proposal.force,
        direct_push=bool(proposal.direct_push and push_mode == "push"),
        audit_extra={},
        approvals=approvers,
        proposed_by=proposal.author,
        token=token,
    )
    if result.deployed:
        await run_in_threadpool(store.delete, dag_id, proposal.etag)
    return result
