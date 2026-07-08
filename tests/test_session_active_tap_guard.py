"""RED contract for #195: destructive session/WAV routes must refuse a
session that has a live tap open on it, the same as they already refuse the
current session and a busy job.

A DETACHED session (a Bridge's bracketed meeting, routed via /tap?session=)
is never the "current" session and has no job while recording, so today
DELETE /api/sessions/{session}, DELETE /api/sessions/{session}/audio,
POST /api/sessions/{target}/absorb, and DELETE /api/wav/{session}/{name} all
happily rmtree/remove files out from under an actively-writing tap.

Registers an ActiveStream carrying the session it writes into (via the
async ActiveStreams.register, driven from these sync tests the same way
test_routes.py's inflight-job tests drive JobTracker.claim) and asserts each
route now refuses with 409 instead of touching the file/dir. A companion
test pins that the guard is scoped to the SESSION actually being modified —
an active tap on an unrelated session must not block these routes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio.from_thread
import pytest
from conftest import repoint_config_files  # type: ignore[import-not-found]
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import ActiveStream, Recorder


def _register_active_tap(recorder: Recorder, session: str, conn_id: str) -> None:
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder.streams.register,
            ActiveStream(
                conn_id=conn_id,
                identity="alice",
                name="Alice",
                filename="20260101T000000Z__alice__abc.wav",
                started_at=datetime.now(UTC),
                session=session,
            ),
        )


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Build a Recorder rooted at tmp_path. Disables auth + auto-start
    so the lifespan doesn't try to spawn whisperlivekit-server. Mirrors
    test_routes.py's fixture of the same name."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
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


def test_session_delete_refuses_active_tap(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", ["20260101T000000Z__alice__abc.wav"])
    _register_active_tap(recorder_under_test, "detached", "conn-1")

    r = client.delete("/api/sessions/detached")

    assert r.status_code == 409
    assert sd.is_dir()  # not rmtree'd out from under the live tap


def test_delete_session_audio_refuses_active_tap(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", ["20260101T000000Z__alice__abc.wav"])
    _register_active_tap(recorder_under_test, "detached", "conn-1")

    r = client.delete("/api/sessions/detached/audio")

    assert r.status_code == 409
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()


def test_delete_wav_refuses_active_tap(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", ["20260101T000000Z__alice__abc.wav"])
    _register_active_tap(recorder_under_test, "detached", "conn-1")

    r = client.delete("/api/wav/detached/20260101T000000Z__alice__abc.wav")

    assert r.status_code == 409
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()


def test_absorb_refuses_active_tap_on_source(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    src = seed_session(root, "src", ["20260101T000000Z__alice__abc.wav"])
    seed_session(root, "target", [])
    _register_active_tap(recorder_under_test, "src", "conn-1")

    r = client.post("/api/sessions/target/absorb", json={"source": "src"})

    assert r.status_code == 409
    assert src.is_dir()  # source not absorbed/deleted out from under the tap
    assert (src / "20260101T000000Z__alice__abc.wav").is_file()


def test_session_delete_allows_when_active_tap_is_on_a_different_session(client, recorder_under_test):
    """A live tap writing into an unrelated session must not block this
    session's delete — the guard must key off the SESSION the tap is
    actually writing into, not merely "some tap is active somewhere"."""
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", ["20260101T000000Z__alice__abc.wav"])
    _register_active_tap(recorder_under_test, "other-session", "conn-1")

    r = client.delete("/api/sessions/detached")

    assert r.status_code == 200
    assert not sd.exists()
