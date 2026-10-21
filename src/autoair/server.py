"""Running the standalone server (``autoair serve``) and the local mode (``autoair local``).

Both build the same FastAPI app (:func:`build_app`) from :class:`ServeOptions` — what the
command line decided on top of the settings — and run it with uvicorn configured from
``server.*``: TLS (certificate, key, mutual TLS, ciphers, minimum version), reverse-proxy
headers, workers, keep-alive, graceful shutdown, access log. With several workers each
process rebuilds the app from the options passed in :data:`OPTIONS_ENV`.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import logging
import os
import ssl
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from autoair.settings import Settings

log = logging.getLogger(__name__)

OPTIONS_ENV = "AUTOAIR__SERVE_OPTIONS"  # internal: options of worker processes
_TLS_VERSIONS = {"TLSv1.2": ssl.TLSVersion.TLSv1_2, "TLSv1.3": ssl.TLSVersion.TLSv1_3}
_CERT_REQS = {"none": ssl.CERT_NONE, "optional": ssl.CERT_OPTIONAL, "required": ssl.CERT_REQUIRED}


@dataclass
class ServeOptions:
    """What the command line adds to the settings (JSON-serialisable for workers)."""

    overrides: dict[str, Any] = field(default_factory=dict)
    no_auth: bool = False
    local: bool = False

    def to_env(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_env(cls, raw: str) -> ServeOptions:
        return cls(**json.loads(raw))


def airflow_installed() -> bool:
    try:
        import airflow.sdk  # noqa: F401
    except ImportError:
        return False
    return True


def local_preset(
    *, dags_folder: Path | None = None, host: str | None = None, port: int | None = None
) -> dict[str, Any]:
    """Settings of ``autoair local``: no Airflow server. The static catalog of installed
    packages, static validation (+ the DagBag import check when Airflow is installed),
    deploys to a local folder (or git, when configured), and the features that need an
    Airflow server (runs, test runs, pickers, connections) switched off."""
    preset: dict[str, Any] = {
        "mode": "local",
        "subfolder": "",
        "dags_folder": str(dags_folder or Path("dags")),
        "features": {"runs": False, "test_runs": False, "airflow": False},
        "import_check_on_deploy": airflow_installed(),
        "dagbag_pool": False,  # one check at a time is plenty on a laptop
        "catalog_strategy": "static",
        "sso": {"enabled": False},
    }
    server: dict[str, Any] = {}
    if host is not None:
        server["host"] = host
    if port is not None:
        server["port"] = port
    if server:
        preset["server"] = server
    return preset


def settings_for(options: ServeOptions) -> Settings:
    from autoair import sections
    from autoair.settings import Settings

    settings = Settings(**options.overrides)
    sections.activate(settings)
    return settings


def build_app(options: ServeOptions, settings: Settings | None = None) -> FastAPI:
    """The app of ``autoair serve`` / ``autoair local``."""
    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend, TokenVerifier, bearer_passthrough, static_token

    settings = settings or settings_for(options)
    auth: AuthBackend
    if options.local:
        token = settings.local.token.get_secret_value() if settings.local.token else None
        auth = static_token(token) if token else AuthBackend(name="none")
    else:
        verifier = TokenVerifier(
            settings.airflow_url, ttl=settings.auth_cache_ttl, timeout=settings.auth_timeout
        )
        auth = bearer_passthrough(verifier, required=not options.no_auth)
    return create_app(build_services(settings, auth))


def app_factory() -> FastAPI:
    """uvicorn ``--factory`` entry of worker processes."""
    from autoair.logging_setup import configure_logging

    options = ServeOptions.from_env(os.environ[OPTIONS_ENV])
    settings = settings_for(options)
    configure_logging(settings.logging)
    return build_app(options, settings)


def tls_context(
    config: Any,
    default: Any,
    *,
    min_version: str = "TLSv1.2",
    ciphers: str | None = None,
) -> ssl.SSLContext:
    """uvicorn ``ssl_context_factory``: uvicorn's context with the minimum TLS version and
    cipher string of ``server.ssl_min_version`` / ``server.ssl_ciphers``."""
    ctx: ssl.SSLContext = default()
    ctx.minimum_version = _TLS_VERSIONS[min_version]
    if ciphers:
        ctx.set_ciphers(ciphers)
    return ctx


def uvicorn_options(settings: Settings, *, host: str, port: int) -> dict[str, Any]:
    """``uvicorn.run``/``uvicorn.Config`` keyword arguments from ``server.*``."""
    server = settings.server
    out: dict[str, Any] = {
        "host": host,
        "port": port,
        "proxy_headers": server.proxy_headers,
        "forwarded_allow_ips": list(server.forwarded_allow_ips),
        "timeout_keep_alive": int(server.keep_alive_timeout),
        "timeout_graceful_shutdown": int(server.graceful_shutdown_timeout),
        "limit_concurrency": server.limit_concurrency,
        "access_log": server.access_log,
        "workers": server.workers,
        "server_header": False,
        "log_config": None,  # configured by autoair.logging_setup
    }
    if server.ssl_certfile:
        out.update(
            ssl_certfile=str(server.ssl_certfile.expanduser()),
            ssl_keyfile=str(server.ssl_keyfile.expanduser()) if server.ssl_keyfile else None,
            ssl_keyfile_password=(
                server.ssl_keyfile_password.get_secret_value()
                if server.ssl_keyfile_password
                else None
            ),
            ssl_ca_certs=str(server.ssl_ca_certs.expanduser()) if server.ssl_ca_certs else None,
            ssl_cert_reqs=_CERT_REQS[server.ssl_cert_reqs],
            ssl_context_factory=functools.partial(
                tls_context, min_version=server.ssl_min_version, ciphers=server.ssl_ciphers
            ),
        )
        if server.ssl_cert_reqs != "none" and not server.ssl_ca_certs:
            raise ValueError("server.ssl_cert_reqs needs server.ssl_ca_certs (mutual TLS)")
    elif server.ssl_cert_reqs != "none" or server.ssl_keyfile:
        raise ValueError("server.ssl_keyfile / ssl_cert_reqs need server.ssl_certfile")
    return out


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _open_when_ready(url: str, health: str, timeout: float = 60) -> None:
    import httpx2

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx2.get(health, timeout=2, verify=False).status_code == 200:  # own local server
                webbrowser.open(url)
                return
        except httpx2.HTTPError:
            pass
        time.sleep(0.5)


def run(
    options: ServeOptions,
    settings: Settings,
    *,
    host: str,
    port: int,
    open_browser: bool = False,
) -> None:
    """Configure logging and run uvicorn (blocking)."""
    import uvicorn

    from autoair.logging_setup import configure_logging

    configure_logging(settings.logging)
    kwargs = uvicorn_options(settings, host=host, port=port)
    scheme = "https" if kwargs.get("ssl_certfile") else "http"
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # noqa: S104 - display only
    if open_browser:
        base = f"{scheme}://{shown}:{port}"
        threading.Thread(
            target=_open_when_ready, args=(f"{base}/ui/", f"{base}/api/health"), daemon=True
        ).start()
    if settings.server.workers > 1:
        os.environ[OPTIONS_ENV] = options.to_env()
        uvicorn.run("autoair.server:app_factory", factory=True, **kwargs)
        return
    kwargs.pop("workers")
    uvicorn.run(build_app(options, settings), **kwargs)


__all__ = [
    "OPTIONS_ENV",
    "ServeOptions",
    "app_factory",
    "build_app",
    "is_loopback",
    "local_preset",
    "run",
    "settings_for",
    "tls_context",
    "uvicorn_options",
]
