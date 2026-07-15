"""TapRelay — the per-`/tap` live leg.

One TapRelay owns the live-captioning half of a single `/tap` WebSocket:
the WhisperLiveKit relay (`WlKRelay`), the per-tap Silero `SpeechGate`,
and the transparent reconnect-with-backoff that keeps captions flowing
across a WhisperLiveKit restart (operator clicks Apply to swap the model,
or the child crashes) without forcing the Bridge to drop and re-open
`/tap`.

It was extracted from `TapFanOut` so the reconnect state machine — the
live path's most intricate, most-broken part — has its own interface and
is the test surface. Before the split it was only assertable by poking
`TapFanOut`'s private fields (`_relay_alive`, `_relay_reconnect_attempts`,
`_relay._language`/`_port`, `_relay_reconnect_task`); now those facts are
the public read-surface `reconnect_attempts` / `connected`.

The seam between `TapFanOut` and `TapRelay` is `feed(buf) -> FedFrames`:
TapRelay feeds the gate and forwards the surviving frames to the relay,
and hands the *post-gate* frames back so TapFanOut can drive the level
meter and the ActiveStream gate-open row. The gate lives behind the seam;
only its output crosses it.

This is an internal sub-unit of the Recorder's fan-out, NOT a new
architectural boundary — ADR-0002 (Bridge → one `/tap` WS → the Recorder
fans out internally) is unchanged. See CONTEXT.md (TapFanOut · TapRelay).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .live_relay import WlKRelay
from .speech_gate import SpeechGate, build_gate_for_config

if TYPE_CHECKING:
    from .live import LiveChannel, LiveConfig

# Minimum seconds between relay reconnect attempts. The relay can die
# mid-utterance for two reasons we want to recover from transparently —
# without forcing the bridge to drop and re-open /tap:
#   1. WhisperLiveKit child crashed.
#   2. Operator clicked Apply (restart) on the dashboard to swap the
#      model / language; the recorder stopped the old child and started
#      a new one (possibly with a different config).
# At 20 ms frames that's 50 candidate reconnect points per second per
# stream — without backoff we'd hammer a still-starting WlK. One second
# leaves a small audio gap during restart but is responsive enough that
# the operator sees captions resume within ~1 cycle past WlK's ready
# time. Lowered to ~0 in tests to keep the suite quick.
RELAY_RECONNECT_BACKOFF_S: float = 1.0


class RelayConnection(Protocol):
    """What TapRelay needs from a relay — the contract `WlKRelay`
    satisfies and a test fake stands in for. Naming it makes the relay
    an explicit, injectable seam."""

    async def connect(self) -> bool: ...
    async def send(self, data: bytes) -> bool: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class RelayHandlers:
    """The settled-line / metrics / buffer callbacks the relay invokes.
    Supplied by TapFanOut as bound methods that read the tap's identity /
    name / session / conn_id at invocation time. A named contract in
    place of three loose bound-method closures."""

    on_settled_line: Callable[[str], None]
    on_metrics: Callable[[float], Awaitable[None]]
    on_buffer: Callable[[str], None]


@dataclass(frozen=True)
class FedFrames:
    """The result of `TapRelay.feed`: the frames that survived the gate
    (for the caller's level meter) and the gate's current open state (for
    the caller's ActiveStream gate-open transition). Empty `frames` means
    the gate is closed / warming up — the meter should decay to dark."""

    frames: tuple[bytes, ...]
    gate_open: bool


RelayFactory = Callable[["LiveConfig", RelayHandlers], RelayConnection]
GateFactory = Callable[["LiveConfig"], "SpeechGate | None"]


def _default_relay_factory(cfg: LiveConfig, handlers: RelayHandlers) -> RelayConnection:
    return WlKRelay(
        host=cfg.host,
        port=cfg.port,
        language=cfg.language,
        on_settled_line=handlers.on_settled_line,
        on_metrics=handlers.on_metrics,
        on_buffer=handlers.on_buffer,
    )


def _default_gate_factory(cfg: LiveConfig) -> SpeechGate | None:
    # Looks up build_gate_for_config in this module's globals at call time,
    # so a test can monkeypatch `tapscribe.tap_relay.build_gate_for_config`.
    return build_gate_for_config(cfg)


class TapRelay:
    """The live leg of one `/tap` WS. Built dormant; `open()` attaches a
    relay when the LiveChannel is running, `feed(buf)` pushes audio
    through the gate to the relay (reconnecting as needed), and `close()`
    tears it down. `do_live=False` (record-only) keeps it permanently
    inert — `feed` then passes audio straight through with no gate."""

    def __init__(
        self,
        live: LiveChannel | Callable[[], LiveChannel],
        *,
        do_live: bool,
        handlers: RelayHandlers,
        label: str = "",
        relay_factory: RelayFactory | None = None,
        gate_factory: GateFactory | None = None,
    ) -> None:
        # `live` may be a channel or a zero-arg resolver. `/api/live/start`
        # REPLACES `recorder.live` wholesale on a family swap (Whisper <->
        # Moonshine, PRD #120), so a relay that captured the channel object
        # at construction would stay bound to the stopped pre-swap instance
        # forever — captions silently dead for every open tap (PR #334
        # finding #1). TapFanOut passes `lambda: recorder.live`; tests and
        # single-channel callers can keep passing the channel directly.
        self._live_resolver: Callable[[], LiveChannel] = live if callable(live) else (lambda: live)
        self._do_live = do_live
        self._handlers = handlers
        self._label = label
        self._relay_factory = relay_factory if relay_factory is not None else _default_relay_factory
        self._gate_factory = gate_factory if gate_factory is not None else _default_gate_factory
        self._relay: RelayConnection | None = None
        # (host, port, language) of the live relay, or None when no relay
        # is alive. Doubles as the liveness flag — `connected is None`
        # means "dead, candidate for reconnect" — and is the read-surface
        # tests assert the reconnect picked up the current config on.
        self._connected: tuple[str, int, str] | None = None
        # Per-tap SpeechGate (Silero-backed). None when gate_kind=
        # "backend" (the backend's own VAD handles silence) or when gate
        # construction failed — PCM then bypasses the gate.
        self._gate: SpeechGate | None = None
        # Backoff bookkeeping for transparent reconnection across WlK
        # restarts. The task handle lets `close` cancel an in-flight
        # attempt cleanly; the monotonic timestamp + counter implement
        # the backoff and the counter is the read-surface that proves a
        # burst of frames coalesces into a single connect attempt.
        self._reconnect_task: asyncio.Task | None = None
        # `None` means "no reconnect attempted yet" — distinct from "attempted
        # at monotonic time 0.0". The backoff window only applies BETWEEN
        # attempts, so the first attempt must never be gated by it; folding
        # "never attempted" into 0.0 made the first reconnect compare
        # `monotonic() - 0.0 < BACKOFF`, which wrongly suppresses it whenever
        # the monotonic clock reads below BACKOFF (a freshly-booted host —
        # CLOCK_MONOTONIC is seconds since boot on Linux).
        self._last_attempt_at: float | None = None
        self._reconnect_attempts: int = 0

    @property
    def _live(self) -> LiveChannel:
        """The CURRENT live channel — resolved per read so a
        `recorder.live` family swap is picked up mid-stream (the whole
        point of the resolver, see `__init__`)."""
        return self._live_resolver()

    # ------------------------------------------------------------------
    # Read-surface (was: private fields poked by tests)
    # ------------------------------------------------------------------

    @property
    def reconnect_attempts(self) -> int:
        """How many reconnect attempts have fired. Backoff coalesces a
        burst of frames against a still-down WlK into one attempt."""
        return self._reconnect_attempts

    @property
    def connected(self) -> tuple[str, int, str] | None:
        """`(host, port, language)` the live relay is bound to, or `None`
        when no relay is alive. After a reconnect this reflects the
        LiveChannel's *current* config, not the one captured at open."""
        return self._connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Attach a relay + gate against the LiveChannel's current config,
        but only when this is a live tap and the channel is running. A
        record-only or live-down tap stays dormant; the first `feed`
        after the channel comes up schedules the first connect."""
        if self._do_live and self._live.running():
            await self._attach(self._live.config)

    async def feed(self, buf: bytes) -> FedFrames:
        """Run one PCM frame through the gate and forward the survivors to
        the relay, reconnecting transparently if the relay died but the
        LiveChannel is back up. Returns the post-gate frames + gate-open
        state for the caller's meter / ActiveStream row. Never raises on a
        dead relay — recording continues regardless (ADR-0002)."""
        if self._gate is not None:
            frames = tuple(self._gate.feed(buf))
            gate_open = self._gate.is_open
        else:
            frames = (buf,)
            gate_open = False

        for frame in frames:
            if self._connected is not None:
                assert self._relay is not None
                if not await self._relay.send(frame):
                    # Relay died (WlK closed the socket). Mark dead; keep
                    # the stale relay ref so close/reconnect can drain it.
                    self._connected = None
                    break
            elif self._do_live and self._live.running():
                self._maybe_schedule_reconnect()
                break

        return FedFrames(frames=frames, gate_open=gate_open)

    async def close(self) -> None:
        """Cancel any in-flight reconnect (so it can't land a fresh relay
        right after we close the current one, leaking a WS to the WlK
        child), then close the relay — which drains tail captions."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._reconnect_task
        self._reconnect_task = None
        if self._relay is not None:
            await self._relay.close()  # drains tail captions per Tail flush

    # ------------------------------------------------------------------
    # Reconnect internals
    # ------------------------------------------------------------------

    def _maybe_schedule_reconnect(self) -> None:
        """Kick off a background reconnect if none is pending and we're
        outside the backoff window. Synchronous (no await) so the caller's
        frame loop keeps moving — the actual connect happens in a task so
        a slow / unreachable WlK can't stall the frame stream."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        now = time.monotonic()
        if self._last_attempt_at is not None and now - self._last_attempt_at < RELAY_RECONNECT_BACKOFF_S:
            return
        self._last_attempt_at = now
        self._reconnect_attempts += 1
        self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Rebuild the relay using the LiveChannel's CURRENT config — so a
        model / language / port change applied via the dashboard takes
        effect for already-open `/tap` WebSockets too. The stale relay is
        closed first (failures swallowed — it's already known-dead). A
        failed connect just leaves `connected=None`; the backoff guard
        rate-limits retries until WlK comes up."""
        stale = self._relay
        self._relay = None
        self._connected = None
        if stale is not None:
            with suppress(Exception):
                await stale.close()
        cfg = self._live.config
        if await self._attach(cfg):
            print(
                f"[tapscribe] /tap relay reconnected{self._label_suffix} "
                f"-> {cfg.host}:{cfg.port} (model={cfg.model}, lang={cfg.language})",
                flush=True,
            )

    async def _attach(self, cfg: LiveConfig) -> bool:
        """Build the relay + (optional) gate against `cfg`. Sets the relay,
        `connected`, and the gate on success and returns True; returns
        False if the relay fails to connect. The gate is only built (and
        only paid for) once the relay is actually going to be fed.

        Gate-construction failures (Silero load error, etc.) don't kill
        the tap — we log and fall through with `gate=None`, so the bridge
        sees passthrough rather than a dropped `/tap` WS.

        Gate construction (the default factory: a lazy `silero_vad` import
        + ONNX model load, ~0.1-0.2 s of synchronous CPU work, more on a
        cold import) runs off the event loop via `asyncio.to_thread` (#249)
        — otherwise the first live `/tap` open (or first reconnect after a
        live-channel restart) stalls every OTHER open tap's frames and the
        dashboard's `/api/state` poll for the duration.

        `self._gate` is cleared BEFORE construction starts, not just on
        failure. Frames that land in that window — this tap's own during
        first attach can't (the WS receive loop doesn't start until `open()`
        resolves — see `TapFanOut._open`), but a RECONNECT runs as a
        background task while frames keep arriving for this same tap — take
        the identical passthrough path as a construction failure, rather
        than continuing to run through a gate built for a config that may
        have just changed (a reconnect can follow an operator swapping the
        gate's own threshold/hangover/pre-roll knobs)."""
        candidate = self._relay_factory(cfg, self._handlers)
        if not await candidate.connect():
            return False
        self._relay = candidate
        self._connected = (cfg.host, cfg.port, cfg.language)
        self._gate = None
        try:
            self._gate = await asyncio.to_thread(self._gate_factory, cfg)
        except Exception as e:
            print(
                f"[tapscribe] /tap gate construction failed{self._label_suffix}: {e}; falling back to passthrough",
                flush=True,
            )
            self._gate = None
        return True

    @property
    def _label_suffix(self) -> str:
        """' for <label>' for log lines, or '' when no label was set."""
        return f" for {self._label}" if self._label else ""
