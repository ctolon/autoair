"""FastAPI application factory (shared by the Airflow plugin and standalone mode)."""

from __future__ import annotations

import contextlib
import datetime
import html
import json
import logging
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from autoair import __version__
from autoair.ai.llm import LLM, build_llm
from autoair.api import platform
from autoair.audit import AuditLog
from autoair.auth import AuthBackend
from autoair.blueprints.catalog import BlueprintCatalog
from autoair.catalog import OperatorCatalog
from autoair.catalog.service import configured_sources
from autoair.collab import collab_secret, key_file
from autoair.deploy_setup import DeployTargetStore, apply_policy
from autoair.iam import deps as iam_deps
from autoair.iam import model as P
from autoair.iam.deps import require
from autoair.iam.login import LoginService
from autoair.iam.model import Principal
from autoair.iam.proposals import ProposalStore
from autoair.icons import IconStore
from autoair.marketplace import Marketplace
from autoair.persistence import document_store
from autoair.persistence.db import Database, database_for
from autoair.persistence.protocols import ProposalRepository
from autoair.registry import HandlerRegistry, default_registry
from autoair.rendering import render_files
from autoair.settings import Settings
from autoair.spec import DagSpec
from autoair.storage import DagRepository, DeployTarget
from autoair.storage.config import (
    StorageSettings,
    UnavailableTarget,
    build_target,
    build_target_or_unavailable,
)
from autoair.storage.render import TargetRepository
from autoair.templates import TemplateStore

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_HASHED = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css|woff2?)$")


class CachedStaticFiles(StaticFiles):
    """Content-hashed chunks are immutable (cached for a year); entry files such as the
    plugin's ``main.js`` keep their name across builds, so browsers must revalidate them —
    otherwise a stale entry can point at chunks a rebuild has removed."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        path = str(args[0]) if args else ""
        from autoair.sections import ServerSettings, active

        max_age = active(ServerSettings).static_max_age  # server.static_max_age
        response.headers["Cache-Control"] = (
            f"public, max-age={max_age}, immutable" if _HASHED.search(path) else "no-cache"
        )
        return response


@dataclass
class Services:
    settings: Settings
    catalog: OperatorCatalog
    registry: HandlerRegistry
    target: DeployTarget
    repository: DagRepository  # spec-level facade over ``target`` (used by MCP tools)
    templates: TemplateStore
    audit: AuditLog
    auth: AuthBackend
    llm: LLM | None = None
    marketplace: Marketplace | None = None
    icons: IconStore | None = None
    collab_secret: bytes = b""
    team_targets: dict[str, DeployTarget] = field(default_factory=dict)
    deploy_store: DeployTargetStore | None = None
    storage_source: str = "settings"  # "settings" (AUTOAIR_STORAGE__*) or "ui"
    # The deploy target from settings, restored when the UI choice is reset.
    configured_storage: StorageSettings | None = None
    # Standalone password sign-in (autoair.iam.login), created by create_api.
    login: LoginService | None = None
    # Deploy proposals + review index (iam.require_approval), see autoair.iam.proposals.
    proposals: ProposalRepository | None = None
    # SQL database of AutoAir's state (database.backend sqlite/postgresql), else None.
    database: Database | None = None
    started_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    deploy_target_version: str | None = None
    _next_heartbeat: float = 0.0
    _sync_thread: threading.Thread | None = None
    _target_lock: threading.Lock = field(default_factory=threading.Lock)

    def apply_storage(self, storage: StorageSettings, *, source: str) -> None:
        """Switch the default deploy target at runtime (no restart)."""
        target = build_target(storage, default_local_path=self.settings.output_dir)
        self.settings = self.settings.model_copy(update={"storage": storage})
        self.target = target
        self.repository = TargetRepository(target)
        self.storage_source = source

    def ensure_target(self, *, force: bool = False) -> DeployTarget:
        """The default deploy target, rebuilt when it could not be built so far (an
        :class:`UnavailableTarget`: e.g. the Airflow connection did not exist at start).
        Reads retry at most every few seconds, ``force`` (deploys) always."""
        target = self.target
        if not isinstance(target, UnavailableTarget):
            return target
        if not force and time.monotonic() < target.retry_at:
            return target
        with self._target_lock:
            if self.target is not target:  # another request rebuilt it
                return self.target
            try:
                built = build_target(target.settings, default_local_path=self.settings.output_dir)
            except Exception as exc:
                target.failed(exc)
                log.info("deploy target still unavailable: %s", exc)
                return target
            log.info("deploy target (storage.mode=%s) is available now", target.kind)
            self.target = built
            self.repository = TargetRepository(built)
            return built

    def target_for(self, team: str | None) -> DeployTarget:
        """Deploy target of a team (multi-team), falling back to the default target. Used
        by writes, so a target unavailable so far is rebuilt first."""
        if team and team in self.settings.team_storage:
            current = self.team_targets.get(team)
            if current is None or isinstance(current, UnavailableTarget):
                self.team_targets[team] = build_target_or_unavailable(
                    self.settings.team_storage[team],
                    default_local_path=self.settings.output_dir / team,
                )
            return self.team_targets[team]
        return self.ensure_target(force=True)

    def sync_shared_state(self, now: float | None = None) -> None:
        """SQL backends: pick up what other replicas changed (the deploy target chosen in
        the UI) and record this replica's heartbeat. Cheap; throttled by the caller."""
        db = self.database
        if db is None:
            return
        now = time.monotonic() if now is None else now
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + db.settings.heartbeat_seconds
            from autoair.persistence.sql import heartbeat

            try:
                heartbeat(
                    db, version=__version__, mode=self.settings.mode, started_at=self.started_at
                )
            except Exception:
                log.warning("cannot record the replica heartbeat", exc_info=True)
        store = self.deploy_store
        if store is None:
            return
        version = store.version()
        if version == self.deploy_target_version:
            return
        self.deploy_target_version = version
        chosen = store.load(self.configured_storage)
        try:
            if chosen is not None:
                self.apply_storage(chosen, source="ui")
            elif self.storage_source == "ui" and self.configured_storage is not None:
                self.apply_storage(self.configured_storage, source="settings")
        except Exception:
            log.warning("ignoring an unusable deploy target from another replica", exc_info=True)

    def all_targets(self) -> list[DeployTarget]:
        teams = [self.team_targets.get(t) or self.target_for(t) for t in self.settings.team_storage]
        return [self.ensure_target(), *teams]

    def render_files(self, spec: DagSpec, environment: str | None = None) -> dict[str, str]:
        return render_files(spec, environment, registry=self.registry)


