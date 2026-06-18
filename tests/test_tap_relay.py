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
from dataclasses import dataclass

import pytest

from tapscribe import tap_relay as tr
from tapscribe.live_relay import WlKRelay
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
