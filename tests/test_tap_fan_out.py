"""Unit tests for tapscribe.tap_fan_out — the per-`/tap`-WS fan-out object.

The `/tap` WebSocket handler in tapscribe.app drives one TapFanOut per
utterance. The fan-out owns the four concerns that the route used to
open-code in its finally block:

  - WAV-file open / resume / writeframes / finalize / unlink-if-empty
  - UtteranceIndex bookkeeping (try_resume, register_new, release)
  - ActiveStream registration + per-frame bytes_received / level updates
  - the live leg, held as one TapRelay (relay + gate + reconnect) — its
    own reconnect/backoff unit tests live in test_tap_relay.py

These tests construct a TapFanOut directly against a real Recorder and
synthesised PCM frames — no TestClient, no WebSocket. The end-to-end
relay path stays exercised by test_tap_endpoint.py.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # pytest puts tests/ on sys.path so `from conftest import` resolves the project's tests/conftest.py
    FakeWlkThread,
    build_tap_recorder,
    wait_for,
)

from tapscribe.recorder import Recorder
from tapscribe.tap_fan_out import TapFanOut

# A 20 ms frame of audible-ish PCM at 16 kHz mono int16 — 320 samples / 640 bytes.
# Real bridges send frames this size (see CONTEXT.md "Bridge" wire contract).
PCM_FRAME = b"\x10\x00" * 320


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    """A Recorder with the live channel marked stopped (live._proc=None),
    so the fan-out's relay path is never attempted. Relay-on tests build
    their own Recorder against the fake WlK fixture in test_tap_endpoint."""
    return build_tap_recorder(tmp_path)


@pytest.fixture
def recorder_with_relay(tmp_path: Path, fake_wlk: FakeWlkThread) -> Recorder:
    """A Recorder pointed at the fake WlK with live._proc forced to
    "running", so write_frame builds and uses a real WlKRelay against
    a real WebSocket server. Same shape as test_tap_endpoint's relay
    fixture, minus the FastAPI app/config monkeypatching since these
    tests don't go through the HTTP layer."""
    return build_tap_recorder(tmp_path, port=fake_wlk.port, gate_kind="backend", live_running=True)


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


async def test_overlapping_same_utterance_id_taps_do_not_clobber_each_other(
    recorder: Recorder,
):
    """Two /tap WSes can briefly carry the SAME utterance_id (a reconnect
    firing before the server has seen the old WS close). They must each get
    their own ActiveStream row, and closing the first must not remove the
    second's row or freeze its byte counter — the pre-fix behaviour, where
    both shared one conn_id/index record, deleted the live successor's row
    and stomped its state when the zombie closed."""
    a = await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-dup",
        do_record=True,
        do_live=False,
    )
    b = await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id="utt-dup",
        do_record=True,
        do_live=False,
    )
    await a.write_frame(PCM_FRAME)
    await b.write_frame(PCM_FRAME)

    # Two live taps → two independent rows, even though they share the id.
    assert len(await recorder.streams.snapshot()) == 2

    # The zombie (a) closes while b is still streaming.
    await a.__aexit__(None, None, None)

    snap = await recorder.streams.snapshot()
    assert len(snap) == 1, "closing the zombie must leave the live tap's row intact"
    surviving = snap[0]
    assert surviving.identity == "alice"
    before = surviving.bytes_received

    # b's counter must still advance — it wasn't frozen by a's close.
    await b.write_frame(PCM_FRAME)
    after = (await recorder.streams.snapshot())[0].bytes_received
    assert after > before, "the surviving tap's byte counter must keep advancing"

    await b.__aexit__(None, None, None)


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
        # Event-based: wait for the relay to round-trip both frames into
        # the fake server's received buffer before we tear down the relay.
        await wait_for(lambda: sum(len(c) for c in fake_wlk.received) >= len(PCM_FRAME) * 2)

    # The fake WlK records every bytes frame it received from the relay.
    received = b"".join(fake_wlk.received)
    assert PCM_FRAME * 2 in received