def start_shared_state_sync(services: Services) -> threading.Thread | None:
    """SQL backends: a daemon thread that, every ``database.sync_seconds``, picks up what
    other replicas changed (deploy target) and records this replica's heartbeat. It holds
    only a weak reference: it ends with the ``Services`` object (tests create many)."""
    db = services.database
    if db is None:
        return None
    ref = weakref.ref(services)
    interval = db.settings.sync_seconds

    def loop() -> None:
        while True:
            current = ref()
            if current is None:
                return
            try:
                current.sync_shared_state()
            except Exception:
                log.warning("cannot sync shared state", exc_info=True)
            del current
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="autoair-shared-state", daemon=True)
    thread.start()
    return thread


async def _remember_user(
    request: Request, principal: Annotated[Principal, Depends(iam_deps.current_principal)]
) -> None:
    """Admin panel "users": who used AutoAir with which AutoAir roles (throttled; never
    fails the request)."""
    if not principal.user:
        return
    from autoair.admin import seen_users_for

    services: Services = request.app.state.services
    seen = seen_users_for(services)
    if not seen.due(principal.user):
        return
    try:
        await run_in_threadpool(
            seen.record,
            principal.user,
            roles=principal.roles,
            permissions=len(principal.permissions),
            auth=principal.auth,
        )
    except Exception:
        log.warning("cannot record the user seen", exc_info=True)


def _catalog(settings: Settings) -> OperatorCatalog:
    sources = configured_sources(settings)
    return OperatorCatalog(
        sources,
        allow=settings.operator_allow,
        deny=settings.operator_deny,
        strategy=settings.catalog_strategy,
        cache_dir=settings.cache_dir,
        catalog_file=settings.catalog_file,
        fallback_timeout=settings.catalog_fallback_timeout,
        presentation=settings.catalog,
    )


