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
from .text import atomic_write_text, parse_iso

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
        if not (isinstance(start, str) and isinstance(end, str) and start and end):
            continue
        try:
            # Parse to VALIDATE, not to store. Shape alone is not enough: the
            # merge-time join parses these instants INSIDE `merge_session`, so an
            # unparseable one would fail a whole transcribe job instead of
            # degrading to "no Voices" the way this module promises.
            parse_iso(start)
            parse_iso(end)
        except ValueError:
            continue
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
    return {"run_id": _run_id_of(value), "voices": voices}


def _run_id_of(entry: Any) -> str:
    """ONE spelling of "this entry's run stamp", so the full coercion and the
    poll's `run_ids` shortcut cannot disagree. A non-string reads as unstamped:
    name resolution compares it against a mapping's stamp, and a truthy
    non-string would silently discard a valid Voice→Person mapping."""
    run_id = entry.get("run_id") if isinstance(entry, dict) else None
    return run_id if isinstance(run_id, str) else ""


def coerce_voices(raw: Any) -> dict[str, dict[str, Any]]:
    """Raw parsed mapping → `{full identity: entry}`, dropping junk. The full
    coercion, for `read_voices`; the poll reads `run_ids` instead."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for identity, entry in raw.items():
        coerced = _coerce_entry(entry)
        if isinstance(identity, str) and identity and coerced is not None:
            out[identity] = coerced
    return out


def run_ids(raw: Any) -> dict[str, str]:
    """`{identity: run_id}` from a raw parsed sidecar — the poll's whole
    interest in this file. Deliberately NOT `coerce_voices(...)` then project:
    that walks and rebuilds every span dict on every tick to read one string
    per identity, which is O(spans) for an O(identities) answer."""
    if not isinstance(raw, dict):
        return {}
    return {
        identity: _run_id_of(entry)
        for identity, entry in raw.items()
        if isinstance(identity, str) and identity and isinstance(entry, dict)
    }


def voices_sig(runs: Mapping[str, str]) -> str:
    """One string that changes whenever any identity's diarization run does.

    The Transcript stage keys its lazy Voices body on this: the runs themselves
    are a join input `attach_people` consumes and drops, so without a projection
    the dashboard has no way to learn a diarize finished. Sorted, so a dict
    reordering is not a change. Empty for an undiarized session.
    """
    return ";".join(f"{identity}:{run}" for identity, run in sorted(runs.items()))


def _load(session_dir: Path) -> dict[str, dict[str, Any]]:
    """The sidecar, parsed and coerced. Missing or torn → `{}` (nothing in a
    torn file is recoverable); every OTHER `OSError` RAISES, so a caller can
    tell "there are no Voices" from "I could not read them" — the distinction a
    read-modify-write needs and a display read does not."""
    try:
        return coerce_voices(json.loads((session_dir / FILENAME_VOICES_JSON).read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return {}


def read_voices(session_dir: Path) -> dict[str, dict[str, Any]]:
    """`{full identity: entry}`. Unreadable for ANY reason → `{}`: an undiarized
    session is the normal case, and the poll must not crash on a bad file. The
    sidecar is regenerable by re-running diarize."""
    try:
        return _load(session_dir)
    except OSError:
        # A permission change or a concurrent delete mid-read. Degrade to "no
        # Voices", which leaves every segment on its plain identity key.
        return {}


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
    collided = target.keys() & source.keys()
    merged = {i: e for i, e in (*target.items(), *source.items()) if i not in collided}
    return merged, set(collided)


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

    Through `_load`, not `read_voices`: the reader degrades an unreadable file to
    `{}`, which is right for display and destructive as the base of a whole-file
    write — a transient `OSError` (a Windows sharing violation against the poll's
    concurrent read) would delete every sibling identity's Voices. Letting it
    raise fails the diarize instead, which is re-runnable.
    """
    current = _load(session_dir)
    voices = {
        label: {"spans": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in windows]}
        for label, windows in spans.items()
    }
    current[identity] = {"run_id": run_id, "voices": voices}
    write_voices(session_dir, current)
