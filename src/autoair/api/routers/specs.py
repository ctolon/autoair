"""Spec CRUD + deploy (validate → render → write via the deploy target → reparse).

Permissions (autoair.iam): ``spec:read`` to read, ``audit:read`` for the audit trail,
``review`` to approve a version, ``spec:write`` to save — plus ``deploy`` when the save goes
live (anything but a pull request) and ``deploy:direct_push`` to bypass a pull request.
Airflow's per-DAG read/edit access is checked by the router dependencies (``create_api``).

Four-eyes deploys (``iam.require_approval``): a deploy that would go live without another
user's approval of that exact version is stored as a *proposal* (HTTP 202) instead; it is
reviewed (``POST /specs/{dag_id}/reviews`` with the proposal's etag) and then deployed by
anyone holding ``deploy`` (``POST /specs/{dag_id}/proposals/{etag}/deploy``). Pull requests
are exempt: on git targets in PR mode the pull request is the proposal. Deletes follow the
same rules: a live delete needs ``deploy`` too, becomes a delete proposal without an
approval (executed with ``POST /specs/{dag_id}/proposals/{etag}/delete``), and follows the
target's mode and ``iam.force_pr_without_direct_push``. The rule itself is in
:mod:`autoair.iam.proposals` (``write_mode``, ``needs_approval``, ``approvers_for``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.app import Services
from autoair.api.deps import (
    EnvironmentQuery,
    ServicesDep,
    get_gateway,
    policy_of,
    read_environment,
)
from autoair.api.features import ensure_feature
from autoair.api.git_token import GitTokenHeader, accept_token, http_error, scrubbed_errors
from autoair.api.routers.ops import DEPLOYS
from autoair.api.source_plans import (
    SOURCE_CODE_DETAIL,
    changes_source_code,
    existing_file,
    plan_for,
    stored_spec,
    writes_client_code,
)
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
    ProposalAction,
    ProposalError,
    ProposalStore,
    ReviewRecord,
    SavedBy,
    VersionAuthors,
    approvers_for,
    delete_etag,
    latest_verdicts,
    may_approve,
    needs_approval,
    new_proposal,
    now_iso,
    proposal_approvers,
    write_mode,
)
from autoair.persistence.protocols import ProposalRepository
from autoair.preserve.service import SourcePlan, applicable, can_force_overwrite
from autoair.secret_mask import mask
from autoair.spec import DagSpec
from autoair.storage import (
    ConflictError,
    DeployTarget,
    EjectedError,
    HandWrittenError,
    NotFoundError,
    SpecSummary,
    StorageError,
    StoredSpec,
    spec_etag,
)
from autoair.storage.base import environment_kwargs, is_owned, sha256_text
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
log = logging.getLogger(__name__)

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
    base_etag: str | None = Field(
        default=None,
        description="Etag of the spec this edit started from (or the If-Match header). "
        "Required to overwrite an existing DAG (storage.require_base_etag); a stale one "
        "returns status 'conflict'.",
    )
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
    force: bool = Field(
        default=False,
        description="Overwrite a generated file that was changed outside AutoAir, or the "
        "hand-written file AutoAir keeps (output modes other than python and raw code; needs "
        "preserve.allow_force_overwrite and 'deploy:overwrite_ejected', otherwise ignored). "
        "Python DAG files use source_resolution instead.",
    )
    source_resolution: Literal["preserve", "overwrite"] | None = Field(
        default=None,
        description="Answer to a deploy that stopped at a DAG file changed outside AutoAir "
        "(status 'ejected'): preserve applies the change to that file (LibCST patch); "
        "overwrite regenerates it and discards the outside changes (needs "
        "preserve.allow_force_overwrite, 'deploy:overwrite_ejected' and, for a file "
        "changed outside AutoAir, preserve.ejected_files=overwrite).",
    )
    source_target_sha: str | None = Field(
        default=None,
        max_length=64,
        description="The `target_sha` of the source preview the user reviewed: the deploy "
        "stops ('source_refused') when the DAG file in the target is no longer that one "
        "('' = there was no file).",
    )
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
    hand_written: bool = Field(
        default=False,
        description="ejected_file is the hand-written DAG file AutoAir keeps for this DAG "
        "(not a generated file changed outside AutoAir).",
    )
    force_allowed: bool = Field(
        default=False,
        description="Deploying again with force=true would overwrite ejected_file for this "
        "caller (preserve.allow_force_overwrite and 'deploy:overwrite_ejected').",
    )


DeployStatus = Literal[
    "deployed", "invalid", "conflict", "ejected", "pending_approval", "source_refused"
]


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
    source: SourcePlan | None = Field(
        default=None,
        description="Python DAGs: how the DAG file was (or would be) written -- generated "
        "or the existing file patched (source policy), with the diff; 'source_refused': why "
        "the change cannot be applied to the existing file.",
    )
    stored_spec: DagSpec | None = Field(
        default=None,
        description="The spec as stored, when it differs from the one sent (a preserve "
        "deploy merged changes made to the file outside AutoAir): reload it.",
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
    action: ProposalAction = Field(
        default="deploy",
        description="'deploy' deploys the proposed spec; 'delete' deletes the DAG "
        "(POST .../proposals/{etag}/delete).",
    )
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
        description="The stored spec changed since the author's base version, or the DAG "
        "exists now and the proposal records no base version: deploying, executing or "
        "approving it is refused (409) until it is proposed again.",
    )
    reviews: list[Review] = Field(default_factory=list)


class ProposalDeployRequest(BaseModel):
    force: bool = Field(
        default=False, description="Overwrite even if the stored spec changed meanwhile."
    )
    message: str | None = Field(
        default=None, max_length=4000, description="Commit message (default: the proposal's)."
    )


DeleteStatus = Literal["deleted", "not_found", "pending_approval", "pr_opened"]


class DeleteResult(BaseModel):
    deleted: bool = Field(description="Something was deleted or a deleting pull request opened.")
    status: DeleteStatus = Field(
        default="deleted",
        description="'deleted': gone from the target; 'pr_opened': a pull request removes it "
        "(still live until merged); 'pending_approval' (HTTP 202): stored as a delete "
        "proposal that needs another user's approval (iam.require_approval); 'not_found'.",
    )
    proposal_etag: str | None = Field(
        default=None, description="Etag of the stored delete proposal (pending_approval)."
    )
    message: str | None = None


def _force_refusal(services: Services, principal: Principal) -> str | None:
    """Why a forced overwrite would not be accepted for this caller (None: it would)."""
    if not can_force_overwrite(services.settings.preserve):
        return "overwriting it is turned off here (preserve.allow_force_overwrite)"
    if not principal.has(P.DEPLOY_OVERWRITE_EJECTED):
        return "overwriting it needs the AutoAir permission 'deploy:overwrite_ejected'"
    return None


def _etag(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().removeprefix("W/").strip('"') or None


def _find(services: Services, dag_id: str, environment: str | None = None) -> StoredSpec | None:
    env = read_environment(services, environment)
    for target in services.all_targets():
        stored = target.get(dag_id, **environment_kwargs(target.get, env))
        if stored is not None:
            return stored
    return None


def _owner(services: Services, dag_id: str, environment: str | None = None) -> DeployTarget:
    env = read_environment(services, environment)
    for target in services.all_targets():
        if target.get(dag_id, **environment_kwargs(target.get, env)) is not None:
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


def record_saver(services: Any, dag_id: str, etags: list[str], user: str | None) -> None:
    """Remember who saved these versions of ``dag_id`` (four-eyes: the saver's approval of
    them never counts). A failure is logged, never fails the write that already happened."""
    authors: VersionAuthors | None = getattr(services, "version_authors", None)
    if authors is None:
        return
    try:
        authors.record(dag_id, [e for e in etags if e], user)
    except Exception:
        log.warning("cannot record who saved %s", dag_id, exc_info=True)


def _saved_by(services: Any, dag_id: str, etag: str) -> SavedBy | None:
    authors = version_authors(services)
    try:
        return authors.saved_by(dag_id, etag) if authors is not None else None
    except Exception:  # unreadable: an unknown saver (the other four-eyes rules still apply)
        log.warning("cannot read who saved %s@%s", dag_id, etag, exc_info=True)
        return None


def version_authors(services: Any) -> VersionAuthors | None:
    authors: VersionAuthors | None = getattr(services, "version_authors", None)
    return authors


def _user(request: Request, principal: Principal) -> str | None:
    return principal.user or request_user(request)


@read_router.get("", operation_id="listSpecs", dependencies=[require(P.SPEC_READ)])
async def list_specs(
    services: ServicesDep, environment: EnvironmentQuery = None
) -> list[SpecSummary]:
    env = read_environment(services, environment)

    def merged() -> list[SpecSummary]:
        seen: dict[str, SpecSummary] = {}
        for target in services.all_targets():
            for summary in target.list(**environment_kwargs(target.list, env)):
                seen.setdefault(summary.dag_id, summary)
        return sorted(seen.values(), key=lambda s: s.dag_id)

    return await run_in_threadpool(merged)


@read_router.get("/{dag_id}", operation_id="getSpec", dependencies=[require(P.SPEC_READ)])
async def get_spec(
    dag_id: str, services: ServicesDep, response: Response, environment: EnvironmentQuery = None
) -> StoredSpecResponse:
    try:
        stored = await run_in_threadpool(_find, services, dag_id, environment)
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
    source: str | None = None,
) -> ValidationReport:
    """The validation every write runs (deploy, promotion): static checks, policy rules and
    OPA for ``env``, plus the import check when enabled and allowed. ``source``: the file a
    source-preserving deploy writes (checked instead of the generated one). Blocking."""
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
        source=source,
    )
    if skipped is not None:
        report.problems.append(skipped)
    return report


async def _validate(
    services: Services,
    principal: Principal,
    effective: DagSpec,
    env: str | None = None,
    *,
    source: str | None = None,
) -> ValidationReport:
    return await run_in_threadpool(
        lambda: validate_for_write(services, principal, effective, env, source=source)
    )


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


async def check_source_code_write(
    request: Request,
    principal: Principal,
    spec: DagSpec,
    target: DeployTarget,
    env: str | None,
) -> None:
    """Storing new or changed hand-written ``source.code`` needs ``code:raw``, whatever the
    source policy: a later deploy (by anyone) may write that code as the DAG file, so the
    check cannot wait for the deploy that does."""
    if spec.source is None or spec.source.code is None or principal.has(P.CODE_RAW):
        return
    stored = await run_in_threadpool(stored_spec, target, spec.dag_id, env)
    if changes_source_code(spec, stored):
        await check(request, principal, P.CODE_RAW, detail=SOURCE_CODE_DETAIL)


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
        },
        428: {
            "description": "The DAG already exists and the request carries no base_etag / "
            "If-Match (storage.require_base_etag); the ETag header holds the current etag."
        },
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
    await check_source_code_write(request, principal, spec, target, env)
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
        if needs_approval(iam_settings(services), push_mode):
            approvers = await run_in_threadpool(
                lambda: approvers_for(
                    _store(services),
                    dag_id,
                    etag,
                    actor=user,
                    authors=version_authors(services),
                )
            )
            if not approvers:
                await _require_base_etag(services, target, dag_id, env, base_etag)
                return await _propose(
                    request, response, services, principal, spec, etag, env, body, base_etag
                )
        await check(request, principal, P.DEPLOY)
    await _require_base_etag(services, target, dag_id, env, base_etag)
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
        resolution=body.source_resolution,
        reviewed_target_sha=body.source_target_sha,
    )
    if result.deployed and approvers:  # an identical pending proposal is now deployed
        await run_in_threadpool(_store(services).delete, dag_id, etag)
    return result


async def _require_base_etag(
    services: Services, target: DeployTarget, dag_id: str, env: str | None, base_etag: str | None
) -> None:
    """428 when the write would overwrite an existing spec without the etag it was loaded
    with: a blind overwrite silently drops a concurrent edit (storage.require_base_etag)."""
    if base_etag or not services.settings.storage.require_base_etag:
        return
    stored = await run_in_threadpool(_stored_etag, target, dag_id, env)
    if stored is not None:
        raise HTTPException(
            status_code=428,
            detail=f"DAG {dag_id!r} already exists: send the etag it was loaded with "
            f"(base_etag or If-Match); its current etag is {stored}",
            headers={"ETag": f'"{stored}"'},
        )


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
    if not principal.has(P.CODE_RAW):
        target = services.target_for(spec.team)
        if await run_in_threadpool(writes_client_code, services, spec, target, env):
            DEPLOYS.labels("source_refused").inc()
            return DeployResult(
                deployed=False,
                status="source_refused",
                report=ValidationReport(ok=True, problems=[]),
                message="this proposal would write the DAG's hand-written Python file "
                "(imported or edited in the code mode), which needs the AutoAir permission "
                "'code:raw' (for example the CodeDeployer role); switch the source policy to "
                "'generate' to propose the generated file instead",
            )
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


# Appended to the deploy message when the file was written but Airflow did not take the
# reparse request: the DAG processor still finds the file on its next bundle refresh.
_NEXT_REFRESH = "Airflow will pick it up on the next bundle refresh"


async def _request_reparse(gateway: AirflowGateway, dag_id: str) -> tuple[bool, str]:
    """Ask Airflow to reparse the deployed DAG's file: (requested, note for the message).

    A refused reparse never fails the deploy (the file is already written). Airflow answers
    403 when the caller may not edit the DAG and, from 3.4, for a file that has no registered
    DAG (an import error) unless the caller has the ``REPARSE_ALL`` view (admins by default);
    3.0-3.3 answer 404 for such a file.
    """
    try:
        status = await gateway.dag_status(dag_id)
    except AirflowError as exc:
        return False, f"; could not contact Airflow ({mask(exc.detail)})"
    if not (status.exists and status.file_token):
        return False, "; Airflow will pick the new DAG up on the next bundle refresh"
    try:
        await gateway.reparse(status.file_token)
    except AirflowError as exc:
        detail = mask(exc.detail)
        if exc.status == 403:
            return False, (
                f"; Airflow refused the reparse request (403: {detail}): reparsing needs edit "
                "permission on the DAG, and on Airflow 3.4+ the REPARSE_ALL permission for a "
                f"file without a registered DAG; {_NEXT_REFRESH}"
            )
        if exc.status == 409:
            # A parsing request for the file is still queued (Airflow keeps one per file and
            # answers a second one with a unique-constraint 409): the new content is parsed.
            return True, "; reparse already requested"
        if exc.status == 404:
            return False, f"; Airflow does not know the DAG file yet (404); {_NEXT_REFRESH}"
        return False, f"; the reparse request failed ({exc.status}: {detail}); {_NEXT_REFRESH}"
    return True, "; reparse requested"


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


def _stored_is_raw(target: DeployTarget, dag_id: str, env: str | None) -> bool:
    """The stored spec is a raw code DAG (unreadable: no)."""
    try:
        if isinstance(target, GitTarget):
            stored = target.get(dag_id, environment=env)
        else:
            stored = target.get(dag_id)
    except Exception:  # unreadable/unreachable: treated as not raw, the safe answer
        return False
    return stored is not None and stored.spec.raw_code is not None


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
    resolution: Literal["preserve", "overwrite"] | None = None,
    reviewed_target_sha: str | None = None,
) -> DeployResult:
    """Validate, render and write ``spec`` (authorization already checked). Every write
    carries the deploy context (autoair.governance): commit/PR text and the audit record.
    ``token``: the git token typed for this request (used for this write only).

    Python DAGs follow their source policy (:mod:`autoair.preserve`): the file is generated,
    or the existing file is patched; a file changed outside AutoAir is never overwritten
    silently (``resolution`` answers a deploy that stopped there)."""
    dag_id = spec.dag_id
    effective = spec.with_environment(env) if env and env in spec.environments else spec
    user = _user(request, principal)
    plan: SourcePlan | None = None
    resolved: ResolvedTarget | None = None
    if applicable(spec):
        resolved = await _write_target(
            services, gateway, target, team=spec.team, user=user, conn_id=git_conn_id, token=token
        )
        try:
            with scrubbed_errors(token), span("source_plan", dag_id=dag_id):
                plan = await run_in_threadpool(
                    lambda: plan_for(
                        services,
                        principal,
                        spec,
                        target=resolved.target if resolved else target,
                        environment=env,
                        push_mode=push_mode,
                        resolution=resolution,
                    )
                )
        except StorageError as exc:
            raise HTTPException(status_code=409, detail=mask(str(exc))) from exc
        if reviewed_target_sha is not None and plan.target_sha != reviewed_target_sha:
            plan.action = "refused"
            plan.source = None
            plan.options = []
            plan.message = (
                "the DAG file in the target changed since you previewed this deploy: preview "
                "it again"
            )
        blocked = _source_stop(plan)
        if blocked is not None:
            return blocked
    preserved = plan.source if plan is not None and plan.action != "generate" else None
    report = await _validate(services, principal, effective, env, source=preserved)
    if not report.ok:
        DEPLOYS.labels("invalid").inc()
        return DeployResult(
            deployed=False,
            status="invalid",
            report=report,
            source=plan,
            message="validation failed; nothing was written",
        )
    if resolved is None:
        resolved = await _write_target(
            services, gateway, target, team=spec.team, user=user, conn_id=git_conn_id, token=token
        )
    writer = resolved.target
    to_store = plan.stored_spec if plan is not None and plan.stored_spec is not None else spec
    may_force = can_force_overwrite(services.settings.preserve) and principal.has(
        P.DEPLOY_OVERWRITE_EJECTED
    )
    if plan is None and force and not may_force:
        # An overwrite of an ejected or kept hand-written file needs the switch and the
        # permission; without them the deploy stops at that file (EjectedError).
        force = False
    takeover: str | None = None
    if spec.raw_code is not None:
        # A hand-written file kept as a raw code DAG ("keep as raw code" after a refused
        # patch): its own content may replace it even though AutoAir did not create it.
        current = await run_in_threadpool(existing_file, writer, dag_id, env, push_mode)
        if current is not None and current.content.rstrip("\n") == spec.raw_code.rstrip("\n"):
            takeover = sha256_text(current.content)
    raw_edit = (
        spec.raw_code is not None
        and takeover is None
        and await run_in_threadpool(_stored_is_raw, target, dag_id, env)
    )
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
        if plan is not None and plan.source is not None and spec.output_mode == "python":
            # The plan already rendered (or patched) the file: write exactly that.
            files = {f"{dag_id}.py": plan.source}
        else:
            with span("render", dag_id=dag_id, output_mode=spec.output_mode):
                files = await run_in_threadpool(services.render_files, spec, env)
        save_args: dict[str, Any] = {
            "base_etag": base_etag,
            "author": user,
            "message": message,
            "environment": env,
            "force": force if plan is None else False,
        }
        if plan is not None and plan.expected_sha is not None:
            # Write over the file the plan was made from, only while it is unchanged.
            save_args["expected"] = {f"{dag_id}.py": plan.expected_sha}
        elif takeover is not None:
            save_args["expected"] = {f"{dag_id}.py": takeover}
            save_args["hand_written"] = True  # deleting it later needs the permission
        if plan is not None and (plan.hand_written or plan.overwrite):
            save_args["hand_written"] = plan.hand_written
        if plan is None and (force or raw_edit):
            # Without a source plan (loader/YAML output modes, raw code) the target refuses
            # to replace a kept hand-written file unless this caller may: force is only
            # still set with deploy:overwrite_ejected; a raw code DAG that already was one
            # replaces its own code, which the editor showed in full.
            save_args["allow_hand_written"] = True
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
            result = await run_in_threadpool(lambda: writer.save(to_store, files, **save_args))
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
        detail = (
            "the file changed while the deploy was prepared; preview the change again"
            if plan is not None
            else _force_refusal(services, principal)
            or "deploy with force to overwrite the manual changes"
        )
        return DeployResult(
            deployed=False,
            status="ejected",
            report=report,
            source=plan,
            message=mask(f"{exc.path} was modified outside AutoAir (ejected); {detail}"),
            conflict=ConflictInfo(
                ejected_file=str(exc.path), force_allowed=plan is None and may_force
            ),
        )
    except HandWrittenError as exc:
        # The hand-written file AutoAir keeps (a deploy without a source plan: another
        # output mode, a conversion to raw code): reported like an ejected file, so the UI
        # offers the overwrite only where it would be accepted.
        DEPLOYS.labels("ejected").inc()
        # Force never replaces a file without AutoAir's marker (check_overwrite): such a
        # kept file changes only through a source plan (python output) or as raw code.
        kept_file = await run_in_threadpool(existing_file, writer, dag_id, env, push_mode)
        owned = kept_file is not None and is_owned(kept_file.content)
        detail = (
            _force_refusal(services, principal)
            if owned
            else "AutoAir never overwrites a file it did not create; change it with the "
            "python output mode (preserve) or keep the DAG as raw code"
        ) or "deploy with force to overwrite it"
        return DeployResult(
            deployed=False,
            status="ejected",
            report=report,
            source=plan,
            message=mask(
                f"{exc.path} is the hand-written DAG file AutoAir keeps for this DAG; "
                f"replacing it discards it: {detail}"
            ),
            conflict=ConflictInfo(
                ejected_file=str(exc.path), hand_written=True, force_allowed=may_force and owned
            ),
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
    if plan is not None:
        details["source_policy"] = plan.policy
        details["source_action"] = plan.action
        if plan.overwrite:
            details["overwrote_ejected"] = True
    await run_in_threadpool(
        lambda: services.audit.record("deploy", user=user, dag_id=dag_id, **details)
    )
    # The author of the version is whoever wrote it: the proposal's author when an approved
    # proposal is deployed, else the caller.
    await run_in_threadpool(
        record_saver,
        services,
        dag_id,
        [spec_etag(spec), result.etag],
        proposed_by if proposed_by is not None else user,
    )
    reparsed = False
    msg = result.message or f"written to {result.path}"
    if target.kind == "local" and not result.pr_url and not services.settings.feature("airflow"):
        msg += "; Airflow will pick it up on the next bundle refresh"  # no Airflow server
    elif target.kind == "local" and not result.pr_url:
        reparsed, note = await _request_reparse(gateway, dag_id)
        msg += note
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
        source=plan,
        stored_spec=_returned_spec(to_store, spec, plan),
        message=msg,
    )


def _returned_spec(stored: DagSpec, sent: DagSpec, plan: SourcePlan | None) -> DagSpec | None:
    """DeployResult.stored_spec: the spec stored instead of the one sent, except for a file
    the caller may not see (its merge result would show what the file contains)."""
    if stored is sent or (plan is not None and plan.content_hidden):
        return None
    return stored


def _source_stop(plan: SourcePlan) -> DeployResult | None:
    """The deploy result when the source plan stops the deploy (an ejected file, a refused
    patch); None when it goes ahead."""
    empty = ValidationReport(ok=True, problems=[])
    if plan.action == "blocked":
        DEPLOYS.labels("ejected").inc()
        return DeployResult(
            deployed=False,
            status="ejected",
            report=empty,
            source=plan,
            message=mask(plan.message),
            conflict=ConflictInfo(ejected_file=plan.target_file),
        )
    if plan.action == "refused":
        DEPLOYS.labels("source_refused").inc()
        return DeployResult(
            deployed=False,
            status="source_refused",
            report=empty,
            source=plan,
            message=mask(plan.message),
        )
    return None


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


class SourcePreviewRequest(BaseModel):
    spec: DagSpec
    environment: str | None = None
    direct_push: bool | None = Field(
        default=None, description="As in the deploy request (decides the git branch read)."
    )
    source_resolution: Literal["preserve", "overwrite"] | None = Field(
        default=None,
        description="Preview the answer to an ejected file: preserve (patch it) or overwrite.",
    )


@write_router.post(
    "/{dag_id}/source-preview",
    operation_id="previewSource",
    dependencies=[require(P.SPEC_WRITE)],
)
async def preview_source(
    dag_id: str,
    body: SourcePreviewRequest,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
    gateway: Annotated[AirflowGateway, Depends(get_gateway)],
    git_token: GitTokenHeader,
) -> SourcePlan:
    """What a deploy of ``body.spec`` would write as the DAG file, without writing: the
    source policy in force, whether the file in the target was changed outside AutoAir, and
    the unified diff of the generated or patched file. Preserve plans import the existing
    and the patched file in a sandboxed subprocess (round-trip check), so this can take a
    few seconds."""
    spec = body.spec
    if spec.dag_id != dag_id:
        raise HTTPException(status_code=400, detail="dag_id in path and body differ")
    env = _environment(services, spec, body.environment)
    target = services.target_for(spec.team)
    token = accept_token(
        services, request, git_token, kind=target.kind, cfg=_git_cfg(services, spec.team)
    )
    push_mode = await _push_mode(request, principal, services, target, body.direct_push, spec)
    if not applicable(spec):
        return SourcePlan(
            applicable=False,
            action="not_applicable",
            message="the source policy applies to Python DAG files only",
        )
    resolved = await _write_target(
        services,
        gateway,
        target,
        team=spec.team,
        user=_user(request, principal),
        conn_id=None,
        token=token,
    )
    try:
        with scrubbed_errors(token):
            plan: SourcePlan = await run_in_threadpool(
                lambda: plan_for(
                    services,
                    principal,
                    spec,
                    target=resolved.target,
                    environment=env,
                    push_mode=push_mode,
                    resolution=body.source_resolution,
                )
            )
    except StorageError as exc:
        raise HTTPException(status_code=409, detail=mask(str(exc))) from exc
    if not plan.content_hidden:
        plan.content = plan.source
    return plan


@write_router.delete(
    "/{dag_id}",
    operation_id="deleteSpec",
    dependencies=[require(P.SPEC_DELETE)],
    responses={
        202: {
            "model": DeleteResult,
            "description": "Stored as a delete proposal awaiting approval (status "
            "'pending_approval'); nothing was deleted.",
        },
        409: {"description": "The If-Match etag is stale: the DAG changed since it was loaded."},
        428: {
            "description": "The DAG exists and the request carries no If-Match "
            "(storage.require_base_etag); the ETag header holds the current etag."
        },
    },
)
async def delete_spec(
    dag_id: str,
    request: Request,
    response: Response,
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
    """Delete a DAG (spec and DAG files). Governed like a deploy: on git targets it follows
    the target's mode and iam.force_pr_without_direct_push (a pull request removes the
    file), a delete that goes live needs 'deploy', and with iam.require_approval it needs
    another user's approval first (else it is stored as a delete proposal, HTTP 202)."""
    return await _delete_dag(
        request,
        response,
        services,
        principal,
        gateway,
        dag_id,
        git_token=git_token,
        base_etag=_etag(if_match),
        git_conn_id=git_conn_id,
    )


