"""Route-level integration tests via TestClient.

The Recorder is constructed per-test against a tmpdir and attached to
`app.state.recorder` via dependency override. No subprocess is spawned
(LiveChannel.start is patched out via dependency); no real Transcriber
is loaded.
"""

from __future__ import annotations

import wave
from datetime import UTC, datetime
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


@pytest.fixture(autouse=True)
def _force_all_probes_installed():
    """/api/models filters out registry entries whose adapter modules
    aren't importable; in a CI env that hasn't installed nemo /
    parakeet-mlx / mlx-audio / mlx-voxtral, the catalog assertions in
    this file would all flap. Pretend every probe module is installed
    so the route tests check the JSON shape, not the host's pip state.
    Tests that exercise the filter itself override per-test."""
    from tapscribe.transcribers.catalog import REGISTRY, set_installed_modules_for_testing

    probes = {b.probe_module for e in REGISTRY.entries() for b in e.backends if b.probe_module}
    set_installed_modules_for_testing(frozenset(probes))
    try:
        yield
    finally:
        set_installed_modules_for_testing(None)


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Build a Recorder rooted at tmp_path. Disables auth + auto-start
    so the lifespan doesn't try to spawn whisperlivekit-server."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(_config, "CONFIG_DIR", cfg)
    # The text helpers and /api/state both read these path constants
    # directly — re-bind them to the tmp config dir so the editable-config
    # writes land where the test expects them (and where the recorder
    # under test reads from).
    monkeypatch.setattr(_config, "PROMPT_FILE", cfg / "prompt.txt")
    monkeypatch.setattr(_config, "LIVE_PROMPT_FILE", cfg / "live-prompt.txt")
    monkeypatch.setattr(_config, "HOTWORDS_FILE", cfg / "hotwords.txt")
    monkeypatch.setattr(_config, "HALLUCINATIONS_FILE", cfg / "hallucinations.txt")
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


def test_api_models_hides_families_whose_adapters_are_not_installed(client):
    """The install picker can pull in only some family extras (e.g.
    Whisper but not Parakeet). The /api/models filter mirrors that so
    the dashboard's dropdowns don't advertise models that would fail
    to lazy-import.

    The autouse fixture above resets the install-probe override to
    `None` between tests, so this test only needs to set its own
    simulated install set — no manual restore.
    """
    from tapscribe.transcribers.catalog import set_installed_modules_for_testing

    set_installed_modules_for_testing(frozenset({"faster_whisper", "mlx_whisper"}))
    r = client.get("/api/models?context=batch")
    assert r.status_code == 200
    ids = {m["model_id"] for m in r.json()["models"]}
    assert "tiny.en" in ids  # whisper survives
    assert "nb-whisper-medium" in ids  # nb-whisper rides on faster_whisper
    assert "voxtral-mini" not in ids
    assert "parakeet-tdt-0.6b-v3" not in ids
    assert "canary-1b-v2" not in ids


def test_api_state_carries_backend_preference_and_available_backends(client):
    r = client.get("/api/state")
    body = r.json()
    assert "backend" in body
    assert "available_backends" in body
    assert isinstance(body["available_backends"], list)


# ---------------------------------------------------------------------------
# /api/state — editable prompt + hotwords blocks. The dashboard renders
# textareas for each one, gated by `inputs_support`.
# ---------------------------------------------------------------------------


def test_api_state_includes_live_prompt_block(client, recorder_under_test):  # noqa: ARG001
    """The live channel has its own prompt file (config/live-prompt.txt),
    independent from the batch prompt.txt. /api/state surfaces both so
    the dashboard renders separate editors."""
    r = client.get("/api/state")
    body = r.json()
    assert "live_prompt" in body
    lp = body["live_prompt"]
    assert "path" in lp
    assert lp["path"].endswith("live-prompt.txt")
    assert "content" in lp
    assert "length" in lp


def test_api_state_includes_inputs_support_flags(client):
    """The dashboard hides each editor when no installed model in that
    context declares the corresponding input. /api/state exposes those
    booleans so the JS doesn't need to re-derive them from /api/models."""
    body = client.get("/api/state").json()
    support = body["inputs_support"]
    # Whisper family is always present in test fixture (faster_whisper
    # is in the dev install), so all three should be True.
    assert support["live_prompt"] is True
    assert support["batch_prompt"] is True
    assert support["batch_hotwords"] is True


def test_api_state_inputs_support_hides_when_only_non_supporting_models_installed(client):
    """If the only installed batch families are Voxtral / Parakeet /
    Canary (none declare initial_prompt or hotwords), batch_prompt and
    batch_hotwords are False. Same logic for live: if the only installed
    live family doesn't declare initial_prompt, live_prompt is False."""
    from tapscribe.transcribers.catalog import set_installed_modules_for_testing

    # Pretend only voxtral (mistral_common + mlx_voxtral) is installed.
    # No Whisper family → no initial_prompt / hotwords support anywhere.
    set_installed_modules_for_testing(frozenset({"mistral_common", "mlx_voxtral"}))
    try:
        body = client.get("/api/state").json()
        support = body["inputs_support"]
        assert support["live_prompt"] is False
        assert support["batch_prompt"] is False
        assert support["batch_hotwords"] is False
    finally:
        # The autouse fixture re-installs every probe on teardown, but
        # restore eagerly so the next assertion in this test couldn't
        # leak across.
        set_installed_modules_for_testing(None)


