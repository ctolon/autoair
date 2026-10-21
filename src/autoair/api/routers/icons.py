from __future__ import annotations

import base64
import binascii
import hashlib

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.deps import ServicesDep
from autoair.api.features import requires_feature
from autoair.icons import (
    ICON_ID_PATTERN,
    IconAssignments,
    IconError,
    IconInfo,
    IconStore,
)

read_router = APIRouter(prefix="/icons", tags=["icons"])
write_router = APIRouter(prefix="/icons", tags=["icons"])
# Image files are public: <img> tags cannot send the bearer token used in standalone mode,
# and icons (integration logos, small user icons) are not sensitive.
public_router = APIRouter(prefix="/icons", tags=["icons"])
_CUSTOM = requires_feature("custom_icons")

# Bundled SVG logos are trusted, but never let an icon URL run script if opened directly.
_SAFE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


MAX_UPLOAD_CEILING = 64 * 1024 * 1024


class IconUpload(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="Display name (slugified).")
    data: str = Field(
        # Schema ceiling; AUTOAIR_ICONS__MAX_UPLOAD_BYTES is checked before decoding.
        max_length=(MAX_UPLOAD_CEILING * 4) // 3 + 256,
        description="Base64 PNG/JPEG/WebP/GIF (the UI sends a resized 64 px PNG).",
    )


class IconAssignment(BaseModel):
    operator: str | None = Field(default=None, description="Operator class FQN.")
    provider: str | None = None
    icon: str | None = Field(default=None, pattern=ICON_ID_PATTERN, description="None clears.")


def _store(services: ServicesDep) -> IconStore:
    if services.icons is None:
        raise HTTPException(status_code=404, detail="icons are not configured")
    return services.icons


@read_router.get("", operation_id="listIcons")
async def list_icons(services: ServicesDep) -> list[IconInfo]:
    """Bundled integration logos and custom icons."""
    return await run_in_threadpool(_store(services).list)


@read_router.get("/assignments", operation_id="getIconAssignments")
async def get_assignments(services: ServicesDep) -> IconAssignments:
    return await run_in_threadpool(_store(services).assignments)


@public_router.get(
    "/{icon_id}",
    operation_id="getIcon",
    response_class=Response,
    responses={200: {"content": {"image/png": {}, "image/svg+xml": {}}}},
)
async def get_icon(icon_id: str, services: ServicesDep, request: Request) -> Response:
    found = await run_in_threadpool(_store(services).read, icon_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"icon {icon_id!r} not found")
    data, media_type = found
    # Content-addressed validator: a replaced icon (same id, new bytes) is never served
    # stale, and unchanged ones revalidate with a 304 instead of a download.
    etag = f'"{hashlib.sha256(data).hexdigest()[:32]}"'
    cache = "public, max-age=86400" if icon_id.startswith("builtin:") else "no-cache"
    headers = {**_SAFE_HEADERS, "Cache-Control": cache, "ETag": etag}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=media_type, headers=headers)


@write_router.post("", operation_id="uploadIcon", dependencies=[_CUSTOM])
async def upload_icon(body: IconUpload, services: ServicesDep) -> IconInfo:
    """Store a custom icon (re-encoded server-side as a small PNG)."""
    limit = services.settings.icons.max_upload_bytes
    if len(body.data) > (limit * 4) // 3 + 256:  # base64 of the limit plus a data-URL prefix
        raise HTTPException(status_code=422, detail=f"icon is larger than {limit // 1024} KB")
    try:
        raw = base64.b64decode(body.data.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="data is not valid base64") from exc
    try:
        return await run_in_threadpool(_store(services).save, body.name, raw)
    except IconError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@write_router.delete("/{icon_id}", operation_id="deleteIcon", dependencies=[_CUSTOM])
async def delete_icon(icon_id: str, services: ServicesDep) -> dict[str, bool]:
    try:
        return {"deleted": await run_in_threadpool(_store(services).delete, icon_id)}
    except IconError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@write_router.put("/assignments", operation_id="assignIcon")
async def assign_icon(body: IconAssignment, services: ServicesDep) -> IconAssignments:
    """Default icon for every node of an operator class (or a whole provider)."""
    store = _store(services)
    try:
        return await run_in_threadpool(
            lambda: store.assign(operator=body.operator, provider=body.provider, icon=body.icon)
        )
    except IconError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
