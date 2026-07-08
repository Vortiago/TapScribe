"""E2E TLS smoke test for the /tap pipeline.

The unit suite exercises `tapscribe.tls.ensure_self_signed_cert` in
isolation, but nothing in the existing E2E suite stands the recorder up
over HTTPS / WSS. Operators reach the dashboard over wss:// in any
real deployment, so a bridge that streams audio through a wss:// /tap
needs to land on disk identically to the ws:// path.

This test builds a one-off TLS uvicorn server (mirroring
`tests.e2e.harness.RecorderServer` but with ssl args) using a
self-signed cert generated on the fly. It then streams a synthetic WAV
via wss:// and confirms the WAV reaches disk and /api/state is
reachable over https://.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import sys as _sys
import threading
import time
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path as _Path
from urllib.parse import urlencode

import httpx
import pytest
import uvicorn
import websockets

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder
from tapscribe.tls import ensure_self_signed_cert

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # explicit sys.path picks up the project's tests/conftest.py
    FakeAliveProc,
    FakeWlkThread,
)

from .harness import (
    SAMPLE_RATE,
    frame_pcm,
    read_wav_as_pcm_bytes,
    streams_drained,
    synth_speech_like_wav,
    wait_until,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


@dataclass
class TlsRunningRecorder:
    """Bundle for a TLS-flavoured Recorder run. Mirrors
    `tests.e2e.conftest.RunningRecorder`'s shape so test code reads the
    same way."""

    server: uvicorn.Server
    thread: threading.Thread
    recorder: Recorder
    fake_wlk: FakeWlkThread
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def ws_base_url(self) -> str:
        return f"wss://{self.host}:{self.port}"


@pytest.fixture
def tls_running_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_wlk: FakeWlkThread,
) -> Iterator[TlsRunningRecorder]:
    """A Recorder served over HTTPS / WSS with a self-signed cert.

    The cert is generated into the per-test tmpdir via
    `tapscribe.tls.ensure_self_signed_cert` — the production code path —
    so a regression there breaks this test too.
    """
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "config").mkdir()
    (tmp_path / "recordings").mkdir()

    recorder = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=fake_wlk.port),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    recorder.live._proc = FakeAliveProc()

    app.state.recorder = recorder
    app.dependency_overrides[get_recorder] = lambda: recorder

    pair = ensure_self_signed_cert(
        tmp_path / "test-cert.pem",
        tmp_path / "test-key.pem",
        host="localhost",
    )

    host = "localhost"
    port = _free_port()
    cfg = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
        ssl_certfile=str(pair.cert_file),
        ssl_keyfile=str(pair.key_file),
    )
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if getattr(server, "started", False):
            break
        time.sleep(0.02)
    else:
        raise RuntimeError("TLS uvicorn didn't report started within timeout")

    try:
        yield TlsRunningRecorder(
            server=server,
            thread=thread,
            recorder=recorder,
            fake_wlk=fake_wlk,
            host=host,
            port=port,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
        app.dependency_overrides.clear()
        if hasattr(app.state, "recorder"):
            del app.state.recorder


# ---------------------------------------------------------------------------
# Test 5: TLS happy path — wss:// /tap + https:// /api/state
# ---------------------------------------------------------------------------


async def test_tls_happy_path_wav_lands_and_api_state_reachable(
    tls_running_recorder: TlsRunningRecorder,
    tmp_path: Path,
):
    """Stand the Recorder up with --tls equivalent (self-signed cert),
    stream one synthetic WAV via wss://, and check the WAV lands on
    disk and /api/state is reachable via https://. Closes the
    "TLS only in unit tests" gap.

    Cert verification is disabled in the test client — production
    operators with self-signed certs accept the browser prompt once;
    here we model that by using a permissive SSL context.
    """
    rec = tls_running_recorder.recorder
    wav_path = synth_speech_like_wav(tmp_path / "alice-tls.wav", seconds=0.5, freq_hz=220.0)

    # Permissive SSL context — production self-signed deployments rely
    # on the browser's "accept once" flow; tests model that by skipping
    # verification entirely.
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # Stream via wss:// — assert the URL scheme actually carries through
    # the harness's ws_base_url to catch a copy-paste regression where
    # the TLS server is up but the WS client falls back to plain ws.
    assert tls_running_recorder.ws_base_url.startswith("wss://"), (
        "harness must hand back a wss:// URL for the TLS variant"
    )
    qs = urlencode({"identity": "alice", "name": "Alice", "utterance_id": "utt-tls"})
    url = f"{tls_running_recorder.ws_base_url}/tap?{qs}"

    pcm = read_wav_as_pcm_bytes(wav_path)
    frames = frame_pcm(pcm)

    async with websockets.connect(url, ssl=ssl_ctx) as ws:
        for frame in frames:
            await ws.send(frame)
            await asyncio.sleep(0.005)
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    wavs = list(rec.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one WAV on disk via wss://, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE
        assert w.getnframes() == len(frames) * 320

    # /api/state over https:// — verify=False mirrors the wss:// path.
    async with httpx.AsyncClient(
        base_url=tls_running_recorder.base_url,
        timeout=5.0,
        verify=False,
    ) as client:
        resp = await client.get("/api/state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current_session"] == rec.session_start
        # The active stream we just closed is gone; the session it wrote
        # to is in the listing.
        session_names = {s["session"] for s in body["sessions"]}
        assert rec.session_start in session_names
