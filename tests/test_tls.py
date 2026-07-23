"""Tests for tapscribe.tls — self-signed cert generation for --tls boot."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography import x509  # noqa: E402

from tapscribe.tls import ensure_self_signed_cert  # noqa: E402


def test_generates_cert_and_key_when_missing(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    pair = ensure_self_signed_cert(cert, key, host="localhost")
    assert pair.cert_file == cert
    assert pair.key_file == key
    assert cert.is_file()
    assert key.is_file()
    # Cert parses, validity covers "now"
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    not_after = (
        parsed.not_valid_after_utc if hasattr(parsed, "not_valid_after_utc") else parsed.not_valid_after
    )
    assert not_after > _dt.datetime.now(_dt.UTC).replace(tzinfo=not_after.tzinfo)


def test_reuses_existing_valid_cert(tmp_path: Path):
    """Second call must NOT regenerate (browsers would re-prompt every boot).
    We assert the file's mtime is unchanged."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert, key, host="localhost")
    mtime = cert.stat().st_mtime_ns
    ensure_self_signed_cert(cert, key, host="localhost")
    assert cert.stat().st_mtime_ns == mtime


def test_san_includes_host_and_loopback(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert, key, host="recorder.lan")
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = {n.value for n in san if isinstance(n, x509.DNSName)}
    assert "localhost" in dns_names
    assert "recorder.lan" in dns_names


def test_san_handles_ip_host(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert, key, host="192.168.1.50")
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ip_names = {str(n.value) for n in san if isinstance(n, x509.IPAddress)}
    assert "192.168.1.50" in ip_names


def test_regenerates_when_cert_corrupt(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("not a real cert")
    key.write_text("not a real key")
    pair = ensure_self_signed_cert(cert, key, host="localhost")
    # Now it parses as a real cert
    x509.load_pem_x509_certificate(pair.cert_file.read_bytes())


# ---------------------------------------------------------------------------
# Torn regeneration. The 365-day cert expires, so `_generate_pair` DOES run
# again on a live install — and a crash (Ctrl-C at startup, OOM, service
# restart) between the two PEM writes used to leave a cert that doesn't match
# the key beside it. `_looks_valid` only parsed the cert, so every later boot
# handed uvicorn the mismatched pair and it died with "key values mismatch".
# The oracle below is `ssl.load_cert_chain` — the SAME OpenSSL check uvicorn
# performs, independent of how tls.py decides a pair belongs together.
# ---------------------------------------------------------------------------


def _ssl_accepts(cert: Path, key: Path) -> bool:
    """True when OpenSSL loads the pair — what uvicorn does at boot."""
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    except (ssl.SSLError, OSError, ValueError):
        return False
    return True


def _write_unrelated_key(path: Path) -> None:
    """Overwrite `path` with a freshly generated key that matches nothing —
    exactly what a crash between the key write and the cert write leaves."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    path.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def test_regenerates_when_key_does_not_match_cert(tmp_path: Path):
    """A cert/key mismatch must self-heal on the next boot. Without the pair
    check the operator has to find and delete the PEMs by hand — the server
    refuses to start and nothing on disk looks wrong."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert, key, host="localhost")
    assert _ssl_accepts(cert, key)

    _write_unrelated_key(key)
    assert not _ssl_accepts(cert, key), "tampering didn't actually break the pair"

    ensure_self_signed_cert(cert, key, host="localhost")
    assert _ssl_accepts(cert, key)


def test_failed_regeneration_leaves_the_previous_cert_intact(tmp_path: Path, monkeypatch):
    """A regen that dies partway must never leave a half-written PEM: the
    files swap in via `os.replace`, so the previous cert is either the old
    one whole or the new one whole. Patching `os.replace` is the only way to
    land inside that window — it IS the atomicity mechanism under test."""
    import os as _os

    from tapscribe import tls as tls_mod

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ensure_self_signed_cert(cert, key, host="localhost")
    old_cert = cert.read_bytes()

    real_replace = _os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if Path(dst).name == cert.name:
            raise OSError("simulated crash mid-regeneration")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(tls_mod.os, "replace", failing_replace)
    key.unlink()  # missing key → ensure_self_signed_cert regenerates
    with pytest.raises(OSError):
        ensure_self_signed_cert(cert, key, host="localhost")
    assert cert.read_bytes() == old_cert  # untouched, not truncated

    # …and the next boot (no injected failure) recovers on its own: the key
    # that did land no longer matches, so the pair is regenerated whole.
    monkeypatch.setattr(tls_mod.os, "replace", real_replace)
    ensure_self_signed_cert(cert, key, host="localhost")
    assert _ssl_accepts(cert, key)
    assert not list(tmp_path.glob("*.tmp")), "temp PEM left behind"
