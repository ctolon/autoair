"""TLS and proxy options of every outgoing connection, from ``AUTOAIR_OUTBOUND__*``.

All HTTP clients AutoAir creates (the Airflow gateway and token checks, git hosting APIs,
marketplaces, AI providers, online research, the sign-in proxy) take their options from
:func:`httpx_kwargs`; the git CLI gets the same through :func:`git_config` / :func:`git_env`.

* ``verify``/``ca_bundle``/``client_cert``: one :class:`ssl.SSLContext` per effective
  configuration (cached; contexts are thread-safe to share);
* proxies: ``http_proxy``/``https_proxy`` become httpx2 *mounts*; ``no_proxy`` hosts are
  mounted without a proxy. A client given an explicit ``transport`` (tests, the AI module's
  pinned transports) keeps it: only its TLS context can be applied there;
* ``verify=false`` logs a warning for each client that is built without verification.
"""

from __future__ import annotations

import ipaddress
import logging
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from autoair.sections import OutboundSettings, active

log = logging.getLogger(__name__)

_CONTEXTS: dict[tuple[Any, ...], ssl.SSLContext] = {}
_LOCK = threading.Lock()
_WARNED: set[str] = set()


@dataclass(frozen=True)
class Effective:
    """The outbound options of one client (overrides applied)."""

    verify: bool
    ca_bundle: Path | None
    client_cert: Path | None
    client_key: Path | None
    client_key_password: str | None
    http_proxy: str | None
    https_proxy: str | None
    no_proxy: tuple[str, ...]
    trust_env: bool


def effective(client: str, settings: OutboundSettings | None = None) -> Effective:
    cfg = settings or active(OutboundSettings)
    over = cfg.clients.get(client)  # type: ignore[call-overload]
    proxy = over.proxy if over is not None else None
    return Effective(
        verify=over.verify if over is not None and over.verify is not None else cfg.verify,
        ca_bundle=(over.ca_bundle if over is not None and over.ca_bundle else cfg.ca_bundle),
        client_cert=(
            over.client_cert if over is not None and over.client_cert else cfg.client_cert
        ),
        client_key=(over.client_key if over is not None and over.client_key else cfg.client_key),
        client_key_password=(
            cfg.client_key_password.get_secret_value() if cfg.client_key_password else None
        ),
        http_proxy=proxy or cfg.http_proxy,
        https_proxy=proxy or cfg.https_proxy,
        no_proxy=tuple(cfg.no_proxy),
        trust_env=cfg.trust_env,
    )


def _warn_unverified(client: str) -> None:
    if client in _WARNED:
        return
    _WARNED.add(client)
    log.warning(
        "TLS certificate verification is DISABLED for outgoing %s connections "
        "(AUTOAIR_OUTBOUND__VERIFY=false): traffic can be intercepted. Use ca_bundle instead.",
        client,
    )


def ssl_context(client: str, settings: OutboundSettings | None = None) -> ssl.SSLContext | bool:
    """``verify`` argument for httpx2: ``True`` (defaults), ``False`` (no verification) or a
    context with the configured CA bundle / client certificate."""
    eff = effective(client, settings)
    if not eff.verify:
        _warn_unverified(client)
        if eff.client_cert is None:
            return False
    elif eff.ca_bundle is None and eff.client_cert is None:
        return True
    key = (
        eff.verify,
        str(eff.ca_bundle or ""),
        str(eff.client_cert or ""),
        str(eff.client_key or ""),
        eff.client_key_password,
    )
    with _LOCK:
        ctx = _CONTEXTS.get(key)
        if ctx is not None:
            return ctx
        ctx = _build_context(eff)
        _CONTEXTS[key] = ctx
        return ctx


def _build_context(eff: Effective) -> ssl.SSLContext:
    if eff.ca_bundle is not None:
        path = eff.ca_bundle.expanduser()
        if path.is_dir():
            ctx = ssl.create_default_context(capath=str(path))
        else:
            ctx = ssl.create_default_context(cafile=str(path))
    else:
        # The operating system's trust store, like httpx2 itself (truststore ships with it).
        try:
            import truststore

            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:  # pragma: no cover - truststore ships with httpx2
            ctx = ssl.create_default_context()
    if not eff.verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if eff.client_cert is not None:
        ctx.load_cert_chain(
            str(eff.client_cert.expanduser()),
            str(eff.client_key.expanduser()) if eff.client_key else None,
            eff.client_key_password,
        )
    return ctx


def reset() -> None:
    """Forget cached contexts and warnings (tests, configuration changes)."""
    with _LOCK:
        _CONTEXTS.clear()
        _WARNED.clear()


