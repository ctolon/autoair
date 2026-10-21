"""``/api/kb``: the documentation knowledge base (search, status, sources, refresh,
bundles).

Reading (status, sources, search) needs ``ai:use``; managing (source overrides, refresh,
rebuild, cancel, bundle export / import) needs ``ai:admin`` or ``admin:config``. Every
route answers 404 while ``features.kb`` is off. None of it needs the ``autoair[ai]``
extra: without an embedder search is keyword (BM25) only.
"""

from __future__ import annotations

import asyncio
import datetime
import tempfile
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response
from pydantic import BaseModel, Field

from autoair.api.deps import ServicesDep
from autoair.audit import request_user
from autoair.iam import model as P
from autoair.iam.deps import PrincipalDep, deny
from autoair.kb.models import (
    ImportReport,
    KbRunStatus,
    KbSearchResult,
    KbStatus,
    SourceInfo,
    SourceOverride,
)
from autoair.kb.service import kb_for

read_router = APIRouter(prefix="/kb", tags=["kb"])
write_router = APIRouter(prefix="/kb", tags=["kb"])

_ID = r"^[a-z0-9][a-z0-9_-]{0,40}$"
_FILTER = r"^[A-Za-z0-9_.+:-]{1,120}$"


async def require_kb_admin(request: Request, principal: PrincipalDep) -> None:
    """``ai:admin`` or ``admin:config``."""
    if not (principal.has(P.AI_ADMIN) or principal.has(P.ADMIN_CONFIG)):
        raise await deny(
            request,
            principal,
            P.AI_ADMIN,
            detail="managing the knowledge base needs ai:admin or admin:config",
        )


KbAdmin = Depends(require_kb_admin)


class RefreshRequest(BaseModel):
    mode: Literal["refresh", "rebuild"] = Field(
        default="refresh",
        description="refresh: incremental (unchanged pages are skipped); rebuild: drop and "
        "re-index the selected sources.",
    )
    sources: list[str] | None = Field(
        default=None, max_length=50, description="Source ids (default: every enabled source)."
    )


@read_router.get("/status", operation_id="kbStatus")
async def status(services: ServicesDep) -> KbStatus:
    """Index size, store, search mode, sources and the last / running refresh."""
    result: KbStatus = await asyncio.to_thread(kb_for(services).status)
    return result


@read_router.get("/sources", operation_id="kbSources")
async def sources(services: ServicesDep) -> list[SourceInfo]:
    kb = kb_for(services)
    stats = await asyncio.to_thread(kb.stats)
    infos: list[SourceInfo] = await asyncio.to_thread(kb.source_infos, stats)
    return infos


@read_router.get("/search", operation_id="kbSearch")
async def search(
    services: ServicesDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    component: Annotated[list[str] | None, Query(max_length=20)] = None,
    package: Annotated[str | None, Query(pattern=_FILTER)] = None,
    version: Annotated[str | None, Query(pattern=_FILTER)] = None,
    source: Annotated[str | None, Query(pattern=_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
) -> KbSearchResult:
    """Hybrid search (BM25 + embeddings, RRF, optional reranker) with citations."""
    for value in component or []:
        if len(value) > 40:
            raise HTTPException(status_code=422, detail="component: at most 40 characters")
    filters = {"component": component, "package": package, "version": version, "source": source}
    result: KbSearchResult = await kb_for(services).asearch(
        q, k=limit, filters=filters, llm=getattr(services, "llm", None)
    )
    return result


@write_router.put("/sources/{source_id}", operation_id="kbPutSource")
async def put_source(
    source_id: Annotated[str, PathParam(pattern=_ID)],
    body: SourceOverride,
    request: Request,
    services: ServicesDep,
) -> SourceInfo:
    """Override a configured source at runtime (enabled, components, versions)."""
    kb = kb_for(services)
    try:
        info: SourceInfo = await asyncio.to_thread(kb.set_source, source_id, body)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"no knowledge-base source {source_id!r}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(
        services.audit.record,
        "kb_source_override",
        user=request_user(request),
        source=source_id,
        **body.model_dump(exclude_none=True),
    )
    return info


@write_router.delete("/sources/{source_id}/override", operation_id="kbResetSource")
async def reset_source(
    source_id: Annotated[str, PathParam(pattern=_ID)], request: Request, services: ServicesDep
) -> SourceInfo:
    """Drop the admin-panel override of a source (back to the configuration)."""
    kb = kb_for(services)
    try:
        info: SourceInfo = await asyncio.to_thread(kb.set_source, source_id, None)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"no knowledge-base source {source_id!r}"
        ) from exc
    await asyncio.to_thread(
        services.audit.record, "kb_source_reset", user=request_user(request), source=source_id
    )
    return info


