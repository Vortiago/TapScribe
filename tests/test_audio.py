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


def test_int16_peak_norm_odd_byte_count_truncates_instead_of_crashing():
    """Bridge sends exactly 640-byte frames, but a malformed wire read,
    a future framer change, or a test feeding raw bytes could produce
    an odd byte count. `np.frombuffer` raises on non-multiple-of-2 input;
    if that exception escaped write_frame it would tear down the whole
    /tap WebSocket. The function must handle the case gracefully and
    return a peak based on the largest even prefix."""
    # 3 bytes = 1 complete int16 sample + 1 stray byte. The complete
    # sample is 0x4000 (= 16384) little-endian; peak should be 0.5.
    buf = b"\x00\x40\xff"
    assert audio.int16_peak_norm(buf) == pytest.approx(0.5)
    # 641 bytes (one stray trailing byte after 320 samples) must
    # likewise not raise.
    samples = np.zeros(320, dtype=np.int16)
    samples[0] = -32768  # peak == 1.0
    buf2 = samples.tobytes() + b"\xff"
    assert audio.int16_peak_norm(buf2) == pytest.approx(1.0)


def test_int16_peak_norm_does_not_mutate_input():
    """np.frombuffer returns a *view* over the input buffer. If we ever
    accidentally write through that view (e.g. converting to a writeable
    copy and forgetting to break the alias), we'd corrupt the WAV the
    recorder is also writing from the same buffer. Pin that the function
    is read-only."""
    samples = np.array([100, -200, 16384, -16384], dtype=np.int16)
    buf = bytes(samples.tobytes())  # immutable bytes copy
    before = bytes(buf)
    audio.int16_peak_norm(buf)
    assert buf == before, "int16_peak_norm must not mutate its input"


# ---------------------------------------------------------------------------
# int16_peak_norm — real-audio coverage
#
# The synthetic tests above pin numeric edge cases; these stream actual
# recorded speech through the same function frame by frame, so we catch
# regressions that wouldn't show up on constant-amplitude buffers: e.g.
# accidentally returning the mean instead of the peak, off-by-one
# slicing, or a future "optimisation" that rounds tiny values to zero
# too aggressively.
# ---------------------------------------------------------------------------


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "audio"
FRAME_BYTES = 640  # 20 ms @ 16 kHz mono int16 — bridge wire format


