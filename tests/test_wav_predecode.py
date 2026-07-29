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
from tapscribe.audio import RECORDER_SAMPLE_RATE as SAMPLE_RATE


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


def test_a_torn_wav_decodes_instead_of_raising(tmp_path):
    """The whole-frame guard lives in `read_recorder_frames`, so THIS path
    inherits it — that inheritance is the point of the shared reader.

    The guard was originally patched into `wav_rms_dbfs` and `compute_peaks`
    instead, which left the transcribe path (here) and the strip path still
    raising `ValueError: buffer size must be a multiple of element size` on a
    WAV truncated mid-sample — a partial flush on ENOSPC, the case
    `tap_fan_out._close` handles. Reverting the truncation reddened only
    audio.py's own tests, so the fix's stated purpose was untested for two of
    its four consumers.
    """
    path = tmp_path / "torn.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.full(1000, 4096, dtype=np.int16).tobytes())
    path.write_bytes(path.read_bytes()[:-1])  # lop one byte off the data chunk

    pcm = wav_predecode.load_recorder_wav_as_pcm(path)

    assert pcm.dtype == np.float32
    assert len(pcm) == 999, "the torn trailing sample is dropped, the rest decodes"
