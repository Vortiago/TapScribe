"""Merge-time attribution: a transcript segment → one or more `slug#<voice>`
pieces, joined on absolute time (ADR-0021). Pure — no disk, no model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tapscribe.transcribers.base import TranscriptionSegment, Word
from tapscribe.voice_join import VoiceSpan, attribute_segment

WAV_START = datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)
SLUG = "System_audio"


def _span(label: str, start_s: float, end_s: float) -> VoiceSpan:
    return VoiceSpan(
        label=label,
        start=WAV_START + timedelta(seconds=start_s),
        end=WAV_START + timedelta(seconds=end_s),
    )


def _seg(start: float, end: float, text: str = "hello", words: tuple[Word, ...] | None = None):
    return TranscriptionSegment(start=start, end=end, text=text, words=words)


def _words(*spec: tuple[str, float, float]) -> tuple[Word, ...]:
    return tuple(Word(start=s, end=e, word=w, prob=0.9) for w, s, e in spec)


def _join(seg, spans):
    return attribute_segment(seg, wav_start=WAV_START, slug=SLUG, spans=spans)


def test_segment_inside_one_span_takes_that_voice() -> None:
    pieces = _join(_seg(1.0, 4.0), [_span("A", 0.0, 10.0)])

    assert len(pieces) == 1
    assert pieces[0].speaker == f"{SLUG}#A"
    assert pieces[0].text == "hello"
    assert pieces[0].abs_start == WAV_START + timedelta(seconds=1.0)
    assert pieces[0].abs_end == WAV_START + timedelta(seconds=4.0)


def test_undiarized_tap_keeps_the_plain_slug() -> None:
    """No spans → byte-identical to the pre-ADR-0021 output."""
    pieces = _join(_seg(1.0, 4.0), [])

    assert [p.speaker for p in pieces] == [SLUG]


def test_segment_overlapping_no_span_keeps_the_plain_slug() -> None:
    """Silence between Voices is not a Voice."""
    pieces = _join(_seg(20.0, 25.0), [_span("A", 0.0, 10.0)])

    assert [p.speaker for p in pieces] == [SLUG]


def test_partial_overlap_resolves_to_the_greater_overlap() -> None:
    pieces = _join(_seg(4.0, 10.0), [_span("A", 0.0, 5.0), _span("B", 5.0, 20.0)])

    assert [p.speaker for p in pieces] == [f"{SLUG}#B"]


def test_equal_overlap_breaks_on_label_so_the_result_is_deterministic() -> None:
    pieces = _join(_seg(4.0, 6.0), [_span("A", 0.0, 5.0), _span("B", 5.0, 10.0)])

    assert [p.speaker for p in pieces] == [f"{SLUG}#B"]


def test_segment_crossing_a_voice_change_splits_on_word_timestamps() -> None:
    """The whole point of carrying words: one segment, two speakers."""
    seg = _seg(
        0.0,
        6.0,
        text="alpha beta gamma delta",
        words=_words(("alpha", 0.0, 1.0), ("beta", 1.0, 2.0), ("gamma", 4.0, 5.0), ("delta", 5.0, 6.0)),
    )

    pieces = _join(seg, [_span("A", 0.0, 3.0), _span("B", 3.0, 10.0)])

    assert [p.speaker for p in pieces] == [f"{SLUG}#A", f"{SLUG}#B"]
    assert [p.text for p in pieces] == ["alpha beta", "gamma delta"]
    assert pieces[0].abs_start == WAV_START
    assert pieces[0].abs_end == WAV_START + timedelta(seconds=2.0)
    assert pieces[1].abs_start == WAV_START + timedelta(seconds=4.0)
    assert pieces[1].abs_end == WAV_START + timedelta(seconds=6.0)


def test_words_that_all_land_on_one_voice_stay_one_piece() -> None:
    seg = _seg(0.0, 2.0, text="alpha beta", words=_words(("alpha", 0.0, 1.0), ("beta", 1.0, 2.0)))

    pieces = _join(seg, [_span("A", 0.0, 10.0)])

    assert len(pieces) == 1
    assert pieces[0].text == "alpha beta"


def test_segment_crossing_a_change_without_words_goes_wholly_to_the_dominant_voice() -> None:
    """Voxtral emits no words, so the split is unavailable — attribute whole
    rather than drop text."""
    seg = _seg(0.0, 6.0, text="alpha beta gamma delta", words=None)

    pieces = _join(seg, [_span("A", 0.0, 2.0), _span("B", 2.0, 10.0)])

    assert [p.speaker for p in pieces] == [f"{SLUG}#B"]
    assert pieces[0].text == "alpha beta gamma delta"


def test_a_word_run_that_matches_no_voice_keeps_the_plain_slug() -> None:
    seg = _seg(
        0.0,
        6.0,
        text="alpha beta",
        words=_words(("alpha", 0.0, 1.0), ("beta", 5.0, 6.0)),
    )

    pieces = _join(seg, [_span("A", 0.0, 2.0)])

    assert [p.speaker for p in pieces] == [f"{SLUG}#A", SLUG]
    assert [p.text for p in pieces] == ["alpha", "beta"]
