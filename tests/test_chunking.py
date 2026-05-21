"""Tests for tapscribe.chunking — the shared windowing helper the
MLX adapters use to split a pre-decoded PCM array into per-window
slices that fit under each backend's per-call budget."""

from __future__ import annotations

import pytest

from tapscribe.chunking import Window, chunk_windows

# 16 kHz mono is the recorder format; the chunking helper takes
# sample_rate as a keyword so tests can use small numbers, but the
# defaults all assume this rate.
SR = 16000


def test_short_audio_returns_single_window():
    """A WAV shorter than `chunk_s` becomes one window covering the
    whole thing — no stitching seam, no per-window overhead beyond
    the wrapping."""
    windows = chunk_windows(SR * 5, chunk_s=120.0, overlap_s=15.0, sample_rate=SR)
    assert windows == [Window(0, SR * 5, 0.0)]


def test_long_audio_steps_by_chunk_minus_overlap():
    """A 200 s WAV at 120 s chunks with 15 s overlap → 2 windows:
    [0, 120) and [105, 200). The second window's start is
    chunk - overlap = 105 s after the first."""
    windows = chunk_windows(SR * 200, chunk_s=120.0, overlap_s=15.0, sample_rate=SR)
    assert windows == [
        Window(0, SR * 120, 0.0),
        Window(SR * 105, SR * 200, 105.0),
    ]


def test_many_windows_cover_entire_audio_without_gaps():
    """The last window's end is always the total length; no audio
    falls between windows."""
    total = SR * 500
    windows = chunk_windows(total, chunk_s=120.0, overlap_s=15.0, sample_rate=SR)
    assert windows[-1].end_sample == total
    # Every window after the first starts within the previous window's
    # span (overlap holds, no gap).
    for prev, nxt in zip(windows, windows[1:], strict=False):
        assert nxt.start_sample < prev.end_sample


def test_zero_overlap_produces_adjacent_windows():
    windows = chunk_windows(SR * 100, chunk_s=30.0, overlap_s=0.0, sample_rate=SR)
    starts = [w.start_sample for w in windows]
    ends = [w.end_sample for w in windows]
    assert starts == [0, SR * 30, SR * 60, SR * 90]
    assert ends[:-1] == starts[1:]  # tight tiling, no overlap


def test_rejects_zero_or_negative_chunk():
    with pytest.raises(ValueError, match="chunk_s must be > 0"):
        chunk_windows(SR * 10, chunk_s=0.0, overlap_s=0.0, sample_rate=SR)
    with pytest.raises(ValueError, match="chunk_s must be > 0"):
        chunk_windows(SR * 10, chunk_s=-5.0, overlap_s=0.0, sample_rate=SR)


def test_rejects_negative_overlap():
    with pytest.raises(ValueError, match="overlap_s must be >= 0"):
        chunk_windows(SR * 10, chunk_s=30.0, overlap_s=-1.0, sample_rate=SR)


def test_rejects_pathologically_large_overlap():
    """Overlap approaching the chunk length makes `step` approach one
    sample, spawning effectively unbounded windows for long audio."""
    with pytest.raises(ValueError, match="pathologically many windows"):
        chunk_windows(SR * 100, chunk_s=10.0, overlap_s=9.5, sample_rate=SR)


def test_start_s_is_window_start_in_seconds():
    """`start_s` is cached on the Window so adapters can offset per-
    window timestamps into session-relative coordinates without
    re-dividing on every segment."""
    windows = chunk_windows(SR * 70, chunk_s=30.0, overlap_s=5.0, sample_rate=SR)
    assert [w.start_s for w in windows] == [0.0, 25.0, 50.0]
