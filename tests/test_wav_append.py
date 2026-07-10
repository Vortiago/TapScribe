"""Direct, hermetic unit tests for `open_recorder_wav_append` (issue #239).

The resume tests in `test_tap_fan_out_resume_append.py` exercise the append
helper only THROUGH `TapFanOut`. This module pins it at the unit boundary,
where its coupling to the stdlib `wave.Wave_write` internals (it seeds several
private counters) lives — so a `wave.py` change across Python versions fails
HERE, next to its cause, not far downstream in an integration test.

Covered: byte-exact prior||new concatenation with a valid, wave-readable header;
the live handle reports prior+new frames (the seeded frame counter); an empty
append leaves the file byte-identical; a non-canonical header (an extra chunk
shifting `data` off byte 44) is REJECTED loudly instead of corrupting the file;
and a prior truncated on disk after close appends at real EOF (no silent gap).
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from tapscribe.audio import open_recorder_wav
from tapscribe.wav_append import _locate_data_chunk, open_recorder_wav_append

PRIOR_FRAME = b"\x11\x11" * 320  # 640-byte / 320-sample 20 ms frame
RESUME_FRAME = b"\x22\x22" * 320
_FRAME_BYTES = 640


def _seed_recorder_wav(path: Path, *, frames: int) -> None:
    """Write a canonical recorder WAV of `frames` PRIOR_FRAMEs and close it."""
    with open_recorder_wav(path) as wf:
        for _ in range(frames):
            wf.writeframes(PRIOR_FRAME)


def test_append_concatenates_prior_and_new_with_a_valid_header(tmp_path: Path) -> None:
    path = tmp_path / "utt.wav"
    _seed_recorder_wav(path, frames=3)

    wf = open_recorder_wav_append(path)
    wf.writeframes(RESUME_FRAME)
    wf.writeframes(RESUME_FRAME)
    wf.close()

    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 320 * 5, "header data size must cover prior+new"
        assert w.readframes(w.getnframes()) == PRIOR_FRAME * 3 + RESUME_FRAME * 2


def test_live_handle_reports_prior_plus_new_frames(tmp_path: Path) -> None:
    """The returned Wave_write's own getnframes()/tell() must include the prior
    frames (the seeded `_nframeswritten`), not just this session's writes."""
    path = tmp_path / "utt.wav"
    _seed_recorder_wav(path, frames=4)

    wf = open_recorder_wav_append(path)
    try:
        assert wf.getnframes() == 320 * 4
        assert wf.tell() == 320 * 4
        wf.writeframes(RESUME_FRAME)
        assert wf.getnframes() == 320 * 5
    finally:
        wf.close()


def test_empty_append_leaves_the_file_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "utt.wav"
    _seed_recorder_wav(path, frames=4)
    before = path.read_bytes()

    open_recorder_wav_append(path).close()  # append nothing, then close

    assert path.read_bytes() == before, "an empty resume must not alter the WAV"


def _wav_with_leading_extra_chunk(path: Path) -> int:
    """Write a valid WAVE whose `data` chunk is pushed off byte 44 by a LIST
    chunk between `fmt ` and `data`. Returns the resulting data offset."""
    data = b"\x33\x33" * 320
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16)
    extra_payload = b"INFOtest"  # even length → no pad byte
    extra = b"LIST" + struct.pack("<I", len(extra_payload)) + extra_payload
    body = b"WAVE" + fmt + extra + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return 12 + len(fmt) + len(extra) + 8  # RIFF(12) + fmt + LIST + data header(8)


def test_non_canonical_header_is_rejected(tmp_path: Path) -> None:
    """A WAV whose `data` is not at byte 44 must raise, not silently seek into
    the middle of the header and corrupt the RIFF size fields on close."""
    path = tmp_path / "with_list.wav"
    data_offset = _wav_with_leading_extra_chunk(path)

    # The walker must genuinely see the shifted offset (so the reject below
    # can't pass for the wrong reason).
    assert data_offset != 44
    assert _locate_data_chunk(path)[0] == data_offset

    with pytest.raises(ValueError, match="canonical recorder header"):
        open_recorder_wav_append(path)


def test_non_riff_input_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "garbage.wav"
    path.write_bytes(b"not a wav at all, definitely")
    with pytest.raises(ValueError, match="not a RIFF/WAVE file"):
        open_recorder_wav_append(path)


def test_prior_truncated_after_close_appends_at_real_eof(tmp_path: Path) -> None:
    """If the prior file was truncated on disk below its header's claim (e.g. a
    post-close torn write), the append must land at the real end of data and
    patch a matching size field — a shorter but clean file, no injected silence."""
    path = tmp_path / "utt.wav"
    _seed_recorder_wav(path, frames=5)
    # Drop the last whole frame off disk; the header still claims 5 frames.
    with open(path, "r+b") as f:
        f.truncate(path.stat().st_size - _FRAME_BYTES)

    wf = open_recorder_wav_append(path)
    wf.writeframes(RESUME_FRAME)
    wf.close()

    with wave.open(str(path), "rb") as w:
        assert w.getnframes() == 320 * 5, "4 surviving prior frames + 1 appended"
        assert w.readframes(w.getnframes()) == PRIOR_FRAME * 4 + RESUME_FRAME
