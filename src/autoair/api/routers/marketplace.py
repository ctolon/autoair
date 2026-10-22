from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from autoair.api.deps import ServicesDep
from autoair.api.routers.templates import check_raw_code
from autoair.marketplace import Marketplace, MarketplaceError, MarketplaceListing
from autoair.templates import Template, TemplateError

read_router = APIRouter(prefix="/marketplace", tags=["marketplace"])
write_router = APIRouter(prefix="/marketplace", tags=["marketplace"])


class InstallRequest(BaseModel):
    source: str
    entry_id: str


def _market(services: ServicesDep) -> Marketplace:
    if services.marketplace is None:
        raise HTTPException(status_code=404, detail="marketplace is not configured")
    return services.marketplace


@read_router.get("", operation_id="listMarketplace")
async def list_marketplace(services: ServicesDep, refresh: bool = False) -> MarketplaceListing:
    """Entries of every configured source (signature-verified indexes only)."""
    return await run_in_threadpool(_market(services).listing, refresh)


@write_router.post("/install", operation_id="installMarketplaceEntry")
async def install_entry(body: InstallRequest, services: ServicesDep, request: Request) -> Template:
    """Downloads a template/fragment, checks its SHA-256 against the signed index, saves it.
    A template with hand-written DAG code needs `code:raw`, like saving it directly."""
    market = _market(services)
    try:
        tpl = await run_in_threadpool(market.fetch, body.source, body.entry_id)
    except (MarketplaceError, TemplateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await check_raw_code(request, tpl)
    try:
        await run_in_threadpool(market.templates.save, tpl)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = await run_in_threadpool(services.templates.get, tpl.id)
    if saved is None:  # pragma: no cover - just written
        raise HTTPException(status_code=500, detail="template was not saved")
    return saved
