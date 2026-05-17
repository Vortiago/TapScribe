"""Self-signed TLS cert generation for the --tls boot flag.

Generates an RSA 2048 / SHA-256 cert + key pair when the operator wants
wss:// without supplying their own. Files persist next to .auth-password
so browser-trust prompts only fire once per machine. Re-uses an existing
cert as long as it parses and hasn't expired.

The cert lists `localhost`, `127.0.0.1`, `::1`, and the bind host as
SubjectAltNames so it works for both LAN clients and the loopback
dashboard. There is no CA chain — operators reach across an untrusted
network should still front this with a real cert (LetsEncrypt /
tailscale-funnel / SSH tunnel); the self-signed default is the
"localhost / trusted LAN" baseline.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class TlsPair:
    cert_file: Path
    key_file: Path


def _alt_names(host: str) -> list[x509.GeneralName]:
    """Build SAN entries for the cert. Always includes localhost +
    loopback IPs; adds the bind host (DNS or IP) when it's something
    different."""
    names: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    host = (host or "").strip()
    if not host or host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return names
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        names.append(x509.DNSName(host))
    return names


def _generate_pair(cert_path: Path, key_path: Path, host: str) -> None:
    """Write a fresh self-signed cert/key pair to disk (PEM, 0600 on the key)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "TapScribe self-signed"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TapScribe"),
        ]
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(_alt_names(host)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    # Best-effort 0600 on the key; harmless on Windows.
    for p in (cert_path, key_path):
        try:
            os.chmod(p, 0o600)
        except (OSError, NotImplementedError):
            pass


def _looks_valid(cert_path: Path) -> bool:
    """True when `cert_path` parses as PEM and hasn't expired yet. We do
    NOT require the SANs to match the current bind host — re-binding
    later (localhost → LAN IP) shouldn't force a regen on every boot."""
    try:
        data = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(data)
    except (OSError, ValueError):
        return False
    not_after = (
        cert.not_valid_after_utc
        if hasattr(cert, "not_valid_after_utc")
        else cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
    )
    return not_after > _dt.datetime.now(_dt.timezone.utc)


def ensure_self_signed_cert(cert_path: Path, key_path: Path, *, host: str) -> TlsPair:
    """Return a usable cert/key pair. Generates one if either file is
    missing or the cert has expired; reuses an existing valid one so the
    browser trust prompt only fires once per machine."""
    if cert_path.is_file() and key_path.is_file() and _looks_valid(cert_path):
        return TlsPair(cert_file=cert_path, key_file=key_path)
    _generate_pair(cert_path, key_path, host)
    return TlsPair(cert_file=cert_path, key_file=key_path)
