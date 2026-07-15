"""MoonshineLiveChannel — a lightweight, low-latency LiveChannel backed by
Moonshine (PRD #120), instead of the supervised whisperlivekit-server
child `WhisperLiveKitChannel` wraps.

Architecture — reuse `WlKRelay` verbatim by speaking its contract
--------------------------------------------------------------------------
`TapRelay` always builds a `WlKRelay` (see `tapscribe.tap_relay`) that opens
ONE outbound WS to `ws://<host>:<port>/asr?language=<lang>`, forwards gated
PCM via `send(bytes)`, and parses the peer's rolling-snapshot JSON
(`{"lines": [...], "buffer_transcription": ..., "remaining_time_transcription":
...}`) into settled lines. `MoonshineLiveChannel` therefore doesn't need a
NEW relay/consumer — it just needs to BE that peer: `MoonshineAsrServer`
below is an in-process `websockets.serve()` server exposing `/asr` that
speaks the exact same JSON shape WhisperLiveKit does, backed by a
`MoonshineWindow` (rolling-chunk pseudo-streaming, see
`transcribers/_moonshine_window.py`) per connection instead of a subprocess.

Consequence: the Recorder, `TapFanOut`, `SpeechGate`, `WlKRelay`, and
`LiveTranscripts` are NOT modified by this module at all — selecting
Moonshine is purely a matter of which concrete `LiveChannel` the Recorder
holds (see `tapscribe.app`'s `/api/live/start` route, which swaps
`recorder.live` based on the requested model's catalog family).

No subprocess here — unlike `WhisperLiveKitChannel`, there is no child
process to spawn/supervise/pump logs from. Instead `start()` spins up a
dedicated background thread running its own asyncio event loop hosting the
`/asr` websockets server; `stop()` tears both down. This mirrors the
"own thread, own loop" shape rather than reusing the FastAPI app's loop,
because `LiveChannel.start()`/`stop()` are synchronous Protocol methods —
`/api/live/start` already calls them via `asyncio.to_thread`, exactly like
`WhisperLiveKitChannel.start()`'s blocking subprocess spawn + NB-Whisper
weight download.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

from .live import GATE_THRESHOLD_DECIMALS, LiveChannel, LiveConfig, _gate_knob_replacements
from .transcribers._moonshine_window import MoonshineWindow

# Model ids this channel knows how to run — kept in sync with the catalog's
# `moonshine` family (transcribers/catalog.py). Duplicated as a frozenset
# here (rather than importing REGISTRY at module scope) so importing this
# module never triggers a catalog import cycle; `_validate_model` below
# double-checks against the live REGISTRY at engine-construction time,
# which is the actual security boundary (PRD #120 user story #23 — a
# model id from a request body must resolve only against the curated
# catalog before any loader/Hub download runs).
_KNOWN_MODEL_IDS = frozenset({"moonshine-tiny", "moonshine-base"})


class MoonshineEngine(Protocol):
    """What `MoonshineWindow` needs from an inference engine — the shape
    both `transcribers.moonshine_mlx.MlxMoonshineEngine` and
    `transcribers.moonshine_onnx.OnnxMoonshineEngine` satisfy."""

    def generate(self, audio: np.ndarray) -> str: ...


def validate_moonshine_model(model_id: str) -> None:
    """The allowlist gate (PRD #120 user story #23, mirrors the summarizer
    `SUMMARY_MODELS` rule): a model id must resolve against the curated
    catalog — as BOTH a known Moonshine id here AND a real, current
    `family="moonshine"` registry entry — before any engine-load / Hub
    download can happen. Raises `ValueError` on any mismatch."""
    if model_id not in _KNOWN_MODEL_IDS:
        raise ValueError(f"{model_id!r} is not a known Moonshine model. Known: {sorted(_KNOWN_MODEL_IDS)!r}")
    from .transcribers.catalog import REGISTRY

    entry = REGISTRY.get(model_id)
    if entry is None or entry.family != "moonshine":
        raise ValueError(f"{model_id!r} is not a registered Moonshine catalog entry")


def default_engine_factory(model_id: str, *, use_mlx: bool) -> MoonshineEngine:
    """Build the real inference engine for `model_id`. Validates against
    the catalog FIRST — before either adapter's `.load()` can reach a
    loader or an HF Hub download."""
    validate_moonshine_model(model_id)
    if use_mlx:
        from .transcribers.moonshine_mlx import MlxMoonshineEngine

        return MlxMoonshineEngine.load(model_id)
    from .transcribers.moonshine_onnx import OnnxMoonshineEngine

    return OnnxMoonshineEngine.load(model_id)


def is_moonshine_model(model_id: str) -> bool:
    """True iff `model_id` belongs to the `moonshine` catalog family —
    the routing predicate `tapscribe.app`'s live-start route uses to
    decide whether `recorder.live` should be a `MoonshineLiveChannel`
    rather than a `WhisperLiveKitChannel`. Never raises — an unknown or
    unregistered id is simply "not moonshine", so an ordinary Whisper
    model name is the (fast, common) False case."""
    try:
        from .transcribers.catalog import REGISTRY

        entry = REGISTRY.get(model_id)
        return entry is not None and entry.family == "moonshine"
    except Exception:  # noqa: BLE001 — routing predicate must never crash the live-start route
        return False


def resolve_live_channel_for_model(
    current: LiveChannel, *, target_model: str, use_mlx: bool
) -> LiveChannel | None:
    """Decide whether the Recorder's live channel needs to become a
    DIFFERENT concrete implementation to run `target_model` — the routing
    `tapscribe.app`'s `/api/live/start` route applies before its usual
    `matches()` / `begin_transition()` / `start()` sequence.

    Returns a freshly constructed channel (carrying `current.config`
    forward) if `target_model`'s family doesn't match what `current`
    already is, or `None` if `current` is already the right concrete type
    (the common case — no swap needed). Never mutates `current`; the
    caller is responsible for `stop()`-ing it first (if running) before
    assigning the returned instance to `recorder.live` — swapping drops
    whatever server/child the old instance owned."""
    target_is_moonshine = is_moonshine_model(target_model)
    current_is_moonshine = isinstance(current, MoonshineLiveChannel)
    if target_is_moonshine == current_is_moonshine:
        return None
    # Reset port to 0 (ephemeral) on the carried-forward config: the old
    # channel's port is about to be freed by the caller's `stop()`, and
    # forcing a fresh pick — same rationale as `WhisperLiveKitChannel`'s
    # own ephemeral-port-per-start — sidesteps any TIME_WAIT collision
    # with the just-stopped server on that same port.
    carried_config = replace(current.config, port=0)
    if target_is_moonshine:
        return MoonshineLiveChannel(config=carried_config, use_mlx=use_mlx)
    from .live import WhisperLiveKitChannel

    return WhisperLiveKitChannel(config=carried_config, use_mlx=use_mlx)


def _bound_socket(host: str, port: int) -> socket.socket:
    """One listening-ready socket bound to the FIRST address `host`
    resolves to. See `MoonshineAsrServer.start` for why serve() must get
    a pre-bound socket instead of host+port. Clients that resolve `host`
    to multiple addresses still connect: asyncio tries each resolved
    address in turn, so the unbound family's refusal falls through to
    the bound one."""
    family, type_, proto, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    sock = socket.socket(family, type_, proto)
    try:
        if os.name == "posix":
            # Same TIME_WAIT-rebind allowance asyncio's create_server
            # applies by default on POSIX; deliberately NOT set on
            # Windows, where SO_REUSEADDR permits port hijacking.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(addr)
    except BaseException:
        sock.close()
        raise
    return sock


# ---------------------------------------------------------------------------
# MoonshineAsrServer — the /asr WebSocket server, one MoonshineWindow per
# connection.
# ---------------------------------------------------------------------------


class MoonshineAsrServer:
    """In-process `/asr` WebSocket server speaking `WlKRelay`'s wire
    contract. One `MoonshineWindow` per connection (one `/tap` utterance),
    so state never leaks across connections. `generate_fn` is injected —
    production wires it to a loaded `MoonshineEngine.generate`; tests use
    a stub."""

    def __init__(self, *, host: str, port: int, generate_fn) -> None:
        self._host = host
        self._port = port
        self._generate_fn = generate_fn
        self._server: Any = None

    @property
    def port(self) -> int:
        """The ACTUAL bound port — differs from the constructor arg when
        it was 0 (kernel-assigned). Read after `start()` returns."""
        return self._port

    async def start(self) -> None:
        # Bind exactly ONE pre-created socket and hand it to the server.
        # Two traps this sidesteps: (a) a pick-a-free-port-then-bind
        # helper leaves a window where another process grabs the picked
        # port first ("address already in use"); (b) passing host+port=0
        # straight to serve() binds one socket PER resolved address
        # family, each with its OWN kernel-assigned port — on Windows
        # "localhost" resolves to ::1 and 127.0.0.1, so sockets[0]'s port
        # disagrees with where half the clients connect (the Windows-CI
        # failure mode of the first attempt at this).
        self._server = await websockets.serve(self._handle, sock=_bound_socket(self._host, self._port))
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws: Any) -> None:
        window = MoonshineWindow(generate_fn=self._generate_fn)
        finalized = False
        try:
            async for message in ws:
                if not isinstance(message, (bytes, bytearray)):
                    continue  # /asr is PCM-only; ignore stray text frames
                if len(message) == 0:
                    # End-of-audio marker — the same wire signal
                    # whisperlivekit's own web client sends on stop
                    # (`audio_processor.process_audio` treats an empty
                    # message as "initiate stop sequence" and replies
                    # `ready_to_stop` after the final results). Decode
                    # everything still buffered and deliver the final
                    # snapshot NOW, while the peer is still listening —
                    # once the close handshake starts, `ws.send()` can
                    # only fail. This is what makes utterance tails
                    # reliable (PR #334 finding #5).
                    lines = await asyncio.to_thread(window.close)
                    await self._send_snapshot(ws, lines, buffer_text="", final=True)
                    finalized = True
                    break
                window.feed_pcm(bytes(message))
                # Inference is synchronous CPU work over up to ~chunk_s of
                # audio — run it on a worker thread so this shared event
                # loop keeps serving every other /asr connection. The
                # cheap `refresh_due` pre-check keeps the per-frame path
                # free of thread dispatch; awaiting inline preserves
                # per-connection ordering (never two concurrent decodes
                # for one window).
                if window.refresh_due:
                    lines = await asyncio.to_thread(window.maybe_refresh)
                    if lines is not None:
                        await self._send_snapshot(ws, lines, buffer_text=window.buffer_text)
        except ConnectionClosed:
            # The peer (WlKRelay) tore the WS down without the end-of-audio
            # marker — a relay death, a cancelled reconnect, or an abrupt
            # /tap drop. Normal connection end for this server, nothing to
            # answer to; the `finally` below still runs the best-effort
            # final push. What's lost by swallowing: only the distinction
            # between a clean and an abrupt peer close, which this server
            # has no consumer for.
            pass
        finally:
            if not finalized:
                # Abrupt close (no end-of-audio marker): best-effort final
                # push. This races the close handshake (the peer may
                # already be gone), hence the suppress — the marker path
                # above is the reliable delivery; this fallback only
                # exists for peers that vanished mid-utterance, where the
                # audio tail is best-effort by nature.
                lines = await asyncio.to_thread(window.close)
                if lines:
                    with contextlib.suppress(Exception):
                        await self._send_snapshot(ws, lines, buffer_text="")

    @staticmethod
    async def _send_snapshot(ws: Any, lines: list[dict], *, buffer_text: str, final: bool = False) -> None:
        payload: dict[str, Any] = {
            "lines": lines,
            "buffer_transcription": buffer_text,
            "remaining_time_transcription": 0.0,
        }
        if final:
            # Same shape whisperlivekit's basic_server sends when the
            # results generator finishes; WlKRelay treats it as "the peer
            # has nothing more to say" and ends its drain immediately.
            payload["type"] = "ready_to_stop"
        await ws.send(json.dumps(payload))


def _initial_moonshine_info() -> dict[str, str]:
    return {
        "model": "",
        "backend": "",  # "mlx-audio" or "moonshine-onnx"
        "device": "",
        "language": "",
        "host": "",
        "port": "",
        "state": "stopped",
        "last_error": "",
        "pid": "",
        "started_at": "",
        # Moonshine has no native VAD — always TapScribe's own gate.
        "gate_kind": "tapscribe",
        "gate_speech_threshold": "",
        "gate_hangover_ms": "",
        "gate_pre_roll_ms": "",
        "gate_min_speech_ms": "",
        "confidence_validation": "",
    }


class MoonshineLiveChannel:
    """Concrete `LiveChannel` (Protocol) backed by Moonshine. No
    subprocess: `start()` loads the inference engine (MLX or ONNX-CPU,
    per `use_mlx`) and spins up a dedicated thread running the `/asr`
    websockets server on its own event loop; `stop()` tears both down.

    `engine_factory` is injected for tests (`(model_id, *, use_mlx) ->
    MoonshineEngine`); production defaults to `default_engine_factory`,
    which validates the model against the catalog before loading it.
    """

    supports_native_vad: bool = False  # no built-in VAC — SpeechGate is the only gate

    def __init__(
        self,
        *,
        config: LiveConfig,
        use_mlx: bool,
        engine_factory=None,
    ) -> None:
        # The carried config is preserved VERBATIM (PR #334 finding #3):
        # Moonshine ignores `language` at inference time (English-only)
        # and has no native VAD to honour `gate_kind="backend"` with, but
        # rewriting those fields here would permanently clobber the
        # operator's choices on the whisper->moonshine->whisper roundtrip
        # (resolve_live_channel_for_model carries `current.config`
        # forward). The engine's ACTUAL behaviour is reflected in `info`
        # (see `_seed_info`) instead of mutating the config.
        self.config = config
        self.use_mlx = use_mlx
        self._engine_factory = engine_factory if engine_factory is not None else default_engine_factory
        self._ephemeral_port = config.port == 0
        # Loaded-engine cache, keyed by model id: the route's apply flow
        # is stop()->start(), and a gate-knob-only apply (finding #8)
        # must not pay a model reload for a restart the engine doesn't
        # need. Deliberately survives stop() — Moonshine engines are tens
        # of MB, and dropping the cache there would defeat the point.
        self._engine: MoonshineEngine | None = None
        self._engine_model_id: str = ""
        self.info: dict[str, str] = _initial_moonshine_info()
        self.log: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: MoonshineAsrServer | None = None
        self._seed_info()

    def _seed_info(self) -> None:
        self.info["model"] = self.config.model
        # `info` reports what the engine actually DOES, not what the
        # carried config says: Moonshine is English-only regardless of
        # config.language, and TapScribe's SpeechGate is its only gate
        # regardless of a carried-forward gate_kind="backend" (TapRelay
        # runs the gate for any channel without native VAD). The config
        # itself stays untouched so the operator's choices survive the
        # roundtrip back to Whisper.
        self.info["language"] = "en"
        self.info["gate_kind"] = "tapscribe"
        self.info["host"] = self.config.host
        self.info["port"] = str(self.config.port)
        self.info["backend"] = "mlx-audio" if self.use_mlx else "moonshine-onnx"
        self.info["device"] = "Apple Silicon GPU" if self.use_mlx else "CPU"
        self._mirror_gate_info()

    def _mirror_gate_info(self) -> None:
        # Mirror the gate knobs the per-tap SpeechGate reads from config —
        # the dashboard's sliders seed from these (same contract as
        # WhisperLiveKitChannel._mirror_gate_info). Called from _seed_info
        # and from apply_gate_knobs' no-restart path.
        self.info["gate_speech_threshold"] = (
            f"{self.config.gate_speech_threshold:.{GATE_THRESHOLD_DECIMALS}f}"
        )
        self.info["gate_hangover_ms"] = str(self.config.gate_hangover_ms)
        self.info["gate_pre_roll_ms"] = str(self.config.gate_pre_roll_ms)
        self.info["gate_min_speech_ms"] = str(self.config.gate_min_speech_ms)

    def running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

    def matches(
        self,
        *,
        model: str | None,
        language: str | None,
        gate_kind: str | None,
        conf: bool | None,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> bool:
        """Same "no requested field differs from current config" contract
        `WhisperLiveKitChannel.matches` implements, with one deliberate
        divergence: `language` never forces a restart, because `start()`
        deliberately ignores it (English-only engine — restarting for a
        language change would reload the engine and change nothing, PR
        #334 finding #9). The four `gate_*` kwargs are Recorder-side per
        #224 — accepted-but-IGNORED here, exactly like the WlK channel: a
        differing knob does NOT force a restart; the route applies it via
        `apply_gate_knobs` on the no-restart path (finding #8)."""
        return (
            self.running()
            and (not model or model == self.config.model)
            and (gate_kind is None or gate_kind == self.config.gate_kind)
            and (conf is None or conf == self.config.confidence_validation)
        )

    def apply_gate_knobs(
        self,
        *,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> None:
        """Apply Recorder-side gate-knob changes to config without a server
        restart — the /asr server never reads these; every per-tap
        SpeechGate is built from `live.config` at attach time (#224). Same
        changed-only + display-precision semantics as
        `WhisperLiveKitChannel.apply_gate_knobs` (the #238 guarantee)."""
        replacements = _gate_knob_replacements(
            gate_speech_threshold=gate_speech_threshold,
            gate_hangover_ms=gate_hangover_ms,
            gate_pre_roll_ms=gate_pre_roll_ms,
            gate_min_speech_ms=gate_min_speech_ms,
        )
        changed: dict[str, Any] = {}
        for field, value in replacements.items():
            current = getattr(self.config, field)
            if field == "gate_speech_threshold":
                if round(value, GATE_THRESHOLD_DECIMALS) == round(current, GATE_THRESHOLD_DECIMALS):
                    continue
            elif value == current:
                continue
            changed[field] = value
        if changed:
            self.config = replace(self.config, **changed)
            self._mirror_gate_info()

    def begin_transition(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        gate_kind: str | None = None,
        conf: bool | None = None,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> None:
        """Same contract as `WhisperLiveKitChannel.begin_transition`:
        write the supplied knobs into `config` so the imminent restart
        (and every per-tap SpeechGate built from this config — PR #334
        finding #8) picks them up, and flip `info` to "starting" so
        dashboards polling mid-transition see the new selection."""
        replacements = _gate_knob_replacements(
            gate_speech_threshold=gate_speech_threshold,
            gate_hangover_ms=gate_hangover_ms,
            gate_pre_roll_ms=gate_pre_roll_ms,
            gate_min_speech_ms=gate_min_speech_ms,
        )
        if gate_kind is not None:
            if gate_kind not in ("tapscribe", "backend"):
                raise ValueError(f"gate_kind must be 'tapscribe' or 'backend', got {gate_kind!r}")
            replacements["gate_kind"] = gate_kind
        if conf is not None:
            replacements["confidence_validation"] = bool(conf)
        if replacements:
            self.config = replace(self.config, **replacements)
        self.info["state"] = "starting"
        self.info["last_error"] = ""
        if model is not None:
            self.info["model"] = model
        # Moonshine is English-only regardless of what the operator's
        # candidate-language set names; reflect that rather than the
        # requested language, so the dashboard never implies a
        # multilingual capability this engine doesn't have. The requested
        # language is deliberately NOT written to config either — see
        # `__init__`'s verbatim-carry rationale (finding #3).
        self.info["language"] = "en"

    def start(self, *, model: str | None = None, language: str | None = None) -> tuple[bool, str]:
        with self._lock:
            if self.running():
                return False, "already running"

            if model is not None:
                self.config = replace(self.config, model=model)
            # language is intentionally NOT threaded through — Moonshine v1
            # is English-only (see PRD #120 Out of Scope); config.language
            # stays "en" no matter what the caller passes.

            if self._ephemeral_port:
                # Bind port 0 and read the kernel's pick back from the
                # server after it's up (below) — never pick-then-bind,
                # which races anything else grabbing ports on this host.
                self.config = replace(self.config, port=0)

            if self._engine is None or self._engine_model_id != self.config.model:
                try:
                    self._engine = self._engine_factory(self.config.model, use_mlx=self.use_mlx)
                    self._engine_model_id = self.config.model
                except Exception as e:
                    msg = f"failed to load Moonshine engine: {e}"
                    self.info["state"] = "error"
                    self.info["last_error"] = msg
                    return False, msg
            engine = self._engine

            server = MoonshineAsrServer(
                host=self.config.host, port=self.config.port, generate_fn=engine.generate
            )
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            errors: list[Exception] = []

            def _run() -> None:
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(server.start())
                except Exception as e:  # noqa: BLE001 — surfaced to the caller via `errors`
                    # Exception, not BaseException: bind/OS failures are what
                    # start() must report ("failed to bind /asr server …");
                    # a BaseException here (nothing realistic raises one in
                    # this daemon thread) should kill the thread visibly —
                    # running() then reads False — not masquerade as a
                    # bind-failure message.
                    errors.append(e)
                finally:
                    ready.set()
                if not errors:
                    loop.run_forever()
                # `stop()` already awaited `server.stop()` to completion
                # before calling `loop.stop()`, but websockets' own
                # per-connection housekeeping (keepalive pings, the
                # close-handshake task) can still have tasks scheduled-but-
                # not-yet-finished at that instant. Draining them here
                # avoids asyncio's noisy "Task was destroyed but it is
                # pending!" warning on `loop.close()` below.
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            if not ready.wait(timeout=5.0):
                msg = "timed out binding the /asr server"
                self.info["state"] = "error"
                self.info["last_error"] = msg
                return False, msg
            if errors:
                msg = f"failed to bind /asr server on {self.config.host}:{self.config.port}: {errors[0]}"
                self.info["state"] = "error"
                self.info["last_error"] = msg
                return False, msg

            self._thread = thread
            self._loop = loop
            self._server = server
            if self._ephemeral_port:
                self.config = replace(self.config, port=server.port)
            self._seed_info()
            self.info["state"] = "running"
            self.info["last_error"] = ""
            self.info["started_at"] = datetime.now(UTC).isoformat()
            return True, "started"

    def stop(self, *, timeout: float = 5.0) -> tuple[bool, str]:
        with self._lock:
            loop = self._loop
            server = self._server
            thread = self._thread
            self._loop = None
            self._server = None
            self._thread = None

        if loop is None:
            self.info["state"] = "stopped"
            return True, "not running"

        if server is not None:
            fut = asyncio.run_coroutine_threadsafe(server.stop(), loop)
            with contextlib.suppress(Exception):
                fut.result(timeout=timeout)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)

        self.info["state"] = "stopped"
        self.info["pid"] = ""
        return True, "stopped"


__all__ = [
    "MoonshineAsrServer",
    "MoonshineEngine",
    "MoonshineLiveChannel",
    "default_engine_factory",
    "is_moonshine_model",
    "resolve_live_channel_for_model",
    "validate_moonshine_model",
]
