"""Tests for the Voxtral sentence-splitter.

Voxtral returns one free-form text blob per WAV without per-token timing.
For a readable merged transcript we sentence-split the blob and
interpolate timestamps proportionally by character count. The fix is
intentionally crude — once HF transformers issue #41999 lands word-level
timestamps upstream, this helper goes away.
"""

from __future__ import annotations

import pytest

from tapscribe.transcribers.voxtral import split_voxtral_text_into_segments


def test_empty_text_returns_no_segments():
    assert split_voxtral_text_into_segments("", duration=10.0) == ()


def test_whitespace_only_text_returns_no_segments():
    assert split_voxtral_text_into_segments("   \n\t  ", duration=10.0) == ()


def test_single_sentence_spans_full_duration():
    segs = split_voxtral_text_into_segments("Hello world", duration=10.0)
    assert len(segs) == 1
    assert segs[0].text == "Hello world"
    assert segs[0].start == 0.0
    assert segs[0].end == 10.0


def test_text_without_terminator_is_one_segment():
    """No '.', '!' or '?' anywhere — treat the whole blob as one sentence."""
    segs = split_voxtral_text_into_segments("hi there how are you doing", duration=5.0)
    assert len(segs) == 1
    assert segs[0].text == "hi there how are you doing"
    assert segs[0].start == 0.0
    assert segs[0].end == 5.0


def test_three_sentences_split_on_terminators():
    segs = split_voxtral_text_into_segments("Hi. How are you? Fine!", duration=10.0)
    assert [s.text for s in segs] == ["Hi.", "How are you?", "Fine!"]


def test_sentence_timings_are_proportional_to_character_count():
    """A 10-char sentence in a 20-char blob over 10 seconds gets 5 seconds."""
    text = "Short. " + ("x" * 13) + "."  # "Short." (6) + " " + "xxxxxxxxxxxxx." (14) = 20 visible chars
    segs = split_voxtral_text_into_segments(text, duration=20.0)
    assert len(segs) == 2
    # First sentence: 6 chars / 20 total → 30% of 20s = 6s.
    assert segs[0].start == 0.0
    assert segs[0].end == pytest.approx(6.0, abs=0.5)
    # Second segment starts where the first ends.
    assert segs[1].start == segs[0].end
    # Last segment always ends exactly at duration (absorbs rounding).
    assert segs[1].end == 20.0


def test_segments_cover_full_duration_with_no_gaps_or_overlap():
    """Adjacency invariant: seg[i].end == seg[i+1].start, and the last
    segment ends exactly at `duration` (no leftover float dust)."""
    text = "One. Two. Three. Four. Five."
    duration = 12.34
    segs = split_voxtral_text_into_segments(text, duration=duration)
    assert len(segs) == 5
    for a, b in zip(segs, segs[1:], strict=False):
        assert a.end == b.start
    assert segs[0].start == 0.0
    assert segs[-1].end == duration


def test_zero_duration_yields_zero_length_segments_not_a_crash():
    """A WAV with zero measured duration shouldn't divide-by-zero or skip
    the text. All segments collapse to start=end=0 but the text survives."""
    segs = split_voxtral_text_into_segments("Hi. There.", duration=0.0)
    assert [s.text for s in segs] == ["Hi.", "There."]
    for s in segs:
        assert s.start == 0.0
        assert s.end == 0.0


def test_mixed_punctuation_keeps_terminators_with_their_sentences():
    segs = split_voxtral_text_into_segments("Wait... what? No way!", duration=6.0)
    # Ellipsis isn't a sentence boundary on its own — the '.' before the
    # space after 'what?' splits, and so does the space after 'way!'.
    assert [s.text for s in segs] == ["Wait... what?", "No way!"]


def test_extra_whitespace_between_sentences_is_normalized():
    segs = split_voxtral_text_into_segments("First.    Second.\n\tThird.", duration=9.0)
    assert [s.text for s in segs] == ["First.", "Second.", "Third."]
