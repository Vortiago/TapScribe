"""Shared recorder-format WAV builders for unit tests.

The Recorder writes 16 kHz mono int16 PCM; every test that needs "a WAV
like the Recorder writes" builds it here instead of re-implementing the
square-wave synthesis per file. Tests that deliberately write
NON-recorder WAVs (test_audio, test_wav_predecode) or shaped
speech/silence sample patterns (test_strip_silence_split) keep their own
parameterised writers — those are different jobs.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


def seed_wav(path: Path, *, amplitude: int = 8000, seconds: float = 1.0) -> Path:
    """Write a recorder-format square-wave WAV. The default amplitude is
    comfortably above SILENT_RMS_DBFS_FLOOR so silence pre-checks pass."""
    n = int(SAMPLE_RATE * seconds)
    samples = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())
    return path


def seed_silent_wav(path: Path) -> Path:
    """All-zeros PCM → RMS is -inf dBFS, deep below SILENT_RMS_DBFS_FLOOR."""
    return seed_wav(path, amplitude=0)


def seed_session(root: Path, name: str, wavs: list[str]) -> Path:
    """Create `<root>/<name>/` containing one audible seed_wav per entry."""
    sd = root / name
    sd.mkdir(parents=True)
    for w in wavs:
        seed_wav(sd / w)
    return sd
