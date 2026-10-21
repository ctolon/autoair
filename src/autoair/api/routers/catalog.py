from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.deps import ServicesDep
from autoair.catalog import (
    CatalogDiagnostics,
    CatalogStats,
    ConnectionTypeInfo,
    NotifierDetail,
    NotifierSummary,
    OperatorDetail,
    OperatorSummary,
    QueueScheme,
    TaskDecoratorInfo,
)
from autoair.catalog.models import CatalogFiltered

router = APIRouter(prefix="/catalog", tags=["catalog"])
# Rebuilding the catalog is expensive (subprocess introspection): DAG editors only.
write_router = APIRouter(prefix="/catalog", tags=["catalog"])


class NodeKindInfo(BaseModel):
    name: str
    title: str
    description: str
    category: str
    config_schema: dict[str, Any]
    icon: str | None = Field(
        default=None, description="Palette/canvas icon id (builtin:<logo> or custom:<name>)."
    )
    color: str | None = Field(default=None, description="Canvas accent colour (#rrggbb).")
    example_config: dict[str, Any] | None = Field(
        default=None, description="Config prefilled when the node is inserted."
    )
    output_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the task's return value (XCom)."
    )
    doc_url: str | None = Field(default=None, description="Documentation link (https).")


def _with_icons(services: Any, summaries: list[OperatorSummary]) -> list[OperatorSummary]:
    store = services.icons
    if store is None:
        return summaries
    assignments = store.assignments()
    rules = tuple((r.match, r.icon) for r in services.settings.icons.rules_extra)
    return [
        s.model_copy(
            update={
                "icon": store.resolve(
                    fqn=s.fqn,
                    class_name=s.class_name,
                    module=s.module,
                    provider=s.provider,
                    integration=s.integration,
                    assignments=assignments,
                    declared=s.icon,  # package metadata
                    rules=rules,
                )
            }
        )
        for s in summaries
    ]


@router.get("/operators", operation_id="listOperators")
async def list_operators(services: ServicesDep) -> list[OperatorSummary]:
    summaries = await run_in_threadpool(services.catalog.summaries)
    return await run_in_threadpool(_with_icons, services, summaries)


@router.get("/operators/{fqn}", operation_id="getOperator")
async def get_operator(fqn: str, services: ServicesDep) -> OperatorDetail:
    detail = await run_in_threadpool(services.catalog.get, fqn)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"operator {fqn!r} not in catalog")
    return detail


@router.get("/stats", operation_id="getCatalogStats")
async def catalog_stats(services: ServicesDep) -> CatalogStats:
    return await run_in_threadpool(services.catalog.stats)


@write_router.post("/refresh", operation_id="refreshCatalog")
async def refresh_catalog(services: ServicesDep) -> CatalogStats:
    return await run_in_threadpool(services.catalog.refresh)


@write_router.get("/diagnostics", operation_id="getCatalogDiagnostics")
async def catalog_diagnostics(services: ServicesDep) -> CatalogDiagnostics:
    """What the catalog build scanned and found, what was filtered out and why, failed
    modules and applied package metadata (catalog:refresh permission)."""
    diag = await run_in_threadpool(services.catalog.diagnostics)
    return diag.model_copy(update={"filtered": [*diag.filtered, *_policy_filtered(services)]})


def _policy_filtered(services: Any) -> list[CatalogFiltered]:
    """Operators listed in the palette but refused by the DAG policy."""
    import fnmatch

    settings = services.settings
    forbidden = list(settings.forbidden_operators or [])
    privileged = (
        []
        if settings.allow_privileged_operators
        else [*settings.policy.privileged_operators, *settings.policy.privileged_operators_extra]
    )
    out: list[CatalogFiltered] = []
    if not forbidden and not privileged:
        return out
    for s in services.catalog.summaries():
        hit = next((p for p in forbidden if fnmatch.fnmatchcase(s.fqn, p)), None)
        if hit is not None:
            out.append(
                CatalogFiltered(
                    name=s.fqn, kind="operator", reason="policy.forbidden_operators", detail=hit
                )
            )
            continue
        hit = next((p for p in privileged if fnmatch.fnmatchcase(s.fqn, p)), None)
        if hit is not None:
            out.append(
                CatalogFiltered(
                    name=s.fqn, kind="operator", reason="policy.privileged_operators", detail=hit
                )
            )
    return out


@router.get("/node-kinds", operation_id="listNodeKinds")
def list_node_kinds(services: ServicesDep) -> list[NodeKindInfo]:
    return [NodeKindInfo.model_validate(d) for d in services.registry.describe()]


@router.get("/notifiers", operation_id="listNotifiers")
async def list_notifiers(services: ServicesDep) -> list[NotifierSummary]:
    return await run_in_threadpool(services.catalog.notifiers)


@router.get("/notifiers/{fqn}", operation_id="getNotifier")
async def get_notifier(fqn: str, services: ServicesDep) -> NotifierDetail:
    detail = await run_in_threadpool(services.catalog.get_notifier, fqn)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"notifier {fqn!r} not in catalog")
    return detail


@router.get("/decorators", operation_id="listDecorators")
async def list_decorators(services: ServicesDep) -> list[TaskDecoratorInfo]:
    return await run_in_threadpool(services.catalog.decorators)


@router.get("/queue-schemes", operation_id="listQueueSchemes")
async def list_queue_schemes(services: ServicesDep) -> list[QueueScheme]:
    return await run_in_threadpool(services.catalog.queue_schemes)


@router.get("/connection-types", operation_id="listConnectionTypes")
async def list_connection_types(services: ServicesDep) -> list[ConnectionTypeInfo]:
    return await run_in_threadpool(services.catalog.connection_types)