def _audit_log(settings: Settings, state: Path, database_repo: Any) -> AuditLog:
    """The audit trail with its configured sinks (``audit.sinks``; docs/iam.md#8-audit)."""
    from autoair.audit import FileAuditRepository

    sinks = settings.audit.effective(database_repo is not None)
    repositories: list[Any] = []
    for sink in sinks:  # the first repository answers queries: db before file
        if sink == "db":
            if database_repo is None:
                log.warning("audit sink 'db' ignored: no database configured (database.backend)")
            else:
                repositories.insert(0, database_repo)
        elif sink == "file":
            repositories.append(FileAuditRepository(settings.audit_url or str(state / "audit")))
    return AuditLog(repositories=repositories, log_sink="log" in sinks, otel_sink="otel" in sinks)


def _may_enable(settings: Settings, name: str) -> bool:
    """Feature ``name`` is on now or may be switched on at runtime (not locked off)."""
    return settings.feature(name) or settings.feature_lock(name) is None


def _feature_gate(app: Any, services: Services, name: str) -> Any:
    """ASGI wrapper of a mounted sub-application: 404 (HTTP) / closed before accept
    (WebSocket) while feature ``name`` is off; checked per request (runtime overrides)."""
    from autoair.api.features import feature_disabled

    async def gated(scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind in ("http", "websocket") and not services.settings.feature(name):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            detail = feature_disabled(name).detail
            await JSONResponse({"detail": detail}, status_code=404)(scope, receive, send)
            return
        await app(scope, receive, send)

    return gated


def build_services(settings: Settings, auth: AuthBackend) -> Services:
    from autoair import sections
    from autoair.validation.pool import configure_pool

    sections.activate(settings)  # AUTOAIR_<SECTION>__* for code without Settings at hand
    if settings.feature_overrides is None:
        # Runtime feature overrides (admin panel), shared by the replicas of the backend.
        from autoair.feature_overrides import override_store_for

        settings.attach_feature_overrides(override_store_for(settings))

    configure_pool(
        enabled=settings.dagbag_pool,
        size=settings.dagbag_pool_size,
        max_tasks=settings.dagbag_pool_max_tasks,
        start_timeout=settings.dagbag_pool_start_timeout,
    )
    configured_storage = settings.storage  # AUTOAIR_STORAGE__* / CLI, before any UI choice
    state = settings.state_dir
    # database.backend: files under the state folder (default) or a SQL database.
    database = database_for(settings)
    deploy_store = DeployTargetStore(
        state / "deploy-target.json",
        documents=document_store(settings, "deploy_target", file_url=str(state), suffix=".json")
        if database is not None
        else None,
    )
    chosen = deploy_store.load(configured_storage)  # chosen in the UI: overrides env
    source = "settings"
    if chosen is not None:
        try:
            chosen = apply_policy(chosen, configured_storage)
            build_target(chosen, default_local_path=settings.output_dir)
        except Exception:
            log.warning("ignoring unusable deploy target from %s", deploy_store.path, exc_info=True)
        else:
            settings = settings.model_copy(update={"storage": chosen})
            source = "ui"
    # Never fails startup: an unbuildable target (e.g. its Airflow connection does not exist
    # yet) starts degraded, is reported by /api/ready and rebuilt on the next deploy.
    target = build_target_or_unavailable(settings.storage, default_local_path=settings.output_dir)
    templates_url = settings.templates_url or str(state / "templates")
    templates = TemplateStore(
        documents=document_store(settings, "templates", file_url=templates_url),
        history=document_store(
            settings, "template_versions", file_url=f"{templates_url.rstrip('/')}/_history"
        ),
        history_limit=settings.templates_history_limit,
    )
    icons_url = settings.icons_url or str(state / "icons")
    proposals: ProposalRepository
    if database is not None:
        from autoair.persistence.sql import (
            SqlAuditRepository,
            SqlDocumentStore,
            SqlProposalStore,
        )

        audit = _audit_log(settings, state, SqlAuditRepository(database))
        proposals = SqlProposalStore(database, ttl=settings.iam.proposal_ttl_seconds)
        icons = IconStore(
            images=SqlDocumentStore(database, "icons"),
            assignments=SqlDocumentStore(database, "icon_assignments"),
        )

        def ticket_key() -> bytes:
            return database.shared_secret("collab-ticket-key")
    else:
        audit = _audit_log(settings, state, None)
        proposals = ProposalStore(
            settings.iam.proposals_url or str(state / "proposals"),
            ttl=settings.iam.proposal_ttl_seconds,
        )
        icons = IconStore(icons_url)

        def ticket_key() -> bytes:
            return key_file(state / "collab.key")

    catalog = _catalog(settings)
    catalog.blueprints = BlueprintCatalog(
        settings.blueprints, enabled=lambda: settings.feature("blueprints")
    )
    icons.package_icons = catalog  # SVG icons shipped by operator packages
    return Services(
        settings=settings,
        catalog=catalog,
        registry=default_registry(),
        target=target,
        repository=TargetRepository(target),
        templates=templates,
        audit=audit,
        proposals=proposals,
        icons=icons,
        database=database,
        deploy_target_version=deploy_store.version() if database is not None else None,
        deploy_store=deploy_store,
        storage_source=source,
        configured_storage=configured_storage,
        auth=auth,
        # Built unless AI is off for good (locked): admins may switch features.ai on at runtime.
        llm=build_llm(settings) if settings.ai.enabled and _may_enable(settings, "ai") else None,
        marketplace=Marketplace(
            settings.marketplace_sources,
            templates,
            timeout=settings.marketplace_timeout,
            cache_ttl=settings.marketplace_cache_ttl,
            max_bytes=settings.marketplace_max_bytes,
        ),
        collab_secret=collab_secret(
            settings.collab_secret.get_secret_value() if settings.collab_secret else None,
            fallback=ticket_key,
        ),
    )


def create_api(services: Services) -> FastAPI:
    """The JSON API (its OpenAPI document drives the generated TypeScript client)."""
    from autoair.api.features import requires_feature as feature
    from autoair.api.routers import (
        admin,
        admin_ai,
        admin_features,
        ai_mount,
        airflow,
        blueprints,
        catalog,
        collab,
        config,
        deploy_target,
        icons,
        interchange,
        kb,
        lsp,
        lsp_ws,
        marketplace,
        meta,
        ops,
        review,
        runs,
        specs,
        templates,
        tools,
        vcs,
    )

    api = FastAPI(
        title="AutoAir API",
        version=__version__,
        description="No-code DAG builder for Apache Airflow 3",
        separate_input_output_schemas=False,
    )
    from autoair.api.errors import install_document_errors
    from autoair.secret_mask import install_log_masking

    install_document_errors(api)
    # Git tokens typed for one request are masked in every log record (autoair.secret_mask).
    install_log_masking()
    api.state.services = services
    api.middleware("http")(ops.metrics_middleware)
    if services.database is not None and services._sync_thread is None:
        services._sync_thread = start_shared_state_sync(services)
    if getattr(services, "_kb_scheduler", None) is None:
        # Scheduled knowledge-base refreshes (kb.refresh_interval_hours); one replica at a
        # time ingests (lease in the state backend).
        from autoair.kb.scheduler import start_scheduler

        with contextlib.suppress(AttributeError):  # a services object without attributes
            services._kb_scheduler = start_scheduler(services) or False  # type: ignore[attr-defined]

    # The operator catalog alone is ~0.7 MB of JSON with every provider installed: compress.
    server = services.settings.server
    api.add_middleware(
        GZipMiddleware, minimum_size=server.gzip_min_size, compresslevel=server.gzip_level
    )
    auth = services.auth
    # Every protected route resolves the caller's AutoAir principal once (autoair.iam) and
    # requires AutoAir permissions; ``auth.read``/``auth.write_dag`` additionally keep
    # Airflow's per-DAG access on the routes about one DAG. See docs/iam.md.
    iam_deps.install(api, services)
    # Airflow UI session cookie + public sign-in routes (/auth/config, /auth/session…) and
    # the rate limiter of expensive routes (docs/sso.md, limits.*).
    platform.install_api(api, services)
    # health/ready/metrics are unauthenticated (probes, Prometheus scraping)
    api.include_router(ops.router)
    if services.settings.mode == "standalone" and auth.name == "bearer":
        from autoair.iam.login import LoginService
        from autoair.iam.login import router as login_router

        if services.login is None:
            services.login = LoginService(services.settings.airflow_url, services.settings.iam)
        db = services.database
        if db is not None and db.settings.shared_rate_limit:
            from autoair.persistence.sql import SqlLoginLimiter

            iam_cfg = services.settings.iam
            # Failed sign-ins counted in the database: the limit holds across replicas.
            services.login.limiter = SqlLoginLimiter(
                db, iam_cfg.login_max_failures, iam_cfg.login_window_seconds
            )
        api.include_router(login_router)  # sign-in happens before there is a token
    protected = APIRouter(
        dependencies=[
            *auth.app,
            Depends(iam_deps.current_principal),
            Depends(_remember_user),
            Depends(platform.rate_limit),  # limits.rate_limits (per user and role)
        ]
    )
    use = require(P.BUILDER_USE)
    protected.include_router(meta.router)  # /meta, /me: any signed-in user
    protected.include_router(catalog.router, dependencies=[use])
    protected.include_router(catalog.write_router, dependencies=[require(P.CATALOG_REFRESH)])
    blueprints_on = feature("blueprints")
    protected.include_router(blueprints.router, dependencies=[use, blueprints_on])
    protected.include_router(
        blueprints.write_router, dependencies=[require(P.CATALOG_REFRESH), blueprints_on]
    )
    protected.include_router(
        airflow.router, dependencies=[require(P.AIRFLOW_READ), feature("airflow")]
    )
    # AI: in-process, or proxied to ai.service_url; nothing when features.ai is off.
    ai_mount.include_ai(protected, services, dependencies=[require(P.AI_USE)])
    protected.include_router(lsp.router, dependencies=[use])
    # Language servers (features.ide_language_server): a ticket from the protected API opens
    # the WebSocket, which cannot use the HTTP auth dependencies.
    protected.include_router(lsp_ws.router, dependencies=[use])
    api.include_router(lsp_ws.ws_router)
    # Validation and imports are open to builder users; the parts that execute DAG code
    # (import check, Python DAG import) check ``code:execute`` in the route.
    protected.include_router(tools.router, dependencies=[use])
    # Rules-only review and secret scan: deterministic, no AI extra or features.ai needed.
    protected.include_router(review.router, dependencies=[use])
    protected.include_router(interchange.router, dependencies=[use])
    # Feature flags (AUTOAIR_FEATURES__*): routes of a disabled feature answer 404.
    templates_on = feature("templates")
    protected.include_router(templates.read_router, dependencies=[use, templates_on])
    protected.include_router(
        templates.write_router, dependencies=[require(P.TEMPLATE_WRITE), templates_on]
    )
    protected.include_router(icons.read_router, dependencies=[use])
    protected.include_router(icons.write_router, dependencies=[require(P.ICON_WRITE)])
    # specs: per-route permissions (read/audit/review/write/delete) in the router.
    protected.include_router(specs.read_router, dependencies=list(auth.read))
    protected.include_router(specs.write_router, dependencies=[*auth.write_dag, feature("deploy")])
    protected.include_router(vcs.read_router, dependencies=[*auth.read, require(P.SPEC_READ)])
    protected.include_router(
        vcs.write_router, dependencies=[*auth.write_dag, require(P.PROMOTE), feature("deploy")]
    )
    # The task runner executes DAG code on the server.
    protected.include_router(runs.router, dependencies=[require(P.RUN_TEST), feature("test_runs")])
    protected.include_router(
        collab.router, dependencies=[*auth.write_dag, require(P.SPEC_WRITE), feature("collab")]
    )
    protected.include_router(deploy_target.read_router, dependencies=[use])
    protected.include_router(
        deploy_target.admin_router, dependencies=[require(P.ADMIN_DEPLOY_TARGET)]
    )
    market_on = feature("marketplace")
    protected.include_router(marketplace.read_router, dependencies=[use, market_on])
    protected.include_router(
        marketplace.write_router, dependencies=[require(P.TEMPLATE_WRITE), market_on]
    )
    # Effective configuration (AUTOAIR_EXPOSE_CONFIG), admins only.
    protected.include_router(config.router, dependencies=[require(P.ADMIN_CONFIG)])
    # Admin panel (/api/admin/*): overview, audit, templates, users, AI usage, database.
    protected.include_router(admin.router, dependencies=[require(P.ADMIN_PANEL)])
    # Runtime feature flags (/api/admin/features; changes need admin:config).
    protected.include_router(admin_features.router, dependencies=[require(P.ADMIN_PANEL)])
    # External MCP servers of the AI agents (writes need admin:config).
    protected.include_router(admin_ai.router, dependencies=[require(P.ADMIN_PANEL)])
    # Documentation knowledge base (/api/kb/*): read with ai:use, manage with ai:admin or
    # admin:config; works without autoair[ai] (keyword search).
    kb_on = feature("kb")
    protected.include_router(kb.read_router, dependencies=[require(P.AI_USE), kb_on])
    protected.include_router(kb.write_router, dependencies=[kb.KbAdmin, kb_on])
    api.include_router(protected)
    api.include_router(icons.public_router)  # after /icons/assignments (protected)
    if services.settings.otel_enabled:
        from autoair.telemetry import setup_tracing

        setup_tracing(api)
    return api


def _config_script(api_base: str) -> str:
    config = json.dumps({"apiBase": api_base})
    return f"window.__AUTOAIR_CONFIG__={html.escape(config, quote=False)}"


def _index_html(api_base: str, title: str | None = None) -> str | None:
    index = STATIC_DIR / "standalone" / "index.html"
    if not index.exists():
        return None
    tag = f"<script>{_config_script(api_base)}</script>"
    page = index.read_text(encoding="utf-8").replace("<!--AUTOAIR_CONFIG-->", tag)
    if title:  # ui.title (branding)
        page = re.sub(
            r"<title>[^<]*</title>", f"<title>{html.escape(title)}</title>", page, count=1
        )
    return page


def create_app(services: Services) -> FastAPI:
    """API under ``/api``, the plugin bundle under ``/static`` and a full-page UI at
    ``/ui`` (used standalone and on Airflow 3.0, which lacks ``react_apps``)."""
    app = FastAPI(
        title="AutoAir", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    # CORS (cors_*), CSRF for cookie sessions, security headers, request body limit.
    platform.install_app(app, services)
    app.mount("/api", create_api(services))
    from autoair import collab

    if _may_enable(services.settings, "collab") and collab.available():
        # Real-time co-editing rooms: /collab/<dag_id> (WebSocket, Yjs sync protocol).
        # Mounted unless locked off; features.collab is checked per connection (runtime).
        from autoair.collab_sync import build_sync

        app.mount(
            "/collab",
            _feature_gate(
                collab.create_collab_app(
                    services.collab_secret,
                    max_rooms=services.settings.collab_max_rooms,
                    # Rooms shared between replicas (collab.sync); None keeps them in memory.
                    sync=build_sync(services.settings, services.database),
                ),
                services,
                "collab",
            ),
        )
    if _may_enable(services.settings, "mcp"):
        from autoair.ai.config import ai_settings_of

        ai_cfg = ai_settings_of(services.settings)
        if ai_cfg.service_url and ai_cfg.service.proxy_mcp:
            from autoair.ai.proxy import create_mcp_proxy_app

            # MCP served by the separate AI service, same auth as the API here.
            app.mount("/mcp", _feature_gate(create_mcp_proxy_app(services), services, "mcp"))
        else:
            try:
                from autoair.mcp.http import create_mcp_asgi_app
            except ImportError as exc:  # optional extra: pip install 'autoair[mcp]'
                log.info("MCP endpoint disabled: %s (install autoair[mcp])", exc)
            else:
                # Model Context Protocol endpoint (streamable HTTP), same auth as the API.
                app.mount("/mcp", _feature_gate(create_mcp_asgi_app(services), services, "mcp"))
    plugin_dir = STATIC_DIR / "plugin"
    if plugin_dir.is_dir():
        app.mount("/static", CachedStaticFiles(directory=plugin_dir), name="autoair-static")
    standalone_dir = STATIC_DIR / "standalone"
    if (standalone_dir / "assets").is_dir():
        app.mount(
            "/ui/assets",
            CachedStaticFiles(directory=standalone_dir / "assets"),
            name="autoair-assets",
        )

    # root_path is set by the mounting server (e.g. "/autoair" inside Airflow).
    @app.get("/", include_in_schema=False)
    def _root(request: Request) -> RedirectResponse:
        return RedirectResponse(url=f"{request.scope.get('root_path', '')}/ui/")

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/{_path:path}", include_in_schema=False)
    def _ui(request: Request, _path: str = "") -> HTMLResponse:
        api_base = f"{request.scope.get('root_path', '')}/api"
        ui_title = services.settings.ui.title
        page = _index_html(api_base, ui_title if ui_title != "AutoAir" else None)
        if page is None:
            return HTMLResponse(
                "<h1>AutoAir</h1><p>Frontend bundle not built. Run "
                "<code>npm run build</code> in <code>frontend/</code>.</p>",
                status_code=503,
            )
        headers = {
            "Cache-Control": "no-cache",
            # CSP allowing exactly the injected configuration script (by hash)
            **platform.page_headers(services.settings, [_config_script(api_base)]),
        }
        return HTMLResponse(page, headers=headers)

    return app
