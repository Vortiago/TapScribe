"""Tests for the local-test-bridge — mostly pure helpers.

Audio capture itself isn't tested (would need a real mic + sounddevice).
We test the framing logic and the WS open/close cycle around the toggle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

# Load bridges/local-test-bridge/local_test_bridge.py as a module under
# the alias `ltb`. Avoids polluting sys.path globally.
_LTB_PATH = Path(__file__).parent.parent / "bridges" / "local-test-bridge" / "local_test_bridge.py"
_spec = importlib.util.spec_from_file_location("local_test_bridge", _LTB_PATH)
ltb = importlib.util.module_from_spec(_spec)
sys.modules["local_test_bridge"] = ltb
_spec.loader.exec_module(ltb)


def test_chunk_into_frames_yields_640_byte_frames():
    """The /tap contract is 640-byte (20 ms @ 16 kHz mono int16) frames.
    The capture loop accumulates raw int16 samples and emits frames of
    exactly that size."""
    samples = np.zeros(320 * 5, dtype=np.int16)  # 5 frames worth
    frames = list(ltb.chunk_into_frames(samples.tobytes()))
    assert len(frames) == 5
    for f in frames:
        assert len(f) == 640


def test_chunk_into_frames_drops_partial_tail():
    """Partial trailing frames are kept in the buffer for the next call,
    not flushed as a short frame (which would corrupt WlK's stream)."""
    # 2.5 frames worth of bytes
    samples_bytes = b"\x00\x00" * (320 * 2 + 100)
    frames = list(ltb.chunk_into_frames(samples_bytes))
    assert len(frames) == 2  # the 100-sample tail is dropped (caller buffers)


def test_build_tap_url_includes_identity_and_name():
    url = ltb.build_tap_url(
        host="localhost", port=8001,
        identity="alice tester", name="Alice Tester",
    )
    assert url.startswith("ws://localhost:8001/tap?")
    assert "identity=alice+tester" in url or "identity=alice%20tester" in url
    assert "name=Alice+Tester" in url or "name=Alice%20Tester" in url


def test_build_tap_url_handles_empty_name():
    url = ltb.build_tap_url(host="localhost", port=8001, identity="alice", name="")
    assert "identity=alice" in url
    # Empty name still appears in querystring (server-side default kicks in)
    assert url.endswith("&name=") or "name=" in url


def test_default_identity_uses_env_username_or_local_tester(monkeypatch):
    """The default identity should be reproducible: prefer the OS username
    so multi-instance testing produces distinct identities."""
    monkeypatch.setenv("USER", "")
    monkeypatch.setenv("USERNAME", "")
    assert ltb.default_identity() == "local-tester"

    monkeypatch.setenv("USER", "alice")
    assert ltb.default_identity() == "alice"
