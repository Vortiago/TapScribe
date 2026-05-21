"""Tests for tapscribe.strip_silence — helper functions that stay on the
production path (the silero detector itself is exercised end-to-end via
the stub in conftest)."""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe import strip_silence as ss


def test_filter_low_energy_regions_drops_quiet_ones():
    sample_count = ss.SAMPLE_RATE
    samples = np.concatenate(
        [
            np.tile(np.array([8000, -8000], dtype=np.int16), sample_count // 2),  # ~-12 dBFS
            np.tile(np.array([200, -200], dtype=np.int16), sample_count // 2),  # quiet
        ]
    )
    # Build two regions matching the two halves.
    regions = [(0, sample_count), (sample_count, 2 * sample_count)]
    filtered = ss.filter_low_energy_regions(samples, regions, floor_dbfs=-40.0)
    assert filtered == [(0, sample_count)]


def test_filter_low_energy_regions_keeps_empty_when_all_below():
    samples = np.full(ss.SAMPLE_RATE, 50, dtype=np.int16)
    regions = [(0, ss.SAMPLE_RATE)]
    filtered = ss.filter_low_energy_regions(samples, regions, floor_dbfs=-20.0)
    assert filtered == []


def test_read_wav_int16_rejects_wrong_rate(tmp_path):
    import wave

    path = tmp_path / "wrong-rate.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)  # wrong: expected 16000
        w.writeframes(np.zeros(800, dtype=np.int16).tobytes())
    with pytest.raises(ValueError):
        ss.read_wav_int16(path)


@pytest.mark.real_silero
def test_detect_speech_silero_without_silero_raises_runtime_error(monkeypatch):
    """silero-vad + torch are core deps; the production path must surface
    a clear "install is corrupt" error — not silently fall back, not
    return None — when the import fails (which now signals a broken
    install rather than an opt-out).

    Opts out of the autouse silero stub via @real_silero so we exercise
    the actual import path."""
    import builtins

    real_import = builtins.__import__

    def _no_silero(name, *args, **kwargs):
        # Block both deps that the production import line uses so the test
        # outcome doesn't depend on whether torch happens to be installed.
        if name in {"torch", "silero_vad"} or name.startswith(("torch.", "silero_vad.")):
            raise ImportError(f"simulated missing dep: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_silero)
    with pytest.raises(RuntimeError, match=r"install is corrupt"):
        ss.detect_speech_silero(np.zeros(16000, dtype=np.int16), min_silence_ms=500, pad_ms=200)
