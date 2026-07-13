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
import socket

import numpy as np
import pytest
from conftest import wait_for  # type: ignore[import-not-found]  # noqa: E402

from tapscribe.live import LiveConfig, WhisperLiveKitChannel
from tapscribe.live_relay import WlKRelay
from tapscribe.moonshine_live import (
    MoonshineAsrServer,
    MoonshineLiveChannel,
    resolve_live_channel_for_model,
)


class _SignalList(list):
    """Same event-backed collector `test_live_relay.py` uses — lets a test
    `await wait_count(n)` instead of a fixed sleep."""

    def __init__(self) -> None:
        super().__init__()
        self._event = asyncio.Event()

    def append(self, item) -> None:  # type: ignore[override]
        super().append(item)
        self._event.set()

    async def wait_count(self, n: int, *, timeout: float = 2.0) -> None:
        async def _wait() -> None:
            while len(self) < n:
                self._event.clear()
                if len(self) >= n:
                    return
                await self._event.wait()

        await asyncio.wait_for(_wait(), timeout=timeout)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


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

    port = _free_port()
    server = MoonshineAsrServer(host="localhost", port=port, generate_fn=stub_generate)
    await server.start()
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

    port = _free_port()
    server = MoonshineAsrServer(host="localhost", port=port, generate_fn=length_reporting_generate)
    await server.start()
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
