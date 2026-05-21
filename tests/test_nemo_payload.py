"""Tests for tapscribe.transcribers._nemo_payload — the shared
NeMo-shape segment-builder that backs the parakeet / canary / mlx_canary
adapters.

The behaviour we lock in here used to live as three byte-identical
copies of `_build_segments` (plus `_lookup` / `_word_from_payload`)
inside the adapter files. After consolidation those adapters import
from this module, so the tests below describe the contract every
NeMo-shape consumer relies on.
"""

from __future__ import annotations

from types import SimpleNamespace

from tapscribe.transcribers._nemo_payload import build_segments_from_nemo_payload
from tapscribe.transcribers.base import TranscriptionSegment, Word


def test_pairs_words_inside_segment_range():
    """A word whose [start, end] sits inside a segment's [start, end]
    range is attached to that segment's `words` tuple. The pairing
    is what makes the segment view useful — without it the merged
    transcript would lose per-word timestamps."""
    seg_dicts = [
        {"start": 0.0, "end": 1.0, "segment": "hello world"},
    ]
    word_dicts = [
        {"start": 0.0, "end": 0.5, "word": "hello"},
        {"start": 0.5, "end": 1.0, "word": "world"},
    ]

    segments = build_segments_from_nemo_payload(seg_dicts, word_dicts)

    assert segments == (
        TranscriptionSegment(
            start=0.0,
            end=1.0,
            text="hello world",
            words=(
                Word(start=0.0, end=0.5, word="hello", prob=1.0),
                Word(start=0.5, end=1.0, word="world", prob=1.0),
            ),
        ),
    )


def test_empty_segment_list_returns_empty_tuple():
    """No segments means nothing to merge. The caller (each adapter)
    has a downstream fallback that synthesises a single "whole WAV"
    segment when text is non-empty but segments came back blank — that
    logic lives at the call site, not here."""
    assert build_segments_from_nemo_payload([], []) == ()
    # And empty segments with non-empty words still produces no
    # segments — words alone don't make a segment row.
    assert build_segments_from_nemo_payload([], [{"start": 0.0, "end": 1.0, "word": "x"}]) == ()


def test_segment_text_falls_back_to_text_key():
    """NeMo's segment dicts usually carry `"segment": "..."`, but
    `mlx-audio`'s Canary port sometimes emits `"text": "..."` instead.
    The pre-consolidation `_build_segments` accepted either; the
    shared helper must keep that tolerance or the MLX-Canary adapter
    silently produces empty segment text."""
    segs = build_segments_from_nemo_payload(
        [{"start": 0.0, "end": 1.0, "text": "fallback used"}],
        [],
    )
    assert segs[0].text == "fallback used"


def test_accepts_attribute_style_payloads():
    """Some MLX wrappers return dataclass-ish objects rather than
    plain dicts. The pre-consolidation `_lookup` walked both via
    getattr; the shared helper preserves that — otherwise the same
    adapter would have to choose between mocks and real return shapes."""
    seg = SimpleNamespace(start=0.0, end=1.0, segment="from attrs")
    word = SimpleNamespace(start=0.0, end=1.0, word="hi")
    segs = build_segments_from_nemo_payload([seg], [word])
    assert segs == (
        TranscriptionSegment(
            start=0.0,
            end=1.0,
            text="from attrs",
            words=(Word(start=0.0, end=1.0, word="hi", prob=1.0),),
        ),
    )


def test_word_at_segment_boundary_within_tolerance_is_included():
    """The original `_build_segments` accepted a 1e-3 second slop on
    both edges of every segment — floating-point rounding from the
    underlying SDK can put a word's start at 0.999 of the segment
    end, or its end at 1.001. A word that's "essentially" inside
    must still be attached, otherwise the merged transcript drops
    timestamps right at segment boundaries.

    pre-rounding values used so that round-to-2dp doesn't mask the
    boundary case (round(0.9995, 2) == 1.0, which would land inside
    without any tolerance check). The chosen values stay distinct
    after rounding."""
    segs = build_segments_from_nemo_payload(
        [{"start": 0.0, "end": 1.0, "segment": "one"}],
        [
            # word.end is 1.001 — outside [0, 1] but inside [0, 1.001]
            {"start": 0.95, "end": 1.001, "word": "edge"},
        ],
    )
    assert segs[0].words is not None
    assert segs[0].words[0].word == "edge"


def test_comparison_uses_raw_segment_bounds_not_rounded():
    """The pre-consolidation `_build_segments` compared word timings
    (rounded to 2 dp) against the segment's RAW `start`/`end` plus
    the 1e-3 slop — segment bounds were NOT rounded before comparison.
    The output `TranscriptionSegment` did carry rounded values.

    Divergence case: seg.end raw = 0.9954, word.end raw = 0.9962.
    After rounding the word ends at 1.0; the segment's raw upper
    bound + slop is 0.9964, so 1.0 > 0.9964 → word excluded. If we
    (wrongly) rounded the segment first we'd get bound 1.001, and
    1.0 ≤ 1.001 → word incorrectly included. Lock the raw semantics
    in so the consolidation doesn't quietly admit words the originals
    rejected."""
    segs = build_segments_from_nemo_payload(
        [{"start": 0.0, "end": 0.9954, "segment": "one"}],
        [{"start": 0.5, "end": 0.9962, "word": "outside-original"}],
    )
    assert segs[0].words is None
    # Output segment still carries rounded bounds — that part WAS
    # rounded in the originals.
    assert segs[0].end == 1.0


def test_word_clearly_outside_segment_is_dropped():
    """A word whose [start, end] sits clearly outside [seg.start - 1e-3,
    seg.end + 1e-3] is dropped from the segment's `words`. Without this
    pruning a word straddling segment boundaries would attach to both."""
    segs = build_segments_from_nemo_payload(
        [{"start": 0.0, "end": 1.0, "segment": "one"}],
        [{"start": 2.0, "end": 3.0, "word": "out"}],
    )
    assert segs[0].words is None


def test_word_text_falls_back_to_text_key():
    """Same tolerance for words — `"word"` vs `"text"`. mlx-audio
    occasionally uses the latter."""
    segs = build_segments_from_nemo_payload(
        [{"start": 0.0, "end": 1.0, "segment": "hi"}],
        [{"start": 0.0, "end": 1.0, "text": "hi"}],
    )
    assert segs[0].words == (Word(start=0.0, end=1.0, word="hi", prob=1.0),)


def test_segment_text_is_stripped():
    """The pre-consolidation adapters all called `.strip()` on segment
    text. Without it leading/trailing whitespace from the SDK leaks
    into the cached transcript and into the merged session view."""
    segs = build_segments_from_nemo_payload(
        [{"start": 0.0, "end": 1.0, "segment": "  padded text  "}],
        [],
    )
    assert segs[0].text == "padded text"


def test_empty_segment_key_falls_back_to_text():
    """Original adapters used `_lookup(seg, "segment", "") or
    _lookup(seg, "text", "")` — falsy-fallback, not key-exists-fallback.
    A payload with `"segment": ""` AND a non-empty `"text"` must use
    the latter. Some MLX wrappers emit both keys, with one blank for
    forwards-compat reasons."""
    segs = build_segments_from_nemo_payload(
        [{"start": 0.0, "end": 1.0, "segment": "", "text": "from text key"}],
        [],
    )
    assert segs[0].text == "from text key"
