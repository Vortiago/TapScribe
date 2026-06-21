"""Route-level integration tests via TestClient.

The Recorder is constructed per-test against a tmpdir and attached to
`app.state.recorder` via dependency override. No subprocess is spawned
(LiveChannel.start is patched out via dependency); no real Transcriber
is loaded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]  # pytest puts tests/ on sys.path so `from conftest` resolves the project's tests/conftest.py
    TranscriberStub,
    py_cmd,
    repoint_config_files,
    seed_merged_transcript,
)
from fastapi.testclient import TestClient
from wav_builders import seed_session, seed_wav  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import ActiveStream, Recorder


@pytest.fixture(autouse=True)
def _force_all_probes_installed():
    """/api/models filters out registry entries whose adapter modules
    aren't importable; in a CI env that hasn't installed transformers /
    parakeet-mlx / mlx-voxtral, the catalog assertions in
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
    # The text helpers and /api/state read the path constants directly —
    # re-bind them all to the tmp config dir so editable-config writes land
    # where the test expects them (and where the recorder under test reads).
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
    # Batch context includes Parakeet.
    ids = {m["model_id"] for m in body["models"]}
    assert "parakeet-tdt-0.6b-v3" in ids


def test_api_models_live_context_excludes_parakeet(client):
    r = client.get("/api/models?context=live")
    assert r.status_code == 200
    body = r.json()
    assert body["context"] == "live"
    ids = {m["model_id"] for m in body["models"]}
    assert "parakeet-tdt-0.6b-v3" not in ids
    # Whisper variants ARE live-eligible.
    assert "tiny.en" in ids


def test_api_models_rejects_unknown_context(client):
    r = client.get("/api/models?context=transcode")
    assert r.status_code == 400


def test_api_models_emits_text_inputs_for_whisper(client):
    r = client.get("/api/models")
    whisper = next(m for m in r.json()["models"] if m["model_id"] == "small.en")
    names = {i["name"] for i in whisper["inputs"]}
    assert names == {"initial_prompt", "hotwords"}


def test_api_models_emits_no_inputs_for_parakeet(client):
    r = client.get("/api/models")
    pk = next(m for m in r.json()["models"] if m["model_id"] == "parakeet-tdt-0.6b-v3")
    assert pk["inputs"] == []


def test_api_models_cache_clear_evicts_idle_models(client, monkeypatch):
    """DELETE /api/models/cache reclaims idle (not-in-use) cached models and
    reports how many it freed. We seed one resident model via the factory's
    own acquire/release with eviction disabled, then assert the endpoint
    drops it."""
    from test_transcribers_cache_eviction import _GenericSpy, _loader_for, _Registry

    from tapscribe import transcribers

    # ttl<0 keeps the model resident on release so the manual evict has a
    # target instead of it being dropped immediately on release.
    monkeypatch.setenv("TAPSCRIBE_MODEL_IDLE_TTL_S", "-1")
    reg = _Registry("cpu", _loader_for(_GenericSpy, []))
    try:
        t = transcribers.load_transcriber("m", backend="cpu", registry=reg)
        transcribers.release_transcriber(t)
        assert t._model is not None  # resident before the call

        r = client.delete("/api/models/cache")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "evicted": 1}
        assert t._model is None  # reclaimed
    finally:
        transcribers.clear_cache()


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


def test_api_state_carries_backend_preference_and_available_backends(client):
    r = client.get("/api/state")
    body = r.json()
    assert "backend" in body
    assert "available_backends" in body
    assert isinstance(body["available_backends"], list)


