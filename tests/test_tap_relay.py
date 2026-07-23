"""Unit tests for TapRelay — the per-`/tap` live leg.

These exercise the reconnect/backoff state machine THROUGH the public
interface (`open` / `feed` / `close` + the `reconnect_attempts` /
`connected` read-surface), with an injected fake relay and gate. No
Recorder, no WhisperLiveKit child, no websocket server — which is the
whole point of extracting the seam: the live path's most-broken part is
now testable in isolation instead of via private-field backdoors on
TapFanOut.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from tapscribe import tap_relay as tr
from tapscribe.live_relay import WlKRelay
from tapscribe.speech_gate import FRAME_BYTES, SpeechGate
from tapscribe.tap_relay import FedFrames, RelayHandlers, TapRelay, _default_relay_factory

PCM_FRAME = b"\x10\x00" * 320  # 20 ms @ 16 kHz mono int16


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class _FakeConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    language: str = "en"
    model: str = "small"


class _FakeLive:
    """Stands in for a LiveChannel: a mutable config + a running flag."""

    def __init__(self, config: _FakeConfig, *, running: bool = True) -> None:
        self.config = config
        self._running = running

    def running(self) -> bool:
        return self._running


class _FakeRelay:
    """A relay that records what it was sent and can be 'killed' so the
    next send reports failure (mimicking a WlK socket close)."""

    def __init__(self, *, connect_ok: bool = True) -> None:
        # `alive` doubles as connectability: connect() returns it, and a
        # test flips it to False to simulate a mid-stream WlK socket close.
        self.alive = connect_ok
        self.sent: list[bytes] = []
        self.closed = False

    async def connect(self) -> bool:
        return self.alive

    async def send(self, data: bytes) -> bool:
        if not self.alive:
            return False
        self.sent.append(data)
        return True

    async def close(self) -> None:
        self.closed = True


class _RecordingFactory:
    """Builds a fresh _FakeRelay per call, recording the (cfg, handlers)
    each was built with — so a test can assert a reconnect read the
    current config and wired the handlers through."""

    def __init__(self, *, connect_ok: bool = True) -> None:
        self.connect_ok = connect_ok
        self.calls: list[tuple[_FakeConfig, RelayHandlers]] = []
        self.relays: list[_FakeRelay] = []

    def __call__(self, cfg: _FakeConfig, handlers: RelayHandlers) -> _FakeRelay:
        self.calls.append((cfg, handlers))
        relay = _FakeRelay(connect_ok=self.connect_ok)
        self.relays.append(relay)
        return relay


class _FakeGate:
    """A gate whose per-frame output and open-state the test controls."""

    def __init__(self, output: list[bytes], *, is_open: bool) -> None:
        self._output = output
        self._open = is_open

    def feed(self, frame: bytes) -> list[bytes]:
        return list(self._output)

    @property
    def is_open(self) -> bool:
        return self._open


async def _noop_metrics(_lag: float) -> None:
    return None


def _handlers(
    *,
    settled: list[str] | None = None,
    buffered: list[str] | None = None,
) -> RelayHandlers:
    return RelayHandlers(
        on_settled_line=(settled.append if settled is not None else (lambda _t: None)),
        on_metrics=_noop_metrics,
        on_buffer=(buffered.append if buffered is not None else (lambda _t: None)),
    )


def _tap_relay(live: _FakeLive, *, do_live: bool, factory: _RecordingFactory, gate=None) -> TapRelay:
    return TapRelay(
        live,
        do_live=do_live,
        handlers=_handlers(),
        label="alice",
        relay_factory=factory,
        gate_factory=(lambda _cfg: gate),
    )


# --------------------------------------------------------------------------
# Inert / record-only
# --------------------------------------------------------------------------


async def test_record_only_tap_is_inert():
    """do_live=False never builds a relay; feed passes audio straight
    through (frames=(buf,), gate closed) and nothing is sent."""
    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=False, factory=factory)

    await relay.open()
    fed = await relay.feed(PCM_FRAME)

    assert relay.connected is None
    assert relay.reconnect_attempts == 0
    assert fed == FedFrames(frames=(PCM_FRAME,), gate_open=False)
    assert factory.calls == []  # no relay ever constructed


async def test_open_dormant_when_channel_not_running():
    """A live tap opened while the channel is down stays dormant — no
    relay, no connect attempt — until the channel comes up."""
    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig(), running=False), do_live=True, factory=factory)

    await relay.open()

    assert relay.connected is None
    assert factory.calls == []


# --------------------------------------------------------------------------
# Attach + forward
# --------------------------------------------------------------------------


async def test_open_attaches_and_feed_forwards():
    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig(port=9100, language="nb")), do_live=True, factory=factory)

    await relay.open()
    assert relay.connected == ("127.0.0.1", 9100, "nb")

    await relay.feed(PCM_FRAME)
    assert factory.relays[0].sent == [PCM_FRAME]


async def test_gate_filters_frames_and_reports_open_state():
    """When a gate is present, feed returns the gate's surviving frames
    (for the meter) and its open-state, and only those frames are sent."""
    factory = _RecordingFactory()
    gate = _FakeGate(output=[], is_open=False)  # warming up: drops the frame
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=True, factory=factory, gate=gate)

    await relay.open()
    fed = await relay.feed(PCM_FRAME)

    assert fed == FedFrames(frames=(), gate_open=False)
    assert factory.relays[0].sent == []  # nothing survived the gate → nothing sent


# --------------------------------------------------------------------------
# Gate-kind coercion for channels with no native VAD (PRD #120 stories 6/15)
# --------------------------------------------------------------------------


@dataclass
class _GateKindConfig(_FakeConfig):
    gate_kind: str = "backend"


@pytest.mark.parametrize(
    ("native_vad", "expected_gate_kind"),
    [(False, "tapscribe"), (True, "backend")],
    ids=["no-native-vad-coerces", "native-vad-keeps-backend"],
)
async def test_gate_kind_backend_coercion_is_keyed_on_native_vad(native_vad, expected_gate_kind):
    """A carried-forward gate_kind="backend" on a channel with no native
    VAD (Moonshine after a family swap — the config deliberately keeps
    the operator's preference for the swap back to Whisper) must NOT
    produce a gate-less tap: there is no backend VAD to hand off to, so
    `build_gate_for_config` returning None would feed raw UNGATED PCM
    (silence included) to the engine. The gate construction coerces to
    "tapscribe" via `effective_gate_config` — matching what such a
    channel's info["gate_kind"] reports (same seam). A channel WITH
    native VAD (WhisperLiveKit's --vac child) keeps the operator's
    "backend" pick — gate=None is then correct, the backend gates."""
    captured: list = []

    def gate_factory(cfg):
        captured.append(cfg)
        return None

    live = _FakeLive(_GateKindConfig())
    live.supports_native_vad = native_vad
    relay = TapRelay(
        live,
        do_live=True,
        handlers=_handlers(),
        relay_factory=_RecordingFactory(),
        gate_factory=gate_factory,
    )
    await relay.open()
    assert [c.gate_kind for c in captured] == [expected_gate_kind]


# --------------------------------------------------------------------------
# Reconnect + backoff (the headline behaviour, now at the interface)
# --------------------------------------------------------------------------


async def test_relay_death_then_backoff_coalesces_burst(monkeypatch: pytest.MonkeyPatch):
    """A dead relay + a burst of frames must fire exactly ONE reconnect
    attempt inside the backoff window, not one per frame."""
    monkeypatch.setattr(tr, "RELAY_RECONNECT_BACKOFF_S", 100.0)
    live = _FakeLive(_FakeConfig())
    factory = _RecordingFactory(connect_ok=True)
    relay = _tap_relay(live, do_live=True, factory=factory)
    await relay.open()  # relay 0 alive

    # WlK goes down: reconnects will fail, and the live relay's next send fails.
    factory.connect_ok = False
    factory.relays[0].alive = False

    # First feed detects the death (send → False) and marks disconnected.
    await relay.feed(PCM_FRAME)
    assert relay.connected is None

    # A burst of further frames inside the backoff window → one attempt.
    for _ in range(8):
        await relay.feed(PCM_FRAME)
        await asyncio.sleep(0)  # let the (failing) reconnect task settle

    assert relay.reconnect_attempts == 1
    assert relay.connected is None  # still down


async def test_first_reconnect_fires_when_monotonic_below_backoff(monkeypatch: pytest.MonkeyPatch):
    """The FIRST reconnect must fire regardless of the absolute monotonic
    clock. The backoff applies only BETWEEN attempts, so with no prior
    attempt there's nothing to back off from. Regression for a CI flake:
    `_last_attempt_at` initialised to 0.0 made the first attempt compare
    `monotonic() - 0.0 < BACKOFF`, which suppressed it on a freshly-booted
    host where CLOCK_MONOTONIC (seconds since boot) reads below the backoff.
    Pinned by forcing monotonic below the (large) backoff window."""
    monkeypatch.setattr(tr, "RELAY_RECONNECT_BACKOFF_S", 100.0)
    monkeypatch.setattr(tr.time, "monotonic", lambda: 42.0)  # < backoff: fresh boot
    live = _FakeLive(_FakeConfig())
    factory = _RecordingFactory(connect_ok=True)
    relay = _tap_relay(live, do_live=True, factory=factory)
    await relay.open()

    factory.connect_ok = False
    factory.relays[0].alive = False
    await relay.feed(PCM_FRAME)  # detect death
    await relay.feed(PCM_FRAME)  # schedule the first reconnect
    await asyncio.sleep(0)

    assert relay.reconnect_attempts == 1  # fired despite monotonic() < backoff


async def test_reconnect_picks_up_current_config(monkeypatch: pytest.MonkeyPatch):
    """After a config swap (operator changed model/language/port), a
    reconnect must bind to the CURRENT config — asserted through the
    public `connected`, not by poking the relay's private fields."""
    monkeypatch.setattr(tr, "RELAY_RECONNECT_BACKOFF_S", 0.0)
    live = _FakeLive(_FakeConfig(port=9100, language="en"))
    factory = _RecordingFactory(connect_ok=True)
    relay = _tap_relay(live, do_live=True, factory=factory)
    await relay.open()
    assert relay.connected == ("127.0.0.1", 9100, "en")

    # Operator swaps the model/language/port; kill the current relay.
    live.config = _FakeConfig(port=9200, language="nb")
    factory.relays[0].alive = False

    # feed (death) → disconnected; feed (schedule) → reconnect task.
    await relay.feed(PCM_FRAME)
    await relay.feed(PCM_FRAME)
    for _ in range(5):
        await asyncio.sleep(0)
        if relay.connected is not None:
            break

    assert relay.connected == ("127.0.0.1", 9200, "nb")
    assert factory.calls[-1][0].port == 9200  # reconnect read the swapped config


async def test_close_cancels_in_flight_reconnect(monkeypatch: pytest.MonkeyPatch):
    """close() must cancel a reconnect that's mid-connect — otherwise the
    task could land a fresh relay after we've torn down, leaking a WS to
    the WlK child."""
    monkeypatch.setattr(tr, "RELAY_RECONNECT_BACKOFF_S", 0.0)
    entered = asyncio.Event()

    class _HangingRelay:
        async def connect(self) -> bool:
            entered.set()
            await asyncio.Event().wait()  # never completes
            return True

        async def send(self, data: bytes) -> bool:
            return True

        async def close(self) -> None:
            return None

    live = _FakeLive(_FakeConfig(), running=False)
    relay = TapRelay(
        live,
        do_live=True,
        handlers=_handlers(),
        relay_factory=lambda _cfg, _h: _HangingRelay(),
        gate_factory=lambda _cfg: None,
    )
    await relay.open()  # dormant (channel down)

    live._running = True
    await relay.feed(PCM_FRAME)  # schedules a reconnect that hangs in connect
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    assert relay.reconnect_attempts == 1

    # The assertion is simply that this returns (doesn't hang on the
    # in-flight connect) and doesn't raise.
    await asyncio.wait_for(relay.close(), timeout=1.0)


async def test_handlers_are_wired_through_the_factory():
    settled: list[str] = []
    factory = _RecordingFactory()
    handlers = _handlers(settled=settled)
    relay = TapRelay(
        _FakeLive(_FakeConfig()),
        do_live=True,
        handlers=handlers,
        relay_factory=factory,
        gate_factory=lambda _cfg: None,
    )
    await relay.open()

    assert factory.calls[0][1] is handlers


# --------------------------------------------------------------------------
# Default factory wiring (the only WlKRelay-construction contract)
# --------------------------------------------------------------------------


def test_default_relay_factory_binds_config_and_handlers():
    """The default factory must read host/port/language off the CURRENT
    config and wire the handler callbacks straight onto WlKRelay — the
    exact wiring whose bug class ('reconnect read the wrong field') this
    extraction exists to pin. A white-box check of the builder: it inspects
    the constructed relay's fields directly."""
    cfg = _FakeConfig(host="10.0.0.5", port=9333, language="nb")
    settled: list[str] = []
    handlers = _handlers(settled=settled)

    relay = _default_relay_factory(cfg, handlers)

    assert isinstance(relay, WlKRelay)
    assert (relay._host, relay._port, relay._language) == ("10.0.0.5", 9333, "nb")
    assert relay._on_settled_line is handlers.on_settled_line
    assert relay._on_metrics is handlers.on_metrics
    assert relay._on_buffer is handlers.on_buffer


# --------------------------------------------------------------------------
# Gate construction off the event loop (#249)
#
# `SpeechGate` construction (real Silero: lazy import + ONNX model load)
# is genuinely blocking CPU work, so a fake that proves "ran off the loop"
# has to actually block a real OS thread — an `asyncio.sleep` fake would
# already yield on its own and couldn't tell a fixed synchronous call
# apart from one dispatched via `asyncio.to_thread`.
#
# The naive way to prove that — have the test await a `threading.Event`
# via `asyncio.to_thread` and check it fired — doesn't work: if the gate
# factory is (still, buggily) called directly on the loop, the ENTIRE
# loop is blocked for the duration of that call, including the coroutine
# that's supposed to be *observing* it, so the observer can't distinguish
# "it's happening right now" from "it already finished" — by the time
# the observer gets scheduled at all, a synchronous call has always
# already returned. The reliable signal is instead the *return value* of
# `threading.Event.wait(timeout=...)`: `True` means something else set it
# before the timeout elapsed (proof of genuine concurrency); `False`
# means only the timeout itself unblocked the wait (nothing else got a
# chance to run). `_blocking_gate_factory` below records that boolean
# into a shared dict rather than just returning the gate.
# --------------------------------------------------------------------------


def _blocking_gate_factory(release: threading.Event, outcome: dict, gate, *, timeout: float = 3.0):
    """A synchronous (non-async) gate factory — the shape `TapRelay` calls
    directly today and would dispatch via `asyncio.to_thread` once fixed.
    Blocks until `release` fires, recording whether that happened before
    `timeout` elapsed into `outcome["released_in_time"]` — the bounded
    timeout keeps a regression a fast, deterministic failure rather than
    a hang."""

    def factory(_cfg):
        outcome["released_in_time"] = release.wait(timeout=timeout)
        return gate

    return factory


async def test_gate_construction_runs_off_the_event_loop():
    """Gate construction must not block the event loop: a `ticker`
    coroutine scheduled alongside `open()` must get to run 20 loop turns
    and flip `release` WHILE the (real-thread-blocked) factory is still
    waiting on it. Pre-fix, `_attach` called the factory directly on the
    loop, so the ticker could never run until the factory had already
    given up and returned on its own bounded timeout — `release` would
    then be set too late to be observed, so `released_in_time` reads
    `False`. This is a structural proof (a bool from `Event.wait`'s
    return value), not a wall-clock threshold, so it holds on slow CI."""
    release = threading.Event()
    outcome: dict = {}

    async def ticker() -> None:
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()

    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=True, factory=factory, gate=None)
    relay._gate_factory = _blocking_gate_factory(release, outcome, None)

    open_task = asyncio.create_task(relay.open())
    ticker_task = asyncio.create_task(ticker())

    await asyncio.wait_for(open_task, timeout=6.0)
    await asyncio.wait_for(ticker_task, timeout=6.0)

    assert outcome.get("released_in_time") is True, (
        "the ticker never ran concurrently with gate construction — construction is stalling the event loop"
    )
    assert relay.connected is not None


async def test_frames_during_gate_construction_pass_through():
    """Frames that arrive while the gate is still under construction take
    the SAME deliberate passthrough path as a construction failure
    (`self._gate is None` → forward unfiltered, `gate_open=False`) —
    rather than being buffered or dropped. The relay is already attached
    (connect() finished) before gate construction even starts, so a slow
    gate must not hold up delivery of frames that arrive in the interim.

    Pre-fix this fails for the same structural reason described above:
    a synchronous factory call blocks `open()`'s single loop turn to
    completion before this test's own polling loop (or `feed()` call)
    ever gets to run, so by the time control returns here the (real)
    gate is already fully built and `fed.gate_open` reads `True`, not
    the expected passthrough `False`."""
    release = threading.Event()
    outcome: dict = {}
    ready_gate = _FakeGate(output=[PCM_FRAME], is_open=True)

    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=True, factory=factory, gate=None)
    relay._gate_factory = _blocking_gate_factory(release, outcome, ready_gate)

    open_task = asyncio.create_task(relay.open())
    for _ in range(5):
        await asyncio.sleep(0)
        if relay.connected is not None:
            break
    assert relay.connected is not None  # attaches before the gate is ready

    fed = await relay.feed(PCM_FRAME)
    assert fed == FedFrames(frames=(PCM_FRAME,), gate_open=False)
    assert factory.relays[0].sent == [PCM_FRAME]

    release.set()
    await asyncio.wait_for(open_task, timeout=6.0)
    # Sanity: we really did observe the mid-construction state above, not
    # a lucky coincidence — the factory's wait was released BY us, not by
    # its own bounded timeout.
    assert outcome.get("released_in_time") is True

    # Once construction completes, the real gate takes over.
    fed2 = await relay.feed(PCM_FRAME)
    assert fed2 == FedFrames(frames=(PCM_FRAME,), gate_open=True)


