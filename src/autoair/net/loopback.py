"""DNS-rebinding protection for servers that run without authentication on loopback.

``autoair serve --no-auth``, ``mcp-serve --http --no-auth``, ``ai-serve --no-auth`` and a
token-less ``autoair local`` trust whoever reaches the port. Binding to loopback keeps
other machines out, but a web page in the user's own browser can still reach
``127.0.0.1`` through a DNS name that resolves there (DNS rebinding) or with a cross-site
request. :class:`LoopbackGuard` therefore accepts a request only when its ``Host`` (and its
``Origin``, when the browser sends one) names a loopback address or the configured public
URL.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback(host: str) -> bool:
    """``host`` (a bind address or host name, IPv6 with or without brackets) is loopback."""
    name = host.strip().lower().removeprefix("[").removesuffix("]")
    if name in LOOPBACK_NAMES or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _hostname(value: str) -> str | None:
    """Host name of a ``Host`` header value (``name[:port]``, ``[v6][:port]``)."""
    try:
        name = urlsplit(f"//{value.strip()}").hostname
    except ValueError:
        return None
    return name.lower() if name else None


def _origin_hostname(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower()


class LoopbackGuard:
    """ASGI middleware: 403 for HTTP requests and WebSockets whose ``Host`` or ``Origin`` is
    neither loopback nor one of ``extra_hosts`` (host names of configured public URLs)."""

    def __init__(self, app: ASGIApp, *, extra_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.extra_hosts = frozenset(h.lower() for h in extra_hosts if h)

    def allowed(self, name: str | None) -> bool:
        return name is not None and (name in self.extra_hosts or is_loopback(name))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        ok = self.allowed(_hostname(headers.get("host", "")))
        origin = headers.get("origin")
        if ok and origin is not None:
            ok = self.allowed(_origin_hostname(origin))
        if ok:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b"Forbidden: unauthenticated AutoAir servers only answer loopback requests"
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def public_hosts(*urls: str | None) -> list[str]:
    """Host names of the configured public URLs (``server.public_url``…), for ``extra_hosts``."""
    out: list[str] = []
    for url in urls:
        name = _origin_hostname(url) if url else None
        if name:
            out.append(name)
    return out


def guard_unauthenticated(app: ASGIApp, *public_urls: str | None) -> LoopbackGuard:
    """``app`` behind :class:`LoopbackGuard` (loopback plus the public URLs' hosts)."""
    return LoopbackGuard(app, extra_hosts=public_hosts(*public_urls))


def refuse_public_bind(host: str, what: str) -> str | None:
    """Error message when an unauthenticated server would bind to a non-loopback address."""
    if is_loopback(host):
        return None
    return (
        f"{what}: refusing to serve {host} without authentication; bind to 127.0.0.1 "
        "(or configure authentication)"
    )