def _no_proxy_patterns(entries: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        if entry == "*":
            out.append("all://")
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            pass
        else:
            if "/" not in entry:  # httpx2 mounts match single hosts, not networks
                out.append(f"all://{entry}")
            continue
        host = entry.lstrip("*").lstrip(".")
        out.append(f"all://{host}")
        out.append(f"all://*.{host}")
    return out


def httpx_kwargs(
    client: str,
    *,
    transport: object | None = None,
    asynchronous: bool = False,
    settings: OutboundSettings | None = None,
) -> dict[str, Any]:
    """Keyword arguments for ``httpx2.Client``/``httpx2.AsyncClient`` of ``client`` (one of
    airflow, git, marketplace, ai, research, login). With an explicit ``transport`` the
    caller's transport is kept (no proxy mounts)."""
    eff = effective(client, settings)
    verify = ssl_context(client, settings)
    out: dict[str, Any] = {"verify": verify, "trust_env": eff.trust_env}
    if transport is not None or not (eff.http_proxy or eff.https_proxy):
        return out
    factory: Any = httpx2.AsyncHTTPTransport if asynchronous else httpx2.HTTPTransport
    mounts: dict[str, Any] = {}
    if eff.http_proxy:
        mounts["http://"] = factory(proxy=eff.http_proxy, verify=verify)
    if eff.https_proxy:
        mounts["https://"] = factory(proxy=eff.https_proxy, verify=verify)
    for pattern in _no_proxy_patterns(eff.no_proxy):
        mounts[pattern] = factory(verify=verify)
    out["mounts"] = mounts
    # Explicit proxies win over HTTP(S)_PROXY from the environment.
    out["trust_env"] = False
    return out


def transport_kwargs(client: str, settings: OutboundSettings | None = None) -> dict[str, Any]:
    """``verify`` for an ``httpx2.HTTPTransport``/``AsyncHTTPTransport`` built by hand (the
    AI module's pinned transports): the TLS options only, never a proxy."""
    return {"verify": ssl_context(client, settings)}


def async_client(
    client: str, *, settings: OutboundSettings | None = None, **kwargs: Any
) -> httpx2.AsyncClient:
    """An ``httpx2.AsyncClient`` for ``client`` with the outbound TLS/proxy options applied
    (``kwargs`` go to the client; an explicit ``transport`` is kept, see :func:`httpx_kwargs`).
    Explicit ``verify``/``trust_env``/``mounts`` in ``kwargs`` win."""
    options = httpx_kwargs(
        client, transport=kwargs.get("transport"), asynchronous=True, settings=settings
    )
    return httpx2.AsyncClient(**{**options, **kwargs})


def proxy_for(client: str, url: str, settings: OutboundSettings | None = None) -> str | None:
    """The proxy ``url`` goes through (``None``: direct), for clients that take one proxy."""
    eff = effective(client, settings)
    proxy = eff.https_proxy if url.lower().startswith("https:") else eff.http_proxy
    if not proxy:
        return None
    host = httpx2.URL(url).host
    for entry in eff.no_proxy:
        e = entry.strip().lstrip("*").lstrip(".").lower()
        if e == "" or entry.strip() == "*" or host == e or host.endswith("." + e):
            return None
    return proxy


def git_config(settings: OutboundSettings | None = None) -> list[tuple[str, str]]:
    """``git -c`` style options for the git CLI (``http.sslCAInfo``, ``http.sslVerify``,
    ``http.sslCert``/``sslKey``, ``http.proxy``) — passed through ``GIT_CONFIG_*``
    environment variables so nothing shows up in ``argv``."""
    eff = effective("git", settings)
    out: list[tuple[str, str]] = []
    if not eff.verify:
        _warn_unverified("git")
        out.append(("http.sslVerify", "false"))
    if eff.ca_bundle is not None:
        path = eff.ca_bundle.expanduser()
        out.append(("http.sslCAPath" if path.is_dir() else "http.sslCAInfo", str(path)))
    if eff.client_cert is not None:
        out.append(("http.sslCert", str(eff.client_cert.expanduser())))
        if eff.client_key is not None:
            out.append(("http.sslKey", str(eff.client_key.expanduser())))
    proxy = eff.https_proxy or eff.http_proxy
    if proxy:
        out.append(("http.proxy", proxy))
    return out


def git_env(settings: OutboundSettings | None = None) -> dict[str, str]:
    """Environment of git CLI processes: ``no_proxy`` (curl reads it) and, unless
    ``trust_env``, the environment's proxy variables removed."""
    eff = effective("git", settings)
    env: dict[str, str] = {}
    if eff.no_proxy:
        value = ",".join(e.strip() for e in eff.no_proxy if e.strip())
        env["no_proxy"] = env["NO_PROXY"] = value
    return env


def strip_proxy_env(env: dict[str, str]) -> None:
    """Remove proxy variables inherited from the environment (``trust_env=false``)."""
    for name in list(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            env.pop(name)


def trust_env(client: str, settings: OutboundSettings | None = None) -> bool:
    return effective(client, settings).trust_env


__all__ = [
    "Effective",
    "async_client",
    "effective",
    "git_config",
    "git_env",
    "httpx_kwargs",
    "proxy_for",
    "reset",
    "ssl_context",
    "strip_proxy_env",
    "transport_kwargs",
    "trust_env",
]
