#!/usr/bin/env python3
"""local-test-bridge — Python dev tool that taps the local mic and
streams to the Recorder's `/tap` endpoint.

Usage:
  python bridges/local-test-bridge/local_test_bridge.py
  python bridges/local-test-bridge/local_test_bridge.py --identity alice --name Alice

Wire contract (see bridges/README.md): one WebSocket per utterance to
`ws://<host>:<port>/tap`, raw 16 kHz mono int16 PCM in 20 ms (640-byte)
frames. The Recorder writes each WS to a WAV AND relays bytes to its
WhisperLiveKit child for live captions — no JSON, no HTTP, no
WlK-protocol awareness needed here.

UI: ENTER toggles between idle and recording. Each idle→recording
transition opens a fresh `/tap` WS (one WAV per cycle). Ctrl+C exits
cleanly, finalising any in-flight WAV before quitting.

Dependencies: `sounddevice` for mic capture (pip install sounddevice
or `pip install -e ".[dev]"` from the repo root) and `websockets`
(already a base TapScribe dep).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import signal
import sys
import threading
import urllib.parse
import uuid
from collections.abc import Iterator

import numpy as np
import websockets

# 16 kHz mono int16 = 2 bytes/sample. /tap expects 20 ms frames =
# 320 samples = 640 bytes. Same constants the Recorder uses.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2


# ---------------------------------------------------------------------------
# Pure helpers (testable without a mic or a WS server)
# ---------------------------------------------------------------------------


def chunk_into_frames(buf: bytes) -> Iterator[bytes]:
    """Yield exact 640-byte frames from `buf`. Partial trailing frames are
    dropped — the caller is expected to keep them in a buffer for the
    next call so frames stay aligned."""
    n_frames = len(buf) // FRAME_BYTES
    for i in range(n_frames):
        yield buf[i * FRAME_BYTES : (i + 1) * FRAME_BYTES]


TAP_SUBPROTOCOL_PREFIX = "tapscribe.v1.tap."


def build_tap_url(
    *,
    host: str,
    port: int,
    identity: str,
    name: str,
    tls: bool = False,
    utterance_id: str | None = None,
    session: str | None = None,
) -> str:
    params: dict[str, str] = {"identity": identity, "name": name}
    if utterance_id:
        params["utterance_id"] = utterance_id
    if session:
        # Detached-session routing: the Recorder refuses the upgrade for
        # unknown ids, so only send the param when the operator asked.
        params["session"] = session
    qs = urllib.parse.urlencode(params)
    scheme = "wss" if tls else "ws"
    return f"{scheme}://{host}:{port}/tap?{qs}"


def new_utterance_id() -> str:
    """Mint a fresh per-utterance id. Matches the spacialchat bridge's
    format (`uuid4().hex`, dashes stripped) for symmetry with how the
    recorder logs it."""
    return uuid.uuid4().hex


def build_subprotocols(tap_token: str) -> list[str]:
    """Return the list to pass to `websockets.connect(subprotocols=...)`.
    Empty list = bridge speaks no subprotocol (operator ran with --no-auth)."""
    return [TAP_SUBPROTOCOL_PREFIX + tap_token] if tap_token else []


def default_identity() -> str:
    """Use the OS username so multi-terminal testing produces distinct
    identities by default. Falls back to a literal so the bridge always
    has *some* identity to send."""
    return os.environ.get("USER") or os.environ.get("USERNAME") or "local-tester"


# ---------------------------------------------------------------------------
# Audio capture — sounddevice puts frames into a thread-safe queue
# ---------------------------------------------------------------------------


class MicCapture:
    """Wraps sounddevice in the simplest shape: start() opens an input
    stream that fills `pcm_queue` with raw int16 bytes; stop() closes it.

    sounddevice runs its own audio thread (PortAudio); the queue lets us
    hand bytes back to the asyncio loop without blocking it.
    """

    def __init__(self, *, device: int | str | None = None) -> None:
        self.pcm_queue: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self._device = device
        self._stream = None  # set on start()

    def start(self) -> None:
        # Lazy import so the test module can load without a working
        # PortAudio install (CI / headless dev).
        import sounddevice as sd  # type: ignore

        def _callback(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                # PortAudio underrun/overflow — surface but don't crash.
                print(f"[bridge] audio stream status: {status}", flush=True)
            # indata is float32 in [-1, 1]; convert to int16 and enqueue.
            int16 = (indata[:, 0] * 32767).astype(np.int16)
            try:
                self.pcm_queue.put_nowait(int16.tobytes())
            except queue.Full:
                # Backpressure — older bytes get dropped. Should never
                # happen during a real /tap session because the WS pump
                # drains as fast as PortAudio fills.
                pass

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME_SAMPLES,
            device=self._device,
            callback=_callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        # Drain residual bytes so the next start() begins fresh.
        with self.pcm_queue.mutex:
            self.pcm_queue.queue.clear()


# ---------------------------------------------------------------------------
# /tap session — open WS, stream queue contents, close
# ---------------------------------------------------------------------------


async def run_tap_session(
    *,
    host: str,
    port: int,
    identity: str,
    name: str,
    pcm_queue: queue.Queue[bytes],
    stop_event: asyncio.Event,
    tap_token: str = "",
    tls: bool = False,
    utterance_id: str | None = None,
    session: str | None = None,
) -> int:
    """Open one /tap WS and stream queue bytes until stop_event is set or
    the WS dies. Returns the total bytes sent."""
    url = build_tap_url(
        host=host,
        port=port,
        identity=identity,
        name=name,
        tls=tls,
        utterance_id=utterance_id,
        session=session,
    )
    subprotocols = build_subprotocols(tap_token)
    print(f"[bridge] connecting → {url}" + (" (with tap-token)" if tap_token else " (no auth)"), flush=True)
    sent = 0
    pending = b""  # bytes carried over between queue gets so frames stay aligned

    try:
        async with websockets.connect(url, subprotocols=subprotocols or None) as ws:
            print("[bridge] /tap open — streaming", flush=True)
            while not stop_event.is_set():
                # Wait for at least one chunk from the audio thread, but
                # don't block the loop forever — re-check stop_event at ~50Hz.
                try:
                    chunk = await asyncio.get_running_loop().run_in_executor(
                        None,
                        _queue_get_with_timeout,
                        pcm_queue,
                        0.02,
                    )
                except _QueueTimeout:
                    continue
                if chunk is None:
                    continue
                pending += chunk
                for frame in chunk_into_frames(pending):
                    await ws.send(frame)
                    sent += len(frame)
                # Keep the leftover tail (< 640 bytes) for next iteration.
                pending = pending[(len(pending) // FRAME_BYTES) * FRAME_BYTES :]
    except websockets.ConnectionClosed as e:
        print(f"[bridge] /tap closed by server: {e}", flush=True)
    except OSError as e:
        print(f"[bridge] couldn't connect: {e}", flush=True)
    finally:
        secs = sent / (SAMPLE_RATE * 2)
        print(f"[bridge] /tap closed — sent {sent} bytes ({secs:.1f}s)", flush=True)
    return sent


class _QueueTimeout(Exception):
    pass


def _queue_get_with_timeout(q: queue.Queue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except queue.Empty as e:
        raise _QueueTimeout from e


# ---------------------------------------------------------------------------
# Main loop — ENTER toggles idle ↔ recording
# ---------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> int:
    capture = MicCapture(device=args.mic)
    state = {"recording": False}
    stop_event = asyncio.Event()  # set briefly to interrupt an active /tap session
    quit_event = asyncio.Event()

    # Ctrl+C handler that finalises in-flight WAV before exit.
    def _on_sigint(*_args):
        print("\n[bridge] Ctrl+C — finishing up…", flush=True)
        stop_event.set()
        quit_event.set()

    signal.signal(signal.SIGINT, _on_sigint)

    # Stdin reader thread: blocks on input(), pushes a token to a queue
    # whenever the user hits ENTER. The asyncio loop polls for tokens.
    enter_queue: queue.Queue[None] = queue.Queue()

    def _stdin_reader():
        while not quit_event.is_set():
            try:
                input()
            except EOFError:
                quit_event.set()
                return
            enter_queue.put(None)

    threading.Thread(target=_stdin_reader, daemon=True).start()

    print(f"[bridge] connected to {args.host}:{args.port} as identity={args.identity!r}, name={args.name!r}")
    print("[bridge] press ENTER to start recording, ENTER again to pause, Ctrl+C to quit")
    print("[bridge] [idle]")

    try:
        while not quit_event.is_set():
            # Wait for the next ENTER (or quit signal).
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _queue_get_with_timeout,
                    enter_queue,
                    0.1,
                )
            except _QueueTimeout:
                continue
            if quit_event.is_set():
                break

            if not state["recording"]:
                # Begin a fresh /tap cycle. One ENTER cycle = one
                # utterance, so we mint a single utterance_id here and
                # keep it stable through the cycle. If the WS dies and
                # we add reconnect later, the same id will let the
                # recorder resume the same WAV instead of fragmenting.
                state["recording"] = True
                stop_event = asyncio.Event()
                utterance_id = new_utterance_id()
                capture.start()
                print(f"[bridge] [recording] utterance_id={utterance_id[:8]}…")

                async def _session_done(this_stop=stop_event, this_utt=utterance_id):
                    # Default-arg trick captures the current stop_event so a
                    # later cycle's reassignment doesn't leak into this task.
                    await run_tap_session(
                        host=args.host,
                        port=args.port,
                        identity=args.identity,
                        name=args.name,
                        pcm_queue=capture.pcm_queue,
                        stop_event=this_stop,
                        tap_token=args.tap_token,
                        tls=args.tls,
                        utterance_id=this_utt,
                        session=args.session,
                    )
                    capture.stop()
                    state["recording"] = False
                    if not quit_event.is_set():
                        print("[bridge] [idle]")

                asyncio.create_task(_session_done())
            else:
                # End the current cycle
                stop_event.set()
                # Wait briefly for the session task to finish closing
                for _ in range(50):  # up to ~500 ms
                    if not state["recording"]:
                        break
                    await asyncio.sleep(0.01)
    finally:
        # Clean shutdown — ensure any active session closes its WS.
        if state["recording"]:
            stop_event.set()
            for _ in range(100):
                if not state["recording"]:
                    break
                await asyncio.sleep(0.01)

    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="local_test_bridge",
        description="Tap the local mic and stream to the TapScribe Recorder's /tap endpoint. "
        "ENTER toggles idle/recording.",
    )
    p.add_argument("--host", default="localhost", help="Recorder host (default: localhost)")
    p.add_argument("--port", type=int, default=8001, help="Recorder port (default: 8001)")
    p.add_argument(
        "--identity",
        default=default_identity(),
        help="Identity sent on each /tap WS. Defaults to $USER / $USERNAME / 'local-tester'.",
    )
    p.add_argument("--name", default="Local Tester", help="Display name (shown on the dashboard)")
    p.add_argument(
        "--mic", default=None, help="sounddevice input device name or index. Default: system default input."
    )
    p.add_argument(
        "--tap-token",
        default=os.environ.get("TAPSCRIBE_TAP_TOKEN", ""),
        help="Bearer token the recorder requires on the /tap WS (carried via "
        "Sec-WebSocket-Protocol). Defaults to $TAPSCRIBE_TAP_TOKEN or empty "
        "(use when the recorder runs with --no-auth).",
    )
    p.add_argument(
        "--tls", action="store_true", help="Connect over wss:// (the recorder was started with --tls)."
    )
    p.add_argument(
        "--session",
        default=None,
        help="Detached-session id to direct this bridge's taps into "
        "(?session= on each /tap WS). Create one with: "
        "curl -X POST -H 'Authorization: Bearer <tap-token>' "
        "-H 'Content-Type: application/json' -d '{\"detached\": true}' "
        "http://<host>:<port>/api/tap/new-session. "
        "Default: the Recorder's global current session.",
    )
    args = p.parse_args()

    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
