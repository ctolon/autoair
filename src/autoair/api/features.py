"""Feature flags on the API: routes of a disabled feature answer 404.

The routes stay in the OpenAPI document (the generated client does not depend on the
configuration); ``/api/meta`` reports the effective flags so the UI hides what is off.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request


def feature_enabled(request: Request, name: str) -> bool:
    services = request.app.state.services
    return bool(services.settings.feature(name))


def feature_disabled(name: str) -> HTTPException:
    """The 404 a route (or the part of a route) of a disabled feature answers."""
    return HTTPException(
        status_code=404,
        detail=f"the {name} feature is disabled (AUTOAIR_FEATURES__{name.upper()})",
    )


def ensure_feature(request: Request, name: str) -> None:
    """Raise :func:`feature_disabled` while ``features.<name>`` is off (in a route body)."""
    if not feature_enabled(request, name):
        raise feature_disabled(name)


def requires_feature(name: str) -> Any:
    """Dependency answering 404 while ``features.<name>`` (``AUTOAIR_FEATURES__<NAME>``) is off."""

    def check(request: Request) -> None:
        ensure_feature(request, name)

    check.__name__ = f"requires_feature_{name}"
    return Depends(check)


__all__ = ["ensure_feature", "feature_disabled", "feature_enabled", "requires_feature"]
