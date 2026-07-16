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


def test_first_refresh_after_cadence_opens_the_line_with_a_volatile_hypothesis():
    """Before `refresh_s` has elapsed, `maybe_refresh` is a no-op (returns
    None). Once enough audio has been fed, one refresh runs a single
    decode and opens the connection's one line at t=0 — the decode itself
    is still unconfirmed (no second decode has agreed yet), so it rides
    in `buffer_text`, not in the committed line text."""
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
    assert lines[0]["start"] == 0.0
    assert win.buffer_text == "hello"


def test_growing_line_updates_in_place_not_appended():
    """Consecutive refreshes of a still-open (non-rolled-over) window
    update the SAME line entry in place — mirroring WlK's cumulative
    `lines` snapshot semantics that WlKRelay depends on for suffix-only
    emission. The committed text is the agreed word-prefix of the last
    two decodes (LocalAgreement-2); the disagreed tail stays volatile."""
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
    assert second[0]["text"] == "hello"  # both decodes agree on "hello"
    assert win.buffer_text == "there"  # the unconfirmed tail stays volatile
    assert second[0]["start"] == first[0]["start"]


def test_rollover_caps_every_decode_near_chunk_s():
    """Once the window's span exceeds `chunk_s`, the buffer is truncated
    to the `overlap_s` tail — so every single `generate()` call stays
    under the sub-30s clip cap regardless of total session length. The
    rollover decode itself runs over the full window (BEFORE truncation,
    so audio fed since the previous refresh is never dropped), which may
    exceed `chunk_s` by at most the audio that arrived past the boundary
    — one cadence tick in practice."""
    seen_lens: list[float] = []

    def stub_generate(arr: np.ndarray) -> str:
        seen_lens.append(arr.shape[0] / 16000)
        return "text"

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=1.0, overlap_s=0.2, refresh_s=0.3)

    # 0.31s feeds: strictly past the 0.3s cadence on every feed, so float
    # accumulation can't skip a refresh and pile extra slack onto the
    # rollover decode.
    for _ in range(12):  # ~3.7s fed — several rollovers past chunk_s=1.0
        win.feed_pcm(_pcm_seconds(0.31))
        win.maybe_refresh()
    win.close()

    assert seen_lens, "expected at least one decode"
    # chunk_s plus at most one 0.31s feed past the boundary.
    assert all(length <= 1.0 + 0.31 + 1e-9 for length in seen_lens), seen_lens


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
    against actual Moonshine ONNX inference, where the line's `end` came
    out far smaller than the total audio fed. Feed enough audio to roll
    over several times and assert the line's `end` keeps tracking the
    TOTAL audio fed, never resetting."""

    def stub_generate(arr: np.ndarray) -> str:
        return f"seen {arr.shape[0]}"

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=1.0, overlap_s=0.2, refresh_s=0.3)

    total_fed_s = 0.0
    ends: list[float] = []
    for _ in range(12):  # 12 * 0.3s = 3.6s fed, well past two 1.0s rollovers
        win.feed_pcm(_pcm_seconds(0.3))
        total_fed_s += 0.3
        lines = win.maybe_refresh()
        if lines is not None:
            ends.append(lines[0]["end"])

    lines = win.close()
    ends.append(lines[0]["end"])
    # Monotonically increasing, and the final end must land at the TOTAL
    # audio fed — not at some small "seconds since last rollover"
    # remainder (the bug produced a value like 1.96 instead of ~3.6).
    assert ends == sorted(ends)
    assert ends[-1] == pytest.approx(total_fed_s, abs=0.05)
    assert lines[0]["start"] == 0.0


def test_no_audio_fed_close_returns_no_lines():
    """A connection that never received PCM (e.g. the gate never opened)
    must not call the model or fabricate a line on close."""

    def stub_generate(arr: np.ndarray) -> str:
        raise AssertionError("generate_fn must not be called with no audio")

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=0.5)
    lines = win.close()
    assert lines == []


# ---------------------------------------------------------------------------
# The window -> relay text contract (PR #334 review findings 4/5/6): the
# single open line extends APPEND-ONLY from the relay's perspective
# (LocalAgreement-2 commit), the volatile hypothesis rides in
# `buffer_text` (-> the snapshot's `buffer_transcription`), and a rollover
# neither re-keys the line nor re-emits the retained overlap's words.
# ---------------------------------------------------------------------------


def test_revised_decode_never_rewrites_committed_text():
    """Finding #6: Moonshine has no LocalAgreement upstream — a re-decode
    of the grown buffer can revise earlier words ("the cat" -> "the cast
    of thousands"). WlKRelay re-emits the ENTIRE line text when an update
    isn't a clean prefix-extension, so the window must only ever extend
    the line's text append-only: commit the word-prefix two consecutive
    decodes agree on, keep the rest as the volatile buffer_text."""
    texts = iter(["the cat", "the cast of thousands", "the cast of thousands tonight"])

    def stub_generate(arr: np.ndarray) -> str:
        return next(texts)

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=0.5)

    seen_texts: list[str] = []
    for _ in range(3):
        win.feed_pcm(_pcm_seconds(0.6))
        lines = win.maybe_refresh()
        assert lines is not None
        assert len(lines) == 1
        seen_texts.append(lines[0]["text"])

    final = win.close()
    seen_texts.append(final[0]["text"])

    # Append-only: every snapshot's text is a prefix of the next one —
    # the relay therefore only ever emits suffixes, never a re-sent line.
    for prev, cur in zip(seen_texts, seen_texts[1:], strict=False):
        assert cur.startswith(prev), f"non-prefix rewrite: {prev!r} -> {cur!r}"
    # The revised decode wins overall (nothing wrong was committed early:
    # "cat" never agreed across two consecutive decodes).
    assert final[0]["text"] == "the cast of thousands tonight"
    assert win.buffer_text == ""


def test_buffer_text_carries_the_uncommitted_hypothesis():
    """Finding #5 groundwork: the not-yet-committed tail of the latest
    decode must be exposed so the /asr server can send it as
    `buffer_transcription` — that's what WlKRelay's `_flush_tail` rescues
    when the close-time snapshot is lost."""

    def stub_generate(arr: np.ndarray) -> str:
        return "hello there"

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=0.5)
    win.feed_pcm(_pcm_seconds(0.6))
    lines = win.maybe_refresh()
    assert lines is not None
    # First decode: nothing agreed yet -> all volatile.
    assert lines[0]["text"] == ""
    assert win.buffer_text == "hello there"

    # Second decode agrees -> the words commit, buffer drains.
    win.feed_pcm(_pcm_seconds(0.6))
    lines = win.maybe_refresh()
    assert lines is not None
    assert lines[0]["text"] == "hello there"
    assert win.buffer_text == ""

    # close() commits everything; nothing volatile is left behind.
    final = win.close()
    assert final[0]["text"] == "hello there"
    assert win.buffer_text == ""


def test_rollover_keeps_the_line_key_and_does_not_reemit_overlap_words():
    """Finding #4: rollover retains `overlap_s` of already-transcribed PCM
    for context; the overlap's words re-appear in every decode of the new
    window and must be stitched away — and the line key (speaker, start)
    must stay stable so WlKRelay never sees the overlap words under a
    fresh key. Scripted decodes simulate the overlap re-decode ("four"
    leads the post-rollover window)."""
    scripted = iter(
        [
            "one two",  # 0.4s window
            "one two three",  # 0.8s window
            "one two three four",  # 1.2s window -> rollover (chunk_s=1.0)
            "four five",  # post-rollover: 0.2s overlap + 0.4s new
            "four five six",  # overlap + 0.8s new
            "four five six",  # close (re-decode of the same window)
        ]
    )

    def stub_generate(arr: np.ndarray) -> str:
        return next(scripted)

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=1.0, overlap_s=0.2, refresh_s=0.3)

    keys: list[tuple] = []
    texts: list[str] = []
    for _ in range(5):
        win.feed_pcm(_pcm_seconds(0.4))
        lines = win.maybe_refresh()
        assert lines is not None
        assert len(lines) == 1, "rollover must not open a second line"
        keys.append((lines[0]["speaker"], lines[0]["start"]))
        texts.append(lines[0]["text"])

    final = win.close()
    texts.append(final[0]["text"])

    assert len(set(keys)) == 1, f"line key changed across rollover: {keys}"
    for prev, cur in zip(texts, texts[1:], strict=False):
        assert cur.startswith(prev), f"non-prefix rewrite: {prev!r} -> {cur!r}"
    assert final[0]["text"] == "one two three four five six"
    # The overlap word must appear exactly once.
    assert final[0]["text"].split().count("four") == 1


def test_close_without_new_audio_commits_the_volatile_tail_without_redecoding():
    """close() right after a cadence refresh has no new audio to decode —
    it must commit the last decode's volatile words as-is instead of
    paying a redundant inference over the identical buffer."""
    calls = {"n": 0}

    def stub_generate(arr: np.ndarray) -> str:
        calls["n"] += 1
        return "only decode"

    win = MoonshineWindow(generate_fn=stub_generate, chunk_s=25.0, overlap_s=3.0, refresh_s=0.5)
    win.feed_pcm(_pcm_seconds(0.6))
    assert win.maybe_refresh() is not None
    assert calls["n"] == 1

    final = win.close()
    assert calls["n"] == 1  # no redundant re-decode of unchanged audio
    assert final[0]["text"] == "only decode"
    assert win.buffer_text == ""


# ---------------------------------------------------------------------------
# Env-tunable knobs (PRD #120 story 21) — the operator's only tuning surface
# until the dashboard wiring lands, so the override must demonstrably change
# the windowing, not just parse.
# ---------------------------------------------------------------------------


def test_env_knobs_override_the_window_defaults(monkeypatch):
    """A knob-less MoonshineWindow must pick up TAPSCRIBE_MOONSHINE_*:
    0.3s of audio is past an overridden 0.25s refresh cadence (under the
    0.5s default it would still be a no-op), and decodes roll over at the
    overridden chunk_s=1.0 (under the 25s default the whole feed would
    ride in one growing buffer). Renaming the ENV_* constants — an env
    override becoming a silent no-op — goes red on the first assert."""
    monkeypatch.setenv("TAPSCRIBE_MOONSHINE_CHUNK_S", "1.0")
    monkeypatch.setenv("TAPSCRIBE_MOONSHINE_OVERLAP_S", "0.2")
    monkeypatch.setenv("TAPSCRIBE_MOONSHINE_REFRESH_S", "0.25")

    seen_lens: list[float] = []

    def stub_generate(arr: np.ndarray) -> str:
        seen_lens.append(arr.shape[0] / 16000)
        return "text"

    win = MoonshineWindow(generate_fn=stub_generate)  # no knobs — env only

    win.feed_pcm(_pcm_seconds(0.3))
    assert win.maybe_refresh() is not None  # > 0.25s override, < 0.5s default

    for _ in range(4):
        win.feed_pcm(_pcm_seconds(0.3))
        win.maybe_refresh()
    win.close()
    # Rollover enforced at the overridden chunk_s: every decode stays
    # under 1.0s + one 0.3s feed past the boundary.
    assert all(length <= 1.0 + 0.3 + 1e-9 for length in seen_lens), seen_lens


def test_env_chunk_override_past_the_clip_ceiling_is_ignored(monkeypatch):
    """Moonshine's documented single-clip ceiling is ~30s; an operator
    override past the 29s bound is IGNORED (warned + default applied,
    the repo's env_float convention) — it must never push a decode
    window past the ceiling."""
    from tapscribe.transcribers._moonshine_window import chunk_s_from_env

    monkeypatch.setenv("TAPSCRIBE_MOONSHINE_CHUNK_S", "500")
    assert chunk_s_from_env() == 25.0  # the default, not 500
