"""``/api/admin/features``: switch feature flags at runtime (admin panel "Features").

Reading needs ``admin:panel`` (the admin router's permission); changing a flag needs
``admin:config``. Overrides are stored in the persistence layer
(:mod:`autoair.feature_overrides`) and picked up by every replica within
``features_override_ttl`` seconds; each change is audited (``feature_override``: who, the
old and new value, the reason). Precedence: locked (``features_locked`` or the
environment) > runtime override > configured value.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.deps import ServicesDep
from autoair.audit import request_user
from autoair.iam import model as P
from autoair.iam.deps import PrincipalDep, require
from autoair.settings import FeatureFlags, Settings

router = APIRouter(prefix="/admin/features", tags=["admin"])

LockReason = Literal["features_locked", "env"]


class FeatureFlagState(BaseModel):
    name: str
    description: str = ""
    configured: bool = Field(description="Value from the configuration (files, env, defaults).")
    override: bool | None = Field(
        default=None, description="Runtime override set in the admin panel (None: none)."
    )
    effective: bool = Field(description="What the server obeys now (as /api/meta reports).")
    locked: LockReason | None = Field(
        default=None,
        description="Why admins cannot change it: listed in features_locked, or set through "
        "the environment (AUTOAIR_FEATURES__<NAME>).",
    )
    constrained: bool = Field(
        default=False,
        description="Kept off by another setting (allow_python_code) or "
        "by the installation (see unavailable) whatever the flag says.",
    )
    unavailable: str | None = Field(
        default=None,
        description="Why the feature cannot run here (e.g. the autoair[ai] extra is not "
        "installed); it cannot be switched on.",
    )
    updated_by: str | None = None
    updated_at: str | None = None
    reason: str | None = None


class FeatureFlagsView(BaseModel):
    flags: list[FeatureFlagState]
    can_write: bool = Field(description="The caller has admin:config.")
    refresh_seconds: float = Field(
        description="features_override_ttl: how long other replicas may take to pick up a change."
    )
    shared: bool = Field(
        description="Overrides are stored in a SQL database shared by the replicas (false: "
        "files in this deployment's state folder)."
    )


class FeatureOverrideRequest(BaseModel):
    value: bool | None = Field(description="New value; null removes the runtime override.")
    reason: str = Field(min_length=3, max_length=500, description="Why (audited).")


def _view(settings: Settings, can_write: bool) -> FeatureFlagsView:
    store = settings.feature_overrides
    doc = store.document(fresh=True) if store is not None else None
    configured = settings.configured_features(overrides=False)
    with_overrides = settings.configured_features()
    effective = settings.effective_features()
    flags: list[FeatureFlagState] = []
    for name, info in FeatureFlags.model_fields.items():
        entry = doc.overrides.get(name) if doc is not None else None
        flags.append(
            FeatureFlagState(
                name=name,
                description=info.description or "",
                configured=configured[name],
                override=entry.value if entry is not None else None,
                effective=effective[name],
                locked=settings.feature_lock(name),
                constrained=with_overrides[name] and not effective[name],
                unavailable=settings.feature_unavailable(name),
                updated_by=entry.user if entry is not None else None,
                updated_at=entry.updated_at if entry is not None else None,
                reason=entry.reason if entry is not None else None,
            )
        )
    return FeatureFlagsView(
        flags=flags,
        can_write=can_write,
        refresh_seconds=settings.features_override_ttl,
        shared=settings.database.is_sql,
    )


@router.get("", operation_id="adminFeatures")
async def admin_features(services: ServicesDep, principal: PrincipalDep) -> FeatureFlagsView:
    """Every feature flag: configured value, runtime override, effective value, lock."""
    can_write = principal.has(P.ADMIN_CONFIG)
    return await run_in_threadpool(_view, services.settings, can_write)


@router.put(
    "/{name}",
    operation_id="adminSetFeature",
    dependencies=[require(P.ADMIN_CONFIG)],
    responses={404: {"description": "Unknown flag"}, 409: {"description": "Flag is locked"}},
)
async def admin_set_feature(
    name: Annotated[str, Path(max_length=64)],
    body: FeatureOverrideRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    request: Request,
) -> FeatureFlagsView:
    """Set (``value``) or remove (``null``) the runtime override of one flag."""
    settings = services.settings
    if name not in FeatureFlags.model_fields:
        raise HTTPException(status_code=404, detail=f"unknown feature {name!r}")
    lock = settings.feature_lock(name)
    if lock is not None:
        where = (
            "features_locked" if lock == "features_locked" else f"AUTOAIR_FEATURES__{name.upper()}"
        )
        raise HTTPException(
            status_code=409,
            detail=f"the {name} feature is locked by the server configuration ({where})",
        )
    unavailable = settings.feature_unavailable(name)
    if body.value and unavailable is not None:
        raise HTTPException(status_code=409, detail=f"{name} cannot be switched on: {unavailable}")
    store = settings.feature_overrides
    if store is None:
        raise HTTPException(status_code=503, detail="runtime feature overrides are unavailable")
    if body.value is not None:
        pending = {**settings.configured_features(), name: body.value}
        if not (pending["visual_editor"] or pending["code_editor"]):
            raise HTTPException(
                status_code=422,
                detail="at least one of visual_editor and code_editor must stay on",
            )
    reason = body.reason.strip()
    if len(reason) < 3:
        raise HTTPException(status_code=422, detail="give a reason (at least 3 characters)")
    user = principal.user or request_user(request)
    old_effective = settings.feature(name)

    def apply() -> tuple[bool | None, bool | None]:
        previous, new = store.set(name, body.value, user=user, reason=reason)
        return (
            previous.value if previous is not None else None,
            new.value if new is not None else None,
        )

    old_override, new_override = await run_in_threadpool(apply)
    new_effective = settings.feature(name)
    await run_in_threadpool(
        services.audit.record,
        "feature_override",
        user=user,
        feature=name,
        old=old_effective,
        new=new_effective,
        old_override=old_override,
        new_override=new_override,
        reason=reason,
    )
    return await run_in_threadpool(_view, settings, True)


__all__ = ["router"]
