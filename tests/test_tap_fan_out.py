"""Unit tests for tapscribe.tap_fan_out — the per-`/tap`-WS fan-out object.

The `/tap` WebSocket handler in tapscribe.app drives one TapFanOut per
utterance. The fan-out owns the four concerns that the route used to
open-code in its finally block:

  - WAV-file open / resume / writeframes / finalize / unlink-if-empty
  - UtteranceIndex bookkeeping (try_resume, register_new, release)
  - ActiveStream registration + per-frame bytes_received updates
  - WlKRelay create / connect / send / close-with-drain (when do_live)

These tests construct a TapFanOut directly against a real Recorder and
synthesised PCM frames — no TestClient, no WebSocket. The end-to-end
relay path stays exercised by test_tap_endpoint.py.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder
from tapscribe.tap_fan_out import TapFanOut
from tests.conftest import FakeWlkThread

# A 20 ms frame of audible-ish PCM at 16 kHz mono int16 — 320 samples / 640 bytes.
# Real bridges send frames this size (see CONTEXT.md "Bridge" wire contract).
PCM_FRAME = b"\x10\x00" * 320


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    """A Recorder with the live channel marked stopped (live._proc=None),
    so the fan-out's relay path is never attempted. Relay-on tests build
    their own Recorder against the fake WlK fixture in test_tap_endpoint."""
    recordings = tmp_path / "recordings"
    config_dir = tmp_path / "config"
    recordings.mkdir()
    config_dir.mkdir()
    return Recorder(
        recordings_dir=recordings,
        config_dir=config_dir,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=9999),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


@pytest.fixture
def recorder_with_relay(tmp_path: Path, fake_wlk: FakeWlkThread) -> Recorder:
    """A Recorder pointed at the fake WlK with live._proc forced to
    "running", so write_frame builds and uses a real WlKRelay against
    a real WebSocket server. Same shape as test_tap_endpoint's relay
    fixture, minus the FastAPI app/config monkeypatching since these
    tests don't go through the HTTP layer."""
    recordings = tmp_path / "recordings"
    config_dir = tmp_path / "config"
    recordings.mkdir()
    config_dir.mkdir()
    r = Recorder(
        recordings_dir=recordings,
        config_dir=config_dir,
        live_config=LiveConfig(
            model="tiny.en",
            language="en",
            host="localhost",
            port=fake_wlk.port,
        ),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )

    class _FakeProc:
        def poll(self):
            return None  # "alive"

    r.live._proc = _FakeProc()
    return r


async def test_open_write_close_writes_wav_and_releases_utterance(recorder: Recorder):
    """Tracer: open with do_record=True, write one PCM frame, exit the
    context. A WAV with the expected frame count lands in session_dir,
    the ActiveStream row is gone, and the UtteranceRecord is released
    with kept=True (still in the index, open=False, bytes_received set)."""
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-tracer",
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)

    wavs = list(recorder.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one WAV, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 320  # one 320-sample frame

    # ActiveStream removed once the fan-out exits.
    assert await recorder.streams.snapshot() == []

    # UtteranceRecord released kept=True.
    rec = recorder.utterances.snapshot()["utt-tracer"]
    assert rec.open is False
    assert rec.bytes_received == len(PCM_FRAME)


async def test_empty_utterance_deletes_wav_and_drops_from_index(recorder: Recorder):
    """Open + close with no frames written: the empty WAV must NOT linger
    on disk, and the UtteranceRecord must be removed from the index so the
    operator doesn't see a phantom utterance_id eligible for resume."""
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-empty",
        do_record=True,
        do_live=False,
    ):
        pass  # no write_frame — bridge opened then closed with no audio

    assert list(recorder.session_dir.glob("*.wav")) == []
    assert "utt-empty" not in recorder.utterances.snapshot()
    assert await recorder.streams.snapshot() == []


