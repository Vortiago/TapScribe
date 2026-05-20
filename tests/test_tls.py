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
