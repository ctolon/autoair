"""``/api/admin/*``: the admin panel (``admin:panel``; changes also need their own permission).

Overview (version, backends, feature flags, replicas, catalog), audit browser + CSV export,
templates, users seen with their AutoAir roles, AI usage per user/model, pending proposals
and reviews, database status/migrations and cache actions. The configuration view reuses
``GET /api/config``.
"""

from __future__ import annotations

import datetime
import logging
import os
import socket
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from autoair import __version__
from autoair._compat import airflow_version_str
from autoair.admin import (
    SeenUser,
    UsageRow,
    aggregate_usage,
    all_pending_proposals,
    audit_csv,
    seen_users_for,
    usage_rows,
    users_overview,
)
from autoair.api.deps import ServicesDep
from autoair.audit import AuditRecord, request_user
from autoair.catalog import CatalogStats
from autoair.iam import model as P
from autoair.iam.deps import PrincipalDep, require
from autoair.sections import ADMIN_EXPORT_MAX, AdminSettings
from autoair.templates import TemplateSummary

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_limits(services: Any) -> AdminSettings:
    """``admin.*`` limits (defaults for settings-like stand-ins without the section)."""
    cfg = getattr(services.settings, "admin", None)
    return cfg if isinstance(cfg, AdminSettings) else AdminSettings()


class ReplicaInfo(BaseModel):
    id: str
    hostname: str
    pid: int
    version: str
    mode: str
    started_at: str
    last_seen: float
    alive: bool
    current: bool = False


class BackendInfo(BaseModel):
    database: Literal["file", "sqlite", "postgresql"]
    database_url: str | None = Field(default=None, description="Credentials masked.")
    schema_version: int | None = None
    schema_head: int | None = None
    collab_sync: str = Field(description="memory, database or redis (effective).")
    deploy_target: str = Field(description="Kind of the default deploy target.")
    deploy_target_source: str


class AdminOverview(BaseModel):
    autoair_version: str
    airflow_version: str | None
    mode: str
    auth: str
    started_at: str
    backends: BackendInfo
    features: dict[str, bool]
    replicas: list[ReplicaInfo]
    catalog: CatalogStats | None = None
    templates: int = 0
    user_templates: int = 0


class AuditPage(BaseModel):
    items: list[AuditRecord]
    offset: int
    limit: int
    has_more: bool


class ProposalRow(BaseModel):
    dag_id: str
    etag: str
    author: str | None
    timestamp: str
    message: str | None = None
    environment: str | None = None
    action: Literal["deploy", "delete"] = Field(
        default="deploy", description="'delete': the proposal deletes the DAG."
    )
    approvals: list[str] = Field(default_factory=list)


class ReviewRow(BaseModel):
    dag_id: str
    etag: str
    user: str | None
    verdict: str
    comment: str | None = None
    timestamp: str


class ProposalsOverview(BaseModel):
    require_approval: bool
    proposals: list[ProposalRow]
    reviews: list[ReviewRow]


class MigrationInfo(BaseModel):
    version: int
    description: str
    applied_at: str | None = None


class DatabaseStatus(BaseModel):
    backend: Literal["file", "sqlite", "postgresql"]
    url: str | None = None
    reachable: bool = True
    current: int | None = None
    head: int | None = None
    pending: list[MigrationInfo] = Field(default_factory=list)
    applied: list[MigrationInfo] = Field(default_factory=list)
    pool: str | None = None
    tables: dict[str, int] = Field(default_factory=dict)
    state_dir: str


class CacheInfo(BaseModel):
    name: str
    description: str
    entries: int | None = None
    detail: str | None = None


class CacheAction(BaseModel):
    cache: str
    ok: bool
    detail: str | None = None


# --------------------------------------------------------------------------- helpers


def _collab_mode(services: Any) -> str:
    cfg = services.settings.collab
    if not services.settings.feature("collab"):
        return "off"
    if cfg.sync != "auto":
        return str(cfg.sync)
    if cfg.redis_url:
        return "redis"
    return "database" if services.database is not None else "memory"


