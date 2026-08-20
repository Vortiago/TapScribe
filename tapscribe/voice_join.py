"""Merge-time attribution — one transcript segment → `slug#<voice>` pieces.

Pure interval join on absolute time, so it never needs to know which WAV a
span came from and the per-WAV cache stays untouched (ADR-0021).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .text import parse_iso, voice_key
from .transcribers.base import TranscriptionSegment


@dataclass(frozen=True)
class VoiceSpan:
    """One Voice speaking, in absolute time."""

    label: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Piece:
    """A whole segment, or one voice-homogeneous run of its words."""

    abs_start: datetime
    abs_end: datetime
    speaker: str
    text: str


def spans_by_slug(voices: Mapping[str, Any], roster: Mapping[str, Any]) -> dict[str, list[VoiceSpan]]:
    """`{speaker slug: spans}` for the merge, which only knows a WAV's slug.

    The Roster owns the slug↔identity join, so a slug with no Roster entry —
    or an identity with no Voices — simply contributes nothing and that tap
    keeps its plain key.
    """
    out: dict[str, list[VoiceSpan]] = {}
    for identity, entry in voices.items():
        slug = (roster.get(identity) or {}).get("slug")
        if not slug:
            continue
        spans: list[VoiceSpan] = []
        for label, body in (entry.get("voices") or {}).items():
            for span in body.get("spans") or []:
                start, end = parse_iso(span.get("start")), parse_iso(span.get("end"))
                if start and end and end > start:
                    spans.append(VoiceSpan(label=label, start=start, end=end))
        if spans:
            out.setdefault(slug, []).extend(spans)
    return out


def _overlap(a_start: datetime, a_end: datetime, span: VoiceSpan) -> float:
    return max(0.0, (min(a_end, span.end) - max(a_start, span.start)).total_seconds())


def _dominant(start: datetime, end: datetime, spans: Sequence[VoiceSpan]) -> str | None:
    """The label overlapping `[start, end)` most — summed ACROSS that label's
    spans, not the single longest one. A diarizer emits one turn as many short
    spans, so a per-span maximum hands a fragmented speaker's words to whoever
    managed one longer run. Ties break on label so the same input always
    attributes the same way.

    A zero-width window falls back to containment: `Word` timestamps are rounded
    to 2 dp, so a short token can collapse to `start == end`, which overlaps
    nothing by measure and would otherwise break a homogeneous run in three.
    """
    totals: dict[str, float] = {}
    for span in spans:
        seconds = _overlap(start, end, span)
        if seconds > 0.0:
            totals[span.label] = totals.get(span.label, 0.0) + seconds
    if not totals and end <= start:
        totals = {s.label: 0.0 for s in spans if s.start <= start <= s.end}
    if not totals:
        return None
    return max(totals.items(), key=lambda kv: (kv[1], kv[0]))[0]


#: Below this, the filter pass costs more than the per-word scans it saves.
_WINDOW_MIN_SPANS = 8


def _window(spans: Sequence[VoiceSpan], lo: datetime, hi: datetime) -> Sequence[VoiceSpan]:
    """The spans that can touch `[lo, hi]` — deliberately inclusive, so it is a
    superset of what `_dominant` could pick and narrowing can never change an
    answer. ONE pass per segment ahead of the per-WORD scans below: `spans` is
    every turn in the SESSION, so without this a merge is O(words x turns) and a
    long diarized meeting spends tens of seconds in `_overlap`."""
    if len(spans) <= _WINDOW_MIN_SPANS:
        return spans
    return [s for s in spans if s.end >= lo and s.start <= hi]


def attribute_segment(
    seg: TranscriptionSegment,
    *,
    wav_start: datetime,
    slug: str,
    spans: Sequence[VoiceSpan],
) -> list[Piece]:
    """Attribute one segment. Returns one Piece unless its words cross a Voice
    change, in which case one per homogeneous run.

    An unattributable segment (no spans, or no overlap) keeps the plain `slug`,
    so an undiarized tap is byte-identical to the pre-ADR-0021 output.
    """
    abs_start = wav_start + timedelta(seconds=seg.start)
    abs_end = wav_start + timedelta(seconds=seg.end)
    if spans:
        # Words are normally inside the segment's own bounds, but nothing in the
        # Transcriber contract says so — take the union before narrowing.
        lo, hi = abs_start, abs_end
        for word in seg.words or ():
            lo = min(lo, wav_start + timedelta(seconds=word.start))
            hi = max(hi, wav_start + timedelta(seconds=word.end))
        spans = _window(spans, lo, hi)
    runs = _word_runs(seg, wav_start, spans) if (spans and seg.words) else []
    if len(runs) > 1:
        return [
            Piece(
                abs_start=wav_start + timedelta(seconds=words[0].start),
                abs_end=wav_start + timedelta(seconds=words[-1].end),
                speaker=_key(slug, label),
                text=" ".join(w.word.strip() for w in words if w.word.strip()),
            )
            for label, words in runs
        ]

    # One run, no words, or nothing overlapping: attribute whole, on the
    # segment's own bounds and text rather than a rejoin of the words. Without
    # words this is the only option — Voxtral emits none — so a crossing segment
    # goes to the dominant Voice rather than losing its text.
    label = runs[0][0] if runs else _dominant(abs_start, abs_end, spans)
    return [Piece(abs_start=abs_start, abs_end=abs_end, speaker=_key(slug, label), text=seg.text)]


def _key(slug: str, label: str | None) -> str:
    """`slug#<voice>`, or the bare slug when no Voice owns the span."""
    return voice_key(slug, label) if label else slug


def _word_runs(seg, wav_start, spans):
    """Consecutive words sharing a Voice, as `[(label | None, words), ...]`."""
    runs: list[tuple[str | None, list]] = []
    for word in seg.words:
        label = _dominant(
            wav_start + timedelta(seconds=word.start),
            wav_start + timedelta(seconds=word.end),
            spans,
        )
        if runs and runs[-1][0] == label:
            runs[-1][1].append(word)
        else:
            runs.append((label, [word]))
    return runs