async def test_gate_construction_failure_falls_back_to_passthrough(capsys: pytest.CaptureFixture[str]):
    """A gate factory that raises must not take the tap down — the relay
    still attaches, and feed() degrades to passthrough. Pins the exact
    log contract (`CLAUDE.md`): 'gate construction failed ... falling
    back to passthrough'."""
    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=True, factory=factory, gate=None)

    def _raise(_cfg):
        raise RuntimeError("boom")

    relay._gate_factory = _raise

    await relay.open()
    assert relay.connected is not None  # the tap still attaches

    fed = await relay.feed(PCM_FRAME)
    assert fed == FedFrames(frames=(PCM_FRAME,), gate_open=False)  # passthrough

    out = capsys.readouterr().out
    assert "gate construction failed" in out
    assert "falling back to passthrough" in out


async def test_reconnect_does_not_feed_through_the_stale_gate_while_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
):
    """A reconnect (WlK restart / operator swapped model+gate settings)
    rebuilds the gate too. While the NEW gate is under construction,
    frames must not keep running through the OLD gate (built for the
    config that just changed) — they take the same deliberate passthrough
    as first-attach construction. Regression for a scenario the naive
    `asyncio.to_thread` swap alone doesn't cover: `_attach` must clear
    `self._gate` BEFORE kicking off construction, not just after it
    finishes."""
    monkeypatch.setattr(tr, "RELAY_RECONNECT_BACKOFF_S", 0.0)
    live = _FakeLive(_FakeConfig())
    factory = _RecordingFactory(connect_ok=True)

    # Both gates forward the frame (output=[PCM_FRAME]) so relay-death
    # detection + reconnect scheduling behave identically regardless of
    # which one is (or isn't) live — the discriminator is `is_open=True`
    # on both, so a stale `old_gate` still wired up during the rebuild
    # would report `gate_open=True` exactly like the eventual new gate.
    # Only a properly-cleared `self._gate is None` forces `gate_open=False`
    # (the passthrough contract), which is what distinguishes "still on
    # the stale gate" from "correctly cleared, pending construction".
    old_gate = _FakeGate(output=[PCM_FRAME], is_open=True)
    new_ready_gate = _FakeGate(output=[PCM_FRAME], is_open=True)
    release = threading.Event()
    outcome: dict = {}
    calls = {"n": 0}

    def gate_factory(_cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            return old_gate
        outcome["released_in_time"] = release.wait(timeout=3.0)
        return new_ready_gate

    relay = TapRelay(
        live,
        do_live=True,
        handlers=_handlers(),
        relay_factory=factory,
        gate_factory=gate_factory,
    )
    await relay.open()
    assert relay.connected is not None

    # WlK dies; the next feed schedules a reconnect (new gate_factory call).
    factory.relays[0].alive = False
    await relay.feed(PCM_FRAME)  # detects the death
    await relay.feed(PCM_FRAME)  # schedules the reconnect

    # Poll with a real (small) delay rather than bare `sleep(0)` — the
    # second gate_factory call happens on a background thread pool
    # worker (`asyncio.to_thread`), and its `call_soon_threadsafe`
    # handoff back to the loop needs the loop to actually reach a timed
    # `select()`/epoll wait at least once; an all-zero-delay busy loop
    # can spin through many iterations without ever giving that handoff
    # a chance to land.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if relay.connected is not None and calls["n"] >= 2:
            break
    assert relay.connected is not None and calls["n"] >= 2

    # The reconnect has a fresh relay attached but the new gate isn't
    # ready yet — frames must pass straight through (gate_open=False),
    # NOT through the stale old_gate (which would report gate_open=True).
    fed = await relay.feed(PCM_FRAME)
    assert fed == FedFrames(frames=(PCM_FRAME,), gate_open=False)

    release.set()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if outcome.get("released_in_time") is not None:
            break
    assert outcome.get("released_in_time") is True

    fed2 = await relay.feed(PCM_FRAME)
    assert fed2 == FedFrames(frames=(PCM_FRAME,), gate_open=True)


async def test_relay_follows_a_live_channel_object_swap(monkeypatch: pytest.MonkeyPatch):
    """PR #334 finding #1: `/api/live/start` can now REPLACE `recorder.live`
    with a different concrete channel (Whisper <-> Moonshine family swap).
    A TapRelay built before the swap must follow it — resolving the
    channel through a provider at use time, not holding the object it was
    constructed with — or every open tap's captions silently die against
    the stopped old channel."""
    monkeypatch.setattr(tr, "RELAY_RECONNECT_BACKOFF_S", 0.0)
    old_live = _FakeLive(_FakeConfig(port=9100, language="en"))
    holder = {"live": old_live}
    factory = _RecordingFactory(connect_ok=True)
    relay = TapRelay(
        lambda: holder["live"],  # the recorder.live resolver TapFanOut passes
        do_live=True,
        handlers=_handlers(),
        label="alice",
        relay_factory=factory,
        gate_factory=(lambda _cfg: None),
    )
    await relay.open()
    assert relay.connected == ("127.0.0.1", 9100, "en")

    # The family swap: old channel stopped, its server gone (next send
    # fails), and recorder.live now points at a NEW running channel on a
    # fresh port.
    old_live._running = False
    factory.relays[0].alive = False
    holder["live"] = _FakeLive(_FakeConfig(port=9300, language="en"))

    # feed (death) → disconnected; feed (schedule) → reconnect against
    # the channel the provider NOW resolves.
    await relay.feed(PCM_FRAME)
    await relay.feed(PCM_FRAME)
    for _ in range(5):
        await asyncio.sleep(0)
        if relay.connected is not None:
            break

    assert relay.connected == ("127.0.0.1", 9300, "en"), (
        "relay stayed bound to the stopped pre-swap channel — captions would be dead"
    )


# --------------------------------------------------------------------------
# Per-frame VAD off the event loop (#248)
#
# Same rationale as the "Gate construction off the event loop (#249)"
# section above: a fake that proves `feed()` ran off the loop has to
# actually block a real OS thread, because an `asyncio.sleep` fake would
# already yield on its own and a synchronous call could never be told
# apart from an offloaded one. `_blocking_gate` below is the `feed()`
# analogue of `_blocking_gate_factory`.
# --------------------------------------------------------------------------


def _blocking_gate(
    release: threading.Event, outcome: dict, output: list[bytes], *, is_open: bool, timeout: float = 3.0
):
    """A gate whose `feed()` blocks a real OS thread until `release`
    fires, recording whether that happened before `timeout` elapsed into
    `outcome["released_in_time"]` — `True` means something else (the
    observing ticker) set it first, proving genuine off-loop concurrency;
    `False` means only the bounded timeout unblocked it."""

    class _Gate:
        def feed(self, frame: bytes) -> list[bytes]:
            outcome["released_in_time"] = release.wait(timeout=timeout)
            return list(output)

        @property
        def is_open(self) -> bool:
            return is_open

    return _Gate()


async def test_gate_feed_runs_off_the_event_loop():
    """#248: `SpeechGate.feed` (Silero ONNX inference) must not block the
    event loop. A `ticker` coroutine running alongside `relay.feed()` must
    get 20 loop turns and flip `release` WHILE the (real-thread-blocked)
    gate is still waiting on it. Pre-fix, `feed()` called
    `self._gate.feed(buf)` directly on the loop, so the ticker could never
    run until the blocking call had already given up on its own bounded
    timeout — `release` would then be set too late to observe, and
    `released_in_time` would read `False`."""
    release = threading.Event()
    outcome: dict = {}
    gate = _blocking_gate(release, outcome, [PCM_FRAME], is_open=True)

    async def ticker() -> None:
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()

    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=True, factory=factory, gate=gate)
    await relay.open()

    feed_task = asyncio.create_task(relay.feed(PCM_FRAME))
    ticker_task = asyncio.create_task(ticker())

    fed = await asyncio.wait_for(feed_task, timeout=6.0)
    await asyncio.wait_for(ticker_task, timeout=6.0)

    assert outcome.get("released_in_time") is True, (
        "the ticker never ran concurrently with gate.feed() — feed() is stalling the event loop"
    )
    assert fed == FedFrames(frames=(PCM_FRAME,), gate_open=True)


