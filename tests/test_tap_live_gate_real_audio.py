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
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from change_signal import CHANGES  # type: ignore[import-not-found]
from conftest import (
    FakeWlkThread,  # type: ignore[import-not-found]  # tests/ is on sys.path — resolves tests/conftest.py
    build_tap_recorder,
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live_relay import WlKRelay
from tapscribe.recorder import Recorder
from tapscribe.tap_fan_out import TapFanOut

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


@dataclass
class RelayLedger:
    """What the server has done with the tap's frames, counted where it happens:
    frames the /tap loop has fully handled (written, gated, relayed), and bytes
    the relay has handed to the fake WlK. Each count signals `CHANGES`."""

    frames_handled: int = 0
    bytes_relayed: int = 0


@pytest.fixture
def relay_ledger(monkeypatch: pytest.MonkeyPatch) -> RelayLedger:
    ledger = RelayLedger()
    write_frame = TapFanOut.write_frame
    send = WlKRelay.send

    async def counted_write_frame(self: TapFanOut, buf: bytes) -> None:
        await write_frame(self, buf)
        ledger.frames_handled += 1
        CHANGES.bump()

    async def counted_send(self: WlKRelay, data: bytes) -> bool:
        ok = await send(self, data)
        if ok:
            ledger.bytes_relayed += len(data)
        return ok

    monkeypatch.setattr(TapFanOut, "write_frame", counted_write_frame)
    monkeypatch.setattr(WlKRelay, "send", counted_send)
    return ledger


def _relay_drained(ledger: RelayLedger, fw: FakeWlkThread, frames: int, *, timeout_s: float = 20.0) -> bool:
    """Wait, with the tap still open, until the server has handled all `frames`
    and the fake WlK holds every byte the relay sent for them.

    Inside the `websocket_connect` block on purpose: leaving it makes TestClient
    cancel the app, which drops whatever frames are still queued. Once the loop
    has handled the last frame, the relay has sent every survivor, so the byte
    count it reports is final; the fake WlK signals as each frame lands.

    This replaces a no-growth plateau (three equal 0.2 s polls). It waits on
    `change_signal.CHANGES`, never on a clock; `timeout_s` only bounds a hang."""
    return CHANGES.wait_until(
        lambda: ledger.frames_handled == frames and _received_len(fw) == ledger.bytes_relayed,
        timeout=timeout_s,
    )


def _received_len(fw: FakeWlkThread) -> int:
    return sum(len(chunk) for chunk in fw.received)


def test_tapscribe_gate_forwards_real_speech_and_drops_silence_end_to_end(
    gate_client: TestClient, fake_wlk: FakeWlkThread, relay_ledger: RelayLedger
):
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster_whisper not installed — install with `pip install -e .[whisper-cpu]`")

    frames, sent = _bracketed_frames()
    with gate_client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        for frame in frames:
            ws.send_bytes(frame)
        assert _relay_drained(relay_ledger, fake_wlk, len(frames)), (
            f"handled {relay_ledger.frames_handled}/{len(frames)} frames; "
            f"the fake WlK holds {_received_len(fake_wlk)}/{relay_ledger.bytes_relayed} relayed bytes"
        )
        received = b"".join(fake_wlk.received)

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