# ---------------------------------------------------------------------------
# PUT /api/config/{key} — dashboard's save button writes prompt /
# live-prompt / hotwords back to disk. Atomic via tempfile + rename so a
# crashed write never leaves a truncated file.
# ---------------------------------------------------------------------------


def test_put_config_prompt_writes_file(client, recorder_under_test):
    r = client.put("/api/config/prompt", json={"content": "Q3 planning · roadmap"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "key": "prompt", "length": len("Q3 planning · roadmap")}
    assert (recorder_under_test.config_dir / "prompt.txt").read_text(
        encoding="utf-8"
    ) == "Q3 planning · roadmap"


def test_put_config_live_prompt_writes_file(client, recorder_under_test):
    r = client.put("/api/config/live-prompt", json={"content": "weekly standup"})
    assert r.status_code == 200
    assert (recorder_under_test.config_dir / "live-prompt.txt").read_text(
        encoding="utf-8"
    ) == "weekly standup"


def test_put_config_hotwords_writes_file(client, recorder_under_test):
    r = client.put("/api/config/hotwords", json={"content": "Acme, Patricia Lin"})
    assert r.status_code == 200
    assert (recorder_under_test.config_dir / "hotwords.txt").read_text(
        encoding="utf-8"
    ) == "Acme, Patricia Lin"


def test_put_config_empty_content_clears_file(client, recorder_under_test):
    (recorder_under_test.config_dir / "prompt.txt").write_text("existing", encoding="utf-8")
    r = client.put("/api/config/prompt", json={"content": ""})
    assert r.status_code == 200
    assert (recorder_under_test.config_dir / "prompt.txt").read_text(encoding="utf-8") == ""


def test_put_config_unknown_key_rejected(client):
    r = client.put("/api/config/halibut", json={"content": "anything"})
    assert r.status_code == 404


def test_put_config_rejects_oversize(client):
    """4000-char cap. Pasting a transcript dump into the prompt field
    should fail loudly at the boundary, not get silently truncated
    downstream where it would surprise the operator with a partial prompt."""
    r = client.put("/api/config/prompt", json={"content": "x" * 5000})
    assert r.status_code == 400


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


def test_new_session_prunes_empty_sessions(client, recorder_under_test):
    """Rotating via the dashboard button now sweeps stale empty sessions,
    while WAV-bearing folders survive."""
    cur = recorder_under_test.session_dir
    cur.mkdir(parents=True, exist_ok=True)
    _seed_wav(cur / "cur.wav")
    empty = recorder_under_test.recordings_dir / "2020-01-01T00-00-00Z"
    empty.mkdir()
    r = client.post("/api/new-session")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "2020-01-01T00-00-00Z" in body["pruned"]["pruned"]
    assert not empty.exists()  # empty session swept
    assert cur.exists()  # WAV-bearing previous session kept


def test_tap_new_session_rotates_without_pruning(client, recorder_under_test):
    """The bridge-facing endpoint rotates (when the current session has audio)
    but — unlike the dashboard — does NOT prune: deleting folders stays a
    Basic-auth action. Auth is disabled in this fixture, so no header needed."""
    cur = recorder_under_test.session_dir
    cur.mkdir(parents=True, exist_ok=True)
    _seed_wav(cur / "cur.wav")
    empty = recorder_under_test.recordings_dir / "2020-01-01T00-00-00Z"
    empty.mkdir()
    prev = recorder_under_test.session_start
    r = client.post("/api/tap/new-session")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rotated"] is True
    # NB: _utc_session_id() has 1s resolution, so a same-second rotation can
    # reuse the id — assert on the rotated flag + previous, not current != prev.
    assert body["previous"] == prev
    assert recorder_under_test.session_start == body["current"]
    assert "pruned" not in body  # tap path never prunes
    assert empty.exists()  # the empty session is NOT deleted by the tap path
    assert cur.exists()


def test_tap_new_session_noop_when_current_empty(client, recorder_under_test):
    """A fresh/empty current session means a tap-initiated rotation is a
    no-op — don't churn the session-id timestamp (and never delete)."""
    empty = recorder_under_test.recordings_dir / "2020-01-01T00-00-00Z"
    empty.mkdir()
    prev = recorder_under_test.session_start
    r = client.post("/api/tap/new-session")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rotated"] is False
    assert body["current"] == prev
    assert recorder_under_test.session_start == prev
    assert "pruned" not in body
    assert empty.exists()  # the tap path leaves empties alone


def test_prune_empty_endpoint_still_works(client, recorder_under_test):
    """Refactor guard: /api/sessions/prune-empty still returns the
    documented shape and preserves labelled sessions."""
    empty = recorder_under_test.recordings_dir / "2020-01-01T00-00-00Z"
    empty.mkdir()
    labelled = recorder_under_test.recordings_dir / "2020-02-02T00-00-00Z"
    labelled.mkdir()
    (labelled / "session-meta.json").write_text('{"label": "keep me"}')
    r = client.post("/api/sessions/prune-empty")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "2020-01-01T00-00-00Z" in body["pruned"]
    assert body["count"] == 1
    assert body["failed"] == []
    assert not empty.exists()
    assert labelled.exists()  # operator label keeps a WAV-less session


# ---------------------------------------------------------------------------
# /api/state
# ---------------------------------------------------------------------------


def test_api_state_runs_gather_sessions_off_event_loop(client, recorder_under_test, monkeypatch):
    """The 500ms /api/state poll walks every session + WAV on disk
    (gather_sessions). If that runs inline on the single-threaded event
    loop, an operator's click POST queues behind the scan and the UI
    feels dead until it finishes. The walk must be offloaded to a worker
    thread (asyncio.to_thread) so the loop stays free.

    Deterministic check: recorder.jobs.snapshot() runs on the event-loop
    thread before the offload, so it pins the loop-thread id;
    gather_sessions must run on a *different* thread."""
    import threading

    from tapscribe.app import gather_sessions as _real_gather

    seen: dict[str, int] = {}

    real_snapshot = recorder_under_test.jobs.snapshot

    def snapshot_spy():
        seen["loop"] = threading.get_ident()
        return real_snapshot()

    monkeypatch.setattr(recorder_under_test.jobs, "snapshot", snapshot_spy)

    def gather_spy(**kw):
        seen["gather"] = threading.get_ident()
        return _real_gather(**kw)

    monkeypatch.setattr("tapscribe.app.gather_sessions", gather_spy)

    assert client.get("/api/state").status_code == 200
    assert seen["gather"] != seen["loop"], (
        "gather_sessions ran on the event-loop thread — /api/state blocks click POSTs during the disk walk"
    )


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
    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.streams.register,
            ActiveStream(
                conn_id="abc-meter",
                identity="meter-test",
                name="Meter",
                filename="meter.wav",
                started_at=datetime.now(UTC),
                level=0.73,
            ),
        )

    body = client.get("/api/state").json()
    row = next(a for a in body["active"] if a["identity"] == "meter-test")
    assert "level" in row, "/api/state must expose `level` for the dashboard meter"
    assert row["level"] == pytest.approx(0.73)


