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
import socket
import threading
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

from .live import LiveChannel, LiveConfig
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


def _pick_ephemeral_port(host: str) -> int:
    """Same ephemeral-port pattern `live.py` uses for whisperlivekit-server
    — ask the kernel for a free port and release it immediately. A fresh
    pick on every `start()` avoids colliding with the previous spawn's
    port while it's sitting in TIME_WAIT."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


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

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws: Any) -> None:
        window = MoonshineWindow(generate_fn=self._generate_fn)
        try:
            async for message in ws:
                if not isinstance(message, (bytes, bytearray)):
                    continue  # /asr is PCM-only; ignore stray text frames
                window.feed_pcm(bytes(message))
                lines = window.maybe_refresh()
                if lines is not None:
                    await self._send_snapshot(ws, lines)
        except ConnectionClosed:
            pass
        finally:
            # Best-effort final push for the trailing words of a burst that
            # ends less than refresh_s after the last frame arrived. This
            # races the close handshake (the client may already be gone),
            # hence the broad suppress — WlKRelay's own close-time
            # `_flush_tail` already covers the common case from the
            # LAST snapshot it actually received, so a lost final push
            # here is a rare, small-window edge case, not silent data loss.
            lines = window.close()
            if lines:
                with contextlib.suppress(Exception):
                    await self._send_snapshot(ws, lines)

    @staticmethod
    async def _send_snapshot(ws: Any, lines: list[dict]) -> None:
        await ws.send(
            json.dumps(
                {
                    "lines": lines,
                    "buffer_transcription": "",
                    "remaining_time_transcription": 0.0,
                }
            )
        )


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
        self.config = replace(config, gate_kind="tapscribe", language="en")
        self.use_mlx = use_mlx
        self._engine_factory = engine_factory if engine_factory is not None else default_engine_factory
        self._ephemeral_port = config.port == 0
        self.info: dict[str, str] = _initial_moonshine_info()
        self.log: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: MoonshineAsrServer | None = None
        self._seed_info()

    def _seed_info(self) -> None:
        self.info["model"] = self.config.model
        self.info["language"] = self.config.language
        self.info["host"] = self.config.host
        self.info["port"] = str(self.config.port)
        self.info["backend"] = "mlx-audio" if self.use_mlx else "moonshine-onnx"
        self.info["device"] = "Apple Silicon GPU" if self.use_mlx else "CPU"

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
        `WhisperLiveKitChannel.matches` implements. Moonshine ignores
        `conf`/gate-tuning knobs (no confidence-validation flag, no native
        VAD to tune) — only `model` (and `gate_kind`, which must stay
        "tapscribe") can force a restart here."""
        return (
            self.running()
            and (not model or model == self.config.model)
            and (not language or language == self.config.language)
            and (gate_kind is None or gate_kind == "tapscribe")
        )

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
        self.info["state"] = "starting"
        self.info["last_error"] = ""
        if model is not None:
            self.info["model"] = model
        # Moonshine is English-only regardless of what the operator's
        # candidate-language set names; reflect that rather than the
        # requested language, so the dashboard never implies a
        # multilingual capability this engine doesn't have.
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
                self.config = replace(self.config, port=_pick_ephemeral_port(self.config.host))

            try:
                engine = self._engine_factory(self.config.model, use_mlx=self.use_mlx)
            except Exception as e:
                msg = f"failed to load Moonshine engine: {e}"
                self.info["state"] = "error"
                self.info["last_error"] = msg
                return False, msg

            server = MoonshineAsrServer(
                host=self.config.host, port=self.config.port, generate_fn=engine.generate
            )
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            errors: list[BaseException] = []

            def _run() -> None:
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(server.start())
                except BaseException as e:  # noqa: BLE001 — surfaced to the caller via `errors`
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
