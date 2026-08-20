"""Merge-time attribution: a transcript segment → one or more `slug#<voice>`
pieces, joined on absolute time (ADR-0021). Pure — no disk, no model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tapscribe.transcribers.base import TranscriptionSegment, Word
from tapscribe.voice_join import VoiceSpan, attribute_segment, spans_by_slug

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
    # The pieces TILE the segment: outer edges are the segment's own bounds, and
    # the boundary is the midpoint of the 2 s–4 s pause where the speaker
    # changed — see `_tile` and the tiling tests below.
    assert pieces[0].abs_start == WAV_START
    assert pieces[0].abs_end == WAV_START + timedelta(seconds=3.0)
    assert pieces[1].abs_start == WAV_START + timedelta(seconds=3.0)
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


def test_a_fragmented_voice_wins_on_total_overlap_not_its_longest_span() -> None:
    """A diarizer emits one turn as many short spans. Picking the single longest
    span hands a fragmented speaker's words to whoever managed one longer run."""
    spans = [
        _span("A", 0.0, 2.0),
        _span("B", 2.0, 5.0),
        _span("A", 5.0, 7.0),
        _span("A", 7.0, 9.0),
        _span("A", 9.0, 11.0),
    ]

    pieces = _join(_seg(0.0, 12.0), spans)

    assert [p.speaker for p in pieces] == [f"{SLUG}#A"], "A speaks 8 s to B's 3 s"


def test_a_zero_width_word_does_not_break_a_homogeneous_run() -> None:
    """`Word` timestamps round to 2 dp, so a short token can collapse to
    `start == end` — which overlaps nothing by measure."""
    seg = _seg(
        0.0,
        3.0,
        text="one two three",
        words=_words(("one", 0.0, 1.0), ("two", 1.5, 1.5), ("three", 2.0, 3.0)),
    )

    pieces = _join(seg, [_span("A", 0.0, 10.0)])

    assert [p.speaker for p in pieces] == [f"{SLUG}#A"]
    assert pieces[0].text == "one two three"


# ---- a slug is not identity-unique (#440) ----------------------------------


def _roster(*pairs: tuple[str, str]) -> dict:
    return {identity: {"slug": slug} for identity, slug in pairs}


def _voices(*identities: str) -> dict:
    return {
        i: {
            "run_id": "r1",
            "voices": {"A": {"spans": [{"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:05Z"}]}},
        }
        for i in identities
    }


def test_two_identities_sharing_a_slug_are_refused_rather_than_unioned() -> None:
    """`slug` is `safe_name(<display name>)` — two tray machines both streaming
    "System audio" collapse into one bucket, and being concurrent their spans
    overlap, so one identity's spans would split the other's segments and be
    stamped with the first identity's mapping. Same "refuse to guess" rule
    `fold_voices` applies to the cross-session case."""
    roster = _roster(("tray-a-111", "System_audio"), ("tray-b-222", "System_audio"))

    by_slug, ambiguous = spans_by_slug(_voices("tray-a-111", "tray-b-222"), roster)

    assert by_slug == {}, "an ambiguous slug must contribute no spans at all"
    assert ambiguous == {"System_audio"}


def test_a_shared_slug_does_not_suppress_an_unrelated_tap() -> None:
    roster = _roster(("tray-a-111", "System_audio"), ("tray-b-222", "System_audio"), ("mic-c", "Alice"))

    by_slug, ambiguous = spans_by_slug(_voices("tray-a-111", "tray-b-222", "mic-c"), roster)

    assert set(by_slug) == {"Alice"}
    assert ambiguous == {"System_audio"}


def test_an_undiarized_tap_sharing_the_slug_still_makes_it_ambiguous() -> None:
    """The likelier collision: one tray box diarized, a second one not. Deriving
    ambiguity from `voices` saw only one owner and let the diarized identity's
    spans split the OTHER tap's segments."""
    roster = _roster(("tray-a-111", "System_audio"), ("tray-b-222", "System_audio"))

    by_slug, ambiguous = spans_by_slug(_voices("tray-a-111"), roster)

    assert by_slug == {}
    assert ambiguous == {"System_audio"}


def test_one_identity_per_slug_is_unaffected() -> None:
    by_slug, ambiguous = spans_by_slug(_voices("mic-c"), _roster(("mic-c", "Alice")))

    assert set(by_slug) == {"Alice"}
    assert ambiguous == set()


# ---- split pieces must tile the segment (#441) -----------------------------


def test_split_pieces_tile_the_segment_with_no_lost_time() -> None:
    """`speaking_seconds` sums piece windows. Word-run bounds exclude the gap
    where the speaker changed, so a split segment used to contribute less than
    its own duration — systematically understating diarized speakers against
    undiarized ones in the same session."""
    seg = _seg(
        0.0,
        6.0,
        text="alpha beta gamma delta",
        words=_words(("alpha", 0.0, 1.0), ("beta", 1.0, 2.0), ("gamma", 4.0, 5.0), ("delta", 5.0, 6.0)),
    )

    pieces = _join(seg, [_span("A", 0.0, 3.0), _span("B", 3.0, 10.0)])

    assert pieces[0].abs_start == WAV_START, "the first piece starts where the segment does"
    assert pieces[-1].abs_end == WAV_START + timedelta(seconds=6.0), "the last piece ends with it"
    assert pieces[0].abs_end == pieces[1].abs_start, "no gap between adjacent pieces"
    total = sum((p.abs_end - p.abs_start).total_seconds() for p in pieces)
    assert total == 6.0, "the pieces account for the whole segment"


def test_the_boundary_between_pieces_is_the_midpoint_of_the_speaker_change() -> None:
    """Neither speaker owns the silence between them, so split it."""
    seg = _seg(
        0.0,
        6.0,
        text="alpha gamma",
        words=_words(("alpha", 0.0, 2.0), ("gamma", 4.0, 6.0)),
    )

    pieces = _join(seg, [_span("A", 0.0, 3.0), _span("B", 3.0, 10.0)])

    assert pieces[0].abs_end == WAV_START + timedelta(seconds=3.0)
