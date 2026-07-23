"""Tests for `_parakeet_tdt.build_segments_from_tdt_tokens` — the token→
word→segment aggregation for the transformers Parakeet TDT adapter.

The HF TDT decode emits a flat token list where a token beginning a new
word carries a leading space and punctuation arrives as its own token.
These tests pin that reconstruction (verified against the real model in
`tapscribe/transcribers/parakeet.py`'s development).
"""

from __future__ import annotations

from tapscribe.transcribers._parakeet_tdt import build_segments_from_tdt_tokens


def _tok(token: str, start: float, end: float) -> dict:
    return {"token": token, "start": start, "end": end}


def test_empty_token_list_yields_no_segments():
    assert build_segments_from_tdt_tokens([]) == ()


def test_leading_space_starts_a_new_word():
    tokens = [_tok("Ye", 0.0, 0.1), _tok("ah", 0.1, 0.2), _tok(" I", 0.3, 0.4)]
    segs = build_segments_from_tdt_tokens(tokens)
    assert len(segs) == 1
    assert [w.word for w in (segs[0].words or ())] == ["Yeah", "I"]
    # continuation token extends the first word's end, not a new word.
    assert segs[0].words[0].start == 0.0
    assert segs[0].words[0].end == 0.2


def test_punctuation_attaches_to_preceding_word():
    tokens = [_tok(" Hello", 0.0, 0.4), _tok(",", 0.4, 0.4), _tok(" there", 0.5, 0.8)]
    segs = build_segments_from_tdt_tokens(tokens)
    assert [w.word for w in (segs[0].words or ())] == ["Hello,", "there"]


def test_apostrophe_contraction_is_one_word():
    # "I'm" arrives as " I" + "'" + "m" — apostrophe is not a word break.
    tokens = [_tok(" I", 0.0, 0.1), _tok("'", 0.1, 0.1), _tok("m", 0.1, 0.2)]
    segs = build_segments_from_tdt_tokens(tokens)
    assert [w.word for w in (segs[0].words or ())] == ["I'm"]


def test_sentence_final_punctuation_splits_segments():
    tokens = [
        _tok("Hi", 0.0, 0.2),
        _tok(".", 0.2, 0.2),
        _tok(" Bye", 0.3, 0.5),
        _tok("?", 0.5, 0.5),
    ]
    segs = build_segments_from_tdt_tokens(tokens)
    assert [s.text for s in segs] == ["Hi.", "Bye?"]
    assert segs[0].start == 0.0
    assert segs[0].end == 0.2
    assert segs[1].start == 0.3


def test_trailing_run_without_terminal_punct_is_a_final_segment():
    tokens = [_tok("no", 0.0, 0.2), _tok(" end", 0.3, 0.5)]
    segs = build_segments_from_tdt_tokens(tokens)
    assert [s.text for s in segs] == ["no end"]


def test_offset_shifts_all_timestamps():
    tokens = [_tok("hi", 0.0, 0.2), _tok(".", 0.2, 0.2)]
    segs = build_segments_from_tdt_tokens(tokens, offset_s=10.0)
    assert segs[0].start == 10.0
    assert segs[0].end == 10.2
    assert segs[0].words[0].start == 10.0


def test_words_carry_pinned_probability():
    # Parakeet emits no per-token confidence — prob is pinned to 1.0 so
    # downstream can distinguish "not reported" from "low confidence".
    segs = build_segments_from_tdt_tokens([_tok(" word", 0.0, 0.3)])
    assert segs[0].words[0].prob == 1.0


# ── whitespace-only timing tokens ───────────────────────────────────
#
# The HF TDT stream can end on a token with no text but a real `end` — a
# trailing gap the model still timed. `_build_words` extends the previous
# word's end from it rather than dropping it. Nothing pinned that, so the
# plausible simplification `if not stripped: continue` passed the whole suite
# while silently shortening the last word's (and the segment's) end.


def test_trailing_whitespace_only_token_extends_the_last_word_end():
    tokens = [_tok(" hi", 0.0, 0.2), _tok("", 0.2, 0.5)]

    segs = build_segments_from_tdt_tokens(tokens)

    assert [s.text for s in segs] == ["hi"]
    assert segs[0].words[0].end == 0.5
    assert segs[0].end == 0.5
    # It contributes timing only — never a word of its own.
    assert [w.word for w in segs[0].words] == ["hi"]


def test_whitespace_only_token_never_shortens_an_already_longer_word():
    """`max(prev.end, end)` — a gap token that ends EARLIER than the word it
    follows must not pull the alignment backwards."""
    tokens = [_tok(" hi", 0.0, 0.9), _tok(" ", 0.2, 0.3)]

    segs = build_segments_from_tdt_tokens(tokens)

    assert segs[0].words[0].end == 0.9


def test_whitespace_only_token_is_shifted_by_the_window_offset():
    tokens = [_tok(" hi", 0.0, 0.2), _tok("", 0.2, 0.5)]

    segs = build_segments_from_tdt_tokens(tokens, offset_s=10.0)

    assert segs[0].words[0].end == 10.5


def test_leading_whitespace_only_token_is_dropped_without_raising():
    """No preceding word to extend — `words[-1]` would be an IndexError."""
    tokens = [_tok("  ", 0.0, 0.3), _tok(" hi", 0.3, 0.5)]

    segs = build_segments_from_tdt_tokens(tokens)

    assert [s.text for s in segs] == ["hi"]
    assert segs[0].start == 0.3


def test_all_whitespace_token_list_yields_no_segments():
    assert build_segments_from_tdt_tokens([_tok(" ", 0.0, 0.1), _tok("", 0.1, 0.4)]) == ()
