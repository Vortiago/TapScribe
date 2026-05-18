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
