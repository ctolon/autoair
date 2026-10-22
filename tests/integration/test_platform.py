"""Server platform settings: limits, rate limits, readiness, metrics, logging, TLS, CORS."""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair.api.platform import RateLimiter, effective_limit, rate_group
from autoair.logging_setup import JsonFormatter, logging_config
from autoair.sections import LimitsSettings, LoggingSettings
from autoair.server import (
    ServeOptions,
    build_app,
    default_ciphers,
    local_preset,
    settings_for,
    tls_context,
    uvicorn_options,
)


def client(tmp_path: Path, **extra: Any) -> TestClient:
    overrides = local_preset(dags_folder=tmp_path / "dags")
    overrides.update(ai={"enabled": False}, import_check_on_deploy=False, **extra)
    token = "autoair-local-test"
    overrides.setdefault("local", {}).setdefault("token", token)
    options = ServeOptions(overrides=overrides, local=True)
    c = TestClient(build_app(options, settings_for(options)), raise_server_exceptions=False)
    if overrides["local"]["token"] == token:
        c.headers["Authorization"] = f"Bearer {token}"
    return c


SPEC = {
    "dag_id": "limited",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def test_rate_groups() -> None:
    assert rate_group("POST", "/validate") == "validate"
    assert rate_group("POST", "/interchange/python/import") == "import"
    assert rate_group("POST", "/interchange/yaml/export") is None
    assert rate_group("POST", "/ai/generate") == "ai"
    assert rate_group("POST", "/runner/test-task") == "test_runs"
    assert rate_group("PUT", "/specs/{dag_id}") == "deploy"
    assert rate_group("GET", "/specs/{dag_id}") is None
    assert rate_group("POST", "/catalog/refresh") == "catalog_refresh"


def test_role_limits() -> None:
    limits = LimitsSettings(
        rate_limits={"ai": 10}, role_rate_limits={"Admin": {"ai": 0}, "Viewer": {"ai": 2}}
    )
    assert effective_limit(limits, "ai", []) == 10
    assert effective_limit(limits, "ai", ["Viewer"]) == 2
    assert effective_limit(limits, "ai", ["Viewer", "Author"]) == 10  # most generous
    assert effective_limit(limits, "ai", ["Viewer", "Admin"]) == 0  # unlimited wins
    limiter = RateLimiter(window=60, max_keys=10)
    assert limiter.hit(("u", "ai"), 2) is None
    assert limiter.hit(("u", "ai"), 2) is None
    wait = limiter.hit(("u", "ai"), 2)
    assert wait is not None
    assert 0 < wait <= 60
    assert limiter.hit(("other", "ai"), 2) is None


def test_validate_is_rate_limited(tmp_path: Path) -> None:
    c = client(tmp_path, limits={"rate_limits": {"validate": 2}})
    statuses = [c.post("/api/validate", json=SPEC).status_code for _ in range(3)]
    assert 429 not in statuses[:2]
    assert statuses[2] == 429
    resp = c.post("/api/validate", json=SPEC)
    assert int(resp.headers["retry-after"]) >= 1
    off = client(
        tmp_path / "off", limits={"rate_limits_enabled": False, "rate_limits": {"validate": 1}}
    )
    assert all(off.post("/api/validate", json=SPEC).status_code != 429 for _ in range(3))


def test_body_limit(tmp_path: Path) -> None:
    c = client(tmp_path, server={"max_body_bytes": 2048})
    big = {**SPEC, "description": "x" * 5000}
    assert c.post("/api/validate", json=big).status_code == 413
    assert c.post("/api/validate", json=SPEC).status_code == 200


def test_metrics_and_readiness_switches(tmp_path: Path) -> None:
    c = client(tmp_path)
    assert c.get("/api/metrics").status_code == 200
    ready = c.get("/api/ready")
    assert ready.status_code == 200
    assert set(ready.json()["checks"]) == {"catalog", "storage"}
    off = client(tmp_path / "off", server={"metrics_enabled": False, "ready_check_storage": False})
    assert off.get("/api/metrics").status_code == 404
    assert set(off.get("/api/ready").json()["checks"]) == {"catalog"}


def test_meta_carries_branding(tmp_path: Path) -> None:
    c = client(
        tmp_path,
        ui={
            "title": "ACME",
            "banner_text": "PRODUCTION",
            "banner_color": "red",
            "default_language": "tr",
            "default_timezone": "Europe/Istanbul",
            "accent_color": "#0055ff",
        },
        server={"public_url": "https://autoair.acme.io"},
    )
    meta = c.get("/api/meta").json()
    assert meta["ui"]["title"] == "ACME"
    assert meta["ui"]["banner_text"] == "PRODUCTION"
    assert meta["ui"]["default_language"] == "tr"
    assert meta["public_url"] == "https://autoair.acme.io"
    with pytest.raises(ValueError, match="accent_color"):
        settings_for(ServeOptions(overrides={"ui": {"accent_color": "url(javascript:x)"}}))


def test_cors_details(tmp_path: Path) -> None:
    c = client(
        tmp_path,
        cors_origins=["https://portal.acme.io"],
        cors_max_age=123,
        cors_allow_methods=["GET"],
    )
    pre = c.options(
        "/api/meta",
        headers={
            "Origin": "https://portal.acme.io",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert pre.headers["access-control-allow-origin"] == "https://portal.acme.io"
    assert pre.headers["access-control-max-age"] == "123"
    assert pre.headers["access-control-allow-methods"] == "GET"


def test_json_logging() -> None:
    cfg = logging_config(LoggingSettings(format="json", levels={"autoair.storage": "debug"}))
    assert cfg["loggers"]["autoair.storage"]["level"] == "DEBUG"
    record = logging.makeLogRecord(
        {"name": "autoair.x", "levelname": "INFO", "msg": "hello %s", "args": ("you",)}
    )
    record.dag_id = "etl"
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "hello you"
    assert line["logger"] == "autoair.x"
    assert line["dag_id"] == "etl"
    with pytest.raises(ValueError, match="unknown log level"):
        LoggingSettings(levels={"x": "LOUD"})


# --------------------------------------------------------------------------- TLS server
def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "server.pem", tmp_path / "server.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


def test_uvicorn_options(tmp_path: Path) -> None:
    cert, key = _self_signed(tmp_path)
    settings = settings_for(
        ServeOptions(
            overrides={
                "server": {
                    "ssl_certfile": str(cert),
                    "ssl_keyfile": str(key),
                    "ssl_min_version": "TLSv1.3",
                    "workers": 3,
                    "forwarded_allow_ips": ["10.0.0.1"],
                    "graceful_shutdown_timeout": 7,
                }
            }
        )
    )
    opts = uvicorn_options(settings, host="127.0.0.1", port=1, context_factory=True)
    assert opts["ssl_certfile"] == str(cert)
    assert opts["workers"] == 3
    assert opts["forwarded_allow_ips"] == ["10.0.0.1"]
    assert opts["timeout_graceful_shutdown"] == 7
    ctx = opts["ssl_context_factory"](
        None, lambda: ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    )
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    bad = settings_for(ServeOptions(overrides={"server": {"ssl_cert_reqs": "required"}}))
    with pytest.raises(ValueError, match="ssl_certfile"):
        uvicorn_options(bad, host="h", port=1)


def test_tls12_offers_modern_ciphers(tmp_path: Path) -> None:
    """uvicorn's default cipher string "TLSv1" left TLS 1.2 only SHA-1 CBC suites, so a
    TLS 1.2 client with Python's defaults could not connect to `autoair serve`."""
    import uvicorn.config

    cert, key = _self_signed(tmp_path)

    def uvicorn_default() -> ssl.SSLContext:
        return uvicorn.config.create_ssl_context(
            str(cert), str(key), None, ssl.PROTOCOL_TLS_SERVER, ssl.CERT_NONE, None, "TLSv1"
        )

    ctx = tls_context(None, uvicorn_default)
    names = {c["name"] for c in ctx.get_ciphers()}
    assert "ECDHE-RSA-AES128-GCM-SHA256" in names
    assert not any(n.endswith("-SHA") for n in names)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_uvicorn_without_a_context_factory(tmp_path: Path) -> None:
    """uvicorn < 0.47 (Airflow 3.0/3.1 constraints) has no ssl_context_factory: passing it
    made `autoair serve` with TLS crash with a TypeError."""
    cert, key = _self_signed(tmp_path)

    def options(**server: Any) -> dict[str, Any]:
        settings = settings_for(
            ServeOptions(
                overrides={"server": {"ssl_certfile": str(cert), "ssl_keyfile": str(key), **server}}
            )
        )
        return uvicorn_options(settings, host="127.0.0.1", port=1, context_factory=False)

    plain = options()
    assert "ssl_context_factory" not in plain
    assert plain["ssl_ciphers"] == default_ciphers()
    assert options(ssl_ciphers="ECDHE+AESGCM")["ssl_ciphers"] == "ECDHE+AESGCM"
    with pytest.raises(ValueError, match=r"uvicorn 0\.47"):
        options(ssl_min_version="TLSv1.3")


def _free_port() -> int:
    for port in range(8830, 8840):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 8830-8839")


def test_https_server_with_tls13_and_hsts(tmp_path: Path) -> None:
    """A real `autoair local` over HTTPS: the outbound helper trusts its certificate
    through a CA bundle (no verify=false), TLS 1.2 clients are refused. uvicorn < 0.47 (no
    ssl_context_factory) serves with the TLS 1.2 minimum instead."""
    from autoair.server import _uvicorn_takes_context_factory

    tls13 = _uvicorn_takes_context_factory()
    cert, key = _self_signed(tmp_path)
    port = _free_port()
    env = {
        **os.environ,
        "AUTOAIR_AI__ENABLED": "false",
        "AUTOAIR_SERVER__SSL_CERTFILE": str(cert),
        "AUTOAIR_SERVER__SSL_KEYFILE": str(key),
        "AUTOAIR_SERVER__SSL_MIN_VERSION": "TLSv1.3" if tls13 else "TLSv1.2",
        "AUTOAIR_SERVER__HSTS_SECONDS": "3600",
        "AUTOAIR_LOGGING__FORMAT": "json",
        "XDG_CACHE_HOME": str(tmp_path),
    }
    env.pop("AUTOAIR_CONFIG_FILE", None)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from autoair.cli import app; app()",
            "local",
            "--port",
            str(port),
            "--dags-folder",
            str(tmp_path / "dags"),
            "--no-open",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"https://127.0.0.1:{port}"
    trusted = ssl.create_default_context(cafile=str(cert))
    try:
        deadline = time.monotonic() + 120
        while True:
            if proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout else ""
                pytest.fail(f"server exited: {out[-2000:]}")
            try:
                resp = httpx2.get(f"{base}/api/health", verify=trusted, timeout=2)
                break
            except httpx2.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        assert resp.status_code == 200
        assert resp.headers["strict-transport-security"] == "max-age=3600"
        old = ssl.create_default_context(cafile=str(cert))
        old.maximum_version = ssl.TLSVersion.TLSv1_2
        if tls13:  # TLS 1.2-only clients are refused
            with pytest.raises(httpx2.ConnectError):
                httpx2.get(f"{base}/api/health", verify=old, timeout=5)
        else:
            assert httpx2.get(f"{base}/api/health", verify=old, timeout=5).status_code == 200
        # an untrusted certificate fails verification (verify stays on by default)
        with pytest.raises(httpx2.ConnectError):
            httpx2.get(f"{base}/api/health", timeout=5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
