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
import os
import re
import socket
import threading
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import numpy as np
import pytest
import uvicorn
import websockets

from tapscribe.auth import TAP_SUBPROTOCOL_PREFIX
from tapscribe.recorder import Recorder

_WORD_TOKENS_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def word_tokens(text: str, *, min_len: int = 4) -> set[str]:
    """≥`min_len`-char lowercased word set — the ONE e2e helper for soft
    reference-overlap assertions. Matches alphabetic runs only: real-backend
    output carries punctuation the references don't. Every e2e module imports
    this; there are no per-file copies left, so tightening the semantics here
    moves all the suites together instead of silently splitting them."""
    return {m.group(0).lower() for m in _WORD_TOKENS_RE.finditer(text) if len(m.group(0)) >= min_len}


@asynccontextmanager
async def playwright_session():
    """`async_playwright()` plus this repo's e2e selector convention.

    `data-slot` is the native `data-*` marker the dashboard templates already
    carry — `slot()`/`pick()` bind through it (`web/js/templates.js`), and
    `pick()` throws on a missing slot, so a renamed slot breaks the app's own
    render in the same commit a test would. That makes it a stable,
    can't-silently-rot test hook. Pointing Playwright's test-id attribute at it
    lets tests address elements by intent — `page.get_by_test_id("waveName")`
    resolves `[data-slot="waveName"]` with auto-waiting and clean errors.
    Existing `[data-slot=...]` CSS locators keep working unchanged; this only
    *adds* the `get_by_test_id` entry point. There is no native HTML `testid`
    attribute — `data-*` is the spec's mechanism for a scriptable handle, so
    this is the native convention, not a borrowed framework idiom.

    The `playwright` import is local: this module is imported during collection
    on the unit-test boxes that don't install playwright (conftest imports
    harness unconditionally), while the e2e test modules skip themselves when
    playwright is absent.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        # One source of truth for the data-slot test-id convention; see docstring.
        pw.selectors.set_test_id_attribute("data-slot")
        yield pw


def bridge_chromium_args(ext_dir: Path) -> list[str]:
    """Chromium launch args shared by the bridge ``browser_e2e`` fixtures.

    Loads the bridge as an MV3 extension (callers pass ``headless=False`` — MV3
    doesn't load headless), fakes the media-stream device so the mock room's
    oscillator track is permitted, autoplays the AudioContext without a user
    gesture, and relaxes Private Network Access so the ``https`` mock
    SpatialChat page can open a ``ws://`` to the loopback test server. Without
    the PNA relax, recent Chromium silently strands every ``/tap`` WS (the dial
    never completes the handshake — see PR #148); in production the operator
    runs on ``localhost`` or enables TLS.

    Also disables Chromium's background/occluded-tab throttling. The bridge
    fixtures open the SpatialChat test tab via ``ctx.new_page()`` alongside
    the persistent context's own initial tab, so Chromium can (and does, in a
    loaded/headless-ish CI display) treat the test tab as occluded and
    deprioritize its renderer — throttling ``setTimeout``/``setInterval``
    (``page-script.js``'s poll loop among them) and delaying postMessage/
    WS-close delivery by seconds. One contributor to the ``bridge E2E`` job's
    intermittent flakes (a prior fix, d1c3860, only widened a timeout from 5s
    to 15s — the flake predated that change and still exceeded 15s
    afterwards); see ``WAIT_POLLING_MS`` below for the other one — Chromium
    also stops firing ``requestAnimationFrame`` for a backgrounded tab, which
    these flags do NOT cover and which is what Playwright's
    ``wait_for_function`` polls on by default.
    """
    return [
        f"--disable-extensions-except={ext_dir}",
        f"--load-extension={ext_dir}",
        "--no-sandbox",
        "--autoplay-policy=no-user-gesture-required",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--allow-running-insecure-content",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        # One flag, comma-joined value — kept on a single line so it isn't read
        # as an implicit string-concat (CodeQL py/implicit-string-concatenation-
        # in-list flags adjacent literals in a list as a likely missing comma).
        "--disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights,LocalNetworkAccessChecks",
    ]


# Pass as `polling=` to every bridge-fixture `page.wait_for_function()` /
# `popup.wait_for_function()` call instead of relying on the default
# polling="raf": rAF-based polling only runs on compositor frames, which
# Chromium simply stops producing for a page that isn't the frontmost tab.
# Every bridge fixture opens more than one tab in the same context (an
# initial blank tab, a popup, the SpatialChat mock page), so any of them can
# be the backgrounded one at a given moment — an interval poll keeps working
# regardless of which tab currently has focus. `bridge_chromium_args()`'s
# throttling flags don't cover this: those affect setTimeout/setInterval and
# renderer scheduling priority, not requestAnimationFrame.
WAIT_POLLING_MS = 50


async def launch_bridge_context(pw, ext_dir: Path, user_data_dir: str):
    """Launch a headed persistent Chromium context with the bridge MV3
    extension loaded — the shared headed launch for the bridge ``browser_e2e``
    tests (extension + meeting).

    MV3 extensions don't load headless, so this needs a real display. With no
    ``DISPLAY`` it SKIPS with an ``xvfb-run`` hint instead of failing with a
    cryptic 'browser has been closed' error; run these under
    ``xvfb-run -a python -m pytest ...`` (see CONTRIBUTING.md "Running tests").
    A launch failure *with* a display present is a real fault and propagates —
    it is not swallowed as a skip, so a genuinely broken Chromium fails red
    rather than silently masking a regression.
    """
    if not os.environ.get("DISPLAY"):
        pytest.skip(
            "headed bridge browser_e2e needs a display — run under xvfb, e.g. "
            "`xvfb-run -a python -m pytest tests/e2e/test_bridge_extension_e2e.py "
            "tests/e2e/test_bridge_meeting_e2e.py -m browser_e2e` (see CONTRIBUTING.md)"
        )
    return await pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=False,  # MV3 extensions don't load headless
        args=bridge_chromium_args(ext_dir),
    )


def free_port() -> int:
    """Borrow an unused localhost port (reserved on IPv4). uvicorn later binds
    the dual-stack `localhost` name, so the port can still collide on ::1;
    `RecorderServer.start()` retries on a fresh port to absorb that."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_PORT_RETRY_ATTEMPTS = 5


class RecorderServer:
    """Runs the FastAPI app + Recorder on a real uvicorn server in a
    background daemon thread."""

    def __init__(self, app: Any, *, host: str = "localhost", port: int | None = None) -> None:
        self.app = app
        self.host = host
        # A caller-supplied port disables retry — we honour it as-is.
        self._fixed_port = port is not None
        self.port = port or free_port()
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._bind_error: BaseException | None = None
        self._build_server(self.port)

    def _build_server(self, port: int) -> None:
        self.port = port
        self._config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(self._config)
        # Suppress uvicorn's signal handlers so a botched test can't
        # hijack the runner's SIGINT/SIGTERM.
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_base_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def _run_server(self) -> None:
        """Thread target: wrap server.run() so a bind failure (port collision)
        becomes a recoverable signal for `start()` rather than an uncatchable
        exception in a daemon thread. uvicorn catches the bind OSError
        internally and re-raises it as SystemExit(STARTUP_FAILURE), so we catch
        that too -- otherwise the retry in start() never fires and the caller
        just times out."""
        assert self._server is not None
        try:
            self._server.run()
        except (OSError, SystemExit) as e:
            self._bind_error = e

    def start(self, *, ready_timeout: float = 5.0) -> None:
        # free_port() reserves the port on IPv4 only, but uvicorn binds the
        # dual-stack `localhost` name, so it can still collide on ::1 (and any
        # process can grab the port in the free-then-bind window). Retry with a
        # fresh port on ANY startup failure -- a captured bind error, a thread
        # that exited before startup, or a ready timeout. A caller-supplied port
        # disables retry (we honour it as-is).
        attempts = 1 if self._fixed_port else _PORT_RETRY_ATTEMPTS
        last_error: BaseException | None = None
        for _ in range(attempts):
            assert self._server is not None
            self._bind_error = None
            self._thread = threading.Thread(target=self._run_server, daemon=True)
            self._thread.start()
            deadline = time.time() + ready_timeout
            while time.time() < deadline:
                if self._bind_error is not None or not self._thread.is_alive():
                    break
                if getattr(self._server, "started", False):
                    return
                time.sleep(0.02)
            if getattr(self._server, "started", False):
                return
            # Not started: a captured bind error, a thread that exited before
            # startup, or a timeout. Stop the old server, then retry fresh.
            last_error = self._bind_error or RuntimeError("uvicorn didn't report started within timeout")
            self._server.should_exit = True
            self._thread.join(timeout=1.0)
            if self._fixed_port:
                raise last_error
            self._build_server(free_port())
        raise RuntimeError(f"uvicorn failed to start after {attempts} attempts: {last_error!r}")

    def stop(self, *, timeout: float = 3.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)


# Recorder wire format: 16 kHz mono int16 PCM, 20 ms frames. Imported, not
# restated — this is Python on the Recorder's own side of the wire, so it can
# simply use the Recorder's constants. (It used to declare its own copies
# under a comment claiming "tap_fan_out hard-codes 16000", which was already
# false: that module contains no such literal. Stale prose about the wire
# contract is the drift #356 is about; a Bridge in another language has to be
# stamped, but a Python test helper just imports.)
from tapscribe.audio import RECORDER_SAMPLE_RATE as SAMPLE_RATE  # noqa: E402
from tapscribe.speech_gate import FRAME_BYTES, FRAME_SAMPLES  # noqa: E402,F401


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
    session: str | None = None,
    frame_interval_s: float = 0.0,
) -> BridgeRun:
    """Open one /tap WS, stream `wav_path` as 20 ms PCM frames, close.

    Mirrors the SpatialChat bridge's per-utterance wire pattern. One
    frame per WebSocket message so the relay's caption granularity is
    realistic; if `frame_interval_s > 0` the bridge paces frames so a
    test can sample mid-stream state rather than only post-close.

    `session` stamps `&session=<id>` on the /tap URL — the bridge's
    detached-session routing (the SpatialChat Bridge does this on every
    open while a meeting is active). Absent → the recorder's global
    current session, exactly as an un-bracketing bridge behaves.
    """
    pcm = read_wav_as_pcm_bytes(wav_path)
    frames = frame_pcm(pcm)

    params = {"identity": identity, "name": name, "utterance_id": utterance_id or ""}
    # Stamp `&session=` only when truthy — same rule the production bridges use
    # (local_test_bridge `if session:`, content.js `if (sessionId)`, the C#
    # client's non-empty check): an empty/absent value means "use the global
    # session, send no param", never an empty `session=` the recorder rejects.
    if session:
        params["session"] = session
    qs = urlencode(params)
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


def utterance_released(recorder: Recorder, utterance_id: str) -> bool:
    """True once this utterance's index record is closed (released) — the
    actual precondition for a same-`utterance_id` reconnect to resume-append
    onto the existing WAV instead of (by design) minting a second one.

    `streams_drained` is a WEAKER proxy. The ActiveStream teardown and the
    UtteranceIndex.release() are two separate steps of the /tap close, and
    under the Windows ProactorEventLoop the stream can be observed gone before
    the release lands — so a reconnect gated only on `streams_drained` can race
    in while the record is still `open` and (correctly, per TapFanOut's
    per-owner keying for overlapping taps) record its OWN second WAV. Because
    `release()` runs only after `wave.close()`, this predicate also guarantees
    the prior WAV is flushed and readable. Gate a reconnect on this, not on
    drained."""
    rec = recorder.utterances.snapshot().get(utterance_id)
    return rec is not None and not rec.open