def _replicas(services: Any) -> list[ReplicaInfo]:
    db = services.database
    me = f"{socket.gethostname()}-{os.getpid()}"
    if db is None:  # no shared store: only this replica is known
        return [
            ReplicaInfo(
                id=me,
                hostname=socket.gethostname(),
                pid=os.getpid(),
                version=__version__,
                mode=services.settings.mode,
                started_at=services.started_at,
                last_seen=datetime.datetime.now(datetime.timezone.utc).timestamp(),
                alive=True,
                current=True,
            )
        ]
    from autoair.persistence.sql import heartbeat, replicas

    heartbeat(db, version=__version__, mode=services.settings.mode, started_at=services.started_at)
    rows = replicas(db, stale_after=db.settings.heartbeat_seconds)
    return [ReplicaInfo(**r, current=r["id"] == me) for r in rows]


def _overview(services: Any, auth: str) -> AdminOverview:
    db = services.database
    version = head = None
    if db is not None:
        from autoair.persistence import migrations

        version, head = migrations.current(db), migrations.HEAD
    try:
        catalog: CatalogStats | None = services.catalog.stats()
    except Exception:
        catalog = None
    templates = services.templates.list()
    return AdminOverview(
        autoair_version=__version__,
        airflow_version=airflow_version_str(),
        mode=services.settings.mode,
        auth=auth,
        started_at=services.started_at,
        backends=BackendInfo(
            database=services.settings.database.backend,
            database_url=db.masked_url if db is not None else None,
            schema_version=version,
            schema_head=head,
            collab_sync=_collab_mode(services),
            deploy_target=str(services.target.kind),
            deploy_target_source=services.storage_source,
        ),
        features=services.settings.effective_features(),
        replicas=_replicas(services),
        catalog=catalog,
        templates=len(templates),
        user_templates=sum(1 for t in templates if not t.builtin),
    )


def _iso(value: str | None, name: str) -> str | None:
    """Accepts a date (2026-10-15) or an ISO timestamp; returns ISO UTC text."""
    if value is None or value == "":
        return None
    try:
        if len(value) == 10:
            parsed = datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc)
        else:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: not an ISO date/time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc).isoformat()


Text = Annotated[str | None, Query(max_length=250)]


# --------------------------------------------------------------------------- overview


@router.get("/overview", operation_id="adminOverview")
async def admin_overview(services: ServicesDep, principal: PrincipalDep) -> AdminOverview:
    """Versions, backends, feature flags, replicas and catalog statistics."""
    return await run_in_threadpool(_overview, services, principal.auth)


# --------------------------------------------------------------------------- audit


@router.get("/audit", operation_id="adminAudit")
async def admin_audit(
    services: ServicesDep,
    user: Text = None,
    action: Text = None,
    dag_id: Text = None,
    since: Text = None,
    until: Text = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> AuditPage:
    """The audit trail, newest first, filtered (``since`` inclusive, ``until`` exclusive)."""
    filters = {
        "user": user or None,
        "action": action or None,
        "dag_id": dag_id or None,
        "since": _iso(since, "since"),
        "until": _iso(until, "until"),
    }
    items = await run_in_threadpool(
        lambda: services.audit.query(limit=limit + 1, offset=offset, **filters)
    )
    return AuditPage(items=items[:limit], offset=offset, limit=limit, has_more=len(items) > limit)


@router.get(
    "/audit.csv",
    operation_id="adminAuditCsv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def admin_audit_csv(
    services: ServicesDep,
    user: Text = None,
    action: Text = None,
    dag_id: Text = None,
    since: Text = None,
    until: Text = None,
    limit: Annotated[int | None, Query(ge=1, le=ADMIN_EXPORT_MAX)] = None,
) -> Response:
    """The filtered audit trail as CSV (formula-like cells are neutralised). ``limit``
    defaults to ``admin.export_default`` and may not exceed ``admin.export_limit``."""
    cfg = _admin_limits(services)
    if limit is not None and limit > cfg.export_limit:
        raise HTTPException(
            status_code=422,
            detail=f"limit: at most {cfg.export_limit} records per export (admin.export_limit)",
        )
    rows = limit if limit is not None else min(cfg.export_default, cfg.export_limit)
    records = await run_in_threadpool(
        lambda: services.audit.query(
            limit=rows,
            user=user or None,
            action=action or None,
            dag_id=dag_id or None,
            since=_iso(since, "since"),
            until=_iso(until, "until"),
        )
    )
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        audit_csv(records),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="autoair-audit-{stamp}.csv"'},
    )


# --------------------------------------------------------------------------- templates


@router.get("/templates", operation_id="adminTemplates")
async def admin_templates(services: ServicesDep) -> list[TemplateSummary]:
    """Every template (built-in and saved), saved ones first."""
    items: list[TemplateSummary] = await run_in_threadpool(services.templates.list)
    return sorted(items, key=lambda t: (t.builtin, t.title.lower()))


