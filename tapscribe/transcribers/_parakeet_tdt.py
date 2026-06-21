"""Token→word→segment aggregation for the `transformers` Parakeet TDT adapter.

The HF `ParakeetForTDT` path returns per-token timestamps, not the
word+segment payload NeMo handed us (so this module replaces the role
`_nemo_payload` played for the NeMo shape). `processor.decode(sequences,
durations=..., timestamps=True)` yields, per input, a flat list of token
dicts::

    [{"token": "Ye", "start": 3.36, "end": 3.52},
     {"token": "ah", "start": 3.52, "end": 3.60},
     {"token": ",",  "start": 3.60, "end": 3.60},
     {"token": " I", "start": 3.68, "end": 3.76}, ...]

Two conventions in that stream make reconstruction exact rather than
heuristic (verified against `nvidia/parakeet-tdt-0.6b-v3`):

* **A token that *begins a new word* carries a leading space** (`" I"`,
  `" land"`, `" now"`); continuation sub-tokens do not (`"ah"`, `"ister"`,
  the `"'"`/`"m"` of `"I'm"`).
* **Punctuation arrives as its own zero-or-near-zero-width token**
  (`","`, `"."`) with no leading space, so it attaches to the word it
  follows.

So: split into words at leading-space tokens, attach punctuation/
continuations to the current word, then break into segments after a word
whose text ends in sentence-final punctuation. `prob` is pinned to 1.0
(Parakeet emits no per-token confidence — same choice as the MLX adapter,
distinct from "missing" so downstream can tell the two apart).
"""

from __future__ import annotations

from typing import Any

from .base import TranscriptionSegment, Word, _lookup

# Punctuation that attaches to the preceding word rather than starting a
# new one. Apostrophe is deliberately absent — `"I'm"` arrives as
# `" I"` + `"'"` + `"m"`, all one word.
_ATTACH_PUNCT = frozenset(",.?!;:")
# Sentence-final marks that close a segment after the word they end.
_SENTENCE_END = frozenset(".?!")


def _build_words(tokens: list[Any], *, offset_s: float) -> list[Word]:
    """Fold a flat token list into `Word`s, shifting every timestamp by
    `offset_s` (non-zero when the tokens came from a chunked window) so
    the merged result stays session-relative."""
    words: list[Word] = []
    for tok in tokens:
        raw = _lookup(tok, "token", "") or ""
        stripped = raw.strip()
        if not stripped:
            # Whitespace-only / empty token: no text to attach, but it can
            # still carry timing — extend the current word's end so a
            # trailing gap token doesn't truncate the alignment.
            if words:
                end = round(float(_lookup(tok, "end", 0.0) or 0.0) + offset_s, 2)
                words[-1] = Word(words[-1].start, max(words[-1].end, end), words[-1].word, 1.0)
            continue
        start = round(float(_lookup(tok, "start", 0.0) or 0.0) + offset_s, 2)
        end = round(float(_lookup(tok, "end", 0.0) or 0.0) + offset_s, 2)
        is_punct = stripped in _ATTACH_PUNCT
        starts_word = raw.startswith(" ") and not is_punct
        if words and (is_punct or not starts_word):
            prev = words[-1]
            words[-1] = Word(prev.start, max(prev.end, end), prev.word + stripped, 1.0)
        else:
            words.append(Word(start, end, stripped, 1.0))
    return words


def build_segments_from_tdt_tokens(
    tokens: list[Any],
    *,
    offset_s: float = 0.0,
) -> tuple[TranscriptionSegment, ...]:
    """Pair a TDT token list into `TranscriptionSegment`s with attached
    `words`. Segments break after a word ending in sentence-final
    punctuation; a trailing run with no terminal punctuation forms a
    final segment. Returns `()` for an empty/speechless window."""
    words = _build_words(tokens, offset_s=offset_s)
    if not words:
        return ()
    out: list[TranscriptionSegment] = []
    current: list[Word] = []
    for word in words:
        current.append(word)
        if word.word[-1:] in _SENTENCE_END:
            out.append(_segment_of(current))
            current = []
    if current:
        out.append(_segment_of(current))
    return tuple(out)


def _segment_of(words: list[Word]) -> TranscriptionSegment:
    return TranscriptionSegment(
        start=words[0].start,
        end=words[-1].end,
        text=" ".join(w.word for w in words),
        words=tuple(words),
    )