async def _delete_dag(
    request: Request,
    response: Response,
    services: Services,
    principal: Principal,
    gateway: AirflowGateway,
    dag_id: str,
    *,
    git_token: str | None,
    base_etag: str | None,
    git_conn_id: str | None,
    proposal: Proposal | None = None,
) -> DeleteResult:
    """Delete ``dag_id`` under the deploy governance; ``proposal``: executing that approved
    delete proposal."""
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
    iam = iam_settings(services)
    push_mode = write_mode(
        writer.mode if isinstance(writer, GitTarget) else None,
        direct_push=None,
        allowed=principal.can_direct_push(direct_push_roles(services, team)),
        iam=iam,
    )
    approvers: list[str] = []
    proposed_by: str | None = None
    key: str | None = None
    if push_mode != "pr":  # goes live: not a pull request
        if stored is not None and needs_approval(iam, push_mode):
            store = _store(services)
            key = wanted = delete_etag(stored.spec)
            if proposal is not None:  # executing it: anyone holding the permissions may
                if proposal.etag != key:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{dag_id!r} changed since this delete was proposed; propose "
                        "deleting the current version",
                    )
                proposed_by = proposal.author
                approvers = await run_in_threadpool(
                    proposal_approvers, store, proposal, version_authors(services)
                )
            else:
                pending = await run_in_threadpool(store.get, dag_id, key)
                proposed_by = pending.author if pending is not None else None
                approvers = await run_in_threadpool(
                    lambda: approvers_for(
                        store, dag_id, wanted, actor=user, authors=version_authors(services)
                    )
                )
            if not approvers:
                if proposal is not None:
                    raise await deny(
                        request,
                        principal,
                        P.REVIEW,
                        detail="this proposal needs an approval by another user than its author",
                    )
                _require_delete_etag(services, dag_id, stored.etag, base_etag)
                return await _propose_delete(
                    request, response, services, principal, stored, key, writer
                )
        await check(request, principal, P.DEPLOY)
    if proposal is None and stored is not None:
        _require_delete_etag(services, dag_id, stored.etag, base_etag)
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
            push_mode=push_mode,
            etag_before=stored.etag,
            report=None,
            approvals=approvers,
            proposed_by=proposed_by,
        )
    as_service = _commit_as_service(services, team, resolved)
    kwargs: dict[str, Any] = {"base_etag": base_etag, "author": user}
    if principal.has(P.DEPLOY_OVERWRITE_EJECTED):
        kwargs["allow_hand_written"] = True  # a hand-written file AutoAir keeps
    if isinstance(writer, GitTarget):
        kwargs["mode"] = push_mode
        if context is not None:
            kwargs.update(context=context, co_author=as_service)
            if not as_service:
                kwargs["author"] = context.user.git_author() or user
    try:
        with scrubbed_errors(token):
            deleted = await run_in_threadpool(lambda: writer.delete(dag_id, **kwargs))
    except NotFoundError:
        return DeleteResult(deleted=False, status="not_found")
    except StorageError as exc:
        raise HTTPException(status_code=409, detail=mask(str(exc))) from exc
    if not deleted:
        return DeleteResult(deleted=False, status="not_found")
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
    done = proposal.etag if proposal is not None else (key if approvers else None)
    if done is not None:  # the (approved) delete proposal is executed
        await run_in_threadpool(_store(services).delete, dag_id, done)
    if push_mode == "pr":
        return DeleteResult(
            deleted=True,
            status="pr_opened",
            message="a pull request removes the DAG; it stays live until the pull request "
            "is merged",
        )
    return DeleteResult(deleted=True)