async def test_feed_offload_survives_a_gate_swap_mid_await(monkeypatch: pytest.MonkeyPatch):
    """The gate is snapshotted into a local BEFORE the executor await
    (`gate = self._gate`), so a reconnect that swaps `self._gate` to a
    fresh object WHILE this frame's inference is in flight must not leak
    into this call's result: both the emitted frames and `gate_open` must
    reflect the gate this frame was actually fed to, not whatever
    `self._gate` has become by the time the await resolves."""
    release = threading.Event()
    outcome: dict = {}
    old_gate = _blocking_gate(release, outcome, [PCM_FRAME], is_open=True)
    new_gate = _FakeGate(output=[], is_open=False)  # would report differently if read post-await

    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=True, factory=factory, gate=old_gate)
    await relay.open()

    feed_task = asyncio.create_task(relay.feed(PCM_FRAME))
    for _ in range(20):
        await asyncio.sleep(0)
    relay._gate = new_gate  # simulate a reconnect landing mid-inference
    release.set()

    fed = await asyncio.wait_for(feed_task, timeout=6.0)
    assert fed == FedFrames(frames=(PCM_FRAME,), gate_open=True), (
        "feed() must report the gate it actually fed, not the one swapped in mid-await"
    )


# --------------------------------------------------------------------------
# Bit-identical equivalence against real Silero (#248 requirement: the
# off-loop move must not change gating AT ALL)
# --------------------------------------------------------------------------

