"""`session-voices.json` — ONE owner for the Voice sidecar's format and I/O.

A [Voice](../CONTEXT.md#voice) is one speaker the diarizer distinguishes inside
one multi-person tap, in one session. This module is the single authority over
the file that records them: the shape gate, the read, the atomic write, and the
prune that runs when the audio underneath a span goes away (ADR-0021).

Machine-written, so it sits on the `session-roster.json` side of the line
ADR-0009 draws — it holds no operator input. The operator's Voice→Person
mapping lives on `session-meta.json`, which is a different file with a
different writer, deliberately.

Shape, keyed by FULL identity (never the truncated WAV slug):

    {"<identity>": {"run_id": "...", "voices": {"A": {"spans": [...]}}}}

`run_id` is stamped **per identity**, not per file: re-diarizing one tap must
not invalidate another's mappings, and absorbing two sessions has to be able to
carry both sides' runs. Spans are ISO-8601 instants in ABSOLUTE session time,
so the merge-time join is a plain interval comparison against a segment's
`abs_start`/`abs_end` and never has to know which WAV a span came from.

Seam convention: consumers reach the owner through the module attribute —
`import tapscribe.voices as voices` — NOT via `from`-import, so monkeypatches
in tests propagate.
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
    """Keep only well-formed `{"start": str, "end": str}` pairs, in order."""
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
    """Coerce a raw parsed mapping into `{full identity: entry}`, dropping
    non-str identities and entries with no usable Voice. Non-dict top level →
    `{}`. Shared by `read_voices` and the cached poll path so both produce the
    identical shape."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for identity, entry in raw.items():
        coerced = _coerce_entry(entry)
        if isinstance(identity, str) and identity and coerced is not None:
            out[identity] = coerced
    return out


def read_voices(session_dir: Path) -> dict[str, dict[str, Any]]:
    """The session's Voices as `{full identity: entry}`. Missing, torn, or
    non-dict top level → `{}`: an undiarized session is the normal case, not an
    error, and a bad file must never crash the poll."""
    path = session_dir / FILENAME_VOICES_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing file (OSError) or torn/garbage JSON (ValueError). The sidecar
        # is regenerable — re-running diarize rebuilds it — so a read failure
        # degrades to "no Voices" (every segment keeps its plain identity key)
        # rather than propagating into the merge or the 500 ms poll.
        return {}
    return coerce_voices(data)


def write_voices(session_dir: Path, data: Mapping[str, Any]) -> None:
    """Atomic whole-file write via the shared helper (tempfile + os.replace, no
    temp droppings on success)."""
    atomic_write_text(
        session_dir / FILENAME_VOICES_JSON,
        json.dumps(dict(data), indent=2, ensure_ascii=False),
    )


def fold_voices(
    target: Mapping[str, Any], source: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Merge one session's Voices into another's for `absorb_session`.

    Returns `(merged, collided)`. Disjoint identities are carried across
    untouched — two sessions that recorded different people merge cleanly.

    An identity present on BOTH sides is dropped from both and named in
    `collided`. A Voice label is session-LOCAL: Monday's `sysaudio` Voice `A`
    and Tuesday's are different humans, and nothing on disk distinguishes them,
    so keeping either side would silently attribute one person's words to
    another. Dropping leaves the tap unattributed — `Speaker A` again — which is
    recoverable by re-diarizing the merged session; a wrong name is not.

    Absorb is the one operation that ADDS audio to a session's time range, which
    is what makes it the one that can invalidate a Voice. Deleting audio cannot:
    a span only ever attributes segments falling inside it, so removing those
    segments leaves the span inert rather than wrong.
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
    """Replace ONE identity's Voices with the result of one diarization run.

    Read-modify-write with no `await` in between, mirroring
    `roster.record_occurrence`. Scoped to the one identity so re-diarizing a
    single tap leaves every sibling's `run_id` — and therefore every mapping
    made against it — untouched.
    """
    current = read_voices(session_dir)
    voices = {
        label: {"spans": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in windows]}
        for label, windows in spans.items()
    }
    current[identity] = {"run_id": run_id, "voices": voices}
    write_voices(session_dir, current)
