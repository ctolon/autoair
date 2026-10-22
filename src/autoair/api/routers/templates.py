from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from autoair.api.deps import ServicesDep
from autoair.api.features import ensure_feature
from autoair.audit import request_user
from autoair.iam import model as P
from autoair.iam.deps import STATE_ATTR, check
from autoair.templates import (
    Template,
    TemplateConflict,
    TemplateError,
    TemplateInput,
    TemplateSummary,
    TemplateVersionInfo,
    carries_raw_code,
)

# Split so the auth backend can guard writes with DAG edit permission.
read_router = APIRouter(prefix="/templates", tags=["templates"])
write_router = APIRouter(prefix="/templates", tags=["templates"])

IfMatch = Annotated[
    str | None,
    Header(
        description="Etag of the template as last read: the write happens only if nobody "
        "changed it since (409 otherwise)."
    ),
]


class DeleteTemplateResult(BaseModel):
    deleted: bool


def _etag(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().removeprefix("W/").strip('"') or None


def _conflict(exc: TemplateConflict) -> HTTPException:
    current = exc.current
    return HTTPException(
        status_code=409,
        detail={
            "message": str(exc),
            "current_version": current.version if current is not None else None,
            "current_etag": current.etag if current is not None else None,
            "updated_by": current.updated_by if current is not None else None,
        },
    )


async def check_owner_team(request: Request, services: ServicesDep, owner: str | None) -> None:
    """Multi-team deployments (``team`` set): a template owned by another team is changed
    only by administrators (``admin:config``); templates without an owner team are shared."""
    team = services.settings.team
    if not team or not owner or owner.casefold() == team.casefold():
        return
    principal = getattr(request.state, STATE_ATTR, None)
    if principal is None:
        raise HTTPException(status_code=403, detail="changing this template is not allowed")
    await check(
        request,
        principal,
        P.ADMIN_CONFIG,
        detail=f"the template is owned by team {owner!r}; this deployment belongs to team "
        f"{team!r} (changing it needs AutoAir permission {P.ADMIN_CONFIG!r})",
    )


async def check_raw_code(request: Request, template: TemplateInput) -> None:
    """A template with hand-written DAG code needs ``code:raw`` (and, for a raw code DAG,
    the code editor and Python code features), exactly like saving that spec."""
    if not carries_raw_code(template):
        return
    detail = (
        "the template holds hand-written DAG code (a raw code DAG or a kept Python file), "
        "which is arbitrary Python: saving it needs the AutoAir permission 'code:raw'"
    )
    if template.spec is not None and template.spec.raw_code is not None:
        ensure_feature(request, "code_editor")
        ensure_feature(request, "python_code")
    principal = getattr(request.state, STATE_ATTR, None)
    if principal is None:
        raise HTTPException(status_code=403, detail=detail)
    await check(request, principal, P.CODE_RAW, detail=detail)


@read_router.get("", operation_id="listTemplates")
async def list_templates(services: ServicesDep) -> list[TemplateSummary]:
    return await run_in_threadpool(services.templates.list)


@read_router.get("/{template_id}", operation_id="getTemplate")
async def get_template(template_id: str, services: ServicesDep, response: Response) -> Template:
    try:
        tpl = await run_in_threadpool(services.templates.get, template_id)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if tpl is None:
        raise HTTPException(status_code=404, detail=f"template {template_id!r} not found")
    if tpl.etag:
        response.headers["ETag"] = f'"{tpl.etag}"'
    return tpl


@read_router.get("/{template_id}/versions", operation_id="listTemplateVersions")
async def list_template_versions(
    template_id: str, services: ServicesDep
) -> list[TemplateVersionInfo]:
    """Stored versions of a template, newest first (operator templates keep a history)."""
    try:
        versions = await run_in_threadpool(services.templates.versions, template_id)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if versions is None:
        raise HTTPException(status_code=404, detail=f"template {template_id!r} not found")
    return versions


@read_router.get("/{template_id}/versions/{version}", operation_id="getTemplateVersion")
async def get_template_version(template_id: str, version: str, services: ServicesDep) -> Template:
    try:
        tpl = await run_in_threadpool(services.templates.get_version, template_id, version)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if tpl is None:
        raise HTTPException(
            status_code=404, detail=f"version {version!r} of template {template_id!r} not found"
        )
    return tpl


@write_router.post("", operation_id="saveTemplate")
async def save_template(
    body: TemplateInput,
    services: ServicesDep,
    request: Request,
    response: Response,
    if_match: IfMatch = None,
    if_none_match: Annotated[
        str | None,
        Header(description="`*`: create only; 409 when a template with this id exists."),
    ] = None,
) -> Template:
    """Create a template, or publish a new version of a saved one (operator templates get
    the next version number; older versions stay readable under `/versions`). A template
    with hand-written DAG code needs `code:raw`."""
    await check_raw_code(request, body)
    try:
        previous = await run_in_threadpool(services.templates.get, body.id)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if previous is not None and not previous.builtin:
        await check_owner_team(request, services, previous.owner_team)
    await check_owner_team(request, services, body.owner_team)
    user = request_user(request)
    try:
        saved = await run_in_threadpool(
            lambda: services.templates.save(
                body,
                user=user,
                if_match=_etag(if_match),
                create_only=(if_none_match or "").strip() == "*",
            )
        )
    except TemplateConflict as exc:
        raise _conflict(exc) from exc
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await run_in_threadpool(
        services.audit.record,
        "template_save",
        user=user,
        template=body.id,
        kind=body.kind,
        version=saved.version,
        previous_version=previous.version if previous is not None else None,
        created=previous is None,
    )
    if saved.etag:
        response.headers["ETag"] = f'"{saved.etag}"'
    return saved


@write_router.delete("/{template_id}", operation_id="deleteTemplate")
async def delete_template(
    template_id: str, services: ServicesDep, request: Request, if_match: IfMatch = None
) -> DeleteTemplateResult:
    try:
        current = await run_in_threadpool(services.templates.get, template_id)
        if current is not None and not current.builtin:
            await check_owner_team(request, services, current.owner_team)
        deleted = await run_in_threadpool(
            lambda: services.templates.delete(template_id, if_match=_etag(if_match))
        )
    except TemplateConflict as exc:
        raise _conflict(exc) from exc
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if deleted:
        await run_in_threadpool(
            services.audit.record,
            "template_delete",
            user=request_user(request),
            template=template_id,
            kind=current.kind if current is not None else None,
            version=current.version if current is not None else None,
        )
    return DeleteTemplateResult(deleted=deleted)