async def test_relay_settled_lines_land_in_live_transcripts(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
    monkeypatch: pytest.MonkeyPatch,
):
    """Settled lines pushed by WlK during the WS lifetime are consumed by
    the fan-out's relay and appended to recorder.transcripts attributed
    to this fan-out's identity / name. The WlK wire format holds the
    in-flight tail until a newer line arrives or the relay closes —
    pushing two lines surfaces the first immediately and the second on
    relay close-drain."""
    # Wrap recorder.transcripts.append so we get an event each time the
    # fan-out's _on_settled_line callback lands a line.
    append_event = asyncio.Event()
    original_append = recorder_with_relay.transcripts.append

    def _signalling_append(entry):
        original_append(entry)
        append_event.set()

    monkeypatch.setattr(recorder_with_relay.transcripts, "append", _signalling_append)

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-relay-settled",
        do_record=True,
        do_live=True,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)
        # Wait for the frame to round-trip into the fake WlK before
        # pushing settled lines, so the fake's connection list is live.
        await wait_for(lambda: sum(len(c) for c in fake_wlk.received) >= len(PCM_FRAME))
        fake_wlk.push_committed("hello from WlK")
        fake_wlk.push_committed("second from WlK")
        # Event-based: wait until at least the first finalized line has
        # been appended to LiveTranscripts. The tail flushes on close below.
        await asyncio.wait_for(append_event.wait(), timeout=2.0)

    # Event-based: the drain on relay close flushes the tail, so the
    # second line is appended after __aexit__ runs. Wait until both
    # lines are visible in the snapshot.
    await wait_for(lambda: len(recorder_with_relay.transcripts.snapshot()) >= 2)
    snap = recorder_with_relay.transcripts.snapshot()
    texts = [e["text"] for e in snap]
    assert "hello from WlK" in texts
    assert "second from WlK" in texts
    for entry in snap:
        assert entry["identity"] == "alice"
        assert entry["name"] == "Alice"


async def test_tapscribe_gate_blocks_silence_from_reaching_relay(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
    monkeypatch: pytest.MonkeyPatch,
):
    """With gate_kind="tapscribe" and a VAD that never says start, the
    gate stays closed and no PCM should leak through to WlK — even
    though we're feeding frames to write_frame at full rate. WAV write
    is unaffected (silence still hits disk)."""
    from dataclasses import replace as dc_replace

    from tapscribe.speech_gate import SpeechGate

    # Force gate_kind="tapscribe" on the recorder's LiveConfig.
    recorder_with_relay.live.config = dc_replace(
        recorder_with_relay.live.config, gate_kind="tapscribe", gate_pre_roll_ms=0
    )

    def _never_starts(*args, **kwargs):
        def analyze(chunk):
            return None

        return SpeechGate(vad=analyze, pre_roll_ms=0)

    monkeypatch.setattr("tapscribe.tap_relay.build_gate_for_config", _never_starts)

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-gate-silence",
        do_record=True,
        do_live=True,
    ) as fan_out:
        for _ in range(20):
            await fan_out.write_frame(PCM_FRAME)
        # Give the relay a beat to push anything queued (there shouldn't
        # be any). Polling rather than sleeping so the test fails fast
        # if bytes DO leak through.
        await asyncio.sleep(0.05)

    # Nothing reached WlK — the gate ate every frame.
    assert sum(len(c) for c in fake_wlk.received) == 0
    # WAV still got all the frames written to disk.
    wavs = list(recorder_with_relay.session_dir.glob("*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getnframes() == 320 * 20


async def test_tapscribe_gate_passes_speech_frames_through(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
    monkeypatch: pytest.MonkeyPatch,
):
    """With gate_kind="tapscribe" and a VAD that opens immediately,
    PCM frames reach WlK normally — gating is a filter, not a wall."""
    from dataclasses import replace as dc_replace

    from tapscribe.speech_gate import SpeechGate

    recorder_with_relay.live.config = dc_replace(
        recorder_with_relay.live.config, gate_kind="tapscribe", gate_pre_roll_ms=0
    )

    def _opens_immediately(*args, **kwargs):
        # Return start on the very first VAD call; None forever after.
        sent_start = [False]

        def analyze(chunk):
            if not sent_start[0]:
                sent_start[0] = True
                return {"start": 0}
            return None

        return SpeechGate(vad=analyze, pre_roll_ms=0)

    monkeypatch.setattr("tapscribe.tap_relay.build_gate_for_config", _opens_immediately)

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-gate-speech",
        do_record=True,
        do_live=True,
    ) as fan_out:
        # First frame buffers (640 < 1024) — no VAD run. Second frame
        # triggers VAD start; from then on every frame flows through.
        for _ in range(5):
            await fan_out.write_frame(PCM_FRAME)
        # Most of the frames should have round-tripped to WlK.
        await wait_for(lambda: sum(len(c) for c in fake_wlk.received) >= len(PCM_FRAME) * 3)


async def test_backend_gate_kind_passes_all_frames_without_a_gate(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
):
    """With gate_kind="backend", TapFanOut does NOT instantiate a
    SpeechGate — bytes pass straight through to the relay (today's
    behaviour preserved as the operator-facing escape hatch)."""
    from dataclasses import replace as dc_replace

    recorder_with_relay.live.config = dc_replace(recorder_with_relay.live.config, gate_kind="backend")

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-backend-gate",
        do_record=True,
        do_live=True,
    ) as fan_out:
        for _ in range(3):
            await fan_out.write_frame(PCM_FRAME)
        await wait_for(lambda: sum(len(c) for c in fake_wlk.received) >= len(PCM_FRAME) * 3)


