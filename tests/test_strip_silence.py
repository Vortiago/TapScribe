"""Tests for tapscribe.strip_silence — RMS detector + region filtering.

We avoid silero-vad here because it pulls in torch, which is too heavy for
CI. detect_speech_silero returns None when not installed, which is
exercised indirectly elsewhere.
"""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe import strip_silence as ss


def _make_speech_silence(speech_lengths_s, silence_lengths_s, amplitude=8000):
    """Build a deterministic int16 sample array with speech bursts (a
    sine-shaped envelope at `amplitude`) separated by silence."""
    chunks = []
    n_speech = len(speech_lengths_s)
    n_silence = len(silence_lengths_s)
    for i in range(max(n_speech, n_silence)):
        if i < n_speech:
            n = int(speech_lengths_s[i] * ss.SAMPLE_RATE)
            # Square wave is the loudest possible signal at a given amplitude;
            # easy to keep above the -45 dBFS floor without depending on
            # randomness.
            block = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
            chunks.append(block)
        if i < n_silence:
            n = int(silence_lengths_s[i] * ss.SAMPLE_RATE)
            chunks.append(np.zeros(n, dtype=np.int16))
    return np.concatenate(chunks)


def test_detect_speech_rms_finds_two_regions():
    # 1 s speech, 1 s silence, 1 s speech → expect 2 detected regions.
    audio = _make_speech_silence([1.0, 1.0], [1.0])
    regions = ss.detect_speech_rms(audio, threshold_db=-30.0, min_silence_ms=500, pad_ms=50)
    assert len(regions) == 2
    for s, e in regions:
        assert 0 <= s < e <= len(audio)


def test_detect_speech_rms_merges_short_silences():
    # 1 s speech, 0.1 s silence, 1 s speech, with min_silence_ms=500 →
    # the gap is short enough to merge into one region.
    audio = _make_speech_silence([1.0, 1.0], [0.1])
    regions = ss.detect_speech_rms(audio, threshold_db=-30.0, min_silence_ms=500, pad_ms=10)
    assert len(regions) == 1


def test_detect_speech_rms_returns_empty_on_silence():
    audio = np.zeros(2 * ss.SAMPLE_RATE, dtype=np.int16)
    regions = ss.detect_speech_rms(audio, threshold_db=-30.0, min_silence_ms=500, pad_ms=50)
    assert regions == []


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