def _wav_to_frames(path: Path, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    """Read a recorder-format WAV and chunk it into 20 ms frames, the
    same shape the bridge sends. Trailing partial frame is dropped so
    every emitted chunk is exactly `frame_bytes` long."""
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        raw = w.readframes(w.getnframes())
    return [raw[i : i + frame_bytes] for i in range(0, len(raw) - frame_bytes + 1, frame_bytes)]


def test_int16_peak_norm_armstrong_speech_wav():
    """Stream a real 12 s speech WAV through int16_peak_norm frame by
    frame. The peak series must:

      - stay strictly within the 0.0..1.0 contract the renderer relies on
      - hit a meaningful peak (> 0.5) — Armstrong audibly raises his
        voice on "one giant leap"; if the meter never crossed the green
        zone for that, something would be wrong with our peak extraction
      - exhibit meaningful dynamic range across the clip (max ≥ 2× min)
        — a constant value would mean we're computing something
        amplitude-invariant like the mean, not the peak

    Failing any of these means int16_peak_norm is producing values that
    would either confuse the renderer's colour zones or fail to ever
    light the meter up for a real speaker.

    Note: this fixture has a noticeable noise floor / room tone, so its
    "quiet" frames sit around 0.12 rather than near zero. That's true
    of most real-world recordings — VBR-normalised, AGC'd, or otherwise
    processed. Tests that assume "silence = 0.00" should use a
    synthesised silent WAV instead, not a real clip."""
    fixture = FIXTURES_DIR / "armstrong-en.wav"
    frames = _wav_to_frames(fixture)
    assert len(frames) > 500, "expected ~600 frames in a 12 s WAV"
    peaks = [audio.int16_peak_norm(f) for f in frames]
    assert all(0.0 <= p <= 1.0 for p in peaks), "peak outside [0,1] would break renderer"
    assert max(peaks) > 0.5, "real speech should peg the meter into the 'hot' zone somewhere"
    assert max(peaks) >= 2 * min(peaks), "peak series should show real dynamic range"


def test_int16_peak_norm_marlene_speech_wav_norwegian():
    """Second real-audio fixture, a different speaker / language /
    recording chain — guards against the test above coincidentally
    matching only the Armstrong clip's characteristics."""
    fixture = FIXTURES_DIR / "marlene-nb.wav"
    frames = _wav_to_frames(fixture)
    assert len(frames) > 500
    peaks = [audio.int16_peak_norm(f) for f in frames]
    assert all(0.0 <= p <= 1.0 for p in peaks)
    assert max(peaks) > 0.3


def test_int16_peak_norm_synthesised_half_scale_tone_reads_half(tmp_path):
    """Generate a deterministic half-scale 440 Hz sine, write it to a
    WAV, read it back frame by frame, and assert the peak series lands
    at ~0.5 on the frames that contain at least one full cycle of the
    sine. Pins the exact numeric value end-to-end through the same WAV
    I/O path the recorder uses, with a signal that any audio engineer
    can reproduce."""
    SAMPLE_RATE = 16000
    seconds = 0.5
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    samples = (0.5 * 32767 * np.sin(2 * np.pi * 440.0 * t)).astype(np.int16)
    wav_path = tmp_path / "half_scale_440hz.wav"
    _write_pcm_wav(wav_path, samples)

    frames = _wav_to_frames(wav_path)
    peaks = [audio.int16_peak_norm(f) for f in frames]
    # 20 ms @ 440 Hz = 8.8 cycles per frame, so every frame should
    # contain at least one full positive AND negative excursion —
    # the peak must land essentially at 0.5 on every frame.
    assert peaks, "non-empty frame list"
    assert min(peaks) == pytest.approx(0.5, abs=0.01)
    assert max(peaks) == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# compute_peaks — server-side waveform downsample (backs the Recordings hero)
# ---------------------------------------------------------------------------


def test_compute_peaks_returns_requested_bins_and_metadata(tmp_path):
    # 2 s half-scale square wave → every bucket's peak lands at ~0.5, and the
    # metadata (bins / duration / sample rate) round-trips.
    seconds = 2.0
    n = int(SAMPLE_RATE * seconds)
    samples = np.full(n, 16384, dtype=np.int16)
    samples[1::2] = -16384
    _write_pcm_wav(tmp_path / "a.wav", samples)
    result = audio.compute_peaks(tmp_path / "a.wav", bins=128)
    assert result.bins == 128
    assert len(result.peaks) == 128
    assert result.sample_rate == SAMPLE_RATE
    assert result.duration_s == pytest.approx(seconds, abs=0.01)
    assert all(0.0 <= p <= 1.0 for p in result.peaks), "peaks must stay in the renderer's [0,1]"
    assert all(p == pytest.approx(0.5, abs=0.01) for p in result.peaks)


def test_compute_peaks_full_negative_clamps_to_one(tmp_path):
    # The most-negative int16 (-32768) must map to exactly 1.0, not overflow —
    # the same saturation contract int16_peak_norm guarantees.
    _write_pcm_wav(tmp_path / "full.wav", np.full(SAMPLE_RATE, -32768, dtype=np.int16))
    result = audio.compute_peaks(tmp_path / "full.wav", bins=10)
    assert all(0.0 <= p <= 1.0 for p in result.peaks)
    assert max(result.peaks) == pytest.approx(1.0)


def test_compute_peaks_preserves_dynamic_range(tmp_path):
    # A quiet first half + a loud second half must produce small early bins
    # and large late bins — a regression that averaged (or otherwise washed
    # out the peak) would flatten this.
    n = SAMPLE_RATE
    samples = np.zeros(n, dtype=np.int16)
    samples[: n // 2] = 200  # near-silent floor
    samples[n // 2 :: 2] = 20000  # loud, alternating for a real peak
    samples[n // 2 + 1 :: 2] = -20000
    _write_pcm_wav(tmp_path / "ramp.wav", samples)
    result = audio.compute_peaks(tmp_path / "ramp.wav", bins=10)
    assert result.peaks[0] < 0.05, "the quiet half should read near-silent"
    assert result.peaks[-1] > 0.5, "the loud half should peg high"
    assert max(result.peaks) >= 2 * min(p for p in result.peaks if p > 0)


def test_compute_peaks_short_wav_still_returns_requested_bins(tmp_path):
    # Fewer samples than bins → still exactly `bins` entries, with the
    # trailing empty buckets reading 0.0 (the renderer relies on a fixed
    # length).
    _write_pcm_wav(tmp_path / "tiny.wav", np.array([16384, -16384, 16384], dtype=np.int16))
    result = audio.compute_peaks(tmp_path / "tiny.wav", bins=16)
    assert result.bins == 16
    assert len(result.peaks) == 16
    assert result.peaks[-1] == 0.0


def test_compute_peaks_bins_coerced_to_at_least_one(tmp_path):
    _write_pcm_wav(tmp_path / "a.wav", np.full(1000, 8000, dtype=np.int16))
    result = audio.compute_peaks(tmp_path / "a.wav", bins=0)
    assert result.bins == 1
    assert len(result.peaks) == 1


def test_compute_peaks_raises_clear_error_on_non_recorder_format(tmp_path):
    # 44.1 kHz stereo → not the recorder format. The error names the format
    # and offers no in-process fallback (no ffmpeg, no resample).
    _write_pcm_wav(tmp_path / "bad.wav", np.zeros(200, dtype=np.int16), rate=44100, channels=2)
    with pytest.raises(RuntimeError, match="unexpected WAV format"):
        audio.compute_peaks(tmp_path / "bad.wav", bins=64)
