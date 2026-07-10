"""RED contract for issue #239 — utterance resume must APPEND to the existing
WAV, not read-all-and-rewrite it synchronously on the event loop.

Today `TapFanOut._open`'s resume branch does
`wave.open(path, "rb").readframes(getnframes())` to slurp every prior frame
into memory, then reopens the file with `open_recorder_wav` (mode "wb", which
TRUNCATES) and rewrites them all before appending the resumed segment. That is
O(prior utterance bytes) of disk read + write on the event loop at every /tap
reconnect — a ~19 MB, 100-300 ms loop stall for a 10-minute utterance, and a
reconnect storm serialises every other tap's audio behind the copies.

The fix is a true append for the pinned recorder format (16 kHz / mono / 16-bit):
open the existing WAV, seek to the end of its data, write the new frames, and
patch the RIFF/`data` chunk sizes on close — O(1), no full re-read, no rewrite.

Test 1 pins the HARM directly: a resume reads ZERO prior frames off disk (the
whole point — no O(bytes) slurp). Tests 2 and 3 are the correctness guardrails
that a naive append must not break — a "seek to end and write but forget to
patch the header's data size" shortcut would pass Test 1 yet leave `getnframes()`
reporting the STALE prior count (the appended audio invisible), so Test 2 pins
the exact byte-for-byte concatenation and Test 3 pins that a resume which appends
nothing leaves the prior WAV intact.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from conftest import build_tap_recorder  # type: ignore[import-not-found]

from tapscribe.recorder import Recorder
from tapscribe.tap_fan_out import TapFanOut

# Distinctive 20 ms frames (320 samples / 640 bytes) so a wrong append offset or
# a truncation is caught by the byte-exact concatenation check, not just a count.
PRIOR_FRAME = b"\x11\x11" * 320
RESUME_FRAME = b"\x22\x22" * 320


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    return build_tap_recorder(tmp_path)


async def _seed_prior_utterance(recorder: Recorder, utt: str, *, frames: int) -> None:
    """Open, write `frames` PRIOR_FRAMEs, and close — leaving a resumable
    UtteranceRecord (kept=True, within the resume window)."""
    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        for _ in range(frames):
            await fan_out.write_frame(PRIOR_FRAME)


async def test_resume_does_not_reread_the_prior_wav_frames(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """The resume open must not slurp the prior frames off disk. A sizable
    prior utterance is seeded; the resume reopen must read ZERO frame bytes
    via `wave` (true append), instead of `readframes(getnframes())` over the
    whole file as the read-all-and-rewrite path does today."""
    utt = "utt-239-noreread"
    await _seed_prior_utterance(recorder, utt, frames=200)  # 200 * 640 = 128 KB of data

    # Count every byte read through wave.Wave_read.readframes anywhere.
    read_bytes = {"n": 0}
    _orig = wave.Wave_read.readframes

    def _counting_readframes(self, nframes):  # type: ignore[no-untyped-def]
        data = _orig(self, nframes)
        read_bytes["n"] += len(data)
        return data

    monkeypatch.setattr(wave.Wave_read, "readframes", _counting_readframes)
    read_bytes["n"] = 0  # count ONLY the resume open below

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(RESUME_FRAME)
        frames_read_during_resume = read_bytes["n"]  # snapshot before any verify read

    assert frames_read_during_resume == 0, (
        "resume re-read the prior WAV frames off the event loop "
        f"({frames_read_during_resume} bytes) — append to the existing file "
        "instead of read-all-and-rewrite (O(1), not O(prior bytes))"
    )


async def test_resume_appends_the_exact_concatenation_with_a_valid_header(recorder: Recorder):
    """Correctness guardrail: after a resume the single WAV must contain the
    prior frames followed by the resumed frames, byte-for-byte, with a header
    whose `data` size was patched to the new total — so `getnframes()` and the
    frame bytes both reflect prior+new, not a stale prior-only count."""
    utt = "utt-239-concat"
    await _seed_prior_utterance(recorder, utt, frames=3)  # 3 prior frames

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(RESUME_FRAME)
        await fan_out.write_frame(RESUME_FRAME)  # 2 resumed frames

    wavs = list(recorder.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one WAV after resume, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 320 * 5, "header data size must cover prior+resumed frames"
        assert w.readframes(w.getnframes()) == PRIOR_FRAME * 3 + RESUME_FRAME * 2, (
            "the WAV must be the exact prior||resumed concatenation — a wrong "
            "append offset, a truncated rewrite, or a stale header would diverge"
        )


async def test_resume_with_no_new_frames_leaves_the_prior_wav_intact(recorder: Recorder):
    """Correctness guardrail: a reconnect that appends nothing must leave the
    prior WAV byte-identical (opening for append must not truncate it), and the
    record stays kept (prior bytes > 0)."""
    utt = "utt-239-emptyresume"
    await _seed_prior_utterance(recorder, utt, frames=4)  # 4 prior frames

    async with await TapFanOut.open(
        recorder,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ):
        pass  # resume, then close without writing any new frame

    wavs = list(recorder.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected the prior WAV to survive an empty resume, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getnframes() == 320 * 4, "an empty resume must not shrink or grow the prior WAV"
        assert w.readframes(w.getnframes()) == PRIOR_FRAME * 4, "the prior audio must be untouched"