async def test_gate_construction_failure_falls_back_to_passthrough(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
    monkeypatch: pytest.MonkeyPatch,
):
    """If build_gate_for_config raises (Silero missing, bad config),
    the /tap must keep working with passthrough — losing the gate is
    a degraded experience, but a dropped tap is unacceptable."""
    from dataclasses import replace as dc_replace

    recorder_with_relay.live.config = dc_replace(recorder_with_relay.live.config, gate_kind="tapscribe")

    def _exploding_factory(*args, **kwargs):
        raise RuntimeError("silero unavailable")

    monkeypatch.setattr("tapscribe.tap_relay.build_gate_for_config", _exploding_factory)

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-gate-fail",
        do_record=True,
        do_live=True,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)
        # Passthrough — frame reaches WlK despite the gate exploding.
        await wait_for(lambda: sum(len(c) for c in fake_wlk.received) >= len(PCM_FRAME))


async def test_gate_open_state_propagates_to_active_stream(
    recorder_with_relay: Recorder,
    monkeypatch: pytest.MonkeyPatch,
):
    """When the SpeechGate transitions open or closed, the per-tap
    ActiveStream.gate_open must follow. The dashboard's status-line
    rendering relies on this — operators see ⟳ vs ⏸ from this flag."""
    from dataclasses import replace as dc_replace

    from tapscribe.speech_gate import SpeechGate

    recorder_with_relay.live.config = dc_replace(
        recorder_with_relay.live.config, gate_kind="tapscribe", gate_pre_roll_ms=0
    )

    # VAD queue: open on call #1, then close on call #2.
    def _open_then_close(*args, **kwargs):
        events = [{"start": 0}, None, {"end": 0}]

        def analyze(chunk):
            return events.pop(0) if events else None

        return SpeechGate(vad=analyze, pre_roll_ms=0)

    monkeypatch.setattr("tapscribe.tap_relay.build_gate_for_config", _open_then_close)

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-gate-state",
        do_record=True,
        do_live=True,
    ) as fan_out:
        # Initially closed.
        snap = await recorder_with_relay.streams.snapshot()
        assert snap[0].gate_open is False

        # Frame 1 (640 buffered, no VAD run yet).
        await fan_out.write_frame(PCM_FRAME)
        # Frame 2 (1280 buffered → VAD runs → start). Gate flips open.
        await fan_out.write_frame(PCM_FRAME)
        snap = await recorder_with_relay.streams.snapshot()
        assert snap[0].gate_open is True

        # Frames 3, 4 keep it open (VAD call #2 returns None).
        await fan_out.write_frame(PCM_FRAME)
        await fan_out.write_frame(PCM_FRAME)
        # Frame 5 fires VAD call #3 → end. Gate flips closed.
        await fan_out.write_frame(PCM_FRAME)
        snap = await recorder_with_relay.streams.snapshot()
        assert snap[0].gate_open is False


