"""Request-scoped dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request

from autoair.api.app import Services
from autoair.auth import forwarded_credentials
from autoair.gateway import AirflowError, AirflowGateway
from autoair.validation import Policy, policy_from_settings


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_gateway(request: Request, services: ServicesDep) -> AsyncIterator[AirflowGateway]:
    headers, cookies = forwarded_credentials(request)
    async with AirflowGateway(
        services.settings.airflow_url,
        headers=headers,
        cookies=cookies,
        timeout=services.settings.airflow_timeout,
        fetch_concurrency=services.settings.server.airflow_fetch_concurrency,
        page_size=services.settings.server.airflow_page_size,
        max_entries=services.settings.server.airflow_list_max_entries,
    ) as gw:
        yield gw


GatewayDep = Annotated[AirflowGateway, Depends(get_gateway)]


def policy_of(services: Services) -> Policy:
    return policy_from_settings(services.settings)


def http_error(exc: AirflowError) -> HTTPException:
    status = exc.status if 400 <= exc.status < 600 else 502
    return HTTPException(status_code=status, detail=exc.detail)


EnvironmentQuery = Annotated[
    str | None,
    Query(
        max_length=64,
        description="Environment to read (git targets: its branch); default: default_environment.",
    ),
]


def read_environment(services: Any, environment: str | None) -> str | None:
    """The environment a read is for: the requested one, else ``default_environment``."""
    settings = getattr(services, "settings", None)
    env: str | None = environment or getattr(settings, "default_environment", None)
    return env
