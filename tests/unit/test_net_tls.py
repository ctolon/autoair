"""autoair.net.tls: one place for TLS/proxy options of every outgoing client."""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

import httpx2
import pytest

from autoair import sections
from autoair.net import tls
from autoair.sections import OutboundSettings


def activate(**values: object) -> OutboundSettings:
    from autoair.settings import Settings

    settings = Settings(outbound=values)  # type: ignore[call-arg]
    sections.activate(settings)
    tls.reset()
    return settings.outbound


def test_defaults_keep_httpx_behaviour() -> None:
    activate()
    kwargs = tls.httpx_kwargs("airflow")
    assert kwargs == {"verify": True, "trust_env": True}
    assert tls.git_config() == []
    assert tls.git_env() == {}


def test_verification_off_warns_loudly(caplog: pytest.LogCaptureFixture) -> None:
    activate(verify=False)
    with caplog.at_level(logging.WARNING, logger="autoair.net.tls"):
        assert tls.httpx_kwargs("git")["verify"] is False
        tls.httpx_kwargs("git")
    warnings = [r for r in caplog.records if "DISABLED" in r.getMessage()]
    assert len(warnings) == 1  # once per client
    assert ("http.sslVerify", "false") in tls.git_config()


def test_ca_bundle_builds_one_shared_context() -> None:
    import certifi

    activate(ca_bundle=certifi.where())
    first = tls.httpx_kwargs("marketplace")["verify"]
    assert isinstance(first, ssl.SSLContext)
    assert first.verify_mode == ssl.CERT_REQUIRED
    assert tls.httpx_kwargs("ai")["verify"] is first  # cached and shared
    assert ("http.sslCAInfo", certifi.where()) in tls.git_config()
    # a real client accepts the context
    with httpx2.Client(**tls.httpx_kwargs("marketplace")):
        pass


def test_client_certificate(tmp_path: Path) -> None:
    crypto = pytest.importorskip("cryptography")
    del crypto
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "autoair-client")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "client.pem"
    key_file = tmp_path / "client.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    activate(client_cert=str(cert_file), client_key=str(key_file))
    ctx = tls.httpx_kwargs("airflow")["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    config = dict(tls.git_config())
    assert config["http.sslCert"] == str(cert_file)
    assert config["http.sslKey"] == str(key_file)


def test_proxies_and_no_proxy() -> None:
    activate(
        http_proxy="http://proxy:3128",
        https_proxy="http://proxy:3129",
        no_proxy=["localhost", ".internal", "10.0.0.1"],
    )
    kwargs = tls.httpx_kwargs("ai")
    mounts = kwargs["mounts"]
    assert {"http://", "https://", "all://localhost", "all://*.internal", "all://10.0.0.1"} <= set(
        mounts
    )
    assert kwargs["trust_env"] is False  # explicit proxies win over the environment
    # an explicit transport (tests, pinned transports) is kept: no proxy mounts
    assert "mounts" not in tls.httpx_kwargs("ai", transport=httpx2.MockTransport(lambda r: None))
    assert tls.proxy_for("research", "https://docs.example.com/x") == "http://proxy:3129"
    assert tls.proxy_for("research", "https://api.internal/x") is None
    assert ("http.proxy", "http://proxy:3129") in tls.git_config()
    assert tls.git_env()["no_proxy"] == "localhost,.internal,10.0.0.1"
    with httpx2.Client(**kwargs):
        pass
    client = httpx2.AsyncClient(**tls.httpx_kwargs("ai", asynchronous=True))
    assert client is not None


def test_per_client_overrides() -> None:
    activate(clients={"git": {"verify": False, "proxy": "http://git-proxy:8080"}})
    assert tls.httpx_kwargs("git")["verify"] is False
    assert tls.httpx_kwargs("airflow")["verify"] is True
    assert tls.proxy_for("git", "https://github.com") == "http://git-proxy:8080"
    assert tls.proxy_for("airflow", "https://airflow") is None


def test_local_git_provider_gets_tls_and_token_config(tmp_path: Path) -> None:
    import certifi

    from autoair.storage.git.local_git import LocalGitProvider

    activate(ca_bundle=certifi.where(), no_proxy=["git.internal"], trust_env=False)
    provider = LocalGitProvider("https://git.internal/x.git", tmp_path, token="s3cret")
    env = provider._env()
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs["http.sslCAInfo"] == certifi.where()
    assert pairs["http.extraHeader"].startswith("Authorization: Basic ")
    assert env["NO_PROXY"] == "git.internal"
