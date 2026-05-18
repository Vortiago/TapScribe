"""Route-level integration tests via TestClient.

The Recorder is constructed per-test against a tmpdir and attached to
`app.state.recorder` via dependency override. No subprocess is spawned
(LiveChannel.start is patched out via dependency); no real Transcriber
is loaded.
"""

from __future__ import annotations

import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import ActiveStream, Recorder


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Build a Recorder rooted at tmp_path. Disables auth + auto-start
    so the lifespan doesn't try to spawn whisperlivekit-server."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "config").mkdir()
    (tmp_path / "recordings").mkdir()

    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


@pytest.fixture
def client(recorder_under_test):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_wav(path: Path, *, amplitude: int = 8000, seconds: float = 1.0) -> Path:
    n = int(16000 * seconds)
    samples = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.tobytes())
    return path


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_ok_with_session_dir(client, recorder_under_test):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["session_dir"] == str(recorder_under_test.session_dir)


def test_healthz_returns_documented_shape(client, recorder_under_test):  # noqa: ARG001
    """Liveness/readiness probe shape — keys present, types right.
    Values are not pinned (live channel state can be 'stopped' or
    'starting' depending on lifespan timing)."""
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]
    assert isinstance(body["recording_enabled"], bool)
    assert isinstance(body["live_channel_state"], str)
    assert isinstance(body["active_taps"], int)
    assert body["active_taps"] >= 0


# ---------------------------------------------------------------------------
# /api/recording/toggle
# ---------------------------------------------------------------------------


def test_recording_toggle_without_body_flips_state(client, recorder_under_test):
    assert recorder_under_test.recording_enabled is True
    r = client.post("/api/recording/toggle")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": False}
    assert recorder_under_test.recording_enabled is False
    r = client.post("/api/recording/toggle")
    assert r.json() == {"ok": True, "enabled": True}
    assert recorder_under_test.recording_enabled is True


def test_recording_toggle_with_explicit_enabled(client, recorder_under_test):
    r = client.post("/api/recording/toggle", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert recorder_under_test.recording_enabled is False


# ---------------------------------------------------------------------------
# /api/new-session
# ---------------------------------------------------------------------------


def test_new_session_rotates_recorder_session(client, recorder_under_test):
    prev = recorder_under_test.session_start
    r = client.post("/api/new-session")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["previous"] == prev


# ---------------------------------------------------------------------------
# /api/state
# ---------------------------------------------------------------------------


def test_api_state_returns_recorder_view(client, recorder_under_test):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["current_session"] == recorder_under_test.session_start
    assert body["recording_enabled"] is True
    assert body["mlx_available"] is False
    assert isinstance(body["active"], list)
    assert isinstance(body["sessions"], list)
    assert isinstance(body["live_feed"], list)


def test_api_state_active_rows_include_level_for_the_dashboard_meter(client, recorder_under_test):
    """The dashboard's per-tap volume meter reads `level` off each entry
    in /api/state's `active` list. The JSON contract MUST include the
    field — if a future refactor switches to a manual dict instead of
    asdict() and forgets `level`, the meter silently stops moving
    without any backend error. Pin it explicitly."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        recorder_under_test.streams.register(
            ActiveStream(
                conn_id="abc-meter",
                identity="meter-test",
                name="Meter",
                filename="meter.wav",
                started_at=datetime.now(timezone.utc),
                level=0.73,
            )
        )
    )

    body = client.get("/api/state").json()
    row = next(a for a in body["active"] if a["identity"] == "meter-test")
    assert "level" in row, "/api/state must expose `level` for the dashboard meter"
    assert row["level"] == pytest.approx(0.73)


def test_api_state_active_rows_reflect_current_tap_pref(client, recorder_under_test):
    """The per-row rec/live toggles render their state from the active
    entry's record/live fields. Those must follow the *current*
    per-identity preference (which is what the PUT mutates), not the
    WS-open snapshot — otherwise a click PUTs the new pref but the
    button never visually flips."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        recorder_under_test.streams.register(
            ActiveStream(
                conn_id="abc-bob",
                identity="bob",
                name="Bob",
                filename="bob.wav",
                started_at=datetime.now(timezone.utc),
                record=True,
                live=True,
            )
        )
    )

    recorder_under_test.tap_settings.set("bob", record=False, live=False)

    body = client.get("/api/state").json()
    row = next(a for a in body["active"] if a["identity"] == "bob")
    assert row["record"] is False
    assert row["live"] is False


def test_tap_settings_put_updates_pref(client, recorder_under_test):
    r = client.put("/api/tap-settings", json={"identity": "alice", "record": False})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "identity": "alice", "record": False, "live": True}
    assert recorder_under_test.tap_settings.get("alice").record is False
    assert recorder_under_test.tap_settings.get("alice").live is True

    r = client.put("/api/tap-settings", json={"identity": "alice", "live": False})
    assert r.json()["live"] is False
    # The previous record=False should persist across partial updates.
    assert recorder_under_test.tap_settings.get("alice").record is False


# ---------------------------------------------------------------------------
# /api/live-transcript
# ---------------------------------------------------------------------------


def test_live_transcript_post_endpoint_is_gone(client):
    """Per ADR-0002, the Bridge no longer POSTs settled lines. The
    Recorder consumes them internally via the WlK relay opened by /tap.
    The POST route is gone; only DELETE remains."""
    r = client.post("/api/live-transcript", json={"text": "should not work"})
    assert r.status_code in (404, 405)


def test_live_transcript_clear_empties_feed(client, recorder_under_test):
    recorder_under_test.transcripts.append({"text": "old"})
    r = client.delete("/api/live-transcript")
    assert r.status_code == 200
    assert recorder_under_test.transcripts.snapshot() == []


# ---------------------------------------------------------------------------
# /api/session-meta
# ---------------------------------------------------------------------------


def test_session_meta_get_returns_empty_for_no_overrides(client, tmp_path: Path, recorder_under_test):  # noqa: ARG001
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    r = client.get("/api/session-meta/fakesession")
    assert r.status_code == 200
    assert r.json() == {}


def test_session_meta_put_persists_label(client, recorder_under_test):
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    r = client.put("/api/session-meta/fakesession", json={"label": "kickoff"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["label"] == "kickoff"
    # Read back
    r2 = client.get("/api/session-meta/fakesession")
    assert r2.json()["label"] == "kickoff"


def test_session_meta_404s_for_nonexistent_session(client):
    r = client.get("/api/session-meta/nonexistent")
    assert r.status_code == 404