REAL_AUDIO_FIXTURE = Path(__file__).parent / "fixtures" / "audio" / "armstrong-en.wav"


def _real_audio_frames(frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    """The real ~12 s Apollo-11 speech clip used by `test_tap_fan_out.py`'s
    real-audio meter tests, read into 20 ms frames — real speech (not
    synthetic silence) so it actually drives Silero through
    silence -> speech -> silence transitions. Reuses that file's
    `_wav_to_frames` so the WAV→frames read lives in one place."""
    from test_tap_fan_out import _wav_to_frames

    return _wav_to_frames(REAL_AUDIO_FIXTURE, frame_bytes=frame_bytes)


def _real_gate() -> SpeechGate:
    """A real, Silero-backed gate built through the PRODUCTION constructor
    (`build_gate_for_config`, gate_kind="tapscribe") rather than hand-rolled,
    so this equivalence test can't silently drift from how production wires a
    gate. Each call loads a FRESH model (per-gate, never shared — see
    `speech_gate.load_silero_model`), so two gates built with identical knobs
    and fed the identical frame sequence produce identical output; Silero's
    ONNX inference is deterministic."""
    from types import SimpleNamespace

    from tapscribe.speech_gate import build_gate_for_config

    cfg = SimpleNamespace(
        gate_kind="tapscribe",
        gate_speech_threshold=0.5,
        gate_hangover_ms=400,
        gate_pre_roll_ms=300,
        gate_min_speech_ms=0,
    )
    gate = build_gate_for_config(cfg)
    assert gate is not None  # gate_kind="tapscribe" always yields a gate
    return gate


async def test_feed_offload_is_bit_identical_to_synchronous_real_silero():
    """The headline #248 correctness requirement: moving `SpeechGate.feed`
    off the event loop must not change gating at all. Feed the same real
    speech clip, frame for frame, through two independently-constructed
    but identically-configured REAL Silero gates — one driven directly
    (`gate.feed(frame)`, the pre-#248 shape) as the baseline, the other
    through `TapRelay.feed` (the post-#248 offloaded shape) — and assert
    the two sequences of emitted frames AND gate-open states match at
    every single frame. Uses the real onnxruntime-backed Silero port
    (a core dependency, #374), not a fake VAD, so this is a real-backend
    check per repo convention, not just a mock."""
    pytest.importorskip("onnxruntime")
    frames_in = _real_audio_frames()
    assert len(frames_in) > 200, "expected a real multi-second speech fixture"

    baseline_gate = _real_gate()
    baseline_frames: list[tuple[bytes, ...]] = []
    baseline_open: list[bool] = []
    for f in frames_in:
        baseline_frames.append(tuple(baseline_gate.feed(f)))
        baseline_open.append(baseline_gate.is_open)

    offloaded_gate = _real_gate()
    factory = _RecordingFactory()
    relay = _tap_relay(_FakeLive(_FakeConfig()), do_live=True, factory=factory, gate=offloaded_gate)
    await relay.open()

    offloaded_frames: list[tuple[bytes, ...]] = []
    offloaded_open: list[bool] = []
    for f in frames_in:
        fed = await relay.feed(f)
        offloaded_frames.append(fed.frames)
        offloaded_open.append(fed.gate_open)

    assert offloaded_open == baseline_open
    assert offloaded_frames == baseline_frames
    assert any(baseline_open), "fixture never opened the gate — test wouldn't catch a broken transition"