async def test_level_meter_reads_zero_while_gate_is_closed_even_on_loud_input(
    recorder_with_relay: Recorder,
    monkeypatch: pytest.MonkeyPatch,
):
    """The level meter reflects POST-VAD audio: when the gate is closed
    (silence as far as the transcriber is concerned), the bar must
    stay dark even if loud audio is hitting the WS. Operators rely on
    this to see at a glance whether the gate is letting their voice
    through."""
    from dataclasses import replace as dc_replace

    from tapscribe.speech_gate import SpeechGate

    recorder_with_relay.live.config = dc_replace(
        recorder_with_relay.live.config, gate_kind="tapscribe", gate_pre_roll_ms=0
    )

    # VAD that never reports speech.
    def _never_starts(*args, **kwargs):
        return SpeechGate(vad=lambda _c: None, pre_roll_ms=0)

    monkeypatch.setattr("tapscribe.tap_relay.build_gate_for_config", _never_starts)

    loud = b"\xff\x7f" * 320  # full-scale int16

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-level-gate-closed",
        do_record=True,
        do_live=True,
    ) as fan_out:
        # Feed many loud frames — without the post-VAD meter change,
        # the bar would peg at 1.0 from the raw input.
        for _ in range(40):
            await fan_out.write_frame(loud)
        snap = await recorder_with_relay.streams.snapshot()

    # Gate stayed closed → meter never lit up. Allow a tiny epsilon
    # because of floating-point compounding (it's effectively zero).
    assert snap[0].level < 0.01, (
        f"level meter lit up to {snap[0].level:.4f} while gate was closed — should be dark (post-VAD reading)"
    )


async def test_relay_buffer_transcription_updates_active_stream(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
):
    """The relay's on_buffer callback must land on
    ActiveStream.buffer_transcription so the dashboard's per-tap
    in-flight indicator updates. Use gate_kind="backend" to keep this
    test focused on the relay→stream wiring without involving the
    gate's own state machine."""
    from dataclasses import replace as dc_replace

    recorder_with_relay.live.config = dc_replace(recorder_with_relay.live.config, gate_kind="backend")

    async with await TapFanOut.open(
        recorder_with_relay,
        identity="alice",
        name="Alice",
        utterance_id="utt-buf-wire",
        do_record=True,
        do_live=True,
    ) as fan_out:
        # Send a frame so the relay is alive and the fake server is
        # connected; then push a snapshot with a buffer_transcription.
        await fan_out.write_frame(PCM_FRAME)
        await wait_for(lambda: sum(len(c) for c in fake_wlk.received) >= len(PCM_FRAME))
        fake_wlk.push_buffer("in flight tail")

        async def _has_buffer() -> bool:
            snap = await recorder_with_relay.streams.snapshot()
            return any(s.buffer_transcription == "in flight tail" for s in snap)

        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if await _has_buffer():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("buffer_transcription never landed on the active stream")


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


# ---------------------------------------------------------------------------
# Live-channel restart: the relay must transparently reconnect
# ---------------------------------------------------------------------------
#
# User scenario: operator changes the WhisperLiveKit model on the dashboard
# and clicks "Apply (restart)". The recorder kills the old WlK child, spawns
# a new one (possibly different model / language), and the new child is
# ready a few seconds later. Currently-open /tap WebSockets must NOT be
# disrupted — the bridge knows nothing about WlK and shouldn't have to
# reconnect. The TapFanOut owns the WlK relay; it's responsible for
# noticing the relay went dead and rebuilding it against whatever WlK
# the recorder is now running.
# ---------------------------------------------------------------------------


async def _drain_wlk_received(wlk: FakeWlkThread, expected_min_bytes: int, timeout_s: float = 1.5) -> bytes:
    """Wait until the fake WlK's `received` buffer reaches at least
    `expected_min_bytes` total bytes, or `timeout_s` seconds elapse.
    Returns the concatenated received bytes."""
    import time as _time

    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        total = sum(len(c) for c in wlk.received)
        if total >= expected_min_bytes:
            break
        await asyncio.sleep(0.02)
    return b"".join(wlk.received)