@router.delete(
    "/templates/{template_id}",
    operation_id="adminDeleteTemplate",
    dependencies=[require(P.TEMPLATE_WRITE)],
)
async def admin_delete_template(
    template_id: str, services: ServicesDep, request: Request
) -> CacheAction:
    from autoair.api.routers.templates import check_owner_team
    from autoair.templates import TemplateError

    try:
        current = await run_in_threadpool(services.templates.get, template_id)
        if current is not None and not current.builtin:
            await check_owner_team(request, services, current.owner_team)
        deleted = await run_in_threadpool(services.templates.delete, template_id)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="no such saved template")
    await run_in_threadpool(
        services.audit.record, "template_delete", user=request_user(request), template=template_id
    )
    return CacheAction(cache=f"template:{template_id}", ok=True, detail="deleted")


# --------------------------------------------------------------------------- users


@router.get("/users", operation_id="adminUsers")
async def admin_users(services: ServicesDep) -> list[SeenUser]:
    """Users seen by this AutoAir (their AutoAir roles when last seen) and in the audit."""

    def load() -> list[SeenUser]:
        seen = seen_users_for(services).all()
        return users_overview(seen, services.audit.query(limit=_admin_limits(services).scan_limit))

    return await run_in_threadpool(load)


# --------------------------------------------------------------------------- AI usage


@router.get("/ai-usage", operation_id="adminAiUsage")
async def admin_ai_usage(
    services: ServicesDep, days: Annotated[int, Query(ge=1, le=3660)] = 30
) -> list[UsageRow]:
    """Requests, tokens and cost per user and model over the last ``days`` (AI audit log)."""
    from autoair.ai.audit_log import audit_for

    since = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    ).isoformat()

    def load() -> list[UsageRow]:
        log = audit_for(services)
        repo = log.repository
        usage = getattr(repo, "usage", None)
        if callable(usage):
            return usage_rows(usage(since))
        return aggregate_usage(log.recent(_admin_limits(services).usage_scan_limit, None, since))

    return await run_in_threadpool(load)


# --------------------------------------------------------------------------- proposals


def proposals_overview(services: Any) -> ProposalsOverview:
    """Pending deploy proposals of every DAG and recent review verdicts (blocking I/O)."""
    from autoair.iam.deps import iam_settings
    from autoair.iam.proposals import proposal_approvers

    store = services.proposals
    proposals = all_pending_proposals(store)
    rows: list[ProposalRow] = []
    reviews: list[ReviewRow] = []
    for p in proposals:
        rows.append(
            ProposalRow(
                dag_id=p.dag_id,
                etag=p.etag,
                author=p.author,
                timestamp=p.timestamp,
                message=p.message,
                environment=p.environment,
                action=p.action,
                approvals=proposal_approvers(store, p, getattr(services, "version_authors", None)),
            )
        )
    listing = getattr(store, "all_reviews", None)
    if callable(listing):
        pairs = listing(200)
    else:
        dags = {p.dag_id for p in proposals}
        pairs = [(d, r) for d in dags for r in (store.reviews(d) if store else [])]
    for dag_id, r in pairs:
        reviews.append(
            ReviewRow(
                dag_id=dag_id,
                etag=r.etag,
                user=r.user,
                verdict=r.verdict,
                comment=r.comment,
                timestamp=r.timestamp,
            )
        )
    reviews.sort(key=lambda r: r.timestamp, reverse=True)
    return ProposalsOverview(
        # what the deploy route enforces (features.iam off turns approvals off)
        require_approval=bool(iam_settings(services).require_approval),
        proposals=rows,
        reviews=reviews[:200],
    )


@router.get("/proposals", operation_id="adminProposals")
async def admin_proposals(services: ServicesDep) -> ProposalsOverview:
    """Pending deploy proposals of every DAG and recent review verdicts."""
    return await run_in_threadpool(proposals_overview, services)


# --------------------------------------------------------------------------- database