def test_api_state_conditional_get_returns_304_when_unchanged(client):
    """The poll path emits a weak ETag and answers a matching If-None-Match with
    a bodyless 304, so an idle dashboard reuses its cached state instead of
    re-parsing the payload every tick. A stale validator gets the full 200."""
    r1 = client.get("/api/state")
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag and etag.startswith('W/"'), f"expected a weak ETag, got {etag!r}"

    # Same validator + unchanged (idle) state → 304 with no body.
    r2 = client.get("/api/state", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers.get("etag") == etag

    # A stale validator → full 200 body again.
    r3 = client.get("/api/state", headers={"If-None-Match": 'W/"0000000000000000"'})
    assert r3.status_code == 200
    assert r3.json()["current_session"] == r1.json()["current_session"]


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
    """If the only installed batch families are Voxtral / Parakeet (none
    declare initial_prompt or hotwords), batch_prompt and batch_hotwords
    are False. Same logic for live: if the only installed live family
    doesn't declare initial_prompt, live_prompt is False."""
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


def test_put_config_batch_model_writes_file_and_validates(client, recorder_under_test):
    """The batch-model default is catalog-validated at write time — it feeds
    the end-of-meeting pipeline's model loader with no operator in the loop,
    so an unknown id must 400 instead of landing on disk."""
    r = client.put("/api/config/batch-model", json={"content": "small.en"})
    assert r.status_code == 200, r.text
    assert (recorder_under_test.config_dir / "batch-model.txt").read_text(encoding="utf-8") == "small.en"

    r = client.put("/api/config/batch-model", json={"content": "not-a-model"})
    assert r.status_code == 400
    # The bad id never replaced the good one.
    assert (recorder_under_test.config_dir / "batch-model.txt").read_text(encoding="utf-8") == "small.en"


def test_api_state_includes_batch_model_default(client, recorder_under_test):  # noqa: ARG001 — recorder fixture pins the tmp config dir
    """The dashboard's Default engine card seeds from the state poll, the
    same way the Live engine card reads live_model_default."""
    assert client.get("/api/state").json()["batch_model_default"] == ""
    client.put("/api/config/batch-model", json={"content": "small.en"})
    assert client.get("/api/state").json()["batch_model_default"] == "small.en"


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
    seed_wav(cur / "cur.wav")
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
    seed_wav(cur / "cur.wav")
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
    """The dashboard renders per-session override badges off the meta
    block. /api/state's per-session entry must surface these so the JS
    doesn't need a second round-trip per session."""
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


def test_session_meta_round_trips_summarizer_override(client, recorder_under_test):
    """#84: the per-session summarizer override (source + prompt) rides in
    session-meta exactly like the prompt/hotwords overrides — and setting it
    preserves fields the caller didn't mention."""
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    client.put("/api/session-meta/fakesession", json={"label": "kickoff"})
    r = client.put(
        "/api/session-meta/fakesession",
        json={"summary_source": "command", "summary_prompt": "Action items only."},
    )
    assert r.status_code == 200, r.text
    meta = client.get("/api/session-meta/fakesession").json()
    assert meta["summary_source"] == "command"
    assert meta["summary_prompt"] == "Action items only."
    assert meta["label"] == "kickoff"


def test_session_meta_rejects_bad_summary_source(client, recorder_under_test):
    """The override source is allowlisted at write time like the global
    default's (an unknown source must never persist); "api" is now valid (#85);
    "" clears the override back to the global default."""
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    assert client.put("/api/session-meta/fakesession", json={"summary_source": "bogus"}).status_code == 400
    assert (
        client.put("/api/session-meta/fakesession", json={"summary_source": "telepathy"}).status_code == 400
    )
    # api is now wired and accepted.
    assert client.put("/api/session-meta/fakesession", json={"summary_source": "api"}).status_code == 200
    assert client.put("/api/session-meta/fakesession", json={"summary_source": ""}).status_code == 200


def test_session_meta_rejects_oversize_summary_prompt(client, recorder_under_test):
    session_dir = recorder_under_test.recordings_dir / "fakesession"
    session_dir.mkdir()
    r = client.put("/api/session-meta/fakesession", json={"summary_prompt": "x" * 5000})
    assert r.status_code == 400


def test_api_state_counts_summarizer_overrides(client, recorder_under_test):
    """The Settings card's '· N sessions override this' footer for the
    summarizer default; surfaced next to the prompt/hotwords counts. The
    per-session meta block in /api/state carries the fields themselves
    (read_session_meta returns every _META_STRING_FIELDS member)."""
    base = recorder_under_test.recordings_dir
    for name in ("s1", "s2", "s3"):
        (base / name).mkdir()
    client.put("/api/session-meta/s1", json={"summary_source": "local"})
    client.put("/api/session-meta/s2", json={"summary_prompt": "Action items."})
    client.put("/api/session-meta/s3", json={"label": "no override"})
    body = client.get("/api/state").json()
    assert body["default_override_counts"]["summarizer"] == 2
    row = next(s for s in body["sessions"] if s["session"] == "s1")
    assert row["session_meta"]["summary_source"] == "local"


# ---------------------------------------------------------------------------
# /api/sessions/{target}/absorb
# ---------------------------------------------------------------------------


def test_absorb_moves_wavs_and_sidecars_and_deletes_source(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    target = seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    source = seed_session(
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
    target = seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    source = seed_session(root, "src", ["20260101T010000Z__alice__def.wav"])
    (source / "stripped").mkdir()
    seed_wav(source / "stripped" / "20260101T010000Z__alice__def.wav")

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text
    assert r.json()["stripped_moved"] == 1
    assert (target / "stripped" / "20260101T010000Z__alice__def.wav").is_file()
    assert not source.exists()


def test_absorb_merges_aliases_with_target_winning(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", [])
    seed_session(root, "src", [])
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
    target = seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    seed_session(root, "src", ["20260101T010000Z__alice__def.wav"])
    (target / "session-transcript.json").write_text('{"stale": true}')

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text
    assert r.json()["transcript_invalidated"] is True
    assert not (target / "session-transcript.json").exists()


def test_absorb_invalidates_target_summary(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    target = seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    seed_session(root, "src", ["20260101T010000Z__alice__def.wav"])
    (target / "session-summary.json").write_text('{"stale": true}')

    r = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert r.status_code == 200, r.text
    assert r.json()["summary_invalidated"] is True
    assert not (target / "session-summary.json").exists()


def test_absorb_refuses_when_source_is_current_session(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", [])
    cur = recorder_under_test.session_start
    seed_session(root, cur, [])
    r = client.post("/api/sessions/tgt/absorb", json={"source": cur})
    assert r.status_code == 409
    assert "current session" in r.json()["detail"]


def test_absorb_allows_target_to_be_current_session(client, recorder_under_test):
    """The whole point of merge-after-restart: roll a previous session
    into the new one the operator is recording into right now."""
    root = recorder_under_test.recordings_dir
    cur = recorder_under_test.session_start
    seed_session(root, cur, [])
    seed_session(root, "prev", ["20260101T010000Z__alice__def.wav"])
    r = client.post(f"/api/sessions/{cur}/absorb", json={"source": "prev"})
    assert r.status_code == 200, r.text
    assert (root / cur / "20260101T010000Z__alice__def.wav").is_file()


def test_absorb_refuses_self(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", [])
    r = client.post("/api/sessions/tgt/absorb", json={"source": "tgt"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Audio deletion — DELETE /api/sessions/{session}/audio + /api/wav/{s}/{name}
# ---------------------------------------------------------------------------


def test_delete_session_audio_removes_all_keeps_transcript(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(
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
    seed_wav(sd / "stripped" / "20260101T000000Z__alice__reg.wav")
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
    sd = seed_session(root, cur, ["20260101T000000Z__alice__abc.wav"])
    r = client.delete(f"/api/sessions/{cur}/audio")
    assert r.status_code == 409
    assert "current session" in r.json()["detail"]
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()  # untouched


def test_delete_session_audio_refuses_inflight_job(client, recorder_under_test):
    from tapscribe.recorder import JobState

    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])

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
    sd = seed_session(
        root,
        "s",
        ["20260101T000000Z__alice__abc.wav", "20260101T010000Z__bob__def.wav"],
    )
    (sd / "20260101T000000Z__alice__abc.wav").with_suffix(".json").write_text("{}")
    # A stripped region sharing the deleted original's speaker — the no-cascade
    # contract means it must survive a per-file delete of the original.
    (sd / "stripped").mkdir()
    seed_wav(sd / "stripped" / "20260101T000000Z__alice__reg.wav")

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
    sd = seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    (sd / "stripped").mkdir()
    seed_wav(sd / "stripped" / "20260101T000000Z__alice__reg.wav")

    r = client.delete("/api/wav/s/20260101T000000Z__alice__reg.wav?source=stripped")
    assert r.status_code == 200, r.text
    assert not (sd / "stripped" / "20260101T000000Z__alice__reg.wav").exists()
    assert (sd / "20260101T000000Z__alice__abc.wav").is_file()  # original kept


def test_delete_wav_rejects_bad_input(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
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


# ---------------------------------------------------------------------------
# Waveform peaks — GET /api/wav/{session}/{name}/peaks
# ---------------------------------------------------------------------------


def test_wav_peaks_shape_and_range(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    # seed_wav writes a 1.0 s audible square wave in the recorder format.
    seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    r = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/peaks?bins=200")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bins"] == 200
    assert len(body["peaks"]) == 200
    assert body["sample_rate"] == 16000
    assert body["duration_s"] == pytest.approx(1.0, abs=0.01)
    assert all(0.0 <= p <= 1.0 for p in body["peaks"])
    assert max(body["peaks"]) > 0.1, "an audible WAV should produce non-trivial peaks"


def test_wav_peaks_clamps_bins(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    # Absurdly large → clamped to the route's upper bound.
    hi = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/peaks?bins=100000").json()
    assert hi["bins"] == 2000
    assert len(hi["peaks"]) == 2000
    # Below the floor → clamped up.
    lo = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/peaks?bins=1").json()
    assert lo["bins"] == 16
    assert len(lo["peaks"]) == 16


def test_wav_peaks_default_bins_and_stripped_source(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    (sd / "stripped").mkdir()
    seed_wav(sd / "stripped" / "20260101T000000Z__alice__reg.wav")
    # source=stripped resolves through the same sanitiser as the download route.
    r = client.get("/api/wav/s/20260101T000000Z__alice__reg.wav/peaks?source=stripped")
    assert r.status_code == 200, r.text
    assert len(r.json()["peaks"]) == 800  # the route's default bins


def test_wav_peaks_rejects_bad_input(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    # Unknown source → 400, whitelisted before any filesystem touch.
    r = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/peaks?source=bogus")
    assert r.status_code == 400
    # Missing WAV → 404 via resolve_wav.
    r = client.get("/api/wav/s/20260101T999999Z__nope__zzz.wav/peaks")
    assert r.status_code == 404
    # Non-.wav name → 404 (resolve_wav rejects non-audio).
    r = client.get("/api/wav/s/session-meta.json/peaks")
    assert r.status_code == 404


def test_wav_peaks_non_recorder_format_is_422(client, recorder_under_test):
    # A WAV that passes the path sanitiser but isn't the recorder format
    # (44.1 kHz stereo) → compute_peaks raises and the route maps it to 422
    # with a clear message, mirroring the WavUnreadable mapping.
    import wave as _wave

    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", [])
    bad = sd / "20260101T000000Z__alice__bad.wav"
    with _wave.open(str(bad), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 100)
    r = client.get("/api/wav/s/20260101T000000Z__alice__bad.wav/peaks")
    assert r.status_code == 422, r.text
    assert "format" in r.json()["detail"].lower()


def test_strip_meta_roundtrips_response_spans(client, recorder_under_test):
    """POST strip-silence returns explicit region_spans per written WAV, and
    GET /strip-meta serves the SAME spans back from the persisted sidecar —
    the no-filename-reconstruction contract of #90."""
    root = recorder_under_test.recordings_dir
    seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    r = client.post("/api/sessions/s/strip-silence", json={})
    assert r.status_code == 200, r.text
    rows = [f for f in r.json()["files"] if f.get("written")]
    assert rows and all(f["region_spans"] for f in rows)
    for sp in rows[0]["region_spans"]:
        assert sp["start_s"] < sp["end_s"]

    m = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/strip-meta")
    assert m.status_code == 200
    body = m.json()
    assert body["spans"] == rows[0]["region_spans"]
    assert body["knobs"] == {"min_silence_ms": 500, "pad_ms": 200, "speech_floor_db": -45.0}
    assert body["stripped_at"] == r.json()["stripped_at"]


def test_strip_meta_null_when_never_stripped_and_404_on_bad_input(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    # Never stripped → JSON null, not an error.
    m = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/strip-meta")
    assert m.status_code == 200
    assert m.json() is None
    # Missing WAV → 404 via resolve_wav.
    assert client.get("/api/wav/s/20260101T999999Z__nope__zzz.wav/strip-meta").status_code == 404
    # Non-.wav name → 404 (resolve_wav rejects non-audio).
    assert client.get("/api/wav/s/strip-meta.json/strip-meta").status_code == 404


def test_strip_meta_null_after_original_rewritten(client, recorder_under_test):
    """The fingerprint guard: a re-recorded/rewritten original must read as
    'no committed cut' rather than draw the old spans against new audio."""
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    r = client.post("/api/sessions/s/strip-silence", json={})
    assert r.status_code == 200, r.text
    m = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/strip-meta")
    assert m.status_code == 200 and m.json() is not None

    # Rewrite the original (longer file -> new size + mtime).
    seed_wav(sd / "20260101T000000Z__alice__abc.wav", seconds=2.0)
    m2 = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/strip-meta")
    assert m2.status_code == 200
    assert m2.json() is None


def test_delete_stripped_clip_prunes_strip_meta(client, recorder_under_test):
    """Deleting one region clip must remove ITS span from strip-meta (an
    original whose spans all vanish loses its entry) while other originals'
    committed cuts stay intact."""
    root = recorder_under_test.recordings_dir
    seed_session(root, "s", ["20260101T000000Z__alice__abc.wav", "20260101T010000Z__bob__def.wav"])
    r = client.post("/api/sessions/s/strip-silence", json={})
    assert r.status_code == 200, r.text
    rows = {f["name"]: f for f in r.json()["files"] if f.get("written")}
    assert set(rows) == {"20260101T000000Z__alice__abc.wav", "20260101T010000Z__bob__def.wav"}
    alice_clip = rows["20260101T000000Z__alice__abc.wav"]["regions_written"][0]

    d = client.delete(f"/api/wav/s/{alice_clip}?source=stripped")
    assert d.status_code == 200, d.text

    # Alice's only clip is gone -> her committed cut reads as absent…
    m = client.get("/api/wav/s/20260101T000000Z__alice__abc.wav/strip-meta")
    assert m.status_code == 200 and m.json() is None
    # …while Bob's is untouched.
    m2 = client.get("/api/wav/s/20260101T010000Z__bob__def.wav/strip-meta")
    assert m2.status_code == 200
    assert m2.json()["spans"] == rows["20260101T010000Z__bob__def.wav"]["region_spans"]


def test_absorb_carries_strip_meta_into_target(client, recorder_under_test):
    """Absorb moves region clips AND their committed-cut sidecar: both the
    target's own spans and the absorbed source's spans must resolve in the
    target afterwards, with the target's knobs preserved."""
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    seed_session(root, "src", ["20260101T010000Z__bob__def.wav"])
    rt = client.post("/api/sessions/tgt/strip-silence", json={"pad_ms": 100})
    assert rt.status_code == 200, rt.text
    rs = client.post("/api/sessions/src/strip-silence", json={"pad_ms": 50})
    assert rs.status_code == 200, rs.text
    tgt_spans = [f for f in rt.json()["files"] if f.get("written")][0]["region_spans"]
    src_spans = [f for f in rs.json()["files"] if f.get("written")][0]["region_spans"]

    a = client.post("/api/sessions/tgt/absorb", json={"source": "src"})
    assert a.status_code == 200, a.text

    m_tgt = client.get("/api/wav/tgt/20260101T000000Z__alice__abc.wav/strip-meta")
    assert m_tgt.status_code == 200 and m_tgt.json() is not None
    assert m_tgt.json()["spans"] == tgt_spans
    assert m_tgt.json()["knobs"]["pad_ms"] == 100  # target's knobs win
    m_src = client.get("/api/wav/tgt/20260101T010000Z__bob__def.wav/strip-meta")
    assert m_src.status_code == 200 and m_src.json() is not None
    assert m_src.json()["spans"] == src_spans


def test_strip_preview_matches_committed_strip_and_writes_nothing(client, recorder_under_test):
    """The preview IS the cut: for the same knobs, /strip-preview's spans
    must equal what a real ✂ strip then commits (modulo the clip filenames
    only the real run mints) — and the preview itself writes nothing."""
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])

    p = client.get(
        "/api/wav/s/20260101T000000Z__alice__abc.wav/strip-preview"
        "?min_silence_ms=400&pad_ms=50&speech_floor_db=-40"
    )
    assert p.status_code == 200, p.text
    preview = p.json()
    assert preview["segments"] >= 1
    assert preview["silent"] is False
    assert preview["detector"] == "silero-vad"
    assert preview["knobs"] == {"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0}
    assert preview["speech_seconds"] <= preview["in_seconds"]
    for sp in preview["spans"]:
        assert 0.0 <= sp["start_s"] < sp["end_s"] <= preview["in_seconds"] + 0.01
    # A preview must not create stripped/ (or anything else).
    assert not (sd / "stripped").exists()

    r = client.post(
        "/api/sessions/s/strip-silence",
        json={"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0},
    )
    assert r.status_code == 200, r.text
    committed = [f for f in r.json()["files"] if f.get("written")][0]["region_spans"]
    assert [{"start_s": sp["start_s"], "end_s": sp["end_s"]} for sp in committed] == preview["spans"]


def test_strip_preview_shares_strip_knob_bounds_and_sanitiser(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "s", ["20260101T000000Z__alice__abc.wav"])
    base = "/api/wav/s/20260101T000000Z__alice__abc.wav/strip-preview"
    # Out-of-range knobs → 400, same bounds as the strip route.
    assert client.get(f"{base}?min_silence_ms=50").status_code == 400
    assert client.get(f"{base}?pad_ms=9999").status_code == 400
    assert client.get(f"{base}?speech_floor_db=5").status_code == 400
    # Unknown source → 400, whitelisted before any filesystem touch.
    assert client.get(f"{base}?source=bogus").status_code == 400
    # Missing WAV → 404 via resolve_wav.
    assert client.get("/api/wav/s/20260101T999999Z__nope__zzz.wav/strip-preview").status_code == 404
    # Omitted knobs fall back to the StripSessionRequest defaults.
    ok = client.get(base)
    assert ok.status_code == 200
    assert ok.json()["knobs"] == {"min_silence_ms": 500, "pad_ms": 200, "speech_floor_db": -45.0}
    # Corrupt bytes behind a .wav name → 422 (wave.Error path), not a 500 —
    # the same unreadable-WAV outcome the peaks route maps.
    bad = root / "s" / "20260101T020000Z__alice__bad.wav"
    bad.write_bytes(b"RIFFgarbage-not-a-wav")
    assert client.get("/api/wav/s/20260101T020000Z__alice__bad.wav/strip-preview").status_code == 422


def test_absorb_refuses_missing_source(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", [])
    r = client.post("/api/sessions/tgt/absorb", json={"source": "nope"})
    assert r.status_code == 404


def test_absorb_refuses_filename_collision(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    src = seed_session(root, "src", ["20260101T000000Z__alice__abc.wav"])
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
    sd = seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    sd = seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    sd = seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    seed_session(recorder_under_test.recordings_dir, "s", [])
    r = client.put(
        "/api/wav/s/missing.wav/primary",
        json={"backend": "x", "model": "y"},
    )
    assert r.status_code == 404


def test_stripped_clip_cache_listing_carries_source_so_set_primary_resolves(client, recorder_under_test):
    """Repro for 'Set primary failed: 404' on stripped-audio transcripts.

    A stripped region clip lives in <session>/stripped/. Its /api/state cache
    listing must carry source="stripped" so the dashboard PUTs that source —
    otherwise the UI fell back to "original", resolve_wav looked in the
    originals dir, and the PUT 404'd. With source present, set-primary on the
    clip resolves and succeeds."""
    from tapscribe.wav_cache import cached_transcribe

    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    stripped = sd / "stripped"
    stripped.mkdir()
    region_name = "2026-01-01T01-00-02Z__alice__abc.wav"
    cached_transcribe(
        seed_wav(stripped / region_name),
        TranscriberStub(backend="parakeet", model="v2"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
        source="stripped",
    )

    # The clip surfaces under the original's regions[], and its cache listing
    # reports source="stripped".
    body = client.get("/api/state").json()
    s = next(s for s in body["sessions"] if s["session"] == "s")
    original = next(f for f in s["files"] if f["name"] == "2026-01-01T01-00-00Z__alice__abc.wav")
    region_row = next(r for r in original["regions"] if r["name"] == region_name)
    assert region_row["transcripts"][0]["source"] == "stripped"

    # Set-primary with that source resolves the stripped/ path → 200, not 404.
    ok = client.put(
        f"/api/wav/s/{region_name}/primary",
        json={"backend": "parakeet", "model": "v2", "source": "stripped"},
    )
    assert ok.status_code == 200, ok.text

    # The pre-fix path the UI took — source omitted, so it defaulted to
    # "original" — still 404s, because the clip genuinely isn't in the originals
    # dir. The fix is that the listing now tells the UI to send "stripped".
    bad = client.put(
        f"/api/wav/s/{region_name}/primary",
        json={"backend": "parakeet", "model": "v2"},
    )
    assert bad.status_code == 404, bad.text


def test_api_state_files_row_lists_single_entry_for_legacy_sidecar(client, recorder_under_test):
    """A WAV with only a legacy `<wav>.json` sidecar should still surface
    a one-element `transcripts` list so the UI can render it consistently."""
    from tapscribe.wav_cache import cached_transcribe

    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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
    sd = seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

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
    seed_wav(sd / "2026-01-01T01-00-00Z__alice__abc.wav", amplitude=0)

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
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

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
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

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


def test_manual_transcribe_session_409_while_pipeline_running(client, recorder_under_test, monkeypatch):
    """The end-of-meeting pipeline holds ONE `kind="pipeline"` slot for its
    whole chain — a manual transcribe started mid-pipeline gets the same 409
    as against any other in-flight job (the other half of issue #102's
    one-slot acceptance criterion; the trigger-side 409 lives in
    test_tap_endpoint.py)."""
    from tapscribe.recorder import JobState

    fake = TranscriberStub(backend="fake-be", model="fake-small.en")
    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005
    monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", lambda *a, **kw: fake)  # noqa: ARG005

    root = recorder_under_test.recordings_dir
    seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.jobs.claim,
            JobState(
                session="s",
                kind="pipeline",
                current=0,
                total=1,
                started_at=datetime.now(UTC),
                status="stripping",
                stage="strip",
            ),
        )

    r = client.post(
        "/api/transcribe-session",
        json={"session": "s", "model": "tiny.en"},
    )
    assert r.status_code == 409, r.text


def test_api_state_files_row_surfaces_primary_transcript(client, recorder_under_test):
    """The dashboard reads each WAV's transcript MARKER out of /api/state's
    `sessions[*].files[*].transcript`. With the lazy-transcript change the
    field is a slim marker (backend/model/transcribed_at/transcribe_ms/source/
    segment_count) — NOT the full body — but it must still surface the
    *primary* (backend, model) so flipping the primary on disk shows up on the
    next poll. The full text is fetched lazily via the per-WAV transcript
    endpoint instead, asserted below."""
    from tapscribe.wav_cache import cached_transcribe, set_primary_transcript

    root = recorder_under_test.recordings_dir
    session = seed_session(root, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
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

    # Default primary is the most-recent write (voxtral). The marker carries
    # backend/model but NOT the segment-level text/words.
    body = client.get("/api/state").json()
    s = next(s for s in body["sessions"] if s["session"] == "s")
    file_row = next(f for f in s["files"] if f["name"] == wav.name)
    assert file_row["transcript"] is not None
    assert file_row["transcript"]["backend"] == "mlx-voxtral"
    assert file_row["transcript"]["model"] == "voxtral-mini"
    assert "text" not in file_row["transcript"], "marker must not embed the transcript body"
    assert "segments" not in file_row["transcript"]

    # The full text is reachable via the lazy per-WAV transcript endpoint.
    full = client.get(f"/api/wav/s/{wav.name}/transcript").json()
    assert full["text"] == "voxtral text"
    assert full["backend"] == "mlx-voxtral"

    # Flip primary back to whisper; the dashboard sees the change in the marker.
    set_primary_transcript(wav, backend="faster-whisper", model="small.en")
    body = client.get("/api/state").json()
    s = next(s for s in body["sessions"] if s["session"] == "s")
    file_row = next(f for f in s["files"] if f["name"] == wav.name)
    assert file_row["transcript"]["backend"] == "faster-whisper"
    assert file_row["transcript"]["model"] == "small.en"
    full = client.get(f"/api/wav/s/{wav.name}/transcript").json()
    assert full["text"] == "whisper text"
    assert full["backend"] == "faster-whisper"


def test_absorb_moves_new_layout_transcripts_directory(client, recorder_under_test):
    """The source WAV may have multiple cached transcripts under the new
    `<wav>.transcripts/` layout. Absorb must move that directory into
    the target alongside the WAV."""
    from tapscribe.wav_cache import cached_transcribe, read_all_cached

    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    source = seed_session(root, "src", ["20260101T010000Z__alice__def.wav"])
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
    seed_session(root, "tgt", [])
    seed_session(root, "src", [])
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


# ---------------------------------------------------------------------------
# Dashboard shell serving — unit-level guards so a broken / route or missing
# asset is caught by the default suite, not only the Playwright job (which
# self-skips without a browser).
# ---------------------------------------------------------------------------


def test_root_serves_stages_shell(client):
    """GET / is the Stages dashboard (next.html): the shell markers and the
    single module script must be present. The classic dashboard was retired —
    this is the only UI."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="next-app"' in r.text
    assert "/web/js/next/main.js" in r.text
    # The shell layers next.css on top of the shared design tokens.
    assert "/dashboard.css" in r.text
    assert "/next.css" in r.text


def test_next_route_is_gone(client):
    """/next was the Stages UI's incubation address; it no longer exists."""
    assert client.get("/next").status_code == 404


def test_dashboard_stylesheets_serve(client):
    for path in ("/dashboard.css", "/next.css"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/css" in r.headers["content-type"], path


def test_stages_assets_serve_from_mounts(client):
    """The shell's entry module and one template bundle must come back via
    the /web mounts — this is exactly what a broken package-data glob or a
    botched StaticFiles mount breaks first."""
    r = client.get("/web/js/next/main.js")
    assert r.status_code == 200
    r = client.get("/web/components/next/views.html")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/sessions/{session}/summarize — domain-error → status mapping
# ---------------------------------------------------------------------------
#
# The route is a thin shim over batch_summarize.summarize_session; these assert
# the status-code mapping for each domain error plus the happy path. The
# Command source runs a real `python -c` subprocess (cross-platform — `cat`
# isn't a PATH executable on Windows) so no model/endpoint is touched.


_SUMMARIZE_CAT = py_cmd("import sys; sys.stdout.write(sys.stdin.read())")


def test_summarize_returns_summary_for_command_source(client, recorder_under_test):
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="we decided to ship")
    r = client.post(
        "/api/sessions/s/summarize",
        json={"source": "command", "command": _SUMMARIZE_CAT, "prompt": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["source"] == "command"
    assert body["summary"] == "we decided to ship"
    assert body["command"] == _SUMMARIZE_CAT


def test_summarize_no_merged_transcript_returns_422(client, recorder_under_test):
    (recorder_under_test.recordings_dir / "empty").mkdir()
    r = client.post(
        "/api/sessions/empty/summarize",
        json={"source": "command", "command": _SUMMARIZE_CAT},
    )
    assert r.status_code == 422, r.text
    assert "transcribe" in r.json()["detail"].lower()


def test_summarize_busy_returns_409(client, recorder_under_test):
    from tapscribe.recorder import JobState

    seed_merged_transcript(recorder_under_test.recordings_dir, "s")

    import anyio.from_thread

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder_under_test.jobs.claim,
            JobState(
                session="s",
                kind="transcribe",
                current=0,
                total=1,
                started_at=datetime.now(UTC),
                status="running",
            ),
        )

    r = client.post(
        "/api/sessions/s/summarize",
        json={"source": "command", "command": _SUMMARIZE_CAT},
    )
    assert r.status_code == 409, r.text


def test_summarize_unwired_source_returns_400(client, recorder_under_test):
    """source='api' with no base_url configured still returns 400 — the
    ApiSummarizer constructor raises Unavailable for an empty base_url."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    r = client.post("/api/sessions/s/summarize", json={"source": "api"})
    assert r.status_code == 400, r.text


def test_summarize_local_without_extra_returns_400(client, recorder_under_test, monkeypatch):
    """The Local source degrades CLEARLY when the [summarize] extra isn't
    installed: a clean 400 at the boundary, not a crash mid-request. We force
    the dependency probe so the result is deterministic regardless of whether
    this box happens to have mlx_lm / llama_cpp installed."""
    import tapscribe.summarizers.catalog as summarizers_catalog

    monkeypatch.setattr(summarizers_catalog, "_backend_module_available", lambda backend: False)
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    r = client.post("/api/sessions/s/summarize", json={"source": "local"})
    assert r.status_code == 400, r.text
    assert "summarize" in r.json()["detail"].lower()


def test_summarize_local_model_load_failure_returns_400(client, recorder_under_test, monkeypatch):
    """A model that imports but won't LOAD (mlx_lm 'Received N parameters not in
    model', a corrupt GGUF, OOM) surfaces as a clean 400 with remediation — not a
    raw 500. Force the gguf route + a deterministic load failure so the result
    doesn't depend on which backends this box happens to have."""
    import tapscribe.summarizers.catalog as summarizers_catalog
    import tapscribe.summarizers.local as summarizers_local
    from tapscribe.transcribers.catalog import set_available_backends_for_testing

    set_available_backends_for_testing(frozenset({"cpu"}))  # deterministic gguf route
    monkeypatch.setattr(summarizers_catalog, "_backend_module_available", lambda backend: True)

    def boom(model_repo, gguf_file, *, max_tokens, n_ctx):
        raise ValueError("Received 126 parameters not in model: language_model...")

    monkeypatch.setattr(summarizers_local, "_build_gguf_generate", boom)
    try:
        seed_merged_transcript(recorder_under_test.recordings_dir, "s")
        r = client.post("/api/sessions/s/summarize", json={"source": "local"})
        assert r.status_code == 400, r.text
        assert summarizers_catalog.ENV_LOCAL_GGUF_MODEL in r.json()["detail"]
    finally:
        set_available_backends_for_testing(None)


def test_summarize_empty_command_returns_400(client, recorder_under_test):
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    r = client.post("/api/sessions/s/summarize", json={"source": "command", "command": ""})
    assert r.status_code == 400, r.text


def test_summarize_failed_command_returns_502(client, recorder_under_test):
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    failing = py_cmd("import sys; sys.exit(1)")
    r = client.post(
        "/api/sessions/s/summarize",
        json={"source": "command", "command": failing, "prompt": ""},
    )
    assert r.status_code == 502, r.text


def test_summarize_unknown_session_returns_404(client, recorder_under_test):  # noqa: ARG001
    r = client.post(
        "/api/sessions/does-not-exist/summarize",
        json={"source": "command", "command": _SUMMARIZE_CAT},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Persisted summary (#83): slim marker in /api/state + lazy GET for the body
# ---------------------------------------------------------------------------


def test_session_summary_get_returns_null_when_absent(client, recorder_under_test):
    (recorder_under_test.recordings_dir / "s").mkdir()
    r = client.get("/api/sessions/s/summary")
    assert r.status_code == 200, r.text
    assert r.json() is None


def test_session_summary_get_unknown_session_returns_404(client, recorder_under_test):  # noqa: ARG001
    r = client.get("/api/sessions/does-not-exist/summary")
    assert r.status_code == 404, r.text


def test_api_state_carries_slim_summary_marker_and_lazy_get_returns_body(client, recorder_under_test):
    """#83/#94: after a summarize, the session's /api/state row carries ONLY the
    slim `session_summary` marker (summarized_at + source + model +
    transcribed_at) — never the body — and GET /api/sessions/{session}/summary
    returns the full persisted summary. Mirrors the merged-transcript
    marker-plus-lazy-body split."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="we decided to ship")
    r = client.post(
        "/api/sessions/s/summarize",
        json={"source": "command", "command": _SUMMARIZE_CAT, "prompt": ""},
    )
    assert r.status_code == 200, r.text
    stamp = r.json()["summarized_at"]
    assert stamp

    state = client.get("/api/state").json()
    row = next(s for s in state["sessions"] if s["session"] == "s")
    # Strict equality pins the marker SLIM: exactly these four fields, no body.
    # transcribed_at (#94) is the stamp of the transcript this summary was built
    # from — the seed's merged transcript carries a fixed stamp.
    assert row["session_summary"] == {
        "summarized_at": stamp,
        "source": "command",
        "model": "",
        "transcribed_at": "2026-01-01T00:00:00+00:00",
    }

    # The synthetic current-session entry must carry the key too (None when
    # the current session has never been summarized).
    current = next(s for s in state["sessions"] if s["is_current"])
    assert "session_summary" in current

    full = client.get("/api/sessions/s/summary").json()
    assert full["summary"] == "we decided to ship"
    assert full["source"] == "command"
    assert full["summarized_at"] == stamp


def test_regenerate_replaces_stored_summary(client, recorder_under_test):
    """#83: one current summary per session — POST summarize twice, the lazy
    GET returns the second result."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="first take")
    r1 = client.post(
        "/api/sessions/s/summarize",
        json={"source": "command", "command": _SUMMARIZE_CAT, "prompt": ""},
    )
    assert r1.status_code == 200, r1.text
    regenerated = py_cmd("import sys; sys.stdin.read(); sys.stdout.write('REGENERATED')")
    r2 = client.post(
        "/api/sessions/s/summarize",
        json={"source": "command", "command": regenerated, "prompt": ""},
    )
    assert r2.status_code == 200, r2.text
    full = client.get("/api/sessions/s/summary").json()
    assert full["summary"] == "REGENERATED"


# ---------------------------------------------------------------------------
# GET /api/summarize/models — the local model dropdown's catalog
# ---------------------------------------------------------------------------


def test_api_summarize_models_lists_catalog(client):
    """The dropdown's source of truth: the hardware-routed catalog with one
    flagged default. The same table is the allowlist the local source validates
    against, so the dropdown can only ever offer loadable choices."""
    r = client.get("/api/summarize/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend"] in ("mlx", "gguf")
    assert body["default"]
    assert body["models"], "the catalog must offer at least one model"
    row = body["models"][0]
    assert {"repo_id", "label", "approx_gb", "context_tokens", "note", "is_default"} <= set(row)
    # Exactly the rows flagged is_default match the top-level default repo.
    assert [m["repo_id"] for m in body["models"] if m["is_default"]] == [body["default"]]
    # The output-cap knob the dropdown's number input seeds + bounds.
    assert body["max_tokens_min"] <= body["max_tokens_default"] <= body["max_tokens_max"]


def test_api_summarize_models_lists_command_presets(client):
    """The Command source's preset dropdown rides the same catalog fetch: known
    CLI tools as {key,label,template,note} rows. NOT an allowlist — the command
    field stays operator-editable free text; a preset only seeds it (the Claude
    row ships tool use disabled, hardening an operator wouldn't know to write)."""
    body = client.get("/api/summarize/models").json()
    presets = body["command_presets"]
    by_key = {p["key"]: p for p in presets}
    assert {"claude", "opencode"} <= set(by_key)
    for p in presets:
        assert {"key", "label", "template", "note"} <= set(p)
        assert p["label"] and p["template"]
    # The Claude preset ships hardened: tool use disabled in print mode.
    assert by_key["claude"]["template"].startswith("claude ")
    assert "--tools" in by_key["claude"]["template"]
    assert by_key["opencode"]["template"].startswith("opencode ")


def test_api_summarize_models_reflects_env_override(client, recorder_under_test, monkeypatch):
    """An operator's TAPSCRIBE_SUMMARIZE_{MLX,GGUF}_MODEL override is surfaced as
    the catalog's `default` AND bypasses the allowlist (it's operator-controlled,
    not untrusted request input). Forces the gguf route so the result is
    deterministic regardless of this box's hardware."""
    import tapscribe.summarizers.catalog as summarizers_catalog
    from tapscribe.transcribers.catalog import set_available_backends_for_testing

    set_available_backends_for_testing(frozenset({"cpu"}))  # deterministic gguf route
    monkeypatch.setenv(summarizers_catalog.ENV_LOCAL_GGUF_MODEL, "vendor/operator-custom-gguf")
    try:
        # 1. The endpoint surfaces the override as the active default.
        body = client.get("/api/summarize/models").json()
        assert body["backend"] == "gguf"
        assert body["default"] == "vendor/operator-custom-gguf"

        # 2. POSTing that override model is NOT rejected as an unknown model — it
        # passes the allowlist and reaches the missing-extra probe instead
        # (llama_cpp isn't importable on CI), proving the override was let through.
        monkeypatch.setattr(summarizers_catalog, "_backend_module_available", lambda backend: False)
        seed_merged_transcript(recorder_under_test.recordings_dir, "s")
        r = client.post(
            "/api/sessions/s/summarize",
            json={"source": "local", "model": "vendor/operator-custom-gguf"},
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "isn't a known" not in detail, f"override must bypass the allowlist, got {detail!r}"
        assert "summarize" in detail.lower()  # the missing-extra message it reached instead
    finally:
        set_available_backends_for_testing(None)


# ---------------------------------------------------------------------------
# GET/PUT /api/summarize/config — the structured global summarizer default
# (#84). Dedicated endpoints (NOT /api/config/{key}: that map is
# {content: str}-shaped) with write-time validation in text.py.
# ---------------------------------------------------------------------------


def test_summarizer_config_round_trips(client):
    client.put(
        "/api/summarize/config",
        json={
            "source": "command",
            "prompt": "Summarize into action items.",
            "command": "claude -p",
            "model": "",
            "max_tokens": 2048,
        },
    )
    got = client.get("/api/summarize/config").json()
    # GET returns the public projection (redacted shape with key_set).
    assert got == {
        "source": "command",
        "prompt": "Summarize into action items.",
        "command": "claude -p",
        "model": "",
        "max_tokens": 2048,
        "base_url": "",
        "key_set": False,
    }


def test_summarizer_config_put_rejects_bad_fields(client):
    from tapscribe.transcribers.catalog import set_available_backends_for_testing

    set_available_backends_for_testing(frozenset({"cpu"}))  # deterministic gguf route
    try:
        assert client.put("/api/summarize/config", json={"source": "bogus"}).status_code == 400
        r = client.put("/api/summarize/config", json={"model": "evil/not-in-catalog"})
        assert r.status_code == 400
        assert "evil/not-in-catalog" in r.json()["detail"]
        assert client.put("/api/summarize/config", json={"max_tokens": 9}).status_code == 400
        assert client.put("/api/summarize/config", json={"prompt": "x" * 5000}).status_code == 400
    finally:
        set_available_backends_for_testing(None)


def test_summarizer_config_put_empty_object_clears(client):
    client.put("/api/summarize/config", json={"source": "command", "command": "claude -p"})
    r = client.put("/api/summarize/config", json={})
    assert r.status_code == 200, r.text
    # GET returns the public projection (redacted shape with key_set).
    assert client.get("/api/summarize/config").json() == {
        "source": "",
        "prompt": "",
        "command": "",
        "model": "",
        "max_tokens": None,
        "base_url": "",
        "key_set": False,
    }


def test_summarize_empty_body_resolves_from_global_default(client, recorder_under_test):
    """#84: a body field the caller omits resolves session-override → global
    default → built-in. With a global Command default saved, POST {} runs the
    configured command — what the Generate button does once the view pre-fills
    from config."""
    client.put("/api/summarize/config", json={"source": "command", "command": _SUMMARIZE_CAT, "prompt": ""})
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="we shipped it")
    r = client.post("/api/sessions/s/summarize", json={})
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "we shipped it"
    assert r.json()["source"] == "command"


def test_summarize_session_override_prompt_reaches_summarizer(client, recorder_under_test):
    """The command source appends the prompt as the last argv element, so a
    prompt-echo command proves the session-meta override prompt (not the
    global one) reached the summarizer."""
    argv_echo = py_cmd("import sys; sys.stdin.read(); sys.stdout.write(sys.argv[-1])")
    client.put("/api/summarize/config", json={"source": "command", "command": argv_echo, "prompt": "GLOBAL"})
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    client.put("/api/session-meta/s", json={"summary_prompt": "SESSION OVERRIDE"})
    r = client.post("/api/sessions/s/summarize", json={})
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "SESSION OVERRIDE"


def test_summarize_explicit_body_beats_override_and_default(client, recorder_under_test):
    """The chain is body → session override → global default: an explicit
    Generate-time prompt wins over both saved layers."""
    argv_echo = py_cmd("import sys; sys.stdin.read(); sys.stdout.write(sys.argv[-1])")
    client.put("/api/summarize/config", json={"source": "command", "command": argv_echo, "prompt": "GLOBAL"})
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    client.put("/api/session-meta/s", json={"summary_prompt": "SESSION"})
    r = client.post("/api/sessions/s/summarize", json={"prompt": "BODY WINS"})
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "BODY WINS"


def test_api_state_surfaces_summarizer_default_public_fields_only(client):
    """The dashboard pre-fills the Settings card and the Summary view from the
    state poll. Strict key equality pins `summarizer_default_public` as the
    #85 redaction seam — api_key never appears, only key_set."""
    client.put(
        "/api/summarize/config",
        json={"source": "command", "prompt": "P", "command": "claude -p", "max_tokens": 512},
    )
    blob = client.get("/api/state").json()["summarizer_default"]
    assert blob == {
        "source": "command",
        "prompt": "P",
        "command": "claude -p",
        "model": "",
        "max_tokens": 512,
        "base_url": "",
        "key_set": False,
    }


def test_summarize_command_source_accepts_max_tokens_field(client, recorder_under_test):
    """The route parses an output-cap field without choking; the command source
    ignores it (an external CLI owns its own length), proving the body coercion
    is harmless across sources."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    r = client.post(
        "/api/sessions/s/summarize",
        json={"source": "command", "command": _SUMMARIZE_CAT, "max_tokens": 4096, "prompt": ""},
    )
    assert r.status_code == 200, r.text


def test_summarize_local_rejects_unknown_model_returns_400(client, recorder_under_test):
    """Proves the route forwards `model` AND that an untrusted, off-catalog repo
    is rejected at the boundary (→ 400) before any Hub access — a stray repo id
    from the dashboard can't reach mlx_lm.load / a download. The allowlist check
    fires inside the factory, before the transcript read, so it 400s regardless
    of which backends this box has."""
    from tapscribe.transcribers.catalog import set_available_backends_for_testing

    set_available_backends_for_testing(frozenset({"cpu"}))  # deterministic gguf route
    try:
        seed_merged_transcript(recorder_under_test.recordings_dir, "s")
        r = client.post(
            "/api/sessions/s/summarize",
            json={"source": "local", "model": "evil/not-in-catalog"},
        )
        assert r.status_code == 400, r.text
        assert "evil/not-in-catalog" in r.json()["detail"]
    finally:
        set_available_backends_for_testing(None)


# ---------------------------------------------------------------------------
# API summarizer source (#85): write-only key redaction at the route level
# ---------------------------------------------------------------------------


def test_api_summarizer_config_key_is_write_only_and_redacted(client):
    """The headline acceptance: PUT an api_key, then assert it NEVER appears
    in any GET response — only key_set (boolean) is exposed. The literal key
    string must not appear in either /api/summarize/config or /api/state."""
    # 1) Store config with a key.
    r = client.put(
        "/api/summarize/config",
        json={
            "source": "api",
            "base_url": "http://h:11434/v1",
            "model": "",
            "api_key": "s3cret-KEY",
        },
    )
    assert r.status_code == 200, r.text

    # 2) GET /api/summarize/config returns the public projection.
    get_resp = client.get("/api/summarize/config")
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["key_set"] is True
    assert get_body["base_url"] == "http://h:11434/v1"
    assert "api_key" not in get_body

    # 3) GET /api/state also returns the redacted projection.
    state_resp = client.get("/api/state")
    state_body = state_resp.json()
    summ_default = state_body["summarizer_default"]
    assert summ_default["key_set"] is True
    assert summ_default["base_url"] == "http://h:11434/v1"
    assert "api_key" not in summ_default

    # 4) Defence in depth: the literal key string must not appear anywhere
    #    in either response text.
    assert "s3cret-KEY" not in get_resp.text
    assert "s3cret-KEY" not in state_resp.text


def test_api_summarizer_config_key_cleared_via_empty_string(client):
    """Setting api_key to '' clears it, and key_set flips to False."""
    client.put(
        "/api/summarize/config",
        json={
            "source": "api",
            "base_url": "http://h:1/v1",
            "api_key": "some-key",
        },
    )
    assert client.get("/api/summarize/config").json()["key_set"] is True

    # Clear the key.
    client.put(
        "/api/summarize/config",
        json={
            "source": "api",
            "base_url": "http://h:1/v1",
            "api_key": "",
        },
    )
    body = client.get("/api/summarize/config").json()
    assert body["key_set"] is False
    assert body["base_url"] == "http://h:1/v1"


# ── Moonshine live surfacing — issue #121 ────────────────────────────────────


def test_api_models_live_includes_moonshine_when_installed(client):
    # autouse `_force_all_probes_installed` marks moonshine's probes importable
    r = client.get("/api/models?context=live")
    assert r.status_code == 200
    ids = {m["model_id"] for m in r.json()["models"]}
    assert {"moonshine-tiny", "moonshine-base"} <= ids


def test_api_models_live_excludes_moonshine_when_probe_absent(client):
    from tapscribe.transcribers.catalog import set_installed_modules_for_testing

    # nothing importable → the install-probe filter drops moonshine
    set_installed_modules_for_testing(frozenset())
    r = client.get("/api/models?context=live")
    assert r.status_code == 200
    ids = {m["model_id"] for m in r.json()["models"]}
    assert "moonshine-tiny" not in ids
    assert "moonshine-base" not in ids


def test_api_models_batch_excludes_moonshine(client):
    r = client.get("/api/models?context=batch")
    assert r.status_code == 200
    ids = {m["model_id"] for m in r.json()["models"]}
    assert "moonshine-tiny" not in ids
    assert "moonshine-base" not in ids