def test_api_state_active_rows_include_buffer_transcription(client, recorder_under_test):
    """The dashboard's per-tap in-flight indicator reads
    `buffer_transcription` off each entry. JSON contract pin so an
    asdict refactor that drops the new field surfaces immediately."""
    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.streams.register,
            ActiveStream(
                conn_id="abc-buf",
                identity="buf-test",
                name="Buf",
                filename="buf.wav",
                started_at=datetime.now(UTC),
                buffer_transcription="words in flight",
            ),
        )

    body = client.get("/api/state").json()
    row = next(a for a in body["active"] if a["identity"] == "buf-test")
    assert row.get("buffer_transcription") == "words in flight"


def test_api_state_live_info_carries_gate_config(client, recorder_under_test):
    """The dashboard's gate-kind dropdown + sliders read live_info to
    seed their default values. gate_kind / gate_speech_threshold /
    gate_hangover_ms / gate_pre_roll_ms / gate_min_speech_ms must all
    be present (the last is a string even when its value is "0",
    since live_info uses empty string as the unset sentinel)."""
    body = client.get("/api/state").json()
    li = body["live_info"]
    assert li.get("gate_kind") in ("tapscribe", "backend")
    assert li.get("gate_speech_threshold")  # non-empty string
    assert li.get("gate_hangover_ms")
    assert li.get("gate_pre_roll_ms")
    # gate_min_speech_ms can read "0" (the default) — assert presence,
    # not truthiness.
    assert "gate_min_speech_ms" in li


def test_api_state_exposes_live_supports_native_vad(client):
    """The dashboard greys out the "backend" gate_kind option when the
    current LiveChannel implementation has no native VAD. WhisperLiveKit
    has --vac, so this is True for the default channel."""
    body = client.get("/api/state").json()
    assert body.get("live_supports_native_vad") is True


def test_live_start_rejects_invalid_gate_kind(client):
    """The dashboard is the only sanctioned source for gate_kind, but
    a stale or hand-crafted POST that doesn't pass "tapscribe" /
    "backend" must surface as a 400 — not a 500 from a downstream
    ValueError. CodeQL treats Request.json() as untrusted input."""
    r = client.post("/api/live/start", json={"gate_kind": "backendd"})
    assert r.status_code == 400, r.text
    assert "gate_kind" in r.text


def test_live_start_rejects_backend_gate_kind_when_unsupported(client, recorder_under_test, monkeypatch):
    """A stale dashboard might POST gate_kind=backend to a future
    channel without a native VAD. UI auto-greys but isn't enforced —
    server-side validation prevents it from silently bypassing the
    only working gate."""
    monkeypatch.setattr(recorder_under_test.live, "supports_native_vad", False, raising=False)
    r = client.post("/api/live/start", json={"gate_kind": "backend"})
    assert r.status_code == 400, r.text
    assert "native" in r.text.lower() or "supports" in r.text.lower()


