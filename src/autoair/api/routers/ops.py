"""Operational endpoints: health, readiness and Prometheus metrics."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

from autoair import __version__
from autoair.api.deps import ServicesDep

router = APIRouter(tags=["ops"])

REGISTRY = CollectorRegistry()
REQUESTS = Counter(
    "autoair_requests_total", "API requests", ["method", "route", "status"], registry=REGISTRY
)
LATENCY = Histogram(
    "autoair_request_seconds",
    "API request latency",
    ["route"],
    registry=REGISTRY,
    buckets=(0.005, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
DEPLOYS = Counter("autoair_deploys_total", "Deploy attempts", ["result"], registry=REGISTRY)
IMPORT_CHECKS = Counter(
    "autoair_import_checks_total",
    "DagBag import checks by where they ran (local pool / validator service) and result "
    "(ok, failed, unavailable)",
    ["backend", "result"],
    registry=REGISTRY,
)


class Health(BaseModel):
    status: str
    version: str


class Readiness(BaseModel):
    ready: bool
    checks: dict[str, str]


@router.get("/health", operation_id="health")
def health() -> Health:
    return Health(status="ok", version=__version__)


@router.get("/ready", operation_id="ready")
def ready(services: ServicesDep, response: Response) -> Readiness:
    """Readiness: the catalog, and (server.ready_check_storage / ready_check_airflow) the
    deploy target and Airflow's health endpoint. 503 when a check fails."""
    server = services.settings.server
    checks: dict[str, str] = {}
    try:
        stats = services.catalog.stats()
        checks["catalog"] = f"ok ({stats.operators} operators)"
    except Exception as exc:
        checks["catalog"] = f"error: {exc}"
    ensure = getattr(services, "ensure_target", None)
    target = ensure() if ensure is not None else getattr(services, "target", None)
    if target is not None and server.ready_check_storage:
        try:
            status = target.status()
            if status.details.get("unavailable"):  # could not be built (see UnavailableTarget)
                checks["storage"] = f"error: {status.details.get('error')}"
            else:
                checks["storage"] = f"ok ({status.kind})" if status.writable else "read-only"
        except Exception as exc:
            checks["storage"] = f"error: {exc}"
    if server.ready_check_airflow and services.settings.mode != "local":
        checks["airflow"] = _airflow_health(services)
    ok = all(v.startswith("ok") for v in checks.values())
    if not ok:
        response.status_code = 503
    return Readiness(ready=ok, checks=checks)


def _airflow_health(services: Any) -> str:
    import httpx2

    from autoair.net import tls

    settings = services.settings
    url = f"{settings.airflow_url.rstrip('/')}/api/v2/monitor/health"
    try:
        with httpx2.Client(
            timeout=settings.server.ready_timeout, **tls.httpx_kwargs("airflow")
        ) as client:
            resp = client.get(url)
    except httpx2.HTTPError as exc:
        return f"error: {type(exc).__name__}"
    return "ok" if resp.status_code == 200 else f"error: HTTP {resp.status_code}"


@router.get("/metrics", operation_id="metrics", response_class=Response)
def metrics(services: ServicesDep) -> Response:
    if not services.settings.server.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics are disabled")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    route = getattr(request.scope.get("route"), "path", "unmatched")
    REQUESTS.labels(request.method, route, str(response.status_code)).inc()
    LATENCY.labels(route).observe(time.perf_counter() - start)
    return response
