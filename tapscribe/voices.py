"""`session-voices.json` — ONE owner for the Voice sidecar's format and I/O.

Machine-written, so it sits on the `session-roster.json` side of ADR-0009's
line. The operator's Voice→Person mapping lives on `session-meta.json`.

Shape, keyed by FULL identity (never the truncated WAV slug):

    {"<identity>": {"run_id": "...", "voices": {"A": {"spans": [...]}}}}

`run_id` is stamped per identity: re-diarizing one tap must not supersede a
sibling's mappings. Spans are ISO-8601 instants in absolute session time, so
the merge-time join is an interval comparison and never needs to know which WAV
a span came from. ADR-0021.

Reach this module by attribute — `import tapscribe.voices as voices` — so test
monkeypatches propagate.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .session_paths import FILENAME_VOICES_JSON
from .text import atomic_write_text

#: One `{"start": iso, "end": iso}` pair, as stored.
Span = dict[str, str]


def _coerce_spans(value: Any) -> list[Span]:
    if not isinstance(value, list):
        return []
    out: list[Span] = []
    for span in value:
        if not isinstance(span, dict):
            continue
        start, end = span.get("start"), span.get("end")
        if isinstance(start, str) and isinstance(end, str) and start and end:
            out.append({"start": start, "end": end})
    return out


def _coerce_entry(value: Any) -> dict[str, Any] | None:
    """One identity's block, or None when it carries no usable Voice."""
    if not isinstance(value, dict):
        return None
    raw = value.get("voices")
    if not isinstance(raw, dict):
        return None
    voices: dict[str, dict[str, Any]] = {}
    for label, body in raw.items():
        if not isinstance(label, str) or not label:
            continue
        spans = _coerce_spans(body.get("spans") if isinstance(body, dict) else None)
        if spans:
            voices[label] = {"spans": spans}
    if not voices:
        return None
    run_id = value.get("run_id")
    return {"run_id": run_id if isinstance(run_id, str) else "", "voices": voices}


def coerce_voices(raw: Any) -> dict[str, dict[str, Any]]:
    """Raw parsed mapping → `{full identity: entry}`, dropping junk. Shared by
    `read_voices` and the cached poll path so both produce one shape."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for identity, entry in raw.items():
        coerced = _coerce_entry(entry)
        if isinstance(identity, str) and identity and coerced is not None:
            out[identity] = coerced
    return out


def read_voices(session_dir: Path) -> dict[str, dict[str, Any]]:
    """`{full identity: entry}`. Missing, torn, or non-dict → `{}`: an
    undiarized session is the normal case, and the poll must not crash on a bad
    file. The sidecar is regenerable by re-running diarize."""
    path = session_dir / FILENAME_VOICES_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing (OSError) or torn (ValueError) — degrade to "no Voices", which
        # leaves every segment on its plain identity key.
        return {}
    return coerce_voices(data)


def write_voices(session_dir: Path, data: Mapping[str, Any]) -> None:
    """Atomic whole-file write."""
    atomic_write_text(
        session_dir / FILENAME_VOICES_JSON,
        json.dumps(dict(data), indent=2, ensure_ascii=False),
    )


def fold_voices(
    target: Mapping[str, Any], source: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Merge two sessions' Voices for `absorb_session` → `(merged, collided)`.

    An identity on both sides is dropped from both and named in `collided`: a
    Voice label is session-local, so each side's `A` is a different human and
    nothing on disk says so. Dropping leaves the tap unattributed, recoverable
    by re-diarizing; keeping either side puts one person's name on another's
    words.
    """
    collided = {i for i in target if i in source}
    merged = {i: dict(e) for i, e in target.items() if i not in collided}
    merged.update({i: dict(e) for i, e in source.items() if i not in collided})
    return merged, collided


def record_voices(
    session_dir: Path,
    *,
    identity: str,
    run_id: str,
    spans: Mapping[str, Iterable[tuple[datetime, datetime]]],
) -> None:
    """Replace ONE identity's Voices with one diarization run's result.

    Read-modify-write with no `await` between, like `roster.record_occurrence`.
    Scoped to one identity so a sibling's `run_id` — and every mapping made
    against it — survives.
    """
    current = read_voices(session_dir)
    voices = {
        label: {"spans": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in windows]}
        for label, windows in spans.items()
    }
    current[identity] = {"run_id": run_id, "voices": voices}
    write_voices(session_dir, current)
