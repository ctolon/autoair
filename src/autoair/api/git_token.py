"""The ``X-AutoAir-Git-Token`` header of git writes (autoair.storage.request_token).

:data:`GitTokenHeader` reads the raw header and registers it with
:mod:`autoair.secret_mask` for the rest of the request (logs, audit records and error
details mask it from then on); :func:`accept_token` checks it against the deploy target's
policy and wraps it in a :class:`~autoair.storage.request_token.RequestToken` that the
route hands explicitly to :func:`~autoair.storage.request_token.build_request_target`.
:func:`scrubbed_errors` turns any failure of a write made with it into an HTTP error whose
detail is masked and that carries no traceback.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request

from autoair.secret_mask import mask, masking
from autoair.storage.base import AuthError, StorageError
from autoair.storage.request_token import (
    HEADER,
    RequestToken,
    RequestTokenError,
    accept,
    root_settings,
)

log = logging.getLogger(__name__)


async def _git_token_header(
    x_autoair_git_token: Annotated[
        str | None,
        Header(
            alias=HEADER,
            description="Git token for this one write (storage.git.request_token): used "
            "instead of the configured credentials, never stored or logged. HTTPS only.",
        ),
    ] = None,
) -> AsyncIterator[str | None]:
    with masking(x_autoair_git_token.strip() if x_autoair_git_token else None):
        yield x_autoair_git_token


GitTokenHeader = Annotated[str | None, Depends(_git_token_header)]


def http_error(exc: RequestTokenError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail())


def accept_token(
    services: Any, request: Request, raw: str | None, *, kind: str, cfg: Any
) -> RequestToken | None:
    """The request's token for a write to a target of ``kind`` with git settings ``cfg``
    (HTTP 400/403 when the header cannot be used here)."""
    try:
        return accept(
            raw,
            kind=kind,
            cfg=cfg,
            root=root_settings(services),
            scheme=request.url.scheme,
            client_host=request.client.host if request.client else None,
        )
    except RequestTokenError as exc:
        raise http_error(exc) from None


def rejected(exc: AuthError) -> HTTPException:
    """The git host refused the user's token (401 unknown/expired, 403 missing access)."""
    return HTTPException(
        status_code=exc.status,
        detail={
            "code": "git_token_rejected",
            "message": "the git host rejected your token"
            + (" (missing scopes or access to the repository)" if exc.status == 403 else "")
            + f": {mask(str(exc))}",
        },
    )


@contextmanager
def scrubbed_errors(
    token: RequestToken | None, *, passthrough: tuple[type[BaseException], ...] = ()
) -> Iterator[None]:
    """Around a write made with ``token``: the host refusing it is a 401/403 (never
    retried with other credentials); unexpected failures become a masked 502 without the
    original exception attached (no traceback can echo the token). ``passthrough``:
    exceptions the caller handles itself."""
    if token is None:
        yield
        return
    try:
        yield
    except passthrough:
        raise
    except HTTPException as exc:
        if isinstance(exc.detail, str):
            exc.detail = mask(exc.detail)
        raise
    except AuthError as exc:
        raise rejected(exc) from None
    except StorageError:
        raise  # callers turn these into responses (their messages are masked there)
    except Exception as exc:
        text = mask(f"{type(exc).__name__}: {exc}")
        log.error("git write with a request token failed: %s", text)
        raise HTTPException(status_code=502, detail=f"git write failed: {text}") from None


__all__ = ["GitTokenHeader", "accept_token", "http_error", "rejected", "scrubbed_errors"]