def test_live_start_rejects_out_of_range_gate_knobs(client):
    """HTML min/max are client-side hints. Server must clamp / reject
    so a malicious or stale client can't push the gate into nonsense
    (negative thresholds, year-long hangovers)."""
    for bad in (
        {"gate_speech_threshold": -0.1},
        {"gate_speech_threshold": 1.5},
        {"gate_hangover_ms": -50},
        {"gate_hangover_ms": 10**7},  # ~3 hours
        {"gate_pre_roll_ms": -1},
        {"gate_pre_roll_ms": 10**7},
        {"gate_min_speech_ms": -1},
        {"gate_min_speech_ms": 10**7},
    ):
        r = client.post("/api/live/start", json=bad)
        assert r.status_code == 400, f"{bad!r} returned {r.status_code}: {r.text}"


def test_live_start_rejects_unparseable_gate_knobs(client):
    """Numeric fields with non-numeric strings must surface as 400,
    not a 500 from `float("hello")`."""
    r = client.post("/api/live/start", json={"gate_speech_threshold": "loud"})
    assert r.status_code == 400, r.text


def test_live_start_rejects_non_finite_gate_knobs(client):
    """JSON doesn't emit NaN / Infinity but `float()` accepts those
    spellings — a hand-crafted client could slip them past the range
    check (lo <= NaN <= hi is always False so the comparison fails
    silently with a confusing "must be in […]" error). `math.isfinite`
    rejects them with a clear "must be a finite number"."""
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.post("/api/live/start", json={"gate_speech_threshold": bad})
        assert r.status_code == 400, f"{bad!r} returned {r.status_code}: {r.text}"
        assert "finite" in r.text.lower(), f"{bad!r} error message: {r.text}"


def test_api_state_active_rows_reflect_current_tap_pref(client, recorder_under_test):
    """The per-row rec/live toggles render their state from the active
    entry's record/live fields. Those must follow the *current*
    per-identity preference (which is what the PUT mutates), not the
    WS-open snapshot — otherwise a click PUTs the new pref but the
    button never visually flips."""
    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.streams.register,
            ActiveStream(
                conn_id="abc-bob",
                identity="bob",
                name="Bob",
                filename="bob.wav",
                started_at=datetime.now(UTC),
                record=True,
                live=True,
            ),
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
# Session-meta: per-session prompt / hotwords overrides. These persist in
# session-meta.json next to label + aliases. Override chain for batch jobs
# is: session-meta → global config/prompt.txt — the per-job ephemeral form
# field that used to ride in the /api/transcribe* body is gone.
# ---------------------------------------------------------------------------


def test_session_meta_round_trips_prompt_and_hotwords(client, recorder_under_test):
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    payload = {"prompt": "team kickoff · Alice, Bob, Patricia", "hotwords": "Acme, Patricia Lin"}
    r = client.put("/api/session-meta/fakesession", json=payload)
    assert r.status_code == 200
    meta = r.json()["meta"]
    assert meta["prompt"] == payload["prompt"]
    assert meta["hotwords"] == payload["hotwords"]
    assert client.get("/api/session-meta/fakesession").json()["prompt"] == payload["prompt"]


def test_session_meta_drops_non_string_prompt_and_hotwords(client, recorder_under_test):
    """Bad payload shouldn't kill the read path. Non-string fields are
    dropped silently, matching how label/aliases already behave."""
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    r = client.put("/api/session-meta/fakesession", json={"prompt": 42, "hotwords": ["nope"]})
    assert r.status_code == 200
    meta = r.json()["meta"]
    assert "prompt" not in meta or meta["prompt"] == ""
    assert "hotwords" not in meta or meta["hotwords"] == ""


def test_session_meta_preserves_label_when_setting_prompt(client, recorder_under_test):
    """Partial updates: setting prompt mustn't wipe an existing label."""
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    client.put("/api/session-meta/fakesession", json={"label": "kickoff"})
    client.put("/api/session-meta/fakesession", json={"prompt": "context"})
    meta = client.get("/api/session-meta/fakesession").json()
    assert meta["label"] == "kickoff"
    assert meta["prompt"] == "context"


def test_session_meta_rejects_oversize_prompt(client, recorder_under_test):
    """Symmetric with PUT /api/config/{key}: the 4000-char cap exists to
    fail loudly at the boundary instead of letting a pasted transcript
    silently land on every batch job as `initial_prompt=`. The same cap
    must apply to session-meta overrides — otherwise a buggy client (or
    a curious operator) can paste a megabyte into session-meta.json and
    bypass the global guardrail."""
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    r = client.put("/api/session-meta/fakesession", json={"prompt": "x" * 5000})
    assert r.status_code == 400


def test_session_meta_rejects_oversize_hotwords(client, recorder_under_test):
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    r = client.put("/api/session-meta/fakesession", json={"hotwords": "x" * 5000})
    assert r.status_code == 400


