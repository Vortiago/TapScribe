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


async def test_write_frame_updates_active_stream_level(recorder: Recorder):
    """The per-tap volume meter is driven by ActiveStream.level. Half-scale
    PCM (16384 / 32768) must land in the snapshot at ~0.5 so the dashboard
    can colour the bar correctly. The exact value matters less than that
    it tracks signal amplitude monotonically — silent → near-zero, loud →
    near-one."""
    half_scale = b"\x00\x40" * 320  # 16384 little-endian int16 → 0.5 peak
    full_scale = b"\xff\x7f" * 320  # 32767 → ~1.0
    silence = b"\x00\x00" * 320

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-level",
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(silence)
        snap_silent = await recorder.streams.snapshot()
        await fan_out.write_frame(half_scale)
        snap_half = await recorder.streams.snapshot()
        await fan_out.write_frame(full_scale)
        snap_full = await recorder.streams.snapshot()

    # A frame of pure silence yields zero peak — the meter stays dark
    # when the WS is open but nobody is speaking.
    assert snap_silent[0].level == pytest.approx(0.0)
    # Half-scale lands at 0.5; the peak-hold is max(peak, decayed prior),
    # and prior was 0, so we get exactly the new peak.
    assert snap_half[0].level == pytest.approx(0.5, abs=1e-4)
    # Full-scale int16 normalised by 32768 is 0.99997…; rounding to 1.0
    # for the renderer is a frontend choice, not a server-side one.
    assert snap_full[0].level == pytest.approx(1.0, abs=1e-3)


async def test_write_frame_level_decays_between_loud_frames(recorder: Recorder):
    """Peak-hold with decay: after a loud frame the meter should stay
    near-full briefly, then fall toward zero as silent frames arrive.
    Pins the "no flicker between syllables, but does fall back to zero
    when the speaker stops" behaviour the operator-facing meter relies
    on."""
    loud = b"\xff\x7f" * 320
    silence = b"\x00\x00" * 320

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-decay",
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(loud)
        first = (await recorder.streams.snapshot())[0].level
        # 30 frames of silence ≈ 600 ms — well past the ~165 ms half-life,
        # so the meter must have decayed close to zero by now.
        for _ in range(30):
            await fan_out.write_frame(silence)
        decayed = (await recorder.streams.snapshot())[0].level

    assert first > 0.9, "loud frame should peg the meter near full scale"
    assert decayed < 0.1, "long silence should drain the peak-hold"


FIXTURES_AUDIO = Path(__file__).parent / "fixtures" / "audio"


def _wav_to_frames(path: Path, frame_bytes: int = 640) -> list[bytes]:
    """Read a recorder-format WAV (16 kHz mono int16) into 20 ms frames,
    the same shape the bridge would send. Used by the real-audio
    integration tests below."""
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        raw = w.readframes(w.getnframes())
    return [raw[i : i + frame_bytes] for i in range(0, len(raw) - frame_bytes + 1, frame_bytes)]


async def test_write_frame_level_tracks_real_speech_wav(recorder: Recorder):
    """End-to-end: stream a real ~12 s speech recording through
    TapFanOut frame by frame and verify the per-tap volume meter
    behaves like a real-world meter would.

    Properties pinned:
      - level stays in [0.0, 1.0] for every frame (renderer contract).
      - level exceeds 0.3 at some point (real speech is loud enough
        to light the meter; if it never did, the dashboard's bar would
        never leave the silent zone for a perfectly normal speaker).
      - peak-hold is monotonic w.r.t. raw peak: at every frame the
        held value is >= the instantaneous peak of that frame (a
        peak-hold meter never dips below the current sample's peak).
    """
    fixture = FIXTURES_AUDIO / "armstrong-en.wav"
    frames = _wav_to_frames(fixture)
    assert len(frames) > 500, "expected ~600 frames from a 12 s WAV"

    levels: list[float] = []
    async with await TapFanOut.open(
        recorder,
        identity="armstrong",
        name="Neil",
        utterance_id="utt-armstrong",
        do_record=True,
        do_live=False,
    ) as fan_out:
        for f in frames:
            await fan_out.write_frame(f)
            snap = await recorder.streams.snapshot()
            levels.append(snap[0].level)

    assert len(levels) == len(frames)
    assert all(0.0 <= L <= 1.0 for L in levels), "level outside [0,1] would break the renderer"
    assert max(levels) > 0.3, "real speech should peg the meter above the silent zone"

    # Peak-hold check: imported here to avoid making it a hard
    # dependency at module load.
    from tapscribe.audio import int16_peak_norm

    for idx, (frame, held) in enumerate(zip(frames, levels, strict=True)):
        raw = int16_peak_norm(frame)
        assert held >= raw - 1e-9, f"peak-hold {held:.4f} below instantaneous peak {raw:.4f} at frame {idx}"


async def test_write_frame_level_decays_through_silence_after_real_audio(recorder: Recorder, tmp_path: Path):
    """Drive the meter with a real speech clip, then feed it 600 ms of
    pure silence. The level must rise above 0.3 during speech (proof
    the meter was actually engaged) and decay below 0.05 after the
    silence (proof the peak-hold drains so a quiet speaker eventually
    reads as quiet). This is exactly the user-facing behaviour: meter
    fills during speech, fades to dark when the speaker stops."""
    fixture = FIXTURES_AUDIO / "armstrong-en.wav"
    frames = _wav_to_frames(fixture)
    silence_frame = b"\x00" * 640
    silent_tail = [silence_frame] * 30  # 30 * 20 ms = 600 ms — well past half-life

    peak_during_speech = 0.0
    final_level_after_silence = 1.0

    async with await TapFanOut.open(
        recorder,
        identity="armstrong",
        name="Neil",
        utterance_id="utt-armstrong-decay",
        do_record=True,
        do_live=False,
    ) as fan_out:
        for f in frames:
            await fan_out.write_frame(f)
            snap = await recorder.streams.snapshot()
            peak_during_speech = max(peak_during_speech, snap[0].level)
        for f in silent_tail:
            await fan_out.write_frame(f)
        final_level_after_silence = (await recorder.streams.snapshot())[0].level

    assert peak_during_speech > 0.3, f"meter never lit up during real speech (peak={peak_during_speech:.3f})"
    assert final_level_after_silence < 0.05, (
        f"meter failed to decay through 600 ms of silence (final={final_level_after_silence:.4f})"
    )


