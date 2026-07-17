"""Tests for tools/strip_silence_cli.py — the operator CLI over
tapscribe.strip_silence.

The CLI routes detection through `plan_strip_regions`, which applies the
whole-file-silence RMS gate (`config.SILENT_RMS_DBFS_FLOOR`) the old
hand-rolled detect-then-filter CLI path never did. That is the DELIBERATE
alignment with the dashboard's strip path (one shared pipeline, #89) —
these tests pin it as intended behavior: an all-silent WAV writes nothing
and prints why, and the verdict comes from the cheap RMS short-circuit
BEFORE any silero import / model load (so the gate costs nothing when it
fires, and these tests run modelless).
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

# tools/ isn't a package — make the CLI importable by name (same pattern
# as test_install_picker.py).
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import strip_silence_cli as cli  # noqa: E402

from tapscribe import strip_silence as ss  # noqa: E402


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(ss.SAMPLE_RATE)
        w.writeframes(samples.astype(np.int16).tobytes())


@pytest.mark.parametrize("mode", ["trim", "split"])
def test_all_silent_wav_writes_nothing_and_says_why(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Whole-file-silence gate: an all-silent WAV produces NO output file
    in either mode and prints the whole-file-silent reason so the operator
    knows exactly why nothing was written. The RMS short-circuit must fire
    BEFORE silero ever runs — pinned by making the detector explode."""

    def _must_not_run(*args, **kwargs):
        raise AssertionError("detect_speech_silero must not run for a whole-file-silent WAV")

    monkeypatch.setattr(ss, "detect_speech_silero", _must_not_run)

    wav = tmp_path / "quiet.wav"
    _write_wav(wav, np.zeros(ss.SAMPLE_RATE, dtype=np.int16))  # 1 s of digital silence

    cli.process_one(wav, mode, 500, 200)

    out = capsys.readouterr().out
    assert "whole-file silent" in out
    assert "no output written" in out
    assert not (tmp_path / "quiet.stripped.wav").exists()
    assert not (tmp_path / "quiet_split").exists()


def test_speech_wav_still_writes_trimmed_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Control for the gate: a WAV with speech-level audio clears the
    whole-file RMS floor and the CLI writes the trimmed clip (detection
    via the conftest RMS-stub detector — no model needed)."""
    n = ss.SAMPLE_RATE
    loud = np.tile(np.array([12000, -12000], dtype=np.int16), n // 2)
    samples = np.concatenate([loud, np.zeros(n, dtype=np.int16), loud])
    wav = tmp_path / "talk.wav"
    _write_wav(wav, samples)

    cli.process_one(wav, "trim", 400, 50)

    out = capsys.readouterr().out
    assert "speech of" in out
    stripped = tmp_path / "talk.stripped.wav"
    assert stripped.exists()
    with wave.open(str(stripped), "rb") as w:
        assert 0 < w.getnframes() < len(samples)