def test_api_state_sessions_include_meta_prompt_and_hotwords(client, recorder_under_test):
    """The dashboard renders the override badge in the session-detail
    pane off the meta block. /api/state's per-session entry must surface
    these so the JS doesn't need a second round-trip per session."""
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    client.put("/api/session-meta/fakesession", json={"prompt": "P", "hotwords": "H"})
    body = client.get("/api/state").json()
    row = next(s for s in body["sessions"] if s["session"] == "fakesession")
    assert row["session_meta"]["prompt"] == "P"
    assert row["session_meta"]["hotwords"] == "H"


def test_api_state_reports_default_override_counts(client, recorder_under_test):
    """The 'default config' panel shows '· N sessions override this' next
    to each editor. /api/state exposes the counts so the JS doesn't have
    to walk every session."""
    base = recorder_under_test.recordings_dir
    for name in ("s1", "s2", "s3"):
        (base / name).mkdir()
    client.put("/api/session-meta/s1", json={"prompt": "x"})
    client.put("/api/session-meta/s2", json={"prompt": "y", "hotwords": "z"})
    client.put("/api/session-meta/s3", json={"label": "no override"})
    body = client.get("/api/state").json()
    counts = body["default_override_counts"]
    assert counts["prompt"] == 2
    assert counts["hotwords"] == 1


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


# ---------------------------------------------------------------------------
# Audio deletion — DELETE /api/sessions/{session}/audio + /api/wav/{s}/{name}
# ---------------------------------------------------------------------------


def test_delete_session_audio_removes_all_keeps_transcript(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = _seed_session(
        root,
        "s",
        ["20260101T000000Z__alice__abc.wav", "20260101T010000Z__bob__def.wav"],
    )
    # Legacy sidecar on one WAV, new-layout cache dir on the other.
    (sd / "20260101T000000Z__alice__abc.wav").with_suffix(".json").write_text("{}")
    txdir = (sd / "20260101T010000Z__bob__def.wav").with_suffix(".transcripts")
    txdir.mkdir()
    (txdir / "faster-whisper__small.en.json").write_text("{}")
    # A stripped region + the merged transcript + session meta.
    (sd / "stripped").mkdir()
    _seed_wav(sd / "stripped" / "20260101T000000Z__alice__reg.wav")
    (sd / "session-transcript.json").write_text('{"merged": true}')
    (sd / "session-transcript.txt").write_text("merged")
    (sd / "session-meta.json").write_text('{"label": "kickoff"}')

    r = client.delete("/api/sessions/s/audio")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["wavs_deleted"] == 2  # originals only; stripped folds into bytes
    assert body["bytes_freed"] > 0

    assert sorted(sd.glob("*.wav")) == []
    assert not (sd / "stripped").exists()
    assert not (sd / "20260101T000000Z__alice__abc.json").exists()
    assert not (sd / "20260101T010000Z__bob__def.transcripts").exists()
    # The transcript + meta survive — the whole point of audio-only delete.
    assert (sd / "session-transcript.json").is_file()
    assert (sd / "session-transcript.txt").is_file()
    assert (sd / "session-meta.json").is_file()


def test_delete_session_audio_refuses_current_session(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    cur = recorder_under_test.session_start
    sd = _seed_session(root, cur, ["20260101T000000Z__alice__abc.wav"])
    r = client.delete(f"/api/sessions/{cur}/audio")
    assert r.status_code == 409
    assert "current session" in r.json()["detail"]
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()  # untouched


def test_delete_session_audio_refuses_inflight_job(client, recorder_under_test):
    from tapscribe.recorder import JobState

    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])

    # Pre-claim a job slot the way the transcribe-session busy test does —
    # JobTracker.claim is async, driven via anyio's sync→async portal.
    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.jobs.claim,
            JobState(
                session="s",
                kind="strip",
                current=0,
                total=1,
                started_at=datetime.now(UTC),
                status="running",
            ),
        )

    r = client.delete("/api/sessions/s/audio")
    assert r.status_code == 409
    assert "in flight" in r.json()["detail"]
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()  # untouched


def test_delete_session_audio_missing_session_404(client, recorder_under_test):  # noqa: ARG001
    r = client.delete("/api/sessions/does-not-exist/audio")
    assert r.status_code == 404


