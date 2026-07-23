"""Real-audio end-to-end for the /tap live gate — the path #248 changed.

`#248` moved the per-frame Silero VAD inference (`SpeechGate.feed`) off the
event loop. That gate only runs for `gate_kind="tapscribe"` taps; every
existing `/tap` test pins `gate_kind="backend"` (gate is `None`), so none of
them exercise the real gate at all. This test does, end to end with real
backends:

  real speech (bracketed by real silence)
    → real `/tap` WebSocket
    → real Silero `SpeechGate` running OFF the event loop (#248)
    → real `WlKRelay`
    → real WlK-wire server (the `FakeWlkThread` stand-in)

Two real-backend assertions:

  (a) The real Silero gate DROPPED the bracketing silence and FORWARDED the
      speech — the sink receives far fewer bytes than were streamed, but a
      non-trivial amount. If the gate were stubbed or broken (e.g. #248's
      off-loop move dropped/duplicated frames) this bracket fails.

  (b) A real `faster-whisper` backend, run over the EXACT bytes the gate
      forwarded, transcribes them to real words — proving the gate kept
      intelligible *speech*, not noise or a silence smear.

The whole pipeline is deterministic (Silero inference and faster-whisper at
`beam_size=1` are both deterministic for a fixed input), so there is no fuzzy
threshold and no flake. We deliberately do NOT assert the transcript matches
the reference text: the fixture is a noisy historical Apollo recording that
even a real Whisper mishears ("step off the LM" → "step off the limb"), and
gating removes inter-word silence, so an exact-words match would be brittle.
Byte-level gate behaviour is the precise signal; a real transcript merely
confirms the survivors are speech.

Marked `real_audio` and self-skips where `faster_whisper` isn't importable —
same lane as `tests/e2e/test_pipeline_e2e.py::test_pipeline_with_real_whisper`.
"""

from __future__ import annotations

import importlib.util
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from conftest import (
    FakeWlkThread,  # type: ignore[import-not-found]  # tests/ is on sys.path — resolves tests/conftest.py
    build_tap_recorder,
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.recorder import Recorder

pytestmark = pytest.mark.real_audio

_FIXTURE = Path(__file__).parent / "fixtures" / "audio" / "armstrong-en.wav"
_FRAME_BYTES = 640  # 20 ms @ 16 kHz mono int16 — the bridge's frame size
_SILENCE_S = 2.0  # generous real silence bracketing the speech, to drop


def _bracketed_frames() -> tuple[list[bytes], int]:
    """`[2 s silence] + armstrong-en.wav + [2 s silence]`, sliced into 20 ms
    frames. The silence is what a correct gate must drop; the clip is what it
    must keep."""
    with wave.open(str(_FIXTURE), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        speech = w.readframes(w.getnframes())
    silence = b"\x00\x00" * int(16000 * _SILENCE_S)
    stream = silence + speech + silence
    frames = [stream[i : i + _FRAME_BYTES] for i in range(0, len(stream) - _FRAME_BYTES + 1, _FRAME_BYTES)]
    return frames, len(stream)


@pytest.fixture
def tapscribe_gate_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_wlk: FakeWlkThread
) -> Recorder:
    """Like conftest's `recorder_with_fake_wlk`, but `gate_kind="tapscribe"` so
    the REAL Silero gate runs (the default; the other tap tests pin "backend"
    because they feed synthetic near-silence the real gate would block)."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path / "config")
    return build_tap_recorder(tmp_path, port=fake_wlk.port, gate_kind="tapscribe", live_running=True)


@pytest.fixture
def gate_client(tapscribe_gate_recorder: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: tapscribe_gate_recorder
    app.state.recorder = tapscribe_gate_recorder
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _received_len(fw: FakeWlkThread) -> int:
    return sum(len(chunk) for chunk in fw.received)


def _wait_until_relay_settles(fw: FakeWlkThread, *, timeout_s: float = 20.0) -> bytes:
    """Poll until the relay has forwarded the gated frames and stopped growing
    (stable across three 0.2 s polls), then return the received bytes. The gate
    streams survivors live as it decides them, so this converges once the last
    speech frame has been gated + forwarded.

    Why a no-growth plateau is a safe completion signal here (not a flake dodge):
    the fixture is ONE contiguous utterance, so the only lasting plateau is the
    real end — trailing silence produces no more survivors. And the completion
    signal isn't load-bearing for correctness anyway: the assertions are wide
    RANGES (`0 < got < 0.6*sent`), so even a premature break on some
    hypothetical mid-clip pause would still land in range and still transcribe
    to words — it can't turn a real regression green or a green run red. The
    `timeout_s` is the backstop; a stuck relay fails loudly, never hangs."""
    deadline = time.monotonic() + timeout_s
    stable = 0
    last = -1
    while time.monotonic() < deadline:
        cur = _received_len(fw)
        stable = stable + 1 if (cur == last and cur > 0) else 0
        if stable >= 3:
            break
        last = cur
        time.sleep(0.2)
    return b"".join(fw.received)


def test_tapscribe_gate_forwards_real_speech_and_drops_silence_end_to_end(
    gate_client: TestClient, fake_wlk: FakeWlkThread
):
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster_whisper not installed — install with `pip install -e .[whisper-cpu]`")

    frames, sent = _bracketed_frames()
    with gate_client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        for frame in frames:
            ws.send_bytes(frame)
        received = _wait_until_relay_settles(fake_wlk)

    got = len(received)

    # (a) The real Silero gate (running off the event loop, #248) forwarded the
    # speech and dropped the ~4 s of bracketing silence. `got == sent` would
    # mean the gate never engaged (stubbed / gate_kind mismatch); `got == 0`
    # would mean it blocked the speech too. On this clip the gate keeps
    # ~2.7 s of ~16 s streamed (~17 %); 0.6 is a wide, non-flaky bracket.
    assert got > 0, "gate forwarded nothing — real speech was not detected"
    assert got < sent * 0.6, f"gate forwarded {got}/{sent} bytes — bracketing silence was not dropped"

    # (b) A REAL faster-whisper backend over the EXACT bytes the gate forwarded:
    # they transcribe to real words, so the gate kept intelligible speech (not
    # noise or a silence smear). Deterministic at beam_size=1; we assert only
    # that words came out, not which (the noisy Apollo clip is mis-heard even
    # ungated — see module docstring).
    from faster_whisper import WhisperModel

    audio = np.frombuffer(received, dtype=np.int16).astype(np.float32) / 32768.0
    # `tiny.en` is one of the models the real-audio CI job pre-provisions
    # (ci.yml), so this adds no model download to that disk-tight runner.
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(audio, language="en", beam_size=1)
    transcript = " ".join(seg.text for seg in segments).strip()
    assert any(ch.isalpha() for ch in transcript), (
        f"the gate's forwarded audio did not transcribe to speech: {transcript!r}"
    )
