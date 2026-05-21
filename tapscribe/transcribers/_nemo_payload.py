"""NeMo-shape segment-builder shared by parakeet / canary / mlx_canary.

Each of those adapters' underlying SDK returns a result whose
`timestamps` field is `{"segment": [...], "word": [...]}` — a list of
segment dicts (start/end/segment-text) and a parallel list of word
dicts (start/end/word). This module pairs them into the
`TranscriptionSegment` tuples the rest of TapScribe expects, applying
a 1e-3 second boundary tolerance so a word landing exactly on a
segment edge still gets attached.
"""

from __future__ import annotations

from typing import Any

from .base import TranscriptionSegment, Word

_MISSING = object()


def _lookup(payload: Any, key: str, default: Any = "") -> Any:
    """Read `key` from `payload`. Dict lookup if the payload is a dict,
    `getattr` otherwise. Missing → `default`. Single-key form mirrors
    the pre-consolidation `_lookup` helper that lived in each adapter.

    Note: `base._lookup` is a sibling helper with `default=None`. The
    divergence is intentional — this module's callers (`_first_truthy`
    + direct calls in `build_segments_from_nemo_payload`) test on
    truthiness, so an empty-string default avoids spurious None / int
    comparisons. `base._lookup` is used by `Word.from_payload` where
    None is the documented "field absent" sentinel for `prob`."""
    if isinstance(payload, dict):
        return payload.get(key, default)
    value = getattr(payload, key, _MISSING)
    return default if value is _MISSING else value


def _first_truthy(payload: Any, *keys: str) -> str:
    """Walk `keys` in order; return the first non-falsy string value.
    The pre-consolidation adapters wrote
    `(_lookup(seg, "segment", "") or _lookup(seg, "text", "") or "").strip()`
    — preserve that falsy-fallback semantic so a payload carrying
    both `"segment": ""` AND `"text": "..."` picks the latter."""
    for k in keys:
        value = _lookup(payload, k, default="")
        if value:
            return str(value).strip()
    return ""


def build_segments_from_nemo_payload(
    seg_dicts: list[Any],
    word_dicts: list[Any],
) -> tuple[TranscriptionSegment, ...]:
    """Pair NeMo's segment list with its word list. Words whose timing
    falls inside a segment's [start - 1e-3, end + 1e-3] range get
    attached to it; words outside every segment are dropped from the
    segments' `words` field."""
    all_words = [
        Word(
            start=round(float(_lookup(w, "start", default=0.0)), 2),
            end=round(float(_lookup(w, "end", default=0.0)), 2),
            word=_first_truthy(w, "word", "text"),
            prob=1.0,
        )
        for w in word_dicts
    ]
    out: list[TranscriptionSegment] = []
    for seg in seg_dicts:
        # Raw start/end for the inclusion check — rounding before the
        # comparison would broaden the tolerance band by up to one
        # rounding-step on each side, silently admitting words the
        # original adapters rejected. Output segment carries the
        # rounded values (the on-disk wire shape was always two-dp).
        start = float(_lookup(seg, "start", default=0.0))
        end = float(_lookup(seg, "end", default=0.0))
        text = _first_truthy(seg, "segment", "text")
        in_range = tuple(w for w in all_words if w.start >= start - 1e-3 and w.end <= end + 1e-3)
        out.append(
            TranscriptionSegment(
                start=round(start, 2),
                end=round(end, 2),
                text=text,
                words=in_range or None,
            )
        )
    return tuple(out)
