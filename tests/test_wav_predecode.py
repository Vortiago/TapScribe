"""Tests for tapscribe.wav_predecode — the ffmpeg-skipping PCM loader.

The MLX transcriber adapters (`mlx_whisper`, `mlx_parakeet`) call
`load_recorder_wav_as_pcm` to hand the model raw PCM, side-stepping the
system `ffmpeg` binary their bundled loaders would otherwise spawn.
These tests pin (a) the strict recorder-format check (so an unusual WAV
falls back to ffmpeg instead of silently mis-decoding) and (b) the
normalisation contract the consumer adapters depend on.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from tapscribe import wav_predecode

SAMPLE_RATE = 16000


def _write_pcm_wav(
    path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE, channels: int = 1, sampwidth: int = 2
) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())


def test_load_recorder_wav_as_pcm_rejects_wrong_format(tmp_path):
    # Stereo (channels=2) — should be rejected so the caller falls back
    # to its backend's ffmpeg-backed loader rather than feeding the
    # model interleaved samples as if they were mono.
    samples = np.zeros(SAMPLE_RATE, dtype=np.int16)
    _write_pcm_wav(tmp_path / "stereo.wav", samples, channels=2)
    with pytest.raises(RuntimeError):
        wav_predecode.load_recorder_wav_as_pcm(tmp_path / "stereo.wav")


def test_load_recorder_wav_as_pcm_returns_normalised_float32(tmp_path):
    samples = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
    _write_pcm_wav(tmp_path / "ok.wav", samples)
    out = wav_predecode.load_recorder_wav_as_pcm(tmp_path / "ok.wav")
    assert out.dtype == np.float32
    # 16384 / 32768 = 0.5 exactly — divisor chosen so the most-negative
    # int16 sample (-32768) maps to -1.0 cleanly, not slightly past it.
    assert out[1] == pytest.approx(0.5)
    assert out[2] == pytest.approx(-0.5)
