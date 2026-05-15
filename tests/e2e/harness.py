"""Server + bridge harness for the E2E pipeline tests.

We launch the real FastAPI app inside a real uvicorn server on a
background thread, then drive it through real `httpx` and `websockets`
clients — same wire as the SpatialChat bridge would use. The Recorder
points at a per-test tmpdir and a fake whisperlivekit-server so no
external subprocess is required.

Why uvicorn-in-thread rather than `TestClient`: TestClient routes
through Starlette's portal, which serialises asyncio operations — two
bridges streaming concurrently is a load shape we want exercised
against a real event loop. The lifespan also runs for real, so a
regression in the boot wiring fails loudly here.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import numpy as np
import uvicorn
import websockets

from tapscribe.auth import TAP_SUBPROTOCOL_PREFIX
from tapscribe.recorder import Recorder


def free_port() -> int:
    """Borrow an unused localhost port. There's a tiny race vs the bind
    below, but uvicorn binds within tens of ms of this call."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


class RecorderServer:
    """Runs the FastAPI app + Recorder on a real uvicorn server in a
    background daemon thread."""

    def __init__(self, app: Any, *, host: str = "localhost", port: int | None = None) -> None:
        self.app = app
        self.host = host
        self.port = port or free_port()
        self._config = uvicorn.Config(
            app=app, host=host, port=self.port, log_level="warning",
            access_log=False, lifespan="on",
        )
        self._server = uvicorn.Server(self._config)
        # Suppress uvicorn's signal handlers so a botched test can't
        # hijack the runner's SIGINT/SIGTERM.
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_base_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def start(self, *, ready_timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.02)
        raise RuntimeError("uvicorn didn't report started within timeout")

    def stop(self, *, timeout: float = 3.0) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)


# Recorder wire format: 16 kHz mono int16 PCM, 20 ms frames. Mirrors the
# constants in bridges/local-test-bridge/local_test_bridge.py — these
# values appear in production too (tap_fan_out hard-codes 16000) but
# there's no shared module yet.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2


def synth_speech_like_wav(out: Path, *, seconds: float, freq_hz: float, amplitude: float = 0.25) -> Path:
    """Write a small 16 kHz mono int16 WAV with a slowly-modulated tone.

    Pure sine waves trip Whisper's VAD (no decoded segments) but they
    clear the recorder's RMS-only silence floor — which is all the
    FakeTranscriber path needs. The 4 Hz amplitude envelope is there so
    the file looks speech-shaped on a spectrogram if a human ever opens
    it.
    """
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    envelope = 0.5 + 0.5 * np.sin(2.0 * np.pi * 4.0 * t)
    samples = amplitude * envelope * np.sin(2.0 * np.pi * freq_hz * t)
    int16 = (samples * 32767.0).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(int16.tobytes())
    return out


def read_wav_as_pcm_bytes(path: Path) -> bytes:
    """Return the raw 16 kHz mono int16 PCM body of a WAV. Raises if
    the file isn't already in the wire format — by design; a real
    bridge would resample upstream, the test bridge does not."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
            raise RuntimeError(
                f"{path.name}: expected 16 kHz mono int16, got "
                f"{w.getframerate()} Hz / {w.getnchannels()}ch / {w.getsampwidth() * 8}-bit"
            )
        return w.readframes(w.getnframes())


def frame_pcm(pcm: bytes) -> list[bytes]:
    """Slice raw PCM into 20 ms (640-byte) frames; drop any trailing
    partial frame. Mirrors local_test_bridge.chunk_into_frames."""
    n = len(pcm) // FRAME_BYTES
    return [pcm[i * FRAME_BYTES : (i + 1) * FRAME_BYTES] for i in range(n)]


@dataclass
class BridgeRun:
    """Summary of one `stream_wav_via_tap` invocation."""
    identity: str
    name: str
    utterance_id: str
    frames_sent: int
    bytes_sent: int


async def stream_wav_via_tap(
    *,
    ws_base_url: str,
    identity: str,
    name: str,
    wav_path: Path,
    utterance_id: str | None = None,
    tap_token: str = "",
    frame_interval_s: float = 0.0,
) -> BridgeRun:
    """Open one /tap WS, stream `wav_path` as 20 ms PCM frames, close.

    Mirrors the SpatialChat bridge's per-utterance wire pattern. One
    frame per WebSocket message so the relay's caption granularity is
    realistic; if `frame_interval_s > 0` the bridge paces frames so a
    test can sample mid-stream state rather than only post-close.
    """
    pcm = read_wav_as_pcm_bytes(wav_path)
    frames = frame_pcm(pcm)

    qs = urlencode({"identity": identity, "name": name, "utterance_id": utterance_id or ""})
    url = f"{ws_base_url}/tap?{qs}"
    subprotocols = [f"{TAP_SUBPROTOCOL_PREFIX}{tap_token}"] if tap_token else None

    sent_bytes = 0
    async with websockets.connect(url, subprotocols=subprotocols) as ws:
        for frame in frames:
            await ws.send(frame)
            sent_bytes += len(frame)
            if frame_interval_s > 0:
                await asyncio.sleep(frame_interval_s)

    return BridgeRun(
        identity=identity,
        name=name,
        utterance_id=utterance_id or "",
        frames_sent=len(frames),
        bytes_sent=sent_bytes,
    )


async def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll `predicate` (sync or async) until truthy or timeout. Returns
    the final value so callers can `assert await wait_until(...)`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(interval)
    result = predicate()
    if asyncio.iscoroutine(result):
        result = await result
    return bool(result)


async def streams_drained(recorder: Recorder) -> bool:
    """True once the recorder has finalised every in-flight /tap WS."""
    return len(await recorder.streams.snapshot()) == 0