def _database_status(services: Any) -> DatabaseStatus:
    db = services.database
    state_dir = str(services.settings.state_dir)
    if db is None:
        return DatabaseStatus(backend="file", state_dir=state_dir)
    from autoair.persistence import migrations

    reachable = db.ping()
    status = DatabaseStatus(
        backend=services.settings.database.backend,
        url=db.masked_url,
        reachable=reachable,
        head=migrations.HEAD,
        pool=db.pool_status(),
        state_dir=state_dir,
    )
    if not reachable:
        return status
    from sqlalchemy import func, select

    status.current = migrations.current(db)
    status.pending = [
        MigrationInfo(version=m.version, description=m.description) for m in migrations.pending(db)
    ]
    status.applied = [
        MigrationInfo(
            version=int(str(h["version"])),
            description=str(h["description"]),
            applied_at=str(h["applied_at"]),
        )
        for h in migrations.history(db)
    ]
    with db.connect() as conn:
        for table in db.tables.all():
            try:
                count = conn.execute(select(func.count()).select_from(table)).scalar()
            except Exception:
                log.debug("cannot count %s", table.name, exc_info=True)
                continue
            status.tables[table.name] = int(count or 0)
    return status


@router.get("/database", operation_id="adminDatabase")
async def admin_database(services: ServicesDep) -> DatabaseStatus:
    """State backend, schema version, pending migrations and table sizes."""
    return await run_in_threadpool(_database_status, services)


@router.post(
    "/database/upgrade",
    operation_id="adminDatabaseUpgrade",
    dependencies=[require(P.ADMIN_CONFIG)],
)
async def admin_database_upgrade(services: ServicesDep, request: Request) -> DatabaseStatus:
    """Apply pending schema migrations (like ``autoair db upgrade``)."""
    from autoair.persistence import migrations

    db = services.database
    if db is None:
        raise HTTPException(status_code=409, detail="the file backend has no schema")
    applied = await run_in_threadpool(migrations.upgrade, db)
    await run_in_threadpool(
        services.audit.record, "db_upgrade", user=request_user(request), applied=applied
    )
    return await run_in_threadpool(_database_status, services)


# --------------------------------------------------------------------------- caches


def _caches(services: Any) -> list[CacheInfo]:
    out: list[CacheInfo] = []
    try:
        stats = services.catalog.stats()
        out.append(
            CacheInfo(
                name="catalog",
                description="Operator catalog (built from installed providers)",
                entries=stats.operators,
                detail=f"{stats.providers} providers, "
                + ("loaded from cache" if stats.loaded_from_cache else "built"),
            )
        )
    except Exception as exc:
        out.append(CacheInfo(name="catalog", description="Operator catalog", detail=str(exc)))
    market = services.marketplace
    out.append(
        CacheInfo(
            name="marketplace",
            description="Fetched marketplace indexes",
            entries=len(getattr(market, "_cache", {}) or {}) if market is not None else 0,
        )
    )
    retriever = getattr(services, "_autoair_ai_retriever", None)
    snapshot = getattr(retriever, "_snapshot", None)
    out.append(
        CacheInfo(
            name="ai-index",
            description="AI retrieval index (knowledge, operators, specs, templates)",
            entries=len(getattr(snapshot, "docs", []) or []) if snapshot is not None else None,
            detail="built" if snapshot is not None else "not built yet",
        )
    )
    return out


@router.get("/caches", operation_id="adminCaches")
async def admin_caches(services: ServicesDep) -> list[CacheInfo]:
    """In-process caches of this replica (each replica has its own)."""
    return await run_in_threadpool(_caches, services)


@router.post(
    "/caches/{cache}/clear",
    operation_id="adminClearCache",
    dependencies=[require(P.ADMIN_CONFIG)],
)
async def admin_clear_cache(
    cache: Literal["catalog", "marketplace", "ai-index"], services: ServicesDep, request: Request
) -> CacheAction:
    """Rebuild the catalog, forget marketplace indexes or rebuild the AI index (this
    replica)."""
    detail: str | None = None
    if cache == "catalog":
        stats = await run_in_threadpool(services.catalog.refresh)
        detail = f"{stats.operators} operators"
    elif cache == "marketplace":
        market = services.marketplace
        if market is not None:
            with market._lock:
                market._cache.clear()
    else:
        from autoair.ai.corpus import retriever_for

        if not services.settings.feature("ai") or not services.settings.ai.enabled:
            raise HTTPException(status_code=404, detail="AI is disabled")
        retriever = retriever_for(services)
        retriever.invalidate()
        detail = "the index is rebuilt on the next AI request"
    await run_in_threadpool(
        services.audit.record, "cache_clear", user=request_user(request), cache=cache
    )
    return CacheAction(cache=cache, ok=True, detail=detail)


__all__ = ["router"]
