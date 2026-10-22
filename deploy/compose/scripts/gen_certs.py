"""One-shot: a throw-away demo CA and one server certificate for the tls profile.

Writes ca.pem, server.pem (leaf + CA chain) and server.key into the given folder, readable by
the Airflow image user (uid 50000) and nginx. Existing files are kept, so restarts reuse them.
SANs cover the compose service names and localhost. Uses `cryptography` (an AutoAir dependency).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NAMES = ["localhost", "tls-proxy", "autoair-tls", "autoair-a", "autoair-b"]
DAYS = 30


def _name(cn: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AutoAir demo (untrusted)"),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if (out / "server.pem").exists() and (out / "ca.pem").exists():
        print(f"certificates in {out} already exist")
        return
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("AutoAir demo CA"))
        .issuer_name(_name("AutoAir demo CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.DNSName(n) for n in NAMES]
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        # Python >= 3.13 verifies strictly (VERIFY_X509_STRICT): key identifiers are required.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    (out / "ca.pem").write_bytes(ca.public_bytes(pem))
    (out / "server.pem").write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    (out / "server.key").write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    for f in ("ca.pem", "server.pem", "server.key"):
        os.chown(out / f, 50000, 0)
    # Owner only: the AutoAir replica reads it as the airflow user (50000), and the nginx master
    # process reads it as root, which needs no group or other bits.
    os.chmod(out / "server.key", 0o600)
    print(f"demo CA and server certificate written to {out} (valid {DAYS} days)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/certs"))