@write_router.post("/refresh", operation_id="kbRefresh", status_code=202)
async def refresh(body: RefreshRequest, request: Request, services: ServicesDep) -> KbRunStatus:
    """Start a refresh or rebuild in the background (409 while one runs anywhere)."""
    from autoair.kb.ingest import AlreadyRunning

    kb = kb_for(services)
    known = {s.id for s in kb.kb.sources}
    unknown = [s for s in body.sources or [] if s not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown sources: {', '.join(unknown)}")
    try:
        result: KbRunStatus = await asyncio.to_thread(kb.start_refresh, body.mode, body.sources)
    except AlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await asyncio.to_thread(
        services.audit.record,
        "kb_refresh",
        user=request_user(request),
        mode=body.mode,
        sources=body.sources,
    )
    return result


@write_router.post("/cancel", operation_id="kbCancel")
async def cancel(request: Request, services: ServicesDep) -> KbRunStatus:
    """Ask the running refresh to stop (it keeps what it indexed)."""
    result: KbRunStatus = await asyncio.to_thread(kb_for(services).cancel)
    return result


@write_router.get(
    "/export",
    operation_id="kbExport",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def export(
    request: Request,
    services: ServicesDep,
    source: Annotated[list[str] | None, Query(max_length=50)] = None,
) -> Response:
    """Download the index as a KB bundle (zip, unsigned; sign with `autoair kb build`)."""
    from autoair.kb.bundle import bundle_bytes

    kb = kb_for(services)
    data = await asyncio.to_thread(bundle_bytes, kb, sources=source or None)
    await asyncio.to_thread(
        services.audit.record, "kb_export", user=request_user(request), sources=source
    )
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        data,
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="autoair-kb-{stamp}.zip"'},
    )


@write_router.post(
    "/import",
    operation_id="kbImport",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def import_bundle(
    request: Request,
    services: ServicesDep,
    source_id: Annotated[str, Query(pattern=_ID)] = "bundle",
) -> ImportReport:
    """Import a KB bundle (request body: the zip; bound by server.max_body_bytes - use
    `autoair kb import` for larger bundles). Checksums are verified, signatures when
    kb.bundle.public_keys is set."""
    from autoair.kb.bundle import BundleError
    from autoair.kb.bundle import import_bundle as run_import
    from autoair.kb.ingest import AlreadyRunning

    kb = kb_for(services)
    limit = kb.kb.bundle.max_bytes
    with tempfile.TemporaryFile() as tmp:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(status_code=413, detail=f"bundle larger than {limit} bytes")
            tmp.write(chunk)
        if not size:
            raise HTTPException(status_code=422, detail="empty request body (send the zip)")
        tmp.seek(0)
        try:
            report, _ = await asyncio.to_thread(run_import, kb, tmp, source_id=source_id)
        except AlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BundleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(
        services.audit.record,
        "kb_import",
        user=request_user(request),
        source=source_id,
        pages=report.pages,
        signed=report.signed,
        verified=report.signature_verified,
    )
    return report


__all__ = ["read_router", "require_kb_admin", "write_router"]