async def test_resume_appends_to_existing_wav(recorder: Recorder):
    """A second TapFanOut with the same utterance_id+identity (within the
    resume window) appends to the same WAV instead of opening a new one.
    The bridge reconnects across a network blip with the stable id; the
    result must be one WAV containing both halves of the utterance."""
    utt = "utt-resume"

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)
        await fan_out.write_frame(PCM_FRAME)

    # Bridge reconnects with the same utterance_id.
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)

    wavs = list(recorder.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one WAV after resume, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getnframes() == 320 * 3  # three appended 20 ms frames

    rec = recorder.utterances.snapshot()[utt]
    assert rec.bytes_received == len(PCM_FRAME) * 3


async def test_do_record_false_skips_wav_but_registers_active_stream(recorder: Recorder):
    """When the operator has toggled `record=False` for this identity, the
    fan-out must NOT write a WAV or register a UtteranceRecord — but the
    ActiveStream row stays visible during the WS so the operator sees the
    tap is open and can flip recording back on for the NEXT utterance."""
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-no-record",
        do_record=False,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)

        active = await recorder.streams.snapshot()
        assert len(active) == 1
        assert active[0].identity == "alice"
        assert active[0].record is False

    assert list(recorder.session_dir.glob("*.wav")) == []
    assert "utt-no-record" not in recorder.utterances.snapshot()
    assert await recorder.streams.snapshot() == []


async def test_write_frame_advances_active_stream_bytes_received(recorder: Recorder):
    """The dashboard's /api/state poll reads ActiveStream.bytes_received
    to draw the in-flight progress bar — that counter must advance per
    frame so the operator sees the WS is alive and ingesting audio."""
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-bytes",
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)
        snap1 = await recorder.streams.snapshot()
        await fan_out.write_frame(PCM_FRAME)
        await fan_out.write_frame(PCM_FRAME)
        snap2 = await recorder.streams.snapshot()

    assert snap1[0].bytes_received == len(PCM_FRAME)
    assert snap2[0].bytes_received == len(PCM_FRAME) * 3


async def test_relay_forwards_frames_to_wlk_when_live_running(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
):
    """When do_live=True and the LiveChannel reports running, write_frame
    fans the PCM bytes out to the WlKRelay alongside writing the WAV.
    The fake WlK on the other end of the relay receives the bytes."""
    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-relay",
        do_record=True,
        do_live=True,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)
        await fan_out.write_frame(PCM_FRAME)
        # The relay sends asynchronously over a real WS; give the fake
        # server's loop a chance to receive before we close the relay.
        await asyncio.sleep(0.15)

    # The fake WlK records every bytes frame it received from the relay.
    received = b"".join(fake_wlk.received)
    assert PCM_FRAME * 2 in received


async def test_relay_settled_lines_land_in_live_transcripts(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
):
    """Settled lines pushed by WlK during the WS lifetime are consumed by
    the fan-out's relay and appended to recorder.transcripts attributed
    to this fan-out's identity / name. The WlK wire format holds the
    in-flight tail until a newer line arrives or the relay closes —
    pushing two lines surfaces the first immediately and the second on
    relay close-drain."""
    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-relay-settled",
        do_record=True,
        do_live=True,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)
        await asyncio.sleep(0.05)
        fake_wlk.push_committed("hello from WlK")
        fake_wlk.push_committed("second from WlK")
        await asyncio.sleep(0.2)

    # Give the relay's drain a tick to flush the tail.
    await asyncio.sleep(0.1)
    snap = recorder_with_relay.transcripts.snapshot()
    texts = [e["text"] for e in snap]
    assert "hello from WlK" in texts
    assert "second from WlK" in texts
    for entry in snap:
        assert entry["identity"] == "alice"
        assert entry["name"] == "Alice"


async def test_relay_skipped_when_live_channel_not_running(recorder: Recorder):
    """do_live=True is necessary but not sufficient: if the LiveChannel
    isn't actually running (live._proc is None — recorder fixture default),
    no relay is attempted and write_frame still writes the WAV without
    blowing up on a missing relay handle."""
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-no-relay",
        do_record=True,
        do_live=True,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)

    wavs = list(recorder.session_dir.glob("*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getnframes() == 320
