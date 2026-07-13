"""Tests for tapscribe.transcribers._moonshine_window.MoonshineWindow — the
backend-agnostic rolling-chunk pseudo-streaming state machine shared by the
MLX and ONNX-CPU Moonshine live engines.

No real model ever loads here: `generate_fn` is an injected stub that
records the arrays it was called with and returns canned text, so these
tests assert the window/refresh/rollover bookkeeping in isolation from any
inference backend.
"""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe.transcribers._moonshine_window import MoonshineWindow


def _pcm_seconds(seconds: float, *, sample_rate: int = 16000) -> bytes:
    """`seconds` of silence as recorder-format PCM (int16 mono)."""
    n = int(seconds * sample_rate)
    return np.zeros(n, dtype=np.int16).tobytes()


def test_first_refresh_after_cadence_emits_one_open_line():
    """Before `refresh_s` has elapsed, `maybe_refresh` is a no-op (returns
    None). Once enough audio has been fed, one refresh produces a single
    open (tail) line starting at t=0."""
    calls: list[np.ndarray] = []

    def stub_generate(arr: np.ndarray) -> str:
        calls.append(arr)
        return "hello"

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=0.5)

    win.feed_pcm(_pcm_seconds(0.2))
    assert win.maybe_refresh() is None  # under the refresh cadence — no call yet
    assert calls == []

    win.feed_pcm(_pcm_seconds(0.4))  # total 0.6s > refresh_s=0.5
    lines = win.maybe_refresh()
    assert lines is not None
    assert len(calls) == 1
    assert len(lines) == 1
    assert lines[0]["text"] == "hello"
    assert lines[0]["start"] == 0.0


def test_growing_line_updates_in_place_not_appended():
    """Consecutive refreshes of a still-open (non-rolled-over) window
    update the SAME line entry in place — mirroring WlK's cumulative
    `lines` snapshot semantics that WlKRelay depends on for suffix-only
    emission."""
    texts = iter(["hello", "hello there"])

    def stub_generate(arr: np.ndarray) -> str:
        return next(texts)

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=0.5)
    win.feed_pcm(_pcm_seconds(0.6))
    first = win.maybe_refresh()
    assert first is not None
    assert len(first) == 1

    win.feed_pcm(_pcm_seconds(0.6))
    second = win.maybe_refresh()
    assert second is not None
    assert len(second) == 1  # still one line — grew in place, not a new entry
    assert second[0]["text"] == "hello there"
    assert second[0]["start"] == first[0]["start"]


def test_rollover_past_chunk_s_starts_a_new_line_with_offset_start():
    """Once the open window's span exceeds `chunk_s`, the current line is
    frozen and a new line begins — its `start` reflecting the retained
    `overlap_s` seconds, not 0. Every single `generate()` call therefore
    stays under the sub-30s clip cap regardless of total session length."""
    texts = iter(["first chunk of text", "second chunk of text"])
    seen_lens: list[float] = []

    def stub_generate(arr: np.ndarray) -> str:
        seen_lens.append(arr.shape[0] / 16000)
        return next(texts)

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=1.0, overlap_s=0.2, refresh_s=0.3)

    win.feed_pcm(_pcm_seconds(0.5))
    first = win.maybe_refresh()
    assert first is not None
    assert len(first) == 1
    assert first[0]["start"] == 0.0

    # Push the open window's span past chunk_s=1.0s.
    win.feed_pcm(_pcm_seconds(0.6))
    second = win.maybe_refresh()
    assert second is not None
    assert len(second) == 2  # rolled over: previous line frozen, new one opened
    assert second[0]["text"] == "first chunk of text"
    # New line starts at (now - overlap_s), not at 0.
    assert second[1]["start"] > 0.0
    # No single generate() call ever saw more than chunk_s seconds of audio.
    assert all(length <= 1.0 + 1e-9 for length in seen_lens)


def test_close_forces_a_final_refresh_bypassing_cadence():
    """`close()` flushes the trailing text even if the refresh cadence
    hasn't elapsed yet — otherwise a short utterance that ends before the
    next scheduled refresh would silently drop its last words."""

    def stub_generate(arr: np.ndarray) -> str:
        return "final words"

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=10.0)
    win.feed_pcm(_pcm_seconds(0.3))
    assert win.maybe_refresh() is None  # cadence not reached

    lines = win.close()
    assert len(lines) == 1
    assert lines[0]["text"] == "final words"


def test_timestamps_stay_on_the_absolute_clock_across_a_rollover():
    """Regression: a rollover truncates the internal PCM buffer down to
    just the retained `overlap_s` tail. Deriving the session clock from
    buffer length (instead of tracking total fed seconds separately)
    silently "rewinds" time at that exact moment, corrupting every
    timestamp computed afterwards — caught by a real end-to-end run
    against actual Moonshine ONNX inference, where a second line's `end`
    came out far smaller than its `start`. Feed enough audio to roll over
    twice and assert every line's `start`/`end` keeps increasing against
    the TOTAL audio fed, never resets."""

    def stub_generate(arr: np.ndarray) -> str:
        return f"seen {arr.shape[0]}"

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=1.0, overlap_s=0.2, refresh_s=0.3)

    total_fed_s = 0.0
    for _ in range(12):  # 12 * 0.3s = 3.6s fed, well past two 1.0s rollovers
        win.feed_pcm(_pcm_seconds(0.3))
        total_fed_s += 0.3
        win.maybe_refresh()

    lines = win.close()
    assert len(lines) >= 3  # at least two rollovers happened
    # Monotonically increasing, and the final line's end must land near the
    # TOTAL audio fed — not near some small "seconds since last rollover"
    # remainder (the bug produced a value like 1.96 instead of ~9.96).
    starts = [line["start"] for line in lines]
    ends = [line["end"] for line in lines]
    assert starts == sorted(starts)
    assert ends == sorted(ends)
    assert ends[-1] == pytest.approx(total_fed_s, abs=0.05)
    for start, end in zip(starts, ends, strict=True):
        assert end >= start


def test_no_audio_fed_close_returns_no_lines():
    """A connection that never received PCM (e.g. the gate never opened)
    must not call the model or fabricate a line on close."""

    def stub_generate(arr: np.ndarray) -> str:
        raise AssertionError("generate_fn must not be called with no audio")

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=0.5)
    assert win.close() == []