def _require_delete_etag(
    services: Services, dag_id: str, stored_etag: str, base_etag: str | None
) -> None:
    """A delete must name the version it removes, like an overwrite: 428 without one
    (storage.require_base_etag), 409 for a stale one, so a concurrent edit is never
    deleted unseen. The target checks the etag again under its lock."""
    if base_etag is None:
        if services.settings.storage.require_base_etag:
            raise HTTPException(
                status_code=428,
                detail=f"deleting {dag_id!r} needs the etag it was loaded with (If-Match); "
                f"its current etag is {stored_etag}",
                headers={"ETag": f'"{stored_etag}"'},
            )
        return
    if base_etag != stored_etag:
        raise HTTPException(
            status_code=409,
            detail=f"{dag_id!r} was changed since it was loaded (current etag {stored_etag}): "
            "reload it before deleting",
            headers={"ETag": f'"{stored_etag}"'},
        )


async def _propose_delete(
    request: Request,
    response: Response,
    services: Services,
    principal: Principal,
    stored: StoredSpec,
    key: str,
    target: DeployTarget,
) -> DeleteResult:
    """Store a delete proposal of ``stored`` (four-eyes): nothing is deleted."""
    dag_id = stored.spec.dag_id
    user = _user(request, principal)
    store = _store(services)
    response.status_code = 202
    existing = await run_in_threadpool(store.get, dag_id, key)
    if existing is not None:  # already proposed: keep its author
        return DeleteResult(
            deleted=False,
            status="pending_approval",
            proposal_etag=key,
            message=f"deleting this DAG is already proposed (by {existing.author or 'unknown'}) "
            "and awaits an approval by another reviewer",
        )
    proposal = new_proposal(
        stored.spec,
        key,
        author=user,
        message=None,
        base_etag=stored.etag,
        environment=None,
        force=False,
        direct_push=None,
        action="delete",
    )
    await run_in_threadpool(store.save, proposal)
    context = _context(
        services,
        request,
        principal,
        stored.spec,
        action="delete",
        env=None,
        target=target,
        push_mode=None,
        etag_before=stored.etag,
        report=None,
    )
    await run_in_threadpool(
        lambda: services.audit.record(
            "propose",
            user=user,
            dag_id=dag_id,
            **{**context.audit(), "etag": key, "base_etag": stored.etag, "proposal": "delete"},
        )
    )
    return DeleteResult(
        deleted=False,
        status="pending_approval",
        proposal_etag=key,
        message="deleting this DAG needs an approval by another reviewer: stored as a delete "
        "proposal; once approved, anyone holding 'deploy' and 'spec:delete' can execute it",
    )


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
    return write_mode(
        target.mode, direct_push=direct_push, allowed=allowed, iam=iam_settings(services)
    )


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
    saved_by = (
        await run_in_threadpool(_saved_by, services, dag_id, body.etag)
        if body.verdict == "approve" and iam_settings(services).require_approval
        else None
    )
    if body.verdict == "approve" and not may_approve(user, proposal, saved_by):
        if saved_by is not None and saved_by.user and saved_by.user == user:
            detail = (
                "you cannot approve a version you saved yourself (iam.require_approval): "
                "another user must review it"
            )
        elif saved_by is not None:
            detail = (
                "this version was saved or is reviewed without a user name, so four-eyes "
                "cannot tell the reviewer from its author: sign in, and save it again signed in"
            )
        else:
            detail = "you cannot approve your own deploy proposal"
        raise await deny(request, principal, P.REVIEW, detail=detail)
    if body.verdict == "approve" and proposal is not None:
        # Approving a proposal the stored spec moved past would endorse an overwrite of a
        # version the reviewer never saw.
        await _refuse_stale_proposal(services, proposal)
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
def _summary_fields(
    store: ProposalRepository, proposal: Proposal, authors: VersionAuthors | None = None
) -> dict[str, Any]:
    import datetime

    reviews = store.reviews(proposal.dag_id, proposal.etag)
    verdicts = latest_verdicts(reviews, proposal.etag)
    approved_by = proposal_approvers(store, proposal, authors)
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
        "action": proposal.action,
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
            fields = _summary_fields(store, proposal, version_authors(services))
            fields.pop("_reviews")
            out.append(ProposalSummary(**fields))
        return out

    try:
        return await run_in_threadpool(load)
    except (StorageError, ProposalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _stale_reason(proposal: Proposal, current_etag: str | None) -> str | None:
    """Why ``proposal`` no longer applies to the stored spec (``current_etag``), else None.

    A proposal records the version its author edited (``base_etag``); deploying it over any
    other version would silently drop that change. A proposal without one was made for a
    DAG that did not exist yet, or before proposals recorded it: once the DAG exists it
    cannot be told apart from a blind overwrite, so it must be proposed again."""
    if current_etag is None:
        return None  # nothing stored (yet): the target refuses a stale write itself
    what = "deleting this DAG" if proposal.action == "delete" else "this version"
    if proposal.base_etag is None:
        return (
            f"{proposal.dag_id!r} exists now, but the proposal does not record which version "
            f"it was made from (a proposal for a new DAG, or one stored by an older AutoAir): "
            f"propose {what} again from the current version"
        )
    if proposal.base_etag != current_etag:
        return (
            f"{proposal.dag_id!r} changed since this proposal was made (proposed from "
            f"{proposal.base_etag}, stored now {current_etag}): propose {what} again from the "
            "current version"
        )
    return None


async def _refuse_stale_proposal(services: Services, proposal: Proposal) -> None:
    """409 when ``proposal`` was made from another version than the one stored now."""
    if proposal.action == "delete":  # looked up like the delete route does
        stored = await run_in_threadpool(_find, services, proposal.dag_id)
        current = stored.etag if stored is not None else None
    else:
        target = services.target_for(proposal.spec.team)
        env = proposal.environment or services.settings.default_environment
        current = await run_in_threadpool(_stored_etag, target, proposal.dag_id, env)
    reason = _stale_reason(proposal, current)
    if reason is not None:
        raise HTTPException(status_code=409, detail=reason)


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
        fields = _summary_fields(store, proposal, version_authors(services))
        reviews = fields.pop("_reviews")
        current = _find(services, dag_id)
        return ProposalDetail(
            **fields,
            spec=proposal.spec,
            current_spec=current.spec if current else None,
            current_etag=current.etag if current else None,
            stale=_stale_reason(proposal, current.etag if current else None) is not None,
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
    if proposal.action == "delete":
        raise HTTPException(
            status_code=409,
            detail="this proposal deletes the DAG: execute it with "
            f"POST /specs/{dag_id}/proposals/{etag}/delete",
        )
    if spec.dag_id != dag_id or spec_etag(spec) != proposal.etag:
        raise HTTPException(status_code=409, detail="the stored proposal is inconsistent")
    await _refuse_stale_proposal(services, proposal)
    approvers = await run_in_threadpool(
        proposal_approvers, store, proposal, version_authors(services)
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


@write_router.post(
    "/{dag_id}/proposals/{etag}/delete",
    operation_id="executeDeleteProposal",
    dependencies=[require(P.SPEC_DELETE, P.DEPLOY)],
)
async def execute_delete_proposal(
    dag_id: str,
    etag: str,
    request: Request,
    response: Response,
    services: ServicesDep,
    principal: PrincipalDep,
    gateway: Annotated[AirflowGateway, Depends(get_gateway)],
    git_token: GitTokenHeader,
    git_conn_id: Annotated[
        str | None,
        Query(max_length=250, description="per_user git credentials: connection to use."),
    ] = None,
) -> DeleteResult:
    """Execute an approved delete proposal. Anyone holding ``spec:delete`` and ``deploy``
    may, including its author, once another user approved it; the delete conflicts (409)
    when the DAG changed since it was proposed."""
    store = _store(services)
    proposal = await run_in_threadpool(_load_proposal, store, dag_id, etag)
    if proposal.action != "delete":
        raise HTTPException(
            status_code=409,
            detail="this proposal deploys a spec: deploy it with "
            f"POST /specs/{dag_id}/proposals/{etag}/deploy",
        )
    await _refuse_stale_proposal(services, proposal)
    return await _delete_dag(
        request,
        response,
        services,
        principal,
        gateway,
        dag_id,
        git_token=git_token,
        base_etag=proposal.base_etag,
        git_conn_id=git_conn_id,
        proposal=proposal,
    )