async def test_write_frame_level_against_synthesised_half_scale_tone(recorder: Recorder, tmp_path: Path):
    """Numeric pin: a known half-scale 440 Hz sine, written to a WAV and
    streamed through the recorder, must produce a level near 0.5 on
    every frame (each 20 ms frame contains ~8.8 sine cycles, so the
    peak is well-defined). Catches regressions where the meter would
    average instead of peak-detect, or where normalisation drifts."""
    import numpy as np

    SAMPLE_RATE = 16000
    seconds = 0.4
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    samples = (0.5 * 32767 * np.sin(2 * np.pi * 440.0 * t)).astype(np.int16)
    wav_path = tmp_path / "half_scale_440hz.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())

    frames = _wav_to_frames(wav_path)
    levels: list[float] = []
    async with await TapFanOut.open(
        recorder,
        identity="tone",
        name="Tone",
        utterance_id="utt-tone",
        do_record=True,
        do_live=False,
    ) as fan_out:
        for f in frames:
            await fan_out.write_frame(f)
            snap = await recorder.streams.snapshot()
            levels.append(snap[0].level)

    # After the very first frame the peak-hold should be at ~0.5 and
    # stay there for the rest of the tone (every frame's instantaneous
    # peak is also ~0.5, so the hold never decays below it).
    assert levels[0] == pytest.approx(0.5, abs=0.01)
    assert min(levels) == pytest.approx(0.5, abs=0.01)
    assert max(levels) == pytest.approx(0.5, abs=0.01)


async def test_concurrent_streams_have_independent_level_meters(recorder: Recorder):
    """Two simultaneous TapFanOuts for different speakers must keep
    separate level state. A loud frame for Alice must not push Bob's
    meter, and vice-versa — otherwise the dashboard would show Bob's
    bar moving while only Alice is speaking, which would defeat the
    whole point of the per-speaker meter."""
    loud = b"\xff\x7f" * 320  # ~1.0
    silence = b"\x00\x00" * 320

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-alice",
        do_record=True,
        do_live=False,
    ) as alice:
        async with await TapFanOut.open(
            recorder,
            identity="bob",
            name="Bob",
            utterance_id="utt-bob",
            do_record=True,
            do_live=False,
        ) as bob:
            await alice.write_frame(loud)
            await bob.write_frame(silence)

            snap = {s.identity: s for s in await recorder.streams.snapshot()}
            # Cross-talk would show as Bob's level rising along with Alice's.
            assert snap["alice"].level > 0.9, "Alice's loud frame should peg her meter"
            assert snap["bob"].level == 0.0, (
                "Bob received a silent frame; his meter must be 0 regardless of Alice"
            )

            # Now the reverse: loud for Bob, silent for Alice. Alice's
            # meter should decay (one frame of decay only — still ~92%
            # of the prior 1.0), Bob's should jump.
            await alice.write_frame(silence)
            await bob.write_frame(loud)

            snap = {s.identity: s for s in await recorder.streams.snapshot()}
            assert snap["bob"].level > 0.9
            assert snap["alice"].level < snap["bob"].level


async def test_level_starts_fresh_on_resume_but_bytes_received_persists(
    recorder: Recorder,
):
    """When a /tap WS reconnects with the same utterance_id (resume after
    a network blip), the recorder appends to the existing WAV — so
    bytes_received reflects the WHOLE utterance so far. The volume
    meter is a short-time peak hold, not a cumulative measure; it
    must reset to 0 on resume so the new connection starts measuring
    from the next frame rather than inheriting a stale peak from
    before the blip.

    Reaching this requires: open utterance with a loud frame, close
    (level ends near 1.0 on the OLD stream), reopen with the same
    utterance_id, snapshot before any new frames lands. The meter
    should read 0.0 even though bytes_received is non-zero."""
    loud = b"\xff\x7f" * 320
    utt = "utt-resume-level"

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(loud)
        first_snap = (await recorder.streams.snapshot())[0]
        assert first_snap.level > 0.9

    # Bridge reconnects with the same utterance_id within the resume
    # window. Recorder appends to the same WAV.
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ):
        # Snapshot BEFORE any new frame arrives.
        resumed_snap = (await recorder.streams.snapshot())[0]

    # bytes_received survives the resume (we wrote one frame previously);
    # level does NOT — stale loudness from before the blip must not
    # be displayed against fresh audio.
    assert resumed_snap.bytes_received == len(loud), "resume preserves byte count"
    assert resumed_snap.level == 0.0, "resume must reset the level meter"


async def test_write_frame_updates_level_even_when_record_off(recorder: Recorder):
    """The volume meter helps confirm "is audio flowing for this speaker?"
    — that question is just as relevant when the operator has turned the
    rec toggle off for an identity. The ActiveStream still exists; its
    level must still update so the dashboard's meter keeps moving."""
    half_scale = b"\x00\x40" * 320
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-no-rec-meter",
        do_record=False,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(half_scale)
        snap = await recorder.streams.snapshot()

    assert snap[0].bytes_received == 0  # record off → no bytes counted
    assert snap[0].level == pytest.approx(0.5, abs=1e-4)


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