async def test_relay_auto_reconnects_after_live_channel_restart(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
    tmp_path: Path,
):
    """The headline test for live-channel-restart resilience.

    Steps:
      1. Open a /tap fan-out; the relay connects to fake WlK A.
      2. Stream some frames — they arrive at A.
      3. Simulate "operator clicks Apply (restart)": stop A entirely, then
         bring up a SECOND fake WlK B on a different port, and rewrite
         recorder.live.config to point at B. This is functionally the
         same as a same-port restart but lets us assert on two
         independent fakes without port-reuse flakiness.
      4. Continue streaming frames on the SAME /tap WS (the bridge
         doesn't reconnect).
      5. Within a couple of seconds, frames must start arriving at B,
         and the live transcript callback wired through the rebuilt
         relay must still attribute settled lines to the original
         identity / name.

    Failing means an operator who changes models loses live captions
    for all currently-talking speakers until each of them mutes and
    unmutes — the exact bug the user reported."""
    from dataclasses import replace

    from tapscribe import tap_relay as tr

    # Shrink the per-fan-out reconnect backoff so the test doesn't sit
    # waiting full production-cadence seconds between attempts.
    original_backoff = tr.RELAY_RECONNECT_BACKOFF_S
    tr.RELAY_RECONNECT_BACKOFF_S = 0.05

    wlk_b: FakeWlkThread | None = None
    try:
        async with await TapFanOut.open(
            recorder_with_relay,
            identity="alice",
            name="Alice",
            utterance_id="utt-restart",
            do_record=True,
            do_live=True,
        ) as fan_out:
            # Phase 1: frames flow to WlK A.
            await fan_out.write_frame(PCM_FRAME)
            await fan_out.write_frame(PCM_FRAME)
            received_a = await _drain_wlk_received(fake_wlk, len(PCM_FRAME) * 2)
            assert PCM_FRAME * 2 in received_a, "phase 1: frames must reach the original WlK"

            # Phase 2: operator clicks Apply (restart). Tear down A and
            # bring up B on a fresh port.
            fake_wlk.stop()
            wlk_b = FakeWlkThread()
            wlk_b.start()
            recorder_with_relay.live.config = replace(
                recorder_with_relay.live.config,
                model="new-model",
                language="nb",
                port=wlk_b.port,
            )

            # Phase 3: keep streaming on the same /tap WS. The first frame
            # to detect the dead relay (after the TCP close propagates
            # through websockets) marks it disconnected; the next kicks off
            # a reconnect. Drive frames until the relay rebinds to B —
            # observed through the public `relay.connected` read-surface,
            # no reconnect-task poking.
            async def _pump_until_rebound_to_b() -> None:
                while fan_out.relay.connected is None or fan_out.relay.connected[1] != wlk_b.port:
                    await fan_out.write_frame(PCM_FRAME)
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(_pump_until_rebound_to_b(), timeout=3.0)
            for _ in range(5):
                await fan_out.write_frame(PCM_FRAME)

            received_b = await _drain_wlk_received(wlk_b, len(PCM_FRAME))
            assert len(received_b) > 0, (
                "phase 3: relay never reconnected to the restarted WlK — "
                "live captions would silently stop for this speaker until "
                "the bridge re-opens /tap (i.e. user mutes/unmutes or "
                "refreshes the tab)"
            )

            # Phase 4: a settled line pushed by the NEW WlK must reach
            # the original fan-out's identity via the rebuilt relay's
            # on_settled_line callback.
            wlk_b.push_committed("hello after restart")
            wlk_b.push_committed("second line settles the first")
            # Event-based: wait until the first line reaches LiveTranscripts.
            await wait_for(
                lambda: any(
                    "hello after restart" in e["text"] for e in recorder_with_relay.transcripts.snapshot()
                )
            )
            snap = recorder_with_relay.transcripts.snapshot()
            our_lines = [e for e in snap if e["identity"] == "alice"]
            assert any("hello after restart" in e["text"] for e in our_lines), (
                "captions from the new WlK must be attributed to the original speaker"
            )
    finally:
        tr.RELAY_RECONNECT_BACKOFF_S = original_backoff
        if wlk_b is not None:
            wlk_b.stop()


