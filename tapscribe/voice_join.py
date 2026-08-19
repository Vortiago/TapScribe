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
    """Label overlapping `[start, end)` most, or None. Ties break on label so
    the same input always attributes the same way."""
    best: tuple[float, str] | None = None
    for span in spans:
        seconds = _overlap(start, end, span)
        if seconds <= 0.0:
            continue
        if best is None or (seconds, span.label) > (best[0], best[1]):
            best = (seconds, span.label)
    return best[1] if best else None


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
