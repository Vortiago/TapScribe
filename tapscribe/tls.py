"""Self-signed TLS cert generation for the --tls boot flag.

Generates an RSA 2048 / SHA-256 cert + key pair when the operator wants
wss:// without supplying their own. Files persist next to .auth-password
so browser-trust prompts only fire once per machine. Re-uses an existing
cert as long as it parses, hasn't expired, and still matches the key
beside it.

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
from cryptography.exceptions import UnsupportedAlgorithm
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


def _replace_atomically(path: Path, data: bytes) -> None:
    """Write `data` to a sibling temp file (0600 before it is ever named
    `path`, so the key is never briefly world-readable) and `os.replace` it
    into place. Same directory ⇒ the rename is atomic, so a crash mid-write
    can never leave a truncated PEM for the next boot to hand uvicorn."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, 0o600)
        except (OSError, NotImplementedError):
            # Best-effort 0600 tightening only: chmod is a no-op concept on
            # Windows/ACL filesystems (some raise NotImplementedError) and
            # can fail on exotic mounts. The pair is still fully usable —
            # what's lost is only the restrictive permission bits, matching
            # SecretFile's best-effort chmod in recorder.py.
            pass
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _generate_pair(cert_path: Path, key_path: Path, host: str) -> None:
    """Write a fresh self-signed cert/key pair to disk (PEM, 0600 on the key),
    each file swapped in atomically — regeneration fires on the first boot
    after the 365-day cert expires, so this DOES run again on a live install
    and a crash partway through must not leave a half-written pair."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "TapScribe self-signed"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TapScribe"),
        ]
    )
    now = _dt.datetime.now(_dt.UTC)
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
    # Two files can't be swapped in one atomic step, so an interrupted regen
    # can still leave the new key beside the old cert. That's why
    # `_looks_valid` checks the PAIR, not just the cert: the half-done state
    # is detected on the next boot and regenerated, instead of being handed
    # to uvicorn (which dies with "key values mismatch" and cannot recover
    # without the operator deleting the PEMs by hand).
    _replace_atomically(key_path, key_pem)
    _replace_atomically(cert_path, cert_pem)


def _looks_valid(cert_path: Path, key_path: Path) -> bool:
    """True when the cert parses as PEM, hasn't expired, AND the key beside
    it is the one it was signed for. We do NOT require the SANs to match the
    current bind host — re-binding later (localhost → LAN IP) shouldn't force
    a regen on every boot.

    The pair check is what makes a torn regeneration self-healing: uvicorn
    dies with an opaque "key values mismatch" on a mismatched pair and
    nothing on disk looks wrong, so without this the operator has to find
    and delete the PEMs by hand. Public keys are compared as DER
    SubjectPublicKeyInfo rather than `public_numbers()` so an operator-
    supplied non-RSA key (Ed25519 has no public numbers) compares instead of
    raising."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        return False
    not_after = (
        cert.not_valid_after_utc
        if hasattr(cert, "not_valid_after_utc")
        else cert.not_valid_after.replace(tzinfo=_dt.UTC)
    )
    if not_after <= _dt.datetime.now(_dt.UTC):
        return False
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    return cert.public_key().public_bytes(serialization.Encoding.DER, spki) == key.public_key().public_bytes(
        serialization.Encoding.DER, spki
    )


def ensure_self_signed_cert(cert_path: Path, key_path: Path, *, host: str) -> TlsPair:
    """Return a usable cert/key pair. Generates one if either file is
    missing, the cert has expired, or the two don't belong together;
    reuses an existing valid one so the browser trust prompt only fires
    once per machine."""
    if cert_path.is_file() and key_path.is_file() and _looks_valid(cert_path, key_path):
        return TlsPair(cert_file=cert_path, key_file=key_path)
    _generate_pair(cert_path, key_path, host)
    return TlsPair(cert_file=cert_path, key_file=key_path)
