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

Consequence: this module touches nothing downstream — selecting Moonshine
is purely a matter of which concrete `LiveChannel` the Recorder holds
(see `tapscribe.live_control.plan_live`, which resolves the family swap
from the requested model's catalog family; `/api/live/start` is a
parse-and-delegate shim over it — ADR-0014). What was generalized
downstream to make that swap seamless is documented in CONTEXT.md's
`MoonshineLiveChannel` section — the single home for that architectural
note.

No subprocess here — unlike `WhisperLiveKitChannel`, there is no child
process to spawn/supervise/pump logs from. Instead `start()` spins up a
dedicated background thread running its own asyncio event loop hosting the
`/asr` websockets server; `stop()` tears both down. This mirrors the
"own thread, own loop" shape rather than reusing the FastAPI app's loop,
because `LiveChannel.start()`/`stop()` are synchronous Protocol methods —
callers already run `apply_live` (which drives them) via
`asyncio.to_thread`, exactly like `WhisperLiveKitChannel.start()`'s
blocking subprocess spawn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

from .live import (
    LOG_TAIL_LINES,
    LiveChannel,
    LiveChannelBase,
    LiveConfig,
    TailLog,
)
from .transcribers._moonshine_window import MoonshineWindow


class MoonshineEngine(Protocol):
    """What `MoonshineWindow` needs from an inference engine — the shape
    both `transcribers.moonshine_mlx.MlxMoonshineEngine` and
    `transcribers.moonshine_onnx.OnnxMoonshineEngine` satisfy."""

    def generate(self, audio: np.ndarray) -> str: ...


