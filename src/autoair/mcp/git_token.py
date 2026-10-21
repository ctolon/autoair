"""Git tokens typed by the user for MCP writes (``storage.git.mcp_request_token``).

Off by default. When on, MCP over HTTP takes the user's git token from the
``X-AutoAir-Git-Token`` header **of the MCP request** (set in the MCP client's server
configuration, like ``Authorization``) for ``save_dag_spec`` and ``delete_dag_spec``.

Why a header and not a tool argument: tool arguments are written by the model and travel
through the agent — they end up in the LLM context, the agent's transcript, MCP client
tool-call logs and any proxy that records calls. A header is set once in the client's
transport configuration, never seen by the model, and is handled exactly like the REST
API's header (:mod:`autoair.storage.request_token`): same policy (``request_token``
off/optional/required, the forced-connection rule, HTTPS or loopback only), same masking
(:mod:`autoair.secret_mask`) of tool errors, results, logs and audit records. Over stdio
there is no header, so no request token (stdio runs as the local user anyway).
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError

from autoair.storage.base import DeployTarget
from autoair.storage.request_token import (
    HEADER,
    RequestTokenError,
    accept,
    build_request_target,
    root_settings,
)


def _http_request() -> Any:
    try:
        from fastmcp.server.dependencies import get_http_request

        return get_http_request()
    except RuntimeError:  # stdio: no HTTP request
        return None


def request_git_token() -> str | None:
    """The ``X-AutoAir-Git-Token`` header of the MCP HTTP request (``None`` over stdio)."""
    request = _http_request()
    if request is None:
        return None
    value = request.headers.get(HEADER)
    return str(value) if value is not None else None


def mcp_tokens_allowed(services: Any) -> bool:
    """``storage.git.mcp_request_token`` on the default target and the configured one."""
    try:
        cfg = services.settings.storage.git
        return bool(cfg.mcp_request_token and root_settings(services).mcp_request_token)
    except AttributeError:
        return False


def off_error() -> ToolError:
    return ToolError(
        f"{HEADER} is not accepted by MCP tools here (storage.git.mcp_request_token is "
        "false): remove the header, or deploy from the AutoAir UI"
    )


def mcp_writer(services: Any, raw: str | None) -> DeployTarget | None:
    """The throw-away target an MCP write with the header's token goes through (``None``:
    no token, the configured target is used). Raises :class:`ToolError` (never echoing the
    token) when the token cannot be used here."""
    if raw is None:
        return None
    if not mcp_tokens_allowed(services):
        raise off_error()
    storage = services.settings.storage
    request = _http_request()
    try:
        token = accept(
            raw,
            kind=storage.mode,
            cfg=storage.git,
            root=root_settings(services),
            scheme=request.url.scheme if request is not None else "",
            client_host=request.client.host if request is not None and request.client else None,
        )
        assert token is not None
        return build_request_target(storage.git, token)
    except RequestTokenError as exc:
        raise ToolError(exc.message) from None


__all__ = ["mcp_tokens_allowed", "mcp_writer", "off_error", "request_git_token"]
