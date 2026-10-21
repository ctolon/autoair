"""Version control: storage status, history, diff, three-way merge and promotion."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.deps import ServicesDep, get_gateway
from autoair.api.git_token import GitTokenHeader, accept_token, scrubbed_errors
from autoair.governance import DeployContext, ValidationSummary
from autoair.merge import MergeResult, SpecDiff, diff_specs, merge_specs
from autoair.secret_mask import mask
from autoair.spec import DagSpec
from autoair.storage import (
    ConflictError,
    DeployTarget,
    NotFoundError,
    SaveResult,
    StorageError,
    TargetStatus,
    VersionInfo,
    render_dag_files,
)
from autoair.validation import ValidationReport, validate_spec

read_router = APIRouter(prefix="/vcs", tags=["vcs"])
write_router = APIRouter(prefix="/vcs", tags=["vcs"])

CURRENT = "current"


class DiffRequest(BaseModel):
    a: DagSpec
    b: DagSpec


class MergeRequest(BaseModel):
    base: DagSpec
    ours: DagSpec
    theirs: DagSpec


class PromoteRequest(BaseModel):
    from_env: str
    to_env: str
    git_conn_id: str | None = Field(
        default=None,
        max_length=250,
        description="per_user git credentials: the Airflow connection to commit with.",
    )


def _target(services: Any) -> DeployTarget:
    target: DeployTarget | None = getattr(services, "target", None)
    if target is None:
        raise HTTPException(status_code=501, detail="no deploy target is configured")
    return target


def storage_http_error(exc: StorageError) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=mask(str(exc)))
    if isinstance(exc, ConflictError):
        detail: dict[str, Any] = {"message": mask(str(exc))}
        if exc.current is not None:
            detail["current_etag"] = exc.current.etag
            detail["current_version"] = exc.current.version
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=400, detail=mask(str(exc)))


async def _call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    except StorageError as exc:
        raise storage_http_error(exc) from exc


async def _spec_at(target: DeployTarget, dag_id: str, version: str) -> DagSpec:
    if version == CURRENT:
        stored = await _call(target.get, dag_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"no AutoAir spec for {dag_id!r}")
        spec: DagSpec = stored.spec
        return spec
    found: DagSpec = await _call(target.get_version, dag_id, version)
    return found


@read_router.get("/status", operation_id="getStorageStatus")
async def get_storage_status(services: ServicesDep) -> TargetStatus:
    status: TargetStatus = await _call(_target(services).status)
    return status


@read_router.get("/{dag_id}/history", operation_id="listVersions")
async def list_versions(
    dag_id: str, services: ServicesDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[VersionInfo]:
    versions: list[VersionInfo] = await _call(_target(services).history, dag_id, limit)
    return versions


@read_router.get("/{dag_id}/versions/{version}", operation_id="getVersion")
async def get_version(dag_id: str, version: str, services: ServicesDep) -> DagSpec:
    return await _spec_at(_target(services), dag_id, version)


@read_router.get("/{dag_id}/diff", operation_id="diffWithVersion")
async def diff_with_version(
    dag_id: str,
    services: ServicesDep,
    from_version: Annotated[str, Query(alias="from", description="Version id or 'current'.")],
    to_version: Annotated[
        str, Query(alias="to", description="Version id or 'current' (default).")
    ] = CURRENT,
) -> SpecDiff:
    target = _target(services)
    a = await _spec_at(target, dag_id, from_version)
    b = await _spec_at(target, dag_id, to_version)
    return diff_specs(a, b)


@read_router.post("/diff", operation_id="diffVersions")
def diff_versions(body: DiffRequest) -> SpecDiff:
    return diff_specs(body.a, body.b)


@read_router.post("/merge", operation_id="mergeSpecs")
def merge(body: MergeRequest) -> MergeResult:
    return merge_specs(body.base, body.ours, body.theirs)


@write_router.post("/{dag_id}/promote", operation_id="promoteEnvironment")
async def promote_environment(
    dag_id: str,
    body: PromoteRequest,
    request: Request,
    services: ServicesDep,
    git_token: GitTokenHeader,
) -> SaveResult:
    """Open a promotion pull request. With AutoAir's services (not in a bare router) the
    commit/PR carry the deploy context, the git token typed for this request (header
    X-AutoAir-Git-Token, storage.git.request_token) or the user's own git credentials
    (per_user mode) are used, and the promotion is audited (autoair.governance)."""
    from autoair.api.routers import specs as specs_router
    from autoair.iam.deps import STATE_ATTR
    from autoair.iam.model import Principal

    target = _target(services)
    if getattr(target, "promote", None) is None:
        raise HTTPException(
            status_code=400, detail=f"the {target.kind!r} deploy target does not support promotion"
        )
    custom = getattr(services, "render_files", None)
    principal: Principal | None = getattr(request.state, STATE_ATTR, None)
    full = principal is not None and getattr(services, "settings", None) is not None

    def validate(spec: DagSpec, environment: str | None) -> ValidationReport:
        """Deploy's validation for the target environment (policy rules and OPA included);
        a bare router (no AutoAir services) only has the static checks."""
        if environment and spec.environments and environment not in spec.environments:
            raise _Rejected(400, f"unknown environment {environment!r}")
        effective = (
            spec.with_environment(environment)
            if environment and environment in spec.environments
            else spec
        )
        if full:
            assert principal is not None
            return specs_router.validate_for_write(
                services, principal, effective, environment, action="promote"
            )
        return validate_spec(
            effective,
            catalog=getattr(services, "catalog", None),
            registry=getattr(services, "registry", None),
            environment=environment,
            context={"action": "promote"},
        )

    def render(spec: DagSpec, environment: str | None) -> dict[str, str]:
        # Called by the target with the spec it promotes, before anything is written.
        report = validate(spec, environment)
        if not report.ok:
            raise _Rejected(422, _invalid_detail(report, environment))
        if context is not None:
            context.validation = ValidationSummary.of(report)
        if custom is not None:
            files: dict[str, str] = custom(spec, environment)
            return files
        return render_dag_files(
            spec, environment=environment, registry=getattr(services, "registry", None)
        )

    context: DeployContext | None = None
    if not full:
        if git_token is not None:
            raise HTTPException(
                status_code=400, detail="this router does not accept X-AutoAir-Git-Token"
            )
        result: SaveResult = await _promote(
            target.promote,  # type: ignore[attr-defined]
            dag_id,
            body.from_env,
            body.to_env,
            render=render,
        )
        return result
    assert principal is not None

    user = specs_router._user(request, principal)
    token = accept_token(
        services, request, git_token, kind=target.kind, cfg=specs_router._git_cfg(services, None)
    )
    gateways = get_gateway(request, services)
    gateway = await anext(gateways)
    try:
        resolved = await specs_router._write_target(
            services, gateway, target, team=None, user=user, conn_id=body.git_conn_id, token=token
        )
    finally:
        await gateways.aclose()  # type: ignore[attr-defined]
    writer = resolved.target
    kwargs: dict[str, Any] = {"render": render, "author": user}
    stored = await run_in_threadpool(_get, target, dag_id, body.from_env)
    if stored is not None:
        context = specs_router._context(
            services,
            request,
            principal,
            stored.spec,
            action="promote",
            env=body.to_env,
            target=writer,
            push_mode="pr",
            etag_before=stored.etag,
            report=None,
        )
        context.extra["From environment"] = body.from_env
        as_service = specs_router._commit_as_service(services, None, resolved)
        kwargs.update(context=context, co_author=as_service)
        if not as_service:
            kwargs["author"] = context.user.git_author() or user
    try:
        with scrubbed_errors(token, passthrough=(_Rejected,)):
            result = await run_in_threadpool(
                lambda: writer.promote(dag_id, body.from_env, body.to_env, **kwargs)  # type: ignore[attr-defined]
            )
    except _Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except StorageError as exc:
        raise storage_http_error(exc) from exc
    credential = await specs_router._credential_audit(resolved)
    await run_in_threadpool(
        lambda: services.audit.record(
            "promote",
            user=user,
            dag_id=dag_id,
            **{
                **(context.audit() if context else {}),
                "from_env": body.from_env,
                "to_env": body.to_env,
                "pr_url": result.pr_url,
                "branch": result.branch,
                "version": result.version,
                **credential,
            },
        )
    )
    return result


class _Rejected(Exception):
    """Raised from the render callback: the promotion is refused before any write."""

    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def _invalid_detail(report: ValidationReport, environment: str | None) -> dict[str, Any]:
    """Deploy's "invalid" outcome as an HTTP 422 detail: message, status and report."""
    errors = [p.message for p in report.problems if p.severity == "error"]
    where = f" for environment {environment!r}" if environment else ""
    shown = "; ".join(errors[:3]) + (f" (+{len(errors) - 3} more)" if len(errors) > 3 else "")
    return {
        "status": "invalid",
        "message": f"validation failed{where}; nothing was written: {shown}",
        "report": report.model_dump(mode="json"),
    }


async def _promote(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await _call(func, *args, **kwargs)
    except _Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _get(target: DeployTarget, dag_id: str, environment: str) -> Any:
    try:
        return target.get(dag_id, environment=environment)  # type: ignore[call-arg]
    except (StorageError, TypeError):
        return None


router = APIRouter()
router.include_router(read_router)
router.include_router(write_router)
