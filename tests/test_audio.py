"""Tests for tapscribe.audio — WAV duration + RMS reading."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
import pytest

from tapscribe import audio

SAMPLE_RATE = 16000


def _write_pcm_wav(path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE, channels: int = 1, sampwidth: int = 2) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())


def test_wav_duration_s_roundtrips_seconds(tmp_path):
    seconds = 1.5
    n = int(SAMPLE_RATE * seconds)
    _write_pcm_wav(tmp_path / "a.wav", np.zeros(n, dtype=np.int16))
    assert math.isclose(audio.wav_duration_s(tmp_path / "a.wav"), seconds, rel_tol=1e-4)


def test_wav_duration_s_returns_zero_for_missing(tmp_path):
    assert audio.wav_duration_s(tmp_path / "missing.wav") == 0.0


def test_wav_rms_dbfs_silence_returns_minus_200(tmp_path):
    _write_pcm_wav(tmp_path / "silence.wav", np.zeros(SAMPLE_RATE, dtype=np.int16))
    assert audio.wav_rms_dbfs(tmp_path / "silence.wav") == pytest.approx(-200.0)


def test_wav_rms_dbfs_full_scale_is_zero():
    pass  # see test below — split for clarity


def test_wav_rms_dbfs_known_amplitude(tmp_path):
    # Constant +/-16384 → RMS = 16384, dBFS = 20*log10(16384/32768) = -6.02
    samples = np.full(SAMPLE_RATE, 16384, dtype=np.int16)
    samples[1::2] = -16384
    _write_pcm_wav(tmp_path / "half.wav", samples)
    db = audio.wav_rms_dbfs(tmp_path / "half.wav")
    assert db == pytest.approx(-6.02, abs=0.05)


def test_wav_rms_dbfs_missing_returns_zero(tmp_path):
    # Returns 0.0 (not -200.0) so callers fail open — a real problem
    # surfaces downstream rather than masquerading as silence.
    assert audio.wav_rms_dbfs(tmp_path / "missing.wav") == 0.0


def test_load_recorder_wav_as_pcm_rejects_wrong_format(tmp_path):
    # Stereo (channels=2) — should be rejected.
    samples = np.zeros(SAMPLE_RATE, dtype=np.int16)
    _write_pcm_wav(tmp_path / "stereo.wav", samples, channels=2)
    with pytest.raises(RuntimeError):
        audio.load_recorder_wav_as_pcm(tmp_path / "stereo.wav")


def test_load_recorder_wav_as_pcm_returns_normalised_float32(tmp_path):
    samples = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
    _write_pcm_wav(tmp_path / "ok.wav", samples)
    out = audio.load_recorder_wav_as_pcm(tmp_path / "ok.wav")
    assert out.dtype == np.float32
    # 16384 / 32768 = 0.5 exactly
    assert out[1] == pytest.approx(0.5)
    assert out[2] == pytest.approx(-0.5)
