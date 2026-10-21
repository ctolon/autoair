"""Mounting of the AI API (pydantic-ai free).

* ``features.ai`` off: only ``GET /ai/status`` (``enabled=false``); other AI routes 404.
* ``ai.service_url`` set: ``/api/ai/*`` is proxied to the separate AI service, which needs
  no AI runtime in this process.
* otherwise the in-process AI routes (:mod:`autoair.api.routers.ai`) - when the
  ``autoair[ai]`` extra is installed. Without it only ``GET /ai/status`` answers, with the
  install hint as ``reason``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from autoair.ai.access import ADMIN_FLAG, EDITOR_FLAG
from autoair.api.deps import ServicesDep
from autoair.auth import AuthBackend, permission_flag


class AIStatus(BaseModel):
    configured: bool
    enabled: bool = Field(default=True, description="False when features.ai is off.")
    reason: str | None = Field(
        default=None,
        description="Why AI is off or unconfigured (e.g. the autoair[ai] extra is missing).",
    )
    provider: str | None = None
    model: str | None = None
    models: list[str] = Field(default_factory=list, description="Models selectable per request.")
    features: list[str] = Field(default_factory=list)
    retrieval: Literal["bm25", "hybrid"] = "bm25"
    reranker: str = "none"
    llm_mode: Literal["central", "per_user", "both"] = "central"
    llm_source: Literal["central", "user", "none"] = Field(
        default="none", description="Which model answers this user's requests."
    )
    user_model_configured: bool = False
    runtime: str = Field(default="pydantic-ai", description="Agent runtime.")
    streaming: bool = True
    research_online: bool = False
    writers: list[str] = Field(default_factory=list)
    role: Literal["viewer", "editor", "admin"] = "viewer"


def _status_router(status: AIStatus) -> APIRouter:
    router = APIRouter(prefix="/ai", tags=["ai"])

    @router.get("/status", operation_id="aiStatusDisabled", include_in_schema=False)
    async def ai_status_off() -> AIStatus:
        return status

    return router


def _locked(settings: Any, name: str) -> bool:
    """Feature ``name`` cannot be switched at runtime (``features_locked`` / environment)."""
    lock = getattr(settings, "feature_lock", None)
    return not callable(lock) or lock(name) is not None


def _runtime_status_router(status: Any) -> APIRouter:
    """``/ai/status`` while ``features.ai`` can change at runtime: ``status`` (the real
    endpoint) when the feature is on, ``enabled: false`` when an admin switched it off."""
    router = APIRouter(prefix="/ai", tags=["ai"])

    @router.get("/status", operation_id="aiStatusRuntime", include_in_schema=False)
    async def ai_status_runtime(request: Request, services: ServicesDep) -> Any:
        if not services.settings.feature("ai"):
            return AIStatus(configured=False, enabled=False)
        return await status(request, services)

    return router


def ai_dependencies(auth: AuthBackend) -> list[Any]:
    """Router dependencies recording the caller's AI role (editor / admin flags)."""
    return [
        permission_flag(auth.write_dag, EDITOR_FLAG),
        permission_flag(auth.admin, ADMIN_FLAG),
    ]


def include_ai(parent: APIRouter, services: Any, *, dependencies: Sequence[Any] = ()) -> None:
    """Mount the AI API: in-process, or a proxy to ``ai.service_url`` (see the module)."""
    from autoair.ai.availability import ai_unavailable_reason
    from autoair.ai.config import activate_ai, ai_feature_enabled, ai_settings_of
    from autoair.ai.prompts import configure
    from autoair.api.features import requires_feature

    if not ai_feature_enabled(services.settings) and _locked(services.settings, "ai"):
        # Off for good (features_locked / AUTOAIR_FEATURES__AI): nothing else is mounted.
        parent.include_router(
            _status_router(AIStatus(configured=False, enabled=False)),
            dependencies=list(dependencies),
        )
        return
    ai = ai_settings_of(services.settings)
    configure(ai.prompts.override_dir)
    activate_ai(services.settings)
    deps = [*dependencies, *ai_dependencies(services.auth)]
    # features.ai may be switched at runtime (admin panel): checked per request.
    gated = [*deps, requires_feature("ai")]
    if ai.service_url:
        from autoair.ai.proxy import create_proxy_router

        parent.include_router(create_proxy_router(services), dependencies=gated)
        return
    reason = ai_unavailable_reason()
    if reason is not None:
        parent.include_router(
            _status_router(AIStatus(configured=False, enabled=False, reason=reason)),
            dependencies=list(dependencies),
        )
        return
    from autoair.api.routers.ai import ai_status, include_ai_routers

    # Registered first: answers /ai/status whether features.ai is on or off right now.
    parent.include_router(_runtime_status_router(ai_status), dependencies=deps)
    include_ai_routers(parent, services, gated)


__all__ = ["AIStatus", "ai_dependencies", "include_ai"]