def test_delete_wav_original_keeps_siblings_no_cascade(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = _seed_session(
        root,
        "s",
        ["20260101T000000Z__alice__abc.wav", "20260101T010000Z__bob__def.wav"],
    )
    (sd / "20260101T000000Z__alice__abc.wav").with_suffix(".json").write_text("{}")
    # A stripped region sharing the deleted original's speaker — the no-cascade
    # contract means it must survive a per-file delete of the original.
    (sd / "stripped").mkdir()
    _seed_wav(sd / "stripped" / "20260101T000000Z__alice__reg.wav")

    r = client.delete("/api/wav/s/20260101T000000Z__alice__abc.wav")
    assert r.status_code == 200, r.text
    assert r.json()["bytes_freed"] > 0

    assert not (sd / "20260101T000000Z__alice__abc.wav").exists()
    assert not (sd / "20260101T000000Z__alice__abc.json").exists()
    # Second original + the stripped region both survive (no cascade).
    assert (sd / "20260101T010000Z__bob__def.wav").is_file()
    assert (sd / "stripped" / "20260101T000000Z__alice__reg.wav").is_file()


def test_delete_wav_stripped_region_only(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    (sd / "stripped").mkdir()
    _seed_wav(sd / "stripped" / "20260101T000000Z__alice__reg.wav")

    r = client.delete("/api/wav/s/20260101T000000Z__alice__reg.wav?source=stripped")
    assert r.status_code == 200, r.text
    assert not (sd / "stripped" / "20260101T000000Z__alice__reg.wav").exists()
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()  # original kept


def test_delete_wav_rejects_bad_input(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    # Non-.wav name → 404 (resolve_wav rejects non-audio). Assign first —
    # an HTTP call inside `assert` would vanish under `python -O`.
    r = client.delete("/api/wav/s/session-meta.json")
    assert r.status_code == 404
    # Unknown source → 400 (whitelisted before any filesystem touch).
    r = client.delete("/api/wav/s/20260101T000000Z__alice__abc.wav?source=bogus")
    assert r.status_code == 400
    # Missing WAV → 404.
    r = client.delete("/api/wav/s/20260101T999999Z__nope__zzz.wav")
    assert r.status_code == 404
    # The real WAV is untouched by any of the above.
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()


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
    sd = _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    wav = sd / "2026-01-01T01-00-00Z__alice__abc.wav"

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
    sd = _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    wav = sd / "2026-01-01T01-00-00Z__alice__abc.wav"
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
        "/api/wav/s/2026-01-01T01-00-00Z__alice__abc.wav/primary",
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
    sd = _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    wav = sd / "2026-01-01T01-00-00Z__alice__abc.wav"
    cached_transcribe(
        wav,
        TranscriberStub(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    r = client.put(
        "/api/wav/s/2026-01-01T01-00-00Z__alice__abc.wav/primary",
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
    sd = _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    wav = sd / "2026-01-01T01-00-00Z__alice__abc.wav"
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
    # Compare the contract fields only — wav_cache.transcripts_listing
    # optionally surfaces `transcribe_ms` when the underlying transcribe
    # call ran for >0 ms (Windows / loaded CI). That's a perf detail,
    # not part of the legacy-sidecar wire contract this test pins.
    listing = row["transcripts"]
    assert len(listing) == 1
    entry = listing[0]
    assert {"backend": entry["backend"], "model": entry["model"], "is_primary": entry["is_primary"]} == {
        "backend": "faster-whisper",
        "model": "small.en",
        "is_primary": True,
    }


def test_api_transcribe_uses_session_meta_prompt_when_set(client, recorder_under_test, monkeypatch):
    """Override chain (per-WAV batch): session-meta.prompt → global
    prompt.txt. The ephemeral form-typed override that used to ride in
    the request body is gone — the persisted meta is the source of truth."""
    captured = {}

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
            captured["initial_prompt"] = initial_prompt
            captured["hotwords"] = hotwords
            return super().transcribe(path, initial_prompt=initial_prompt, hotwords=hotwords)

    fake = _Spy(backend="fake-backend", model="fake-small.en")
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    (recorder_under_test.config_dir / "prompt.txt").write_text("GLOBAL", encoding="utf-8")
    (recorder_under_test.config_dir / "hotwords.txt").write_text("Acme", encoding="utf-8")
    client.put("/api/session-meta/s", json={"prompt": "SESSION OVERRIDE", "hotwords": "Patricia"})

    client.post(
        "/api/transcribe",
        json={"session": "s", "name": "2026-01-01T01-00-00Z__alice__abc.wav", "model": "fake-small.en"},
    )
    assert captured["initial_prompt"] == "SESSION OVERRIDE"
    assert captured["hotwords"] == "Patricia"


def test_api_transcribe_falls_back_to_global_when_session_meta_empty(
    client, recorder_under_test, monkeypatch
):
    captured = {}

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
            captured["initial_prompt"] = initial_prompt
            captured["hotwords"] = hotwords
            return super().transcribe(path, initial_prompt=initial_prompt, hotwords=hotwords)

    fake = _Spy(backend="fake-backend", model="fake-small.en")
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    (recorder_under_test.config_dir / "prompt.txt").write_text("GLOBAL DEFAULT", encoding="utf-8")
    (recorder_under_test.config_dir / "hotwords.txt").write_text("Acme", encoding="utf-8")

    client.post(
        "/api/transcribe",
        json={"session": "s", "name": "2026-01-01T01-00-00Z__alice__abc.wav", "model": "fake-small.en"},
    )
    assert captured["initial_prompt"] == "GLOBAL DEFAULT"
    assert captured["hotwords"] == "Acme"


def test_api_transcribe_session_re_runs_when_session_meta_prompt_changes(
    client, recorder_under_test, monkeypatch
):
    """Cache invariant: editing the session-meta override and re-running
    /api/transcribe-session must NOT return the previously-cached
    transcripts that were produced under the old prompt. The cache hit
    check has to compare initial_prompt_used / hotwords_used too —
    otherwise an operator editing the prompt and clicking
    'transcribe whole session' silently sees the stale merge.

    (The per-WAV /api/transcribe route already passes force=True so it
    bypasses the cache; this regression bites only the session endpoint,
    which is the common case after the override refactor.)

    Reproducer: transcribe once under prompt 'A' (warms the cache),
    flip the session-meta to prompt 'B', transcribe again WITHOUT
    force=true, assert the transcriber was invoked a second time and
    got prompt 'B'."""
    captured: list[dict] = []

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
            captured.append({"initial_prompt": initial_prompt, "hotwords": hotwords})
            return super().transcribe(path, initial_prompt=initial_prompt, hotwords=hotwords)

    fake = _Spy(backend="fake-backend", model="fake-small.en")
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    payload = {"session": "s", "model": "fake-small.en"}

    # First run under prompt A — caches sidecar with initial_prompt_used="A".
    client.put("/api/session-meta/s", json={"prompt": "PROMPT A"})
    client.post("/api/transcribe-session", json=payload)
    assert captured[-1]["initial_prompt"] == "PROMPT A"

    # Flip the session-meta to a new prompt; re-run without force. The
    # cache match key must include initial_prompt, so the stale "A"
    # sidecar should NOT satisfy the hit and the transcriber must re-run
    # with "B".
    captured.clear()
    client.put("/api/session-meta/s", json={"prompt": "PROMPT B"})
    client.post("/api/transcribe-session", json=payload)
    assert captured, "cache returned stale sidecar — initial_prompt mismatch went unnoticed"
    assert captured[-1]["initial_prompt"] == "PROMPT B"


def test_api_transcribe_session_re_runs_when_session_meta_hotwords_change(
    client, recorder_under_test, monkeypatch
):
    """Same invariant as the prompt test above, for hotwords."""
    captured: list[dict] = []

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
            captured.append({"hotwords": hotwords})
            return super().transcribe(path, initial_prompt=initial_prompt, hotwords=hotwords)

    fake = _Spy(backend="fake-backend", model="fake-small.en")
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    payload = {"session": "s", "model": "fake-small.en"}

    client.put("/api/session-meta/s", json={"hotwords": "Acme"})
    client.post("/api/transcribe-session", json=payload)

    captured.clear()
    client.put("/api/session-meta/s", json={"hotwords": "Acme, Patricia"})
    client.post("/api/transcribe-session", json=payload)
    assert captured, "cache returned stale sidecar — hotwords mismatch went unnoticed"
    assert captured[-1]["hotwords"] == "Acme, Patricia"


def test_api_transcribe_session_re_uses_cache_when_prompt_unchanged(client, recorder_under_test, monkeypatch):
    """The other half of the cache invariant: when prompt + hotwords
    haven't changed, the cache MUST be reused (the whole point of the
    cache). Without this, the override widening would degenerate the
    cache to a perpetual miss."""
    runs: list[None] = []

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
            runs.append(None)
            return super().transcribe(path, initial_prompt=initial_prompt, hotwords=hotwords)

    fake = _Spy(backend="fake-backend", model="fake-small.en")
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    payload = {"session": "s", "model": "fake-small.en"}

    client.put("/api/session-meta/s", json={"prompt": "STABLE"})
    client.post("/api/transcribe-session", json=payload)
    first_run_count = len(runs)
    assert first_run_count == 1

    # Re-run with no changes — cache should hit, transcriber should not
    # be invoked again.
    client.post("/api/transcribe-session", json=payload)
    assert len(runs) == first_run_count, "cache missed despite unchanged prompt/hotwords"


def test_api_transcribe_returns_freshly_written_transcript(client, recorder_under_test, monkeypatch):
    """The single-WAV transcribe route writes a new sidecar via the
    cache and returns the wire JSON. With the multi-cache layout there
    is no `<wav>.json` to read back; the route must serve the primary
    that cached_transcribe just promoted."""
    fake = TranscriberStub(backend="fake-backend", model="fake-small.en", text="route transcript")
    # Patch both the canonical binding and the local rebinding in
    # batch_transcribe (which does `from .transcribers import load_transcriber`
    # at module load, so a later patch on the source package alone
    # wouldn't reach it).
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    sd = _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

    r = client.post(
        "/api/transcribe",
        json={"session": "s", "name": "2026-01-01T01-00-00Z__alice__abc.wav", "model": "fake-small.en"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "route transcript"
    assert body["backend"] == "fake-backend"
    assert body["model"] == "fake-small.en"
    # Sidecar lives in the new layout, not at <wav>.json.
    wav = sd / "2026-01-01T01-00-00Z__alice__abc.wav"
    assert not wav.with_suffix(".json").is_file()
    assert wav.with_suffix(".transcripts").is_dir()


# ---------------------------------------------------------------------------
# Route-layer error mapping: each domain exception raised by
# tapscribe.batch_transcribe must surface as the documented HTTP status.
# These guard the wire contract that the dashboard and any external HTTP
# client rely on; a regression here is silent until something downstream
# blows up.
# ---------------------------------------------------------------------------


def test_api_transcribe_returns_422_for_empty_wav(client, recorder_under_test):
    """An on-disk WAV file shorter than 64 bytes (truncated upload,
    aborted recording, etc.) maps to `WavUnreadable` in the orchestrator,
    which the route translates to 422 so the dashboard can surface a
    clear error rather than waiting on a stalled model."""
    root = recorder_under_test.recordings_dir
    sd = root / "s"
    sd.mkdir(parents=True)
    (sd / "2026-01-01T01-00-00Z__alice__abc.wav").write_bytes(b"")  # 0 bytes

    r = client.post(
        "/api/transcribe",
        json={"session": "s", "name": "2026-01-01T01-00-00Z__alice__abc.wav", "model": "tiny.en"},
    )
    assert r.status_code == 422, r.text


def test_api_transcribe_returns_422_for_silent_wav(client, recorder_under_test):
    """An all-zeros WAV trips the silence floor (`WavTooQuiet`) before
    any model is loaded. Without the 422 mapping Whisper would chew
    through noise and produce a hallucinated transcript."""
    root = recorder_under_test.recordings_dir
    sd = root / "s"
    sd.mkdir(parents=True)
    # amplitude=0 → all-zero samples → -inf dBFS, far below SILENT_RMS_DBFS_FLOOR.
    _seed_wav(sd / "2026-01-01T01-00-00Z__alice__abc.wav", amplitude=0)

    r = client.post(
        "/api/transcribe",
        json={"session": "s", "name": "2026-01-01T01-00-00Z__alice__abc.wav", "model": "tiny.en"},
    )
    assert r.status_code == 422, r.text


def test_api_transcribe_session_returns_400_for_unparseable_from_iso(client, recorder_under_test):
    """`InvalidRange` is the "syntactically wrong input" path —
    distinct from `NoUsableWavs` (empty result with valid inputs).
    Routes map it to 400 so an operator typing a bad timestamp gets
    a "client error, fix your input" signal instead of a 404 that
    would suggest the session was empty."""
    root = recorder_under_test.recordings_dir
    _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

    r = client.post(
        "/api/transcribe-session",
        json={"session": "s", "model": "tiny.en", "from_iso": "not-a-timestamp"},
    )
    assert r.status_code == 400, r.text


def test_api_transcribe_session_returns_404_for_empty_range(client, recorder_under_test):
    """Valid inputs but the session has no WAVs. The dashboard's "no
    transcripts to merge" path depends on the 404."""
    (recorder_under_test.recordings_dir / "s").mkdir(parents=True)

    r = client.post(
        "/api/transcribe-session",
        json={"session": "s", "model": "tiny.en"},
    )
    assert r.status_code == 404, r.text


def test_api_transcribe_session_returns_409_when_job_already_in_flight(
    client, recorder_under_test, monkeypatch
):
    """One transcribe / strip job per session at a time. Pre-claiming
    the slot simulates a concurrent operator click; the route must
    refuse with 409 rather than corrupting JobTracker state by
    starting a second loop alongside the first.

    `transcribe_session` calls `load_transcriber` BEFORE the
    JobTracker.claim, so without stubbing the loader CI (where
    `faster_whisper` isn't installed) would hit `ModuleNotFoundError`
    instead of reaching the SessionBusy branch we're trying to exercise."""
    from tapscribe.recorder import JobState

    fake = TranscriberStub(backend="fake-be", model="fake-small.en")
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

    # Pre-claim the slot — TestClient's sync API doesn't expose the loop,
    # but JobTracker.claim is async. anyio.from_thread mirrors the
    # pattern starlette.testclient uses internally for sync→async calls.
    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.jobs.claim,
            JobState(
                session="s",
                kind="strip",
                current=0,
                total=1,
                started_at=datetime.now(UTC),
                status="running",
            ),
        )

    r = client.post(
        "/api/transcribe-session",
        json={"session": "s", "model": "tiny.en"},
    )
    assert r.status_code == 409, r.text


def test_api_state_files_row_surfaces_primary_transcript(client, recorder_under_test):
    """The dashboard reads each WAV's transcript out of /api/state's
    `sessions[*].files[*].transcript`. With the new multi-cache layout,
    that field must surface the *primary* transcript so flipping the
    primary on disk shows up on the next poll."""
    from tapscribe.wav_cache import cached_transcribe, set_primary_transcript

    root = recorder_under_test.recordings_dir
    session = _seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    wav = session / "2026-01-01T01-00-00Z__alice__abc.wav"

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
    import anyio.from_thread

    from tapscribe.recorder import JobState

    root = recorder_under_test.recordings_dir
    _seed_session(root, "tgt", [])
    _seed_session(root, "src", [])
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.jobs.claim,
            JobState(
                session="src",
                kind="transcribe",
                current=0,
                total=1,
                started_at=datetime.now(UTC),
            ),
        )
    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 409