def validate_moonshine_model(model_id: str) -> None:
    """The allowlist gate (PRD #120 user story #23, mirrors the summarizer
    `SUMMARY_MODELS` rule): a model id must resolve to a current
    `family="moonshine"` registry entry before any engine-load / Hub
    download can happen — the catalog is the ONE allowlist, so adding
    `moonshine-small` there is the whole change. Imported lazily (not at
    module scope) to avoid a catalog import cycle. Raises `ValueError`
    on any mismatch."""
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
    the routing predicate `resolve_live_channel_for_model` (and through it
    `live_control.plan_live`) uses to decide whether `recorder.live`
    should be a `MoonshineLiveChannel` rather than a
    `WhisperLiveKitChannel`. Never raises — an unknown or unregistered id
    is simply "not moonshine", so an ordinary Whisper model name is the
    (fast, common) False case."""
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
    `live_control.plan_live` resolves (unconditionally, #259) before the
    usual `matches()` / `begin_transition()` / `start()` sequence
    `apply_live` then executes (ADR-0014).

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


# How long the `/asr` handler's close-time (abrupt-disconnect) decode may
# run before it is abandoned. Bounds teardown: `MoonshineLiveChannel.stop()`
# waits on the loop thread, which waits on this handler. Comfortably above a
# real full-window decode on a slow CPU, well under stop()'s 5 s default.
_FINAL_DECODE_TIMEOUT_S = 3.0


class MoonshineAsrServer:
    """In-process `/asr` WebSocket server speaking `WlKRelay`'s wire
    contract. One `MoonshineWindow` per connection (one `/tap` utterance),
    so state never leaks across connections. `generate_fn` is injected —
    production wires it to a loaded `MoonshineEngine.generate`; tests use
    a stub. `decode_executor` (when supplied — the channel always does)
    pins every decode to one dedicated thread; see `_decode`."""

    def __init__(self, *, host: str, port: int, generate_fn, decode_executor=None) -> None:
        self._host = host
        self._port = port
        self._generate_fn = generate_fn
        self._decode_executor = decode_executor
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

    async def _decode(self, fn):
        """Run a blocking model op (`window.maybe_refresh` / `window.close`
        → `engine.generate`) off the event loop.

        With the channel-supplied single-thread `decode_executor`, EVERY
        model op — the engine load in `MoonshineLiveChannel.start()` and
        all decodes here — lands on the same one thread. MLX's Metal
        stream is thread-local (see `transcribers.MODEL_THREAD_PREFIX`):
        weights created on one thread can't be evaluated from another, and
        `asyncio.to_thread`'s multi-worker default executor guarantees
        exactly that violation — plus concurrent `generate()` calls on one
        engine with 2+ open taps (mic + loopback). The ONNX engine doesn't
        need the affinity (onnxruntime inference sessions are thread-safe)
        but routes through the same executor anyway: one code path, and
        serialised decodes match the repo's one-model-op-at-a-time
        convention. The `asyncio.to_thread` fallback exists only for tests
        that construct this server directly with a stub `generate_fn` — no
        real engine, no affinity to preserve."""
        if self._decode_executor is None:
            return await asyncio.to_thread(fn)
        return await asyncio.get_running_loop().run_in_executor(self._decode_executor, fn)

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
                    lines = await self._decode(window.close)
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
                    lines = await self._decode(window.maybe_refresh)
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
                #
                # Bounded: this is the decode a channel teardown waits on
                # (`server.stop()` closes the peers, every handler lands
                # here), and on a slow CPU one full-window Moonshine decode
                # outlasts `MoonshineLiveChannel.stop()`'s join timeout —
                # holding the loop thread, and with it the `/asr` socket,
                # hostage. Timing out abandons only this best-effort tail;
                # the decode thread finishes on its own.
                try:
                    lines = await asyncio.wait_for(
                        self._decode(window.close), timeout=_FINAL_DECODE_TIMEOUT_S
                    )
                except TimeoutError:
                    lines = None
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


# How long start() waits for the server thread to report bind success or
# failure. Module-level so the ready-timeout teardown test can shrink it.
_READY_TIMEOUT_S = 5.0


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


class MoonshineLiveChannel(LiveChannelBase):
    """Concrete `LiveChannel` (Protocol) backed by Moonshine. No
    subprocess: `start()` loads the inference engine (MLX or ONNX-CPU,
    per `use_mlx`) and spins up a dedicated thread running the `/asr`
    websockets server on its own event loop; `stop()` tears both down.

    `engine_factory` is injected for tests (`(model_id, *, use_mlx) ->
    MoonshineEngine`); production defaults to `default_engine_factory`,
    which validates the model against the catalog before loading it.
    """

    supports_native_vad: bool = False  # no built-in VAC — SpeechGate is the only gate
    # English-only engine: `language` never forces a restart and `info`
    # always reports "en" (the shared `LiveChannelBase` honours the
    # `fixed_language` hook).
    fixed_language: str | None = "en"
    # No confidence-validation knob (that's a WhisperLiveKit feature), so
    # `info["confidence_validation"]` stays "" ("not applicable").
    supports_confidence_validation: bool = False

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
        # Dedicated single-thread executor for EVERY Moonshine model op —
        # engine load + all window decodes (`MoonshineAsrServer._decode`).
        # The live-channel analogue of `transcribers._MODEL_EXECUTOR`
        # (deliberately NOT shared with it: a latency-sensitive live decode
        # must not queue behind a multi-second batch-transcribe window).
        # Created lazily on first `start()` and NOT shut down in `stop()`:
        # the engine cache above survives stop() (gate-knob applies are
        # stop→start), and MLX weights must keep being evaluated on the
        # thread that created them — so the executor lives exactly as long
        # as the cached engine, i.e. the channel. Its idle worker exits
        # when the channel (and executor) are garbage-collected.
        self._model_executor: ThreadPoolExecutor | None = None
        self.info: dict[str, str] = _initial_moonshine_info()
        # TailLog for parity with WhisperLiveKitChannel: /api/state and
        # /api/live/log iterate `log` on the event loop; any future
        # thread-side appender is safe by construction.
        self.log: deque[str] = TailLog(maxlen=LOG_TAIL_LINES)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: MoonshineAsrServer | None = None
        self._seed_info()

    def _seed_info(self) -> None:
        self.info["model"] = self.config.model
        # `info` reports what the engine actually DOES, not what the
        # carried config says: Moonshine is English-only regardless of
        # config.language. (gate_kind is reported the same way, from the
        # shared `_mirror_gate_info` below — via the `effective_gate_config`
        # seam TapRelay builds the tap gate from, so report and behavior
        # can't diverge.) The config itself stays untouched so the
        # operator's choices survive the roundtrip back to Whisper.
        self.info["language"] = "en"
        self.info["host"] = self.config.host
        self.info["port"] = str(self.config.port)
        self.info["backend"] = "mlx-audio" if self.use_mlx else "moonshine-onnx"
        self.info["device"] = "Apple Silicon GPU" if self.use_mlx else "CPU"
        self._mirror_gate_info()

    def running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

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

            if self._model_executor is None:
                self._model_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="tapscribe-moonshine-model"
                )
            if self._engine is None or self._engine_model_id != self.config.model:
                try:
                    # Load ON the model thread, not the caller's: decodes run
                    # there (see MoonshineAsrServer._decode), and MLX weights
                    # must be created on the thread that will evaluate them.
                    self._engine = self._model_executor.submit(
                        self._engine_factory, self.config.model, use_mlx=self.use_mlx
                    ).result()
                    self._engine_model_id = self.config.model
                except Exception as e:
                    msg = f"failed to load Moonshine engine: {e}"
                    self.info["state"] = "error"
                    self.info["last_error"] = msg
                    return False, msg
            engine = self._engine

            server = MoonshineAsrServer(
                host=self.config.host,
                port=self.config.port,
                generate_fn=engine.generate,
                decode_executor=self._model_executor,
            )
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            # Set by the ready-timeout branch below: the caller gave up on
            # this spawn, so `_run` must fall through to teardown instead of
            # parking in `run_forever` (a bind completing after the deadline
            # would otherwise leave a running server nothing references).
            abandoned = threading.Event()
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
                if not errors and not abandoned.is_set():
                    loop.run_forever()
                # Close the server on the loop's own thread before draining.
                # Idempotent no-op after a normal `stop()` (which already
                # awaited `server.stop()` before `loop.stop()`) and after a
                # failed bind — but on `start()`'s ready-timeout path this
                # is the ONLY teardown: a bind that completed AFTER the
                # deadline is unwound here instead of leaving an orphaned
                # /asr listener nothing references. The RuntimeError
                # suppressions: the timeout branch queues ONE loop.stop()
                # that, depending on where the late bind was when it landed,
                # may be consumed not by run_forever but by one of these
                # teardown mini-runs ("Event loop stopped before Future
                # completed") — absorb it so the rest of the teardown and
                # loop.close() still run. The no-op sleep(0) mini-run first
                # consumes such a stray stop (harmless on every other path),
                # so the server close itself is never the run it interrupts.
                with contextlib.suppress(RuntimeError):
                    loop.run_until_complete(asyncio.sleep(0))
                with contextlib.suppress(RuntimeError):
                    loop.run_until_complete(server.stop())
                # Even after a clean server close, websockets' own
                # per-connection housekeeping (keepalive pings, the
                # close-handshake task) can still have tasks scheduled-but-
                # not-yet-finished at that instant. Draining them here
                # avoids asyncio's noisy "Task was destroyed but it is
                # pending!" warning on `loop.close()` below.
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    with contextlib.suppress(RuntimeError):
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            if not ready.wait(timeout=_READY_TIMEOUT_S):
                # A bind completing AFTER this deadline must not leave a
                # running /asr server nothing can tear down. Flag the spawn
                # abandoned FIRST (so `_run` skips `run_forever` however
                # late the bind lands), then stop the loop (threadsafe;
                # covers the sliver where `_run` checked the flag and
                # entered `run_forever` just before it was set) — either
                # way `_run` falls through to its `server.stop()` + drain +
                # `loop.close()` tail. Join briefly; `_thread`/`_loop`/
                # `_server` were never assigned, so `running()` stays False
                # and `stop()` remains a safe idempotent no-op
                # ("not running"). If the thread is wedged in the blocking
                # `getaddrinfo` past the join timeout it stays a daemon
                # thread with the teardown already queued — best effort.
                # The suppress covers the loop having finished and closed
                # between the wait and this call.
                abandoned.set()
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=2.0)
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
        """Tear the `/asr` server and its loop thread down. Idempotent.

        Returns `(False, …)` when the loop thread is STILL alive after the
        join — see the `is_alive` check below for why claiming a clean
        shutdown there is worse than reporting the timeout."""
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
            if thread.is_alive():
                # The join did NOT complete: `loop.stop()` only takes effect
                # after the current callback returns, so one slow in-flight
                # decode (the `/asr` handler's close-time `window.close()`)
                # can outlast `timeout` — leaving the thread alive and still
                # holding the `/asr` socket. Reporting "stopped" here made
                # the immediately-following start() fail with a bare
                # `[Errno 98] Address already in use` on an operator-pinned
                # port, with no trace of the cause.
                msg = f"live channel did not shut down within {timeout}s"
                with self._lock:
                    if self._thread is None:
                        self.info["state"] = "error"
                        self.info["last_error"] = msg
                return False, msg

        with self._lock:
            if self._thread is not None or self._loop is not None:
                # A concurrent start() (a double-clicked Apply: both requests
                # offload `apply_live` to a worker) installed a replacement
                # while we were joining — `info` now describes THAT server
                # ("running", its fresh port). Stamping "stopped" would tell
                # the dashboard the channel is down while it is serving. Same
                # ownership guard `WhisperLiveKitChannel.stop()` carries; our
                # own loop IS down, so still report success.
                return True, "stopped"
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
