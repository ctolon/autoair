"""Astronomer Blueprint templates for the palette, the config form and task previews."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path
from fastapi.concurrency import run_in_threadpool

from autoair.api.deps import ServicesDep
from autoair.blueprints.catalog import BlueprintCatalog
from autoair.blueprints.models import (
    BlueprintCatalogStatus,
    BlueprintDetail,
    BlueprintPreview,
    BlueprintPreviewRequest,
    BlueprintSummary,
)
from autoair.iam import model as P
from autoair.iam.deps import PrincipalDep, iam_settings

router = APIRouter(prefix="/blueprints", tags=["blueprints"])
# Rebuilding imports every blueprint module in a child process: catalog:refresh only.
write_router = APIRouter(prefix="/blueprints", tags=["blueprints"])

_NAME = Path(max_length=100, pattern=r"^[a-z][a-z0-9_]*$")


def _catalog(services: ServicesDep) -> BlueprintCatalog:
    catalog: BlueprintCatalog | None = services.catalog.blueprints
    if catalog is None:
        raise HTTPException(status_code=404, detail="the blueprint catalog is not configured")
    return catalog


@router.get("", operation_id="listBlueprints")
async def list_blueprints(services: ServicesDep) -> list[BlueprintSummary]:
    """Every blueprint version found (entry points, template folders, configured modules)."""
    return await run_in_threadpool(_catalog(services).summaries)


@router.get("/status", operation_id="getBlueprintStatus")
async def blueprint_status(services: ServicesDep) -> BlueprintCatalogStatus:
    return await run_in_threadpool(_catalog(services).status)


@router.get("/{name}/{version}", operation_id="getBlueprint")
async def get_blueprint(
    services: ServicesDep, name: str = _NAME, version: int = Path(ge=1, le=10_000)
) -> BlueprintDetail:
    """One blueprint version with its config schema and default task preview."""
    detail = await run_in_threadpool(_catalog(services).get, name, version)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"blueprint {name!r} v{version} not found")
    return detail


@router.post("/preview", operation_id="previewBlueprint")
async def preview_blueprint(
    body: BlueprintPreviewRequest, services: ServicesDep, principal: PrincipalDep
) -> BlueprintPreview:
    """The task ids the step renders with ``config``: the step is generated as in a deployed
    DAG and imported by the DagBag worker pool (rate limit group ``validate``)."""
    from autoair.blueprints.preview import preview_step

    catalog = _catalog(services)
    # Rendering runs template code with the caller's config, like the import check of a deploy.
    if iam_settings(services).import_check_requires_code_execute and not principal.has(
        P.CODE_EXECUTE
    ):
        return BlueprintPreview(
            error="preview skipped: it needs the AutoAir permission 'code:execute' "
            "(iam.import_check_requires_code_execute)"
        )
    timeout = services.settings.blueprints.preview_timeout
    return await run_in_threadpool(preview_step, catalog, body, timeout)


@write_router.post("/refresh", operation_id="refreshBlueprints")
async def refresh_blueprints(services: ServicesDep) -> BlueprintCatalogStatus:
    catalog = _catalog(services)
    await run_in_threadpool(catalog.refresh)
    return await run_in_threadpool(catalog.status)
