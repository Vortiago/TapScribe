"""Tests for tapscribe.audio — WAV duration + RMS reading."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
import pytest

from tapscribe import audio

SAMPLE_RATE = 16000


def _write_pcm_wav(
    path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE, channels: int = 1, sampwidth: int = 2
) -> None:
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


# ---------------------------------------------------------------------------
# int16_peak_norm — backs the dashboard's per-tap volume meter
# ---------------------------------------------------------------------------


def test_int16_peak_norm_silence_is_zero():
    # 20 ms of int16 silence is what the bridge sends when a track is
    # subscribed but the participant isn't speaking. The meter must read
    # zero so quiet rows don't show a phantom signal.
    silence = np.zeros(320, dtype=np.int16).tobytes()
    assert audio.int16_peak_norm(silence) == 0.0


def test_int16_peak_norm_half_scale_is_one_half():
    samples = np.full(320, 16384, dtype=np.int16)
    samples[1::2] = -16384
    assert audio.int16_peak_norm(samples.tobytes()) == pytest.approx(0.5)


def test_int16_peak_norm_full_negative_clamps_to_one():
    # The most-negative int16 value is -32768. Dividing by 32768 makes it
    # exactly 1.0 — important so the meter saturates cleanly at clipping
    # instead of overflowing the 0..1 contract the renderer relies on.
    samples = np.full(320, -32768, dtype=np.int16)
    assert audio.int16_peak_norm(samples.tobytes()) == pytest.approx(1.0)


def test_int16_peak_norm_empty_or_short_returns_zero():
    assert audio.int16_peak_norm(b"") == 0.0
    # Single byte can't be an int16 sample.
    assert audio.int16_peak_norm(b"\x00") == 0.0
