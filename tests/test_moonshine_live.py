"""Tests for tapscribe.moonshine_live — MoonshineAsrServer (the /asr wire
contract) and MoonshineLiveChannel (the LiveChannel Protocol lifecycle).

MoonshineAsrServer tests are driven by the REAL `WlKRelay` client (the
same production code WhisperLiveKit sessions use) rather than a hand-rolled
WS client, so passing tests prove the server is a faithful `/asr` speaker
— zero translation needed on the consumer side. No real Moonshine model
ever loads: `generate_fn` is an injected stub.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from conftest import wait_for  # type: ignore[import-not-found]  # noqa: E402

from tapscribe.live import LiveConfig, WhisperLiveKitChannel
from tapscribe.live_relay import WlKRelay
from tapscribe.moonshine_live import (
    MoonshineAsrServer,
    MoonshineLiveChannel,
    default_engine_factory,
    resolve_live_channel_for_model,
)


class _SignalList:
    """Same event-backed collector `test_live_relay.py` uses — lets a test
    `await wait_count(n)` instead of a fixed sleep. Composition over a
    list subclass: adding `_event` to a list subclass without `__eq__`
    trips CodeQL's py/missing-equals, and the tests only need append/len/
    iter/bool/indexing."""

    def __init__(self) -> None:
        self._items: list = []
        self._event = asyncio.Event()

    def append(self, item) -> None:
        self._items.append(item)
        self._event.set()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, i):
        return self._items[i]

    def __bool__(self) -> bool:
        return bool(self._items)

    async def wait_count(self, n: int, *, timeout: float = 2.0) -> None:
        async def _wait() -> None:
            while len(self) < n:
                self._event.clear()
                if len(self) >= n:
                    return
                await self._event.wait()

        await asyncio.wait_for(_wait(), timeout=timeout)


def _pcm_seconds(seconds: float, *, sample_rate: int = 16000) -> bytes:
    n = int(seconds * sample_rate)
    return np.zeros(n, dtype=np.int16).tobytes()


# ---------------------------------------------------------------------------
# MoonshineAsrServer — the /asr contract, driven by a real WlKRelay client.
# ---------------------------------------------------------------------------


@pytest.fixture
async def running_server():
    calls = {"n": 0}
    texts = ["hello", "hello there", "hello there friend"]

    def stub_generate(arr: np.ndarray) -> str:
        i = min(calls["n"], len(texts) - 1)
        calls["n"] += 1
        return texts[i]

    server = MoonshineAsrServer(host="localhost", port=0, generate_fn=stub_generate)
    await server.start()
    port = server.port
    try:
        yield server, port, calls
    finally:
        await server.stop()


async def test_growing_line_reaches_wlkrelay_as_final_settled_text(running_server):
    """The server's cumulative `lines` snapshot must be exactly what
    WlKRelay already knows how to consume. Here the stub's text grows
    across three refreshes on a SINGLE still-open line (well under the
    default chunk_s, so no rollover) — WlKRelay holds it as the tail the
    whole time (it never stabilises for 3 consecutive snapshots since the
    text keeps changing) and flushes the final text whole on close, via
    the exact same `_flush_tail` path production WhisperLiveKit sessions
    rely on."""
    server, port, _calls = running_server
    settled = _SignalList()
    relay = WlKRelay(host="localhost", port=port, language="en", on_settled_line=settled.append)
    assert await relay.connect() is True

    # Feed enough PCM to trigger three refreshes (refresh_s default ~0.5s).
    for _ in range(3):
        await relay.send(_pcm_seconds(0.6))
        await asyncio.sleep(0.05)

    await relay.close()
    assert list(settled) == ["hello there friend"]


async def test_server_never_calls_generate_fn_with_no_audio(running_server):
    """A client that connects and disconnects without ever sending PCM
    (e.g. the gate never opened) must not invoke the model at all."""
    server, port, calls = running_server
    relay = WlKRelay(host="localhost", port=port, language="en", on_settled_line=lambda _t: None)
    assert await relay.connect() is True
    await relay.close()
    assert calls["n"] == 0


async def test_two_connections_get_independent_windows():
    """Each `/asr` connection is one `/tap` utterance — state must not
    leak between connections (no shared rolling window). The stub reports
    how many samples IT was handed; if a second connection's window
    somehow inherited the first connection's buffer, the sample count
    would be roughly double what a single 0.6 s feed produces."""
    seen_lengths: list[int] = []

    def length_reporting_generate(arr: np.ndarray) -> str:
        seen_lengths.append(arr.shape[0])
        return "ok"

    server = MoonshineAsrServer(host="localhost", port=0, generate_fn=length_reporting_generate)
    await server.start()
    port = server.port
    try:
        relay1 = WlKRelay(host="localhost", port=port, language="en", on_settled_line=lambda _t: None)
        await relay1.connect()
        await relay1.send(_pcm_seconds(0.6))
        await wait_for(lambda: len(seen_lengths) >= 1)
        await relay1.close()

        relay2 = WlKRelay(host="localhost", port=port, language="en", on_settled_line=lambda _t: None)
        await relay2.connect()
        await relay2.send(_pcm_seconds(0.6))
        await wait_for(lambda: len(seen_lengths) >= 2)
        await relay2.close()
    finally:
        await server.stop()

    one_feed_samples = int(0.6 * 16000)
    assert seen_lengths[0] == one_feed_samples
    assert seen_lengths[1] == one_feed_samples  # NOT ~2x — no cross-connection leak


# ---------------------------------------------------------------------------
# MoonshineLiveChannel — Protocol lifecycle, with an injected engine.
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self) -> None:
        self.generate_calls = 0

    def generate(self, audio: np.ndarray) -> str:
        self.generate_calls += 1
        return "fake text"


def _channel(*, use_mlx: bool = False, port: int = 0) -> MoonshineLiveChannel:
    config = LiveConfig(model="moonshine-tiny", language="en", host="localhost", port=port)
    fake = _FakeEngine()
    return MoonshineLiveChannel(config=config, use_mlx=use_mlx, engine_factory=lambda *a, **k: fake)


def test_supports_native_vad_is_false():
    """Moonshine has no built-in VAC — `gate_kind='backend'` must be a
    no-op/rejected, mirroring how the dashboard greys it out for any
    channel with no native VAD (see live.py's LiveChannel docstring)."""
    ch = _channel()
    assert ch.supports_native_vad is False


def test_not_running_before_start():
    ch = _channel()
    assert ch.running() is False
    assert ch.info["state"] == "stopped"


def test_start_binds_server_and_reports_running():
    ch = _channel()
    try:
        ok, _msg = ch.start()
        assert ok is True
        assert ch.running() is True
        assert ch.info["state"] == "running"
        assert ch.info["model"] == "moonshine-tiny"
        assert ch.info["language"] == "en"
    finally:
        ch.stop()


def test_stop_is_idempotent_and_clears_running():
    ch = _channel()
    ch.start()
    ok, _msg = ch.stop()
    assert ok is True
    assert ch.running() is False
    assert ch.info["state"] == "stopped"
    # Second stop is a clean no-op, not an error.
    ok2, _msg2 = ch.stop()
    assert ok2 is True


def test_matches_true_only_when_running_with_same_model_and_language():
    ch = _channel()
    ch.start()
    try:
        assert ch.matches(model="moonshine-tiny", language="en", gate_kind=None, conf=None) is True
        assert ch.matches(model="moonshine-base", language="en", gate_kind=None, conf=None) is False
        assert ch.matches(model=None, language=None, gate_kind=None, conf=None) is True
    finally:
        ch.stop()


def test_matches_ignores_conf_change_when_engine_has_no_confidence_knob():
    """Moonshine has no confidence-validation knob
    (`supports_confidence_validation=False`), so a `conf` change must NOT
    force a restart — `matches` stays True even when `conf` differs from the
    carried config (WhisperLiveKit, which HAS the knob, returns False here).
    Pins the capability flag being honoured in `matches`, not just the info
    mirror — the two flags (`fixed_language`, `supports_confidence_validation`)
    are genuinely parallel."""
    ch = _channel()
    ch.start()
    try:
        assert ch.supports_confidence_validation is False
        assert ch.config.confidence_validation is True  # LiveConfig default
        assert ch.matches(model="moonshine-tiny", language="en", gate_kind=None, conf=False) is True
    finally:
        ch.stop()


def test_begin_transition_marks_starting_before_the_real_restart():
    ch = _channel()
    ch.start()
    try:
        ch.begin_transition(model="moonshine-base", language="en")
        assert ch.info["state"] == "starting"
        assert ch.info["model"] == "moonshine-base"
    finally:
        ch.stop()


def test_restart_with_new_model_swaps_engine():
    """stop() then start(model=...) — the existing machinery's restart
    sequence — must load a NEW engine for the new model, not keep serving
    the old one."""
    calls: list[str] = []

    def engine_factory(model_id: str, *, use_mlx: bool):
        calls.append(model_id)
        return _FakeEngine()

    config = LiveConfig(model="moonshine-tiny", language="en", host="localhost", port=0)
    ch = MoonshineLiveChannel(config=config, use_mlx=False, engine_factory=engine_factory)
    ch.start()
    ch.stop()
    ch.start(model="moonshine-base")
    try:
        assert calls == ["moonshine-tiny", "moonshine-base"]
        assert ch.info["model"] == "moonshine-base"
    finally:
        ch.stop()


# ---------------------------------------------------------------------------
# resolve_live_channel_for_model — the family-swap routing predicate
# `/api/live/start` applies before its usual matches/start sequence.
# ---------------------------------------------------------------------------


def test_resolve_no_swap_when_target_is_same_family():
    whisper = WhisperLiveKitChannel(
        config=LiveConfig(model="tiny.en", language="en", host="localhost", port=0), use_mlx=False
    )
    assert resolve_live_channel_for_model(whisper, target_model="large-v3", use_mlx=False) is None

    moonshine = _channel()
    assert resolve_live_channel_for_model(moonshine, target_model="moonshine-base", use_mlx=False) is None


def test_resolve_swaps_whisper_to_moonshine():
    whisper = WhisperLiveKitChannel(
        config=LiveConfig(model="tiny.en", language="en", host="localhost", port=12345), use_mlx=False
    )
    new_channel = resolve_live_channel_for_model(whisper, target_model="moonshine-tiny", use_mlx=False)
    assert isinstance(new_channel, MoonshineLiveChannel)
    # Port reset to ephemeral (0) rather than carrying forward the old
    # channel's (about-to-be-freed) port.
    assert new_channel.config.port == 0


def test_resolve_swaps_moonshine_to_whisper():
    moonshine = _channel(port=54321)
    new_channel = resolve_live_channel_for_model(moonshine, target_model="tiny.en", use_mlx=False)
    assert isinstance(new_channel, WhisperLiveKitChannel)
    assert new_channel.config.port == 0


def test_resolve_unknown_model_id_treated_as_not_moonshine():
    """An unrecognized model id must never crash the routing predicate —
    it's simply routed as "not Moonshine" (the common/safe case)."""
    moonshine = _channel()
    new_channel = resolve_live_channel_for_model(moonshine, target_model="not-a-real-model", use_mlx=False)
    assert isinstance(new_channel, WhisperLiveKitChannel)


def test_info_reports_backend_for_both_runtimes():
    """PRD #120 story 13: live_info must say WHICH engine actually runs.
    Positive value assertions on both runtime labels — the verification
    pass found blanking info["backend"] tripped nothing."""
    assert _channel(use_mlx=False).info["backend"] == "moonshine-onnx"
    assert _channel(use_mlx=True).info["backend"] == "mlx-audio"


def test_start_with_failing_engine_factory_fails_loudly():
    """PRD #120 story 14: a misconfigured/unavailable install surfaces as
    (False, actionable message) + info state "error" + last_error — never
    a silent success with no captions. A swallow-and-claim-success
    mutation in start()'s except branch must go red here."""

    def exploding_factory(model_id: str, *, use_mlx: bool):
        raise RuntimeError(
            "useful-moonshine-onnx is not installed. Install `pip install tapscribe[moonshine-cpu]`."
        )

    ch = MoonshineLiveChannel(
        config=LiveConfig(model="moonshine-tiny", language="en", host="localhost", port=0),
        use_mlx=False,
        engine_factory=exploding_factory,
    )
    ok, msg = ch.start()
    assert ok is False
    assert "failed to load Moonshine engine" in msg
    assert "moonshine-cpu" in msg  # the actionable install hint travels
    assert ch.info["state"] == "error"
    assert "moonshine-cpu" in ch.info["last_error"]
    assert ch.running() is False


def test_default_engine_factory_validates_before_any_adapter_load(monkeypatch):
    """PRD #120 story 23, the defence-in-depth layer under the route's
    allowlist: `validate_moonshine_model` must reject an uncataloged id
    BEFORE either adapter's `load()` can reach a loader / Hub download."""
    monkeypatch.setattr(
        "tapscribe.transcribers.moonshine_mlx.MlxMoonshineEngine.load",
        classmethod(lambda cls, model_id: pytest.fail("MLX load() must not run for a rejected id")),
    )
    monkeypatch.setattr(
        "tapscribe.transcribers.moonshine_onnx.OnnxMoonshineEngine.load",
        classmethod(lambda cls, model_id: pytest.fail("ONNX load() must not run for a rejected id")),
    )
    with pytest.raises(ValueError, match="evil-model"):
        default_engine_factory("evil-model", use_mlx=False)
    with pytest.raises(ValueError, match="evil-model"):
        default_engine_factory("evil-model", use_mlx=True)


# ---------------------------------------------------------------------------
# Boot-time auto-start (config.AUTO_START_LIVE) must apply the SAME swap —
# otherwise a persisted Moonshine default live model would try to spawn
# whisperlivekit-server with an unsupported --model at boot (issue #259's
# failure mode, re-triggered from a different entry point).
# ---------------------------------------------------------------------------


def test_auto_start_swaps_to_moonshine_before_starting(tmp_path, monkeypatch):
    from conftest import repoint_config_files  # type: ignore[import-not-found]

    from tapscribe import config as _config
    from tapscribe.app import app, get_recorder
    from tapscribe.recorder import Recorder

    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", True)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()

    class _FakeEngine:
        def generate(self, audio):
            return "ok"

    monkeypatch.setattr(
        "tapscribe.moonshine_live.default_engine_factory",
        lambda model_id, *, use_mlx: _FakeEngine(),
    )

    recorder = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        # The persisted default live model names a Moonshine model — the
        # Recorder still boots a WhisperLiveKitChannel unconditionally
        # (see recorder.py); the lifespan's auto-start must swap it.
        live_config=LiveConfig(model="moonshine-tiny", language="en", host="localhost", port=0),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    from starlette.testclient import TestClient

    app.dependency_overrides[get_recorder] = lambda: recorder
    app.state.recorder = recorder
    try:
        with TestClient(app):
            assert isinstance(recorder.live, MoonshineLiveChannel)
            assert recorder.live.running() is True
            assert recorder.live.info["state"] == "running"
    finally:
        app.dependency_overrides.clear()
        if recorder.live.running():
            recorder.live.stop()


async def test_burst_tail_fed_after_last_refresh_reaches_the_relay(running_server):
    """Finding #5: audio that arrives after the last cadence refresh is
    only ever decoded by the close-time flush. That flush must actually
    REACH the relay — the server delivers the final snapshot in response
    to the end-of-audio signal (`b""`, the same wire signal
    whisperlivekit's own web client sends on stop), BEFORE the close
    handshake cuts off its send path. Pre-fix the final send raced the
    already-initiated WS close and lost the words on essentially every
    utterance end."""
    server, port, _calls = running_server
    settled = _SignalList()
    relay = WlKRelay(host="localhost", port=port, language="en", on_settled_line=settled.append)
    assert await relay.connect() is True

    # One refresh-worth of audio ("hello"), then a sub-cadence tail that
    # only the close-time decode will ever see.
    await relay.send(_pcm_seconds(0.6))
    await asyncio.sleep(0.05)
    await relay.send(_pcm_seconds(0.3))

    await relay.close()
    # The stub's later decodes say "hello there" / "hello there friend";
    # whatever the close-time decode produced, the final words must be in
    # the settled output — not truncated at the last mid-stream snapshot.
    assert settled, "no settled lines at all"
    full_text = " ".join(settled)
    assert full_text.endswith("friend") or full_text.endswith("there"), full_text
    assert full_text != "hello", "tail words were truncated at close"


async def test_inference_runs_off_the_asr_server_event_loop():
    """Finding #7: `generate_fn` is synchronous model inference over up to
    ~25s of audio — run on the /asr server's event loop it stalls every
    other connection on that loop. Structural proof (no wall-clock
    thresholds, house pattern from test_tap_relay.py): a genuinely
    thread-blocking generate_fn records whether a loop-side coroutine got
    to run WHILE it was blocked. Pre-fix the loop is blocked for the
    whole call, so the release can only fire after the factory has given
    up on its own bounded timeout — `released_in_time` reads False."""
    import threading

    started = threading.Event()
    release = threading.Event()
    outcome: dict = {}

    def blocking_generate(arr: np.ndarray) -> str:
        started.set()
        outcome["released_in_time"] = release.wait(timeout=3.0)
        return "slow words"

    server = MoonshineAsrServer(host="localhost", port=0, generate_fn=blocking_generate)
    await server.start()
    port = server.port
    try:
        relay = WlKRelay(host="localhost", port=port, language="en", on_settled_line=lambda _t: None)
        assert await relay.connect() is True

        async def release_while_blocked() -> None:
            # Resumes only if the loop is alive while generate blocks.
            await asyncio.to_thread(started.wait, 3.0)
            for _ in range(5):
                await asyncio.sleep(0)
            release.set()

        releaser = asyncio.create_task(release_while_blocked())
        await relay.send(_pcm_seconds(0.6))  # crosses the refresh cadence -> one decode
        await asyncio.wait_for(releaser, timeout=6.0)
        await asyncio.to_thread(lambda: wait_for_sync(lambda: "released_in_time" in outcome))
        await relay.close()
    finally:
        await server.stop()

    assert outcome.get("released_in_time") is True, (
        "the loop never ran concurrently with inference — generate_fn is stalling the /asr event loop"
    )


def wait_for_sync(predicate, *, timeout: float = 3.0) -> None:
    """Tiny thread-side poll helper for the off-loop test above."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while not predicate():
        if _time.monotonic() > deadline:
            raise TimeoutError("condition not met in time")
        _time.sleep(0.01)


# ---------------------------------------------------------------------------
# Config roundtrip + gate-tuning contract (PR #334 findings 3/8/9).
# ---------------------------------------------------------------------------


def test_operator_config_survives_a_moonshine_roundtrip():
    """Finding #3: swapping whisper(no) -> moonshine -> whisper must carry
    the operator's language and gate_kind through unchanged. Moonshine
    IGNORES language at inference time (English-only) — reflected in
    `info` for the dashboard — but must not mutate the persisted/carried
    LiveConfig."""
    whisper = WhisperLiveKitChannel(
        config=LiveConfig(
            model="nb-whisper-small",
            language="no",
            host="localhost",
            port=0,
            gate_kind="backend",
            gate_speech_threshold=0.7,
        ),
        use_mlx=False,
    )
    moonshine = resolve_live_channel_for_model(whisper, target_model="moonshine-tiny", use_mlx=False)
    assert isinstance(moonshine, MoonshineLiveChannel)
    # The carried config is preserved verbatim (bar the ephemeral port)...
    # The carried "backend" never yields a gate-less Moonshine tap:
    # TapRelay._attach coerces gate construction to "tapscribe" for
    # channels with no native VAD (see test_tap_relay.py::
    # test_gate_kind_backend_coerced_to_tapscribe_when_channel_has_no_native_vad).
    assert moonshine.config.language == "no"
    assert moonshine.config.gate_kind == "backend"
    assert moonshine.config.gate_speech_threshold == 0.7
    # ...while the dashboard-facing info reflects the engine's actual
    # (English-only, TapScribe-gated) behaviour.
    assert moonshine.info["language"] == "en"
    assert moonshine.info["gate_kind"] == "tapscribe"

    back = resolve_live_channel_for_model(moonshine, target_model="nb-whisper-small", use_mlx=False)
    assert isinstance(back, WhisperLiveKitChannel)
    assert back.config.language == "no"
    assert back.config.gate_kind == "backend"
    assert back.config.gate_speech_threshold == 0.7


def test_matches_ignores_language_because_start_does():
    """Finding #9: start() deliberately ignores language (English-only
    engine), so a language mismatch must not force a pointless full
    engine-reload restart — matches() treats language as always-matching."""
    ch = _channel()
    ch.start()
    try:
        assert ch.matches(model=None, language="no", gate_kind=None, conf=None) is True
        assert ch.matches(model="moonshine-tiny", language="da", gate_kind=None, conf=None) is True
    finally:
        ch.stop()


def test_gate_knob_change_is_honored_without_any_restart():
    """Finding #8, post-#224/#338 contract: the tapscribe SpeechGate is
    Moonshine's ONLY gate and each /tap builds it from live.config — so a
    knob change must land in config. Per #338, gate knobs are
    Recorder-side: matches() accepts-but-ignores them (no restart at
    all), and the route applies them via apply_gate_knobs on the
    no-restart path."""
    factory_calls: list[str] = []

    def engine_factory(model_id: str, *, use_mlx: bool):
        factory_calls.append(model_id)
        return _FakeEngine()

    config = LiveConfig(model="moonshine-tiny", language="en", host="localhost", port=0)
    ch = MoonshineLiveChannel(config=config, use_mlx=False, engine_factory=engine_factory)
    ch.start()
    try:
        # Gate knobs never force a restart — matches() ignores them, same
        # as WhisperLiveKitChannel post-#338.
        assert (
            ch.matches(model=None, language=None, gate_kind=None, conf=None, gate_speech_threshold=0.9)
            is True
        )

        # The route's no-restart path: apply_gate_knobs writes changed
        # knobs into config + info, with NO stop/start and NO engine load.
        ch.apply_gate_knobs(gate_speech_threshold=0.9, gate_hangover_ms=750)
        assert ch.config.gate_speech_threshold == 0.9
        assert ch.config.gate_hangover_ms == 750
        assert ch.running() is True
        assert ch.info["state"] == "running"
        assert factory_calls == ["moonshine-tiny"], "knob apply must not reload the engine"
        # The dashboard sliders read live_info — it must mirror the knobs.
        assert ch.info["gate_speech_threshold"] == "0.90"
        assert ch.info["gate_hangover_ms"] == "750"

        # The #238 guard: a display-rounded re-submit of the SAME threshold
        # must not churn the frozen config object.
        before = ch.config
        ch.apply_gate_knobs(gate_speech_threshold=0.9, gate_hangover_ms=750)
        assert ch.config is before
    finally:
        ch.stop()


def test_rejected_live_start_leaves_running_channel_untouched(tmp_path, monkeypatch):
    """PR #334 finding #2: a request the route rejects with 400 must leave
    the running live channel exactly as it was — pre-fix the family swap
    (stop + reassign recorder.live) executed BEFORE boundary validation,
    so a bad knob killed the operator's healthy channel. The
    gate_kind='backend' rejection must also be judged against the TARGET
    channel (Moonshine: no native VAD), not the pre-swap one."""
    from conftest import repoint_config_files  # type: ignore[import-not-found]
    from starlette.testclient import TestClient

    from tapscribe import config as _config
    from tapscribe.app import app, get_recorder
    from tapscribe.recorder import Recorder

    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()

    recorder = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=0),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    # A running fake in place of the whisperlivekit child: the route only
    # consults running()/matches()/config/info, and the point is that NONE
    # of stop()/begin_transition()/start() runs on a rejected request.
    whisper_live = recorder.live

    class _RunningFake:
        config = whisper_live.config
        info = dict(whisper_live.info, state="running")
        supports_native_vad = True

        def __init__(self) -> None:
            self.stopped = False

        def running(self) -> bool:
            return True

        def stop(self, *, timeout: float = 5.0):
            self.stopped = True
            return True, "stopped"

    fake = _RunningFake()
    recorder.live = fake

    app.dependency_overrides[get_recorder] = lambda: recorder
    try:
        with TestClient(app) as client:
            # Out-of-bounds knob alongside a family-swapping model.
            r = client.post(
                "/api/live/start",
                json={"model": "moonshine-tiny", "gate_hangover_ms": 99999},
            )
            assert r.status_code == 400
            assert recorder.live is fake, "400 must not swap the channel"
            assert fake.stopped is False, "400 must not stop the running channel"

            # gate_kind='backend' is judged against the TARGET (Moonshine,
            # no native VAD) — rejected, again with no side effects.
            r = client.post(
                "/api/live/start",
                json={"model": "moonshine-tiny", "gate_kind": "backend"},
            )
            assert r.status_code == 400
            assert recorder.live is fake
            assert fake.stopped is False
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# start() ready-timeout — a late-completing bind must be torn down, and
# model-thread affinity — load + decodes on ONE dedicated thread.
# ---------------------------------------------------------------------------


def test_start_ready_timeout_tears_down_a_late_binding_server(monkeypatch):
    """A bind completing AFTER start()'s ready deadline used to leave a
    running /asr server + event loop that no channel field referenced —
    an orphan stop() could never reach, listening for the process
    lifetime (and retry-blocking a fixed port). The timeout branch now
    flags the spawn abandoned and stops the loop, so `_run` unwinds:
    server closed, loop closed, and stop() stays a safe no-op."""
    import time as _time

    from tapscribe import moonshine_live as ml

    monkeypatch.setattr(ml, "_READY_TIMEOUT_S", 0.05)
    real_bound = ml._bound_socket

    def slow_bound(host: str, port: int):
        _time.sleep(0.4)  # block the loop thread well past the deadline
        return real_bound(host, port)

    monkeypatch.setattr(ml, "_bound_socket", slow_bound)

    # Record the loop the channel spins up so the test can observe its
    # teardown — the channel deliberately never stores it on a timeout.
    created: dict = {}
    real_new_loop = asyncio.new_event_loop

    def recording_new_event_loop():
        loop = real_new_loop()
        created["loop"] = loop
        return loop

    monkeypatch.setattr(ml.asyncio, "new_event_loop", recording_new_event_loop)

    ch = _channel()
    ok, msg = ch.start()

    assert ok is False
    assert "timed out" in msg
    assert ch.info["state"] == "error"
    assert ch.running() is False
    # The late bind must be unwound: _run's teardown tail closes the
    # server and THEN the loop, so a closed loop proves the whole tail ran.
    wait_for_sync(lambda: created["loop"].is_closed(), timeout=5.0)
    # And stop() after a timed-out start is a clean idempotent no-op.
    assert ch.stop() == (True, "not running")


def test_engine_load_and_all_decodes_share_one_dedicated_model_thread():
    """Decodes used to run via asyncio.to_thread (the loop's multi-worker
    default executor) while the engine was loaded on a different thread —
    violating the MLX thread-affinity rule transcribers/__init__.py pins
    (weights created on one thread can't be evaluated from another) and
    allowing concurrent generate() calls with 2+ taps. The channel now
    routes the engine load AND every window decode through one dedicated
    single-worker executor; pin it via the executor's thread-name prefix."""
    import threading as _threading

    thread_names: list[str] = []

    class _RecordingEngine:
        def __init__(self) -> None:
            thread_names.append(_threading.current_thread().name)  # load site

        def generate(self, audio: np.ndarray) -> str:
            thread_names.append(_threading.current_thread().name)  # decode site
            return "recorded"

    config = LiveConfig(model="moonshine-tiny", language="en", host="localhost", port=0)
    ch = MoonshineLiveChannel(config=config, use_mlx=False, engine_factory=lambda *a, **k: _RecordingEngine())
    ok, _msg = ch.start()
    assert ok is True
    try:

        async def drive_one_utterance() -> None:
            relay = WlKRelay(
                host="localhost", port=ch.config.port, language="en", on_settled_line=lambda _t: None
            )
            assert await relay.connect() is True
            await relay.send(_pcm_seconds(0.6))  # crosses the refresh cadence
            await relay.close()  # end-of-audio marker -> window.close() decode

        asyncio.run(drive_one_utterance())
    finally:
        ch.stop()

    assert len(thread_names) >= 2, "expected the load plus at least one decode"
    assert all(n.startswith("tapscribe-moonshine-model") for n in thread_names), thread_names
