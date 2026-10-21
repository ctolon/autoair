"""Outbound URL policy (SSRF protection) for online research and user model endpoints.

A URL is allowed when its scheme is http(s), its host matches the allow-list (domain
suffix match or URL prefix) and - unless private networks are explicitly allowed -
every address the host resolves to is public (not loopback, private, link-local, CGNAT,
multicast, reserved or the cloud metadata address). Redirects are followed manually so
every hop is checked again.

The check alone is racy (DNS rebinding: the name may resolve to a public address for the
check and to a private one for the connection), so outbound clients use
:class:`PinnedAsyncTransport` / :class:`PinnedTransport`: the host is resolved once per
request, every address is validated, and the connection goes to the validated IP with the
original ``Host`` header and TLS SNI / certificate hostname.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx2

from autoair.net import tls


class UrlNotAllowed(ValueError):
    """The URL violates the outbound policy."""


class BlockedAddress(httpx2.ConnectError):
    """Raised by the pinned transports: the host resolves to a non-public address. A
    transport error (so HTTP clients surface it like an unreachable host) that callers
    should not retry."""


_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def host_matches(host: str, domains: Iterable[str]) -> bool:
    """``host`` equals a domain or is a sub-domain of it (case-insensitive)."""
    host = host.lower().rstrip(".")
    for domain in domains:
        d = domain.lower().strip().lstrip("*.").rstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def prefix_matches(url: str, prefixes: Iterable[str]) -> bool:
    """``url`` starts with an allowed prefix at a path boundary."""
    for prefix in prefixes:
        p = prefix.rstrip("/")
        if p and (url == p or url.startswith(p + "/") or url.startswith(p + "?")):
            return True
    return False


def split_checked(url: str, schemes: Sequence[str] = ("https", "http")) -> tuple[str, str]:
    parts = urlsplit(url)
    if parts.scheme not in schemes:
        raise UrlNotAllowed(f"scheme {parts.scheme or '(none)'!r} is not allowed")
    if not parts.hostname:
        raise UrlNotAllowed("URL has no host")
    if parts.username or parts.password:
        raise UrlNotAllowed("credentials in URLs are not allowed")
    return parts.scheme, parts.hostname


def _check_addresses(host: str, addresses: Iterable[str]) -> None:
    addrs = list(addresses)
    if not addrs:
        raise UrlNotAllowed(f"cannot resolve {host!r}")
    bad = [a for a in addrs if not is_public_ip(a)]
    if bad:
        raise UrlNotAllowed(f"{host!r} resolves to a non-public address ({bad[0]})")


async def resolve(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UrlNotAllowed(f"cannot resolve {host!r}: {exc}") from exc
    return sorted({str(info[4][0]) for info in infos})


async def check_url(
    url: str,
    *,
    domains: Sequence[str] = (),
    prefixes: Sequence[str] = (),
    allow_private: bool = False,
    schemes: Sequence[str] = ("https", "http"),
) -> None:
    """Raise :class:`UrlNotAllowed` unless ``url`` satisfies the policy. With neither
    ``domains`` nor ``prefixes`` every public host is allowed."""
    _, host = split_checked(url, schemes)
    if (domains or prefixes) and not (host_matches(host, domains) or prefix_matches(url, prefixes)):
        raise UrlNotAllowed(f"{host!r} is not in the allow-list")
    if not allow_private:
        _check_addresses(host, await resolve(host))


def check_url_sync(
    url: str,
    *,
    domains: Sequence[str] = (),
    prefixes: Sequence[str] = (),
    allow_private: bool = False,
    schemes: Sequence[str] = ("https", "http"),
) -> None:
    """Blocking variant of :func:`check_url` (for worker threads)."""
    _, host = split_checked(url, schemes)
    if (domains or prefixes) and not (host_matches(host, domains) or prefix_matches(url, prefixes)):
        raise UrlNotAllowed(f"{host!r} is not in the allow-list")
    if allow_private:
        return
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            addrs = sorted({str(i[4][0]) for i in socket.getaddrinfo(host, None)})
        except OSError as exc:
            raise UrlNotAllowed(f"cannot resolve {host!r}: {exc}") from exc
    _check_addresses(host, addrs)


# --------------------------------------------------------------------------- pinned transports


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _ordered(infos: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.append(addr)
    return seen


async def _agetaddrinfo(host: str, port: int | None) -> list[Any]:
    loop = asyncio.get_running_loop()
    return list(await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM))


def _getaddrinfo(host: str, port: int | None) -> list[Any]:
    return list(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))


def _pin(request: httpx2.Request, host: str, addresses: list[str]) -> None:
    try:
        _check_addresses(host, addresses)
    except UrlNotAllowed as exc:
        raise BlockedAddress(str(exc), request=request) from None
    if _ip_literal(host):
        return
    # The Host header was set from the original URL when the request was built; TLS
    # verifies the certificate against ``sni_hostname`` (httpcore2), not the IP.
    request.headers.setdefault("host", request.url.netloc.decode("ascii"))
    request.extensions = {**request.extensions, "sni_hostname": host}
    request.url = request.url.copy_with(host=addresses[0])


class PinnedAsyncTransport(httpx2.AsyncBaseTransport):
    """Resolve the host once, refuse non-public addresses and connect to the checked IP."""

    def __init__(self, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self._inner = (
            inner if inner is not None else httpx2.AsyncHTTPTransport(**tls.transport_kwargs("ai"))
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        if _ip_literal(host):
            addresses = [host]
        else:
            try:
                infos = await _agetaddrinfo(host, request.url.port)
            except OSError as exc:
                raise httpx2.ConnectError(
                    f"cannot resolve {host!r}: {exc}", request=request
                ) from exc
            addresses = _ordered(infos)
        _pin(request, host, addresses)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class PinnedTransport(httpx2.BaseTransport):
    """Blocking variant of :class:`PinnedAsyncTransport`."""

    def __init__(self, inner: httpx2.BaseTransport | None = None) -> None:
        self._inner = (
            inner if inner is not None else httpx2.HTTPTransport(**tls.transport_kwargs("ai"))
        )

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        if _ip_literal(host):
            addresses = [host]
        else:
            try:
                infos = _getaddrinfo(host, request.url.port)
            except OSError as exc:
                raise httpx2.ConnectError(
                    f"cannot resolve {host!r}: {exc}", request=request
                ) from exc
            addresses = _ordered(infos)
        _pin(request, host, addresses)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


__all__ = [
    "BlockedAddress",
    "PinnedAsyncTransport",
    "PinnedTransport",
    "UrlNotAllowed",
    "check_url",
    "check_url_sync",
    "host_matches",
    "is_public_ip",
    "prefix_matches",
    "resolve",
    "split_checked",
]
