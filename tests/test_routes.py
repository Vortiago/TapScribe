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
from conftest import (
    TranscriberStub,  # type: ignore[import-not-found]  # NeMo ships an installed `tests` package — collides with our project's tests/ dir; pytest puts tests/ on sys.path so `from conftest` resolves correctly
)
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
# /api/models
# ---------------------------------------------------------------------------


def test_api_models_default_context_is_batch(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert body["context"] == "batch"
    assert isinstance(body["available_backends"], list)
    assert isinstance(body["models"], list)
    # Batch context includes Parakeet + Canary (the new families).
    ids = {m["model_id"] for m in body["models"]}
    assert "parakeet-tdt-0.6b-v3" in ids
    assert "canary-1b-v2" in ids


def test_api_models_live_context_excludes_parakeet_and_canary(client):
    r = client.get("/api/models?context=live")
    assert r.status_code == 200
    body = r.json()
    assert body["context"] == "live"
    ids = {m["model_id"] for m in body["models"]}
    assert "parakeet-tdt-0.6b-v3" not in ids
    assert "canary-1b-v2" not in ids
    # Whisper variants ARE live-eligible.
    assert "tiny.en" in ids


def test_api_models_rejects_unknown_context(client):
    r = client.get("/api/models?context=transcode")
    assert r.status_code == 400


def test_api_models_emits_select_input_for_canary(client):
    r = client.get("/api/models")
    canary = next(m for m in r.json()["models"] if m["model_id"] == "canary-1b-v2")
    inputs_by_name = {i["name"]: i for i in canary["inputs"]}
    assert inputs_by_name["source_lang"]["type"] == "select"
    assert inputs_by_name["target_lang"]["type"] == "select"
    # English option present with the right value.
    opts = inputs_by_name["source_lang"]["options"]
    assert any(o["value"] == "en" and o["label"] == "English" for o in opts)


def test_api_models_emits_text_inputs_for_whisper(client):
    r = client.get("/api/models")
    whisper = next(m for m in r.json()["models"] if m["model_id"] == "small.en")
    names = {i["name"] for i in whisper["inputs"]}
    assert names == {"initial_prompt", "hotwords"}


def test_api_models_emits_no_inputs_for_parakeet(client):
    r = client.get("/api/models")
    pk = next(m for m in r.json()["models"] if m["model_id"] == "parakeet-tdt-0.6b-v3")
    assert pk["inputs"] == []


def test_api_state_carries_backend_preference_and_available_backends(client):
    r = client.get("/api/state")
    body = r.json()
    assert "backend" in body
    assert "available_backends" in body
    assert isinstance(body["available_backends"], list)


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


# ---------------------------------------------------------------------------
# /api/sessions/{target}/absorb
# ---------------------------------------------------------------------------


def _seed_session(root: Path, name: str, wavs: list[str]) -> Path:
    sd = root / name
    sd.mkdir(parents=True)
    for w in wavs:
        _seed_wav(sd / w)
    return sd


def test_absorb_moves_wavs_and_sidecars_and_deletes_source(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    target = _seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    source = _seed_session(
        root,
        "src",
        [
            "20260101T010000Z__alice__def.wav",
            "20260101T010500Z__bob__ghi.wav",
        ],
    )
    # Drop a sidecar on one source WAV so we can verify it follows.
    (source / "20260101T010000Z__alice__def.wav").with_suffix(".json").write_text("{}")

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["wavs_moved"] == 2
    assert body["stripped_moved"] == 0

    assert not source.exists()
    moved = sorted(p.name for p in target.glob("*.wav"))
    assert moved == [
        "20260101T000000Z__alice__abc.wav",
        "20260101T010000Z__alice__def.wav",
        "20260101T010500Z__bob__ghi.wav",
    ]
    assert (target / "20260101T010000Z__alice__def.json").is_file()


def test_absorb_moves_stripped_subfolder(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    target = _seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    source = _seed_session(root, "src", ["20260101T010000Z__alice__def.wav"])
    (source / "stripped").mkdir()
    _seed_wav(source / "stripped" / "20260101T010000Z__alice__def.wav")

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text
    assert r.json()["stripped_moved"] == 1
    assert (target / "stripped" / "20260101T010000Z__alice__def.wav").is_file()
    assert not source.exists()


def test_absorb_merges_aliases_with_target_winning(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", [])
    _seed_session(root, "src", [])
    client.put(
        "/api/session-meta/tgt",
        json={
            "label": "kickoff",
            "aliases": {"alice": "Alice T", "shared": "Target Says"},
        },
    )
    client.put(
        "/api/session-meta/src",
        json={
            "label": "ignored",
            "aliases": {"bob": "Bob S", "shared": "Source Says"},
        },
    )

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text
    assert set(r.json()["aliases_added"]) == {"bob"}

    meta = client.get("/api/session-meta/tgt").json()
    assert meta["label"] == "kickoff"
    assert meta["aliases"] == {"alice": "Alice T", "bob": "Bob S", "shared": "Target Says"}


def test_absorb_invalidates_target_merged_transcript(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    target = _seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    _seed_session(root, "src", ["20260101T010000Z__alice__def.wav"])
    (target / "session-transcript.json").write_text('{"stale": true}')

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text
    assert r.json()["transcript_invalidated"] is True
    assert not (target / "session-transcript.json").exists()


def test_absorb_refuses_when_source_is_current_session(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", [])
    cur = recorder_under_test.session_start
    _seed_session(root, cur, [])
    r = client.post("/api/sessions/tgt/absorb", json={"source": cur})
    assert r.status_code == 409
    assert "current session" in r.json()["detail"]


def test_absorb_allows_target_to_be_current_session(client, recorder_under_test):
    """The whole point of merge-after-restart: roll a previous session
    into the new one the operator is recording into right now."""
    root = recorder_under_test.recordings_dir
    cur = recorder_under_test.session_start
    _seed_session(root, cur, [])
    _seed_session(root, "prev", ["20260101T010000Z__alice__def.wav"])
    r = client.post(f"/api/sessions/{cur}/absorb", json={"source": "prev"})
    assert r.status_code == 200, r.text
    assert (root / cur / "20260101T010000Z__alice__def.wav").is_file()


def test_absorb_refuses_self(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", [])
    r = client.post("/api/sessions/tgt/absorb", json={"source": "tgt"})
    assert r.status_code == 400


def test_absorb_refuses_missing_source(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", [])
    r = client.post("/api/sessions/tgt/absorb", json={"source": "nope"})
    assert r.status_code == 404


def test_absorb_refuses_filename_collision(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    src = _seed_session(root, "src", ["20260101T000000Z__alice__abc.wav"])
    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 409
    # Source must be untouched on a refused merge.
    assert src.exists()
    assert (src / "20260101T000000Z__alice__abc.wav").is_file()


def test_api_state_files_row_lists_all_cached_transcripts(client, recorder_under_test):
    """The dashboard's per-WAV picker needs to know what's cached. The
    `transcripts` field on each file row enumerates every (backend, model)
    sidecar with an `is_primary` flag so the UI can render a switcher."""
    from tapscribe.wav_cache import cached_transcribe, set_primary_transcript

    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T010000Z__alice__abc.wav"])
    wav = sd / "20260101T010000Z__alice__abc.wav"

    cached_transcribe(
        wav,
        TranscriberStub(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    cached_transcribe(
        wav,
        TranscriberStub(backend="mlx-voxtral", model="voxtral-mini"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    set_primary_transcript(wav, backend="faster-whisper", model="small.en")

    body = client.get("/api/state").json()
    s = next(s for s in body["sessions"] if s["session"] == "s")
    row = next(f for f in s["files"] if f["name"] == wav.name)
    listing = row.get("transcripts")
    assert listing is not None and len(listing) == 2
    by_key = {(t["backend"], t["model"]): t for t in listing}
    assert ("faster-whisper", "small.en") in by_key
    assert ("mlx-voxtral", "voxtral-mini") in by_key
    assert by_key[("faster-whisper", "small.en")]["is_primary"] is True
    assert by_key[("mlx-voxtral", "voxtral-mini")]["is_primary"] is False


def test_api_set_primary_flips_pointer(client, recorder_under_test):
    """PUT /api/wav/{session}/{name}/primary points the merge layer at a
    different cached transcript without re-running anything."""
    from tapscribe.wav_cache import cached_transcribe, read_cached

    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T010000Z__alice__abc.wav"])
    wav = sd / "20260101T010000Z__alice__abc.wav"
    cached_transcribe(
        wav,
        TranscriberStub(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    cached_transcribe(
        wav,
        TranscriberStub(backend="mlx-voxtral", model="voxtral-mini"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    # voxtral is the default primary (newest write) — flip to whisper.
    r = client.put(
        "/api/wav/s/20260101T010000Z__alice__abc.wav/primary",
        json={"backend": "faster-whisper", "model": "small.en"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["primary"] == {"backend": "faster-whisper", "model": "small.en"}
    primary = read_cached(wav)
    assert primary is not None
    assert primary.result.backend == "faster-whisper"


def test_api_set_primary_422_for_uncached_combo(client, recorder_under_test):
    from tapscribe.wav_cache import cached_transcribe

    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T010000Z__alice__abc.wav"])
    wav = sd / "20260101T010000Z__alice__abc.wav"
    cached_transcribe(
        wav,
        TranscriberStub(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    r = client.put(
        "/api/wav/s/20260101T010000Z__alice__abc.wav/primary",
        json={"backend": "mlx-voxtral", "model": "voxtral-mini"},
    )
    assert r.status_code == 422


def test_api_set_primary_404_for_missing_wav(client, recorder_under_test):
    _seed_session(recorder_under_test.recordings_dir, "s", [])
    r = client.put(
        "/api/wav/s/missing.wav/primary",
        json={"backend": "x", "model": "y"},
    )
    assert r.status_code == 404


def test_api_state_files_row_lists_single_entry_for_legacy_sidecar(client, recorder_under_test):
    """A WAV with only a legacy `<wav>.json` sidecar should still surface
    a one-element `transcripts` list so the UI can render it consistently."""
    from tapscribe.wav_cache import cached_transcribe

    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T010000Z__alice__abc.wav"])
    wav = sd / "20260101T010000Z__alice__abc.wav"
    cached_transcribe(
        wav,
        TranscriberStub(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    body = client.get("/api/state").json()
    s = next(s for s in body["sessions"] if s["session"] == "s")
    row = next(f for f in s["files"] if f["name"] == wav.name)
    assert row["transcripts"] == [{"backend": "faster-whisper", "model": "small.en", "is_primary": True}]


def test_api_transcribe_returns_freshly_written_transcript(client, recorder_under_test, monkeypatch):
    """The single-WAV transcribe route writes a new sidecar via the
    cache and returns the wire JSON. With the multi-cache layout there
    is no `<wav>.json` to read back; the route must serve the primary
    that cached_transcribe just promoted."""
    fake = TranscriberStub(backend="fake-backend", model="fake-small.en", text="route transcript")
    # Patch both the canonical binding and the local rebinding in app.py
    # (which does `from .transcribers import load_transcriber` at module
    # load, so a later patch on the source package wouldn't reach it).
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.app.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T010000Z__alice__abc.wav"])

    r = client.post(
        "/api/transcribe",
        json={"session": "s", "name": "20260101T010000Z__alice__abc.wav", "model": "fake-small.en"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "route transcript"
    assert body["backend"] == "fake-backend"
    assert body["model"] == "fake-small.en"
    # Sidecar lives in the new layout, not at <wav>.json.
    wav = sd / "20260101T010000Z__alice__abc.wav"
    assert not wav.with_suffix(".json").is_file()
    assert wav.with_suffix(".transcripts").is_dir()


def test_api_state_files_row_surfaces_primary_transcript(client, recorder_under_test):
    """The dashboard reads each WAV's transcript out of /api/state's
    `sessions[*].files[*].transcript`. With the new multi-cache layout,
    that field must surface the *primary* transcript so flipping the
    primary on disk shows up on the next poll."""
    from tapscribe.wav_cache import cached_transcribe, set_primary_transcript

    root = recorder_under_test.recordings_dir
    session = _seed_session(root, "s", ["20260101T010000Z__alice__abc.wav"])
    wav = session / "20260101T010000Z__alice__abc.wav"

    cached_transcribe(
        wav,
        TranscriberStub(backend="faster-whisper", model="small.en", text="whisper text"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    cached_transcribe(
        wav,
        TranscriberStub(backend="mlx-voxtral", model="voxtral-mini", text="voxtral text"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    # Default primary is the most-recent write (voxtral).
    body = client.get("/api/state").json()
    s = next(s for s in body["sessions"] if s["session"] == "s")
    file_row = next(f for f in s["files"] if f["name"] == wav.name)
    assert file_row["transcript"] is not None
    assert file_row["transcript"]["text"] == "voxtral text"
    assert file_row["transcript"]["backend"] == "mlx-voxtral"

    # Flip primary back to whisper; the dashboard sees the change.
    set_primary_transcript(wav, backend="faster-whisper", model="small.en")
    body = client.get("/api/state").json()
    s = next(s for s in body["sessions"] if s["session"] == "s")
    file_row = next(f for f in s["files"] if f["name"] == wav.name)
    assert file_row["transcript"]["text"] == "whisper text"
    assert file_row["transcript"]["backend"] == "faster-whisper"


def test_absorb_moves_new_layout_transcripts_directory(client, recorder_under_test):
    """The source WAV may have multiple cached transcripts under the new
    `<wav>.transcripts/` layout. Absorb must move that directory into
    the target alongside the WAV."""
    from tapscribe.wav_cache import cached_transcribe, read_all_cached

    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    source = _seed_session(root, "src", ["20260101T010000Z__alice__def.wav"])
    src_wav = source / "20260101T010000Z__alice__def.wav"

    # Seed two cached transcripts via the cache API.
    cached_transcribe(
        src_wav,
        TranscriberStub(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    cached_transcribe(
        src_wav,
        TranscriberStub(backend="mlx-voxtral", model="voxtral-mini"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text

    moved_wav = root / "tgt" / "20260101T010000Z__alice__def.wav"
    assert moved_wav.is_file()
    entries = read_all_cached(moved_wav)
    backends_models = {(e.result.backend, e.result.model) for e in entries}
    assert backends_models == {
        ("faster-whisper", "small.en"),
        ("mlx-voxtral", "voxtral-mini"),
    }


def test_absorb_refuses_when_job_in_flight(client, recorder_under_test):
    import asyncio
    from datetime import datetime
    from datetime import timezone as _tz

    from tapscribe.recorder import JobState

    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", [])
    _seed_session(root, "src", [])
    asyncio.get_event_loop().run_until_complete(
        recorder_under_test.jobs.claim(
            JobState(
                session="src",
                kind="transcribe",
                current=0,
                total=1,
                started_at=datetime.now(_tz.utc),
            )
        )
    )
    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 409
