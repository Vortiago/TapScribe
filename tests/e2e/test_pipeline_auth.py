"""Full-stack tap-token auth E2E.

Every OTHER e2e fixture runs with `AUTH_ENABLED=False` (auth has its own
route-level tests, and bridges don't have to juggle subprotocols). That
leaves the security boundary every real Bridge crosses — the `/tap` WS
subprotocol token gate — untested against a real uvicorn server: the route
suite (`tests/test_tap_endpoint.py::TestTapAuth`) exercises it through
Starlette's `TestClient`, and the SpatialChat token-rotation reconnect
(`test_bridge_extension_e2e`) runs against a FAKE `/tap` server.

This module stands the real Recorder up with auth ON and drives the gate
with the real `websockets` client the bridge uses:

  - the correct `tapscribe.v1.tap.<token>` subprotocol upgrades and the WAV lands;
  - a missing / wrong token is refused at the handshake and nothing is recorded;
  - after the operator rotates the tap token, the old token is refused and the
    bridge redials with the new one under the same `utterance_id`, resuming the
    single WAV (PRD #99 stories 9/10; #131 story 22).
"""

from __future__ import annotations

import sys as _sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path as _Path

import pytest
import websockets

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # NeMo ships an installed `tests` package — explicit sys.path insertion picks up the project's tests/conftest.py
    repoint_config_files,
)

from .harness import (  # noqa: E402
    RecorderServer,
    free_port,
    stream_wav_via_tap,
    streams_drained,
    synth_speech_like_wav,
    wait_until,
)


@dataclass
class AuthRecorder:
    server: RecorderServer
    recorder: Recorder

    @property
    def ws_base_url(self) -> str:
        return self.server.ws_base_url


@pytest.fixture
def auth_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuthRecorder]:
    """A real uvicorn Recorder with `AUTH_ENABLED=True`, so the `/tap`
    subprotocol token gate actually runs. No live relay (`live._proc = None`)
    — recording degrades gracefully without one, so the upgrade-gate
    assertions aren't racing a relay handshake, the same simplification the
    unit `/tap` auth tests make."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    repoint_config_files(monkeypatch, tmp_path / "config")
    (tmp_path / "config").mkdir()
    (tmp_path / "recordings").mkdir()

    recorder = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=free_port()),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    recorder.live._proc = None  # no relay — WAV + upgrade-gate focus

    app.state.recorder = recorder
    app.dependency_overrides[get_recorder] = lambda: recorder
    server = RecorderServer(app)
    server.start()
    try:
        yield AuthRecorder(server=server, recorder=recorder)
    finally:
        server.stop()
        app.dependency_overrides.clear()
        if hasattr(app.state, "recorder"):
            del app.state.recorder


@pytest.fixture
def speech_wav(tmp_path: Path) -> Path:
    """One short 16 kHz mono int16 WAV the bridge streams over /tap."""
    return synth_speech_like_wav(tmp_path / "speech.wav", seconds=0.5, freq_hz=220.0)


async def test_correct_tap_token_upgrades_and_wav_lands(auth_recorder: AuthRecorder, speech_wav: Path):
    """The documented `tapscribe.v1.tap.<token>` subprotocol upgrades the WS
    and the WAV is written — the bridge's happy path against a real auth-on
    Recorder."""
    rec = auth_recorder.recorder
    await stream_wav_via_tap(
        ws_base_url=auth_recorder.ws_base_url,
        identity="alice",
        name="Alice",
        wav_path=speech_wav,
        utterance_id="utt-auth-ok",
        tap_token=rec.tap.value,
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
    wavs = list(rec.session_dir.glob("*.wav"))
    assert len(wavs) == 1 and "Alice" in wavs[0].name, [p.name for p in wavs]


@pytest.mark.parametrize(
    "bad_token",
    [
        "",  # no subprotocol offered at all
        "definitely-not-the-token",  # well-formed subprotocol, wrong token
    ],
)
async def test_missing_or_wrong_tap_token_is_refused_and_records_nothing(
    auth_recorder: AuthRecorder, speech_wav: Path, bad_token: str
):
    """A missing or wrong token is refused at the WS handshake (the recorder
    closes the upgrade before accept → HTTP 403), and nothing is recorded
    anywhere."""
    rec = auth_recorder.recorder
    with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
        await stream_wav_via_tap(
            ws_base_url=auth_recorder.ws_base_url,
            identity="alice",
            name="Alice",
            wav_path=speech_wav,
            utterance_id="utt-auth-bad",
            tap_token=bad_token,
        )
    assert exc_info.value.response.status_code == 403
    assert list(rec.recordings_dir.rglob("*.wav")) == []


async def test_rotated_tap_token_refuses_old_and_resumes_under_new(
    auth_recorder: AuthRecorder, speech_wav: Path
):
    """Operator rotates the tap token mid-meeting: the old token is refused,
    and the bridge redials with the new one under the SAME utterance_id,
    resuming the single WAV rather than fragmenting it. Mirrors the
    SpatialChat popup's rotate-then-reconnect (`test_bridge_extension_e2e`),
    here against a real Recorder so the rotation, rejection, and resume seams
    all run end to end."""
    rec = auth_recorder.recorder
    ws_base = auth_recorder.ws_base_url
    utt = "utt-rotate-1"
    old_token = rec.tap.value

    # First leg: stream under the original token.
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=speech_wav,
        utterance_id=utt,
        tap_token=old_token,
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    # Operator rotates the tap token (the --rotate-tap-token path).
    rec.tap.rotate()
    new_token = rec.tap.value
    assert new_token != old_token

    # The old token is now refused at the handshake.
    with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
        await stream_wav_via_tap(
            ws_base_url=ws_base,
            identity="alice",
            name="Alice",
            wav_path=speech_wav,
            utterance_id=utt,
            tap_token=old_token,
        )
    assert exc_info.value.response.status_code == 403

    # The bridge redials with the new token, same utterance_id → the WAV
    # resumes rather than a second file appearing.
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=speech_wav,
        utterance_id=utt,
        tap_token=new_token,
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    wavs = list(rec.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one resumed WAV, got {[p.name for p in wavs]}"
