"""End-to-end tests for the /tap WebSocket — the merged Bridge contract.

We spin up a fake whisperlivekit-server in-process and point the Recorder
at it (via LiveConfig host/port). Then we open a /tap WS via TestClient,
send PCM bytes, and verify both the WAV is written AND the relay
forwarded bytes to the fake WlK AND settled-lines pushed by the fake
landed in recorder.transcripts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
import websockets
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder

# ---------------------------------------------------------------------------
# Fake WlK running in a background asyncio loop in another thread
# ---------------------------------------------------------------------------

class _FakeWlkThread:
    """Minimal whisperlivekit-server stand-in. Runs its own event loop in
    a daemon thread so the synchronous TestClient can drive a WS that
    triggers a relay back to this server."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.connections: list = []
        self._port = _free_port()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._thread.start()
        # Wait for the server to actually be listening
        assert self._ready.wait(timeout=2.0), "fake WlK didn't start"

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)

    def push_committed(self, text: str) -> None:
        """Schedule a settled-line broadcast on the WlK loop's thread."""
        if self._loop is None:
            return
        msg = json.dumps({"committed_lines": [{"text": text}]})

        async def _push():
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    await c.send(msg)

        asyncio.run_coroutine_threadsafe(_push(), self._loop)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handler, "localhost", self._port)
        self._ready.set()
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
        finally:
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    await c.close()
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws) -> None:
        self.connections.append(ws)
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.received.append(msg)
        finally:
            if ws in self.connections:
                self.connections.remove(ws)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_wlk() -> Iterator[_FakeWlkThread]:
    wlk = _FakeWlkThread()
    wlk.start()
    try:
        yield wlk
    finally:
        wlk.stop()


@pytest.fixture
def recorder_with_fake_wlk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_wlk: _FakeWlkThread) -> Recorder:
    """Build a Recorder pointed at the fake WlK. Pretend the live channel
    is 'running' by tweaking LiveChannel.info — we don't actually spawn
    a subprocess, but the relay only needs the host/port to connect."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "config").mkdir()
    (tmp_path / "recordings").mkdir()

    r = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=fake_wlk.port),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    # The relay opens only when LiveChannel.running() is True. Force-mark
    # the channel as running by injecting a fake proc that .poll() returns
    # None (i.e., still alive). The real subprocess.Popen would do the
    # same; we just don't have one in tests.
    class _FakeProc:
        def poll(self):
            return None  # "alive"
    r.live._proc = _FakeProc()
    return r


@pytest.fixture
def client(recorder_with_fake_wlk: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder_with_fake_wlk
    app.state.recorder = recorder_with_fake_wlk
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tap_endpoint_writes_wav_and_records_bytes_in_active_streams(
    client: TestClient, recorder_with_fake_wlk: Recorder, fake_wlk: _FakeWlkThread,
):
    """Tracer bullet: open /tap, send a PCM frame, close. Assert WAV
    written + relay forwarded bytes."""
    pcm_frame = b"\x10\x00" * 320  # 20 ms at 16 kHz mono int16
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(pcm_frame)
        ws.send_bytes(pcm_frame)
        # Give the relay a tick to push to the fake server
        time.sleep(0.15)

    # WAV file landed in the session dir
    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 640  # two 320-sample frames

    # Relay forwarded bytes to the fake WlK
    received = b"".join(fake_wlk.received)
    assert pcm_frame * 2 in received


def test_tap_settled_lines_from_wlk_land_in_live_transcripts(
    client: TestClient, recorder_with_fake_wlk: Recorder, fake_wlk: _FakeWlkThread,
):
    """When WlK pushes a settled line during a /tap WS, it should land in
    recorder.transcripts attributed to the WS's identity/name."""
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(b"\x10\x00" * 320)
        time.sleep(0.05)
        fake_wlk.push_committed("hello from WlK")
        time.sleep(0.15)

    snap = recorder_with_fake_wlk.transcripts.snapshot()
    assert len(snap) == 1
    entry = snap[0]
    assert entry["text"] == "hello from WlK"
    assert entry["identity"] == "alice"
    assert entry["name"] == "Alice"


def test_tap_drains_tail_caption_after_bridge_disconnects(
    client: TestClient, recorder_with_fake_wlk: Recorder, fake_wlk: _FakeWlkThread,
):
    """Per Q2: settled lines emitted by WlK during the post-disconnect
    drain window must still land in transcripts."""
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(b"\x10\x00" * 320)
        time.sleep(0.05)
        # Push the settled line as the WS context exits — the relay's
        # close() drain window must catch it.
        fake_wlk.push_committed("tail caption")
        time.sleep(0.05)

    # Allow the relay's drain timeout to do its thing
    time.sleep(0.3)
    texts = [e["text"] for e in recorder_with_fake_wlk.transcripts.snapshot()]
    assert "tail caption" in texts


def test_tap_with_recording_paused_closes_immediately(
    client: TestClient, recorder_with_fake_wlk: Recorder,
):
    """The recording-toggle pause behaviour pre-existed on /record; it
    survives the rename to /tap. Bridge connects, gets accepted, gets
    closed cleanly with no WAV written."""
    from starlette.websockets import WebSocketDisconnect
    recorder_with_fake_wlk.recording_enabled = False
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        # Server closes immediately after accept; receive raises on close
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert wavs == []


def test_tap_without_wlk_running_still_writes_wav(
    client: TestClient, recorder_with_fake_wlk: Recorder,
):
    """Per Q2: silent graceful degradation. If LiveChannel isn't running,
    the relay isn't attempted; WAV writing proceeds normally."""
    # Mark the live channel as stopped
    recorder_with_fake_wlk.live._proc = None

    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(b"\x10\x00" * 320)

    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 1


def test_old_record_route_no_longer_exists(
    client: TestClient, recorder_with_fake_wlk: Recorder,  # noqa: ARG001
):
    """The /record endpoint was renamed to /tap. The old name should 404
    so old bridges fail loudly rather than silently writing nothing."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/record?identity=alice&name=Alice"):
        pass


def test_old_live_transcript_post_route_is_gone(
    client: TestClient, recorder_with_fake_wlk: Recorder,  # noqa: ARG001
):
    """Per the architectural cleanup, the Bridge no longer POSTs settled
    lines back to the Recorder — those are consumed internally by the
    relay. The old POST route is removed; DELETE stays for the
    dashboard's clear-feed button."""
    r = client.post("/api/live-transcript", json={"text": "should not work"})
    assert r.status_code in (404, 405)
    # DELETE still works
    r2 = client.delete("/api/live-transcript")
    assert r2.status_code == 200