async def test_relay_reconnect_attempts_respect_backoff(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
):
    """When the relay is dead and LiveChannel reports running, write_frame
    must NOT fire a fresh reconnect attempt on every single 20 ms frame
    — that would mean ~50 connect attempts per second per stream against
    a still-starting WlK. The backoff guard limits attempts to one per
    `RELAY_RECONNECT_BACKOFF_S` window per stream."""
    from tapscribe import tap_relay as tr

    original_backoff = tr.RELAY_RECONNECT_BACKOFF_S
    # Long enough that 10 fast frames clearly fit inside one window.
    tr.RELAY_RECONNECT_BACKOFF_S = 5.0

    try:
        async with await TapFanOut.open(
            recorder_with_relay,
            identity="alice",
            name="Alice",
            utterance_id="utt-backoff",
            do_record=True,
            do_live=True,
        ) as fan_out:
            # Kill the relay by stopping WlK; subsequent send() returns
            # False, so the relay reports disconnected (relay.connected None).
            fake_wlk.stop()
            # Drive write_frames until the relay is marked dead — the WS
            # close has to propagate through the websockets library before
            # send() reports the failure, which is a real-clock event.
            for _ in range(20):
                await fan_out.write_frame(PCM_FRAME)
                if fan_out.relay.connected is None:
                    break
                await asyncio.sleep(0.005)
            assert fan_out.relay.connected is None, "relay should be marked dead after WlK stops"
            # Now the relay is known-dead. Fire a burst of frames; only
            # ONE reconnect task should be scheduled because backoff is
            # 5s and the burst takes ~50 ms.
            for _ in range(10):
                await fan_out.write_frame(PCM_FRAME)
                # real-time timeout test — sleep is intentional (pacing
                # the burst to ensure it fits inside one backoff window).
                await asyncio.sleep(0.005)

            # The relay's public reconnect_attempts read-surface must be
            # exactly 1 for the burst (backoff coalesced it).
            assert fan_out.relay.reconnect_attempts == 1, (
                "backoff window should coalesce burst of frames into one reconnect attempt"
            )
    finally:
        tr.RELAY_RECONNECT_BACKOFF_S = original_backoff


async def test_relay_reconnect_picks_up_new_config(
    recorder_with_relay: Recorder,
    fake_wlk: FakeWlkThread,
    tmp_path: Path,
):
    """When LiveChannel.config is swapped (operator changed model /
    language), a relay reconnect must read the NEW config — otherwise
    the model change wouldn't actually take effect for currently-open
    taps even after reconnect."""
    from dataclasses import replace

    from tapscribe import tap_relay as tr

    original_backoff = tr.RELAY_RECONNECT_BACKOFF_S
    tr.RELAY_RECONNECT_BACKOFF_S = 0.05

    wlk_b: FakeWlkThread | None = None
    try:
        async with await TapFanOut.open(
            recorder_with_relay,
            identity="alice",
            name="Alice",
            utterance_id="utt-newcfg",
            do_record=True,
            do_live=True,
        ) as fan_out:
            await fan_out.write_frame(PCM_FRAME)
            # The relay is established by TapFanOut.open() (synchronously,
            # as part of _open) — assert on the public read-surface.
            assert fan_out.relay.connected is not None
            assert fan_out.relay.connected[2] == recorder_with_relay.live.config.language

            # Swap config to a new language + port (new WlK instance).
            fake_wlk.stop()
            wlk_b = FakeWlkThread()
            wlk_b.start()
            recorder_with_relay.live.config = replace(
                recorder_with_relay.live.config,
                language="nb",
                port=wlk_b.port,
            )

            # Drive frames on the SAME /tap WS until the relay detects the
            # death and rebinds to the NEW config — observed through
            # `relay.connected`, not by poking the relay's private fields
            # or forcing internal state. Same death-then-reconnect path
            # the auto-reconnect test exercises.
            async def _pump_until_rebound_to_b() -> None:
                while fan_out.relay.connected is None or fan_out.relay.connected[1] != wlk_b.port:
                    await fan_out.write_frame(PCM_FRAME)
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(_pump_until_rebound_to_b(), timeout=3.0)

            # The rebuilt relay must reflect the NEW config — language gets
            # baked into the WlK WS URL, so reading the wrong field at
            # reconnect time would request the wrong-language model on the
            # recorder restart.
            assert fan_out.relay.connected == (
                recorder_with_relay.live.config.host,
                wlk_b.port,
                "nb",
            ), "rebuilt relay must bind to the current (post-restart) host/port/language"
    finally:
        tr.RELAY_RECONNECT_BACKOFF_S = original_backoff
        if wlk_b is not None:
            wlk_b.stop()
