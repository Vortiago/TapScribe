"""Per-session Roster — the durable record of which Identity appeared in a
session, with the bridge-sent display name, whether it was recorded or
live-only, and the WAV(s) it produced.

This is the seam that makes the FULL (untruncated) bridge identity recoverable
for a recorded occurrence. The WAV filename only carries
`safe_name(identity)[:10]` (see `parse_wav_speaker_ident`), which is lossy and
collision-prone, so it can't be the cross-session join key the People Registry
(`people.py`) needs. The tap path writes a roster entry at WS open; the
registry and per-session name resolution read it (CONTEXT.md: Person · Identity
· Roster · People Registry; ADR-0009).

One sidecar per session — `<session_dir>/session-roster.json`, machine-written,
deliberately separate from the operator-editable `session-meta.json`:

    { "<full identity>": {
        "name":   "<bridge display name>",
        "source": "recorded" | "live",
        "slug":   "<name-slug as it appears in WAV filenames>",
        "wavs":   ["<wav filename>", ...]
    }, ... }

`slug` bridges the slug-keyed merged transcript (`parse_wav_speaker_slug`) to
the Identity-keyed registry: name resolution maps slug → identity → Person.

Concurrency: `record_occurrence` does a synchronous read-modify-write with no
`await` in between, so under the single asyncio event loop two concurrent taps
can't interleave it (cooperative scheduling runs the sync body to completion).
`atomic_write_text` adds crash-safety on top.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .session_paths import FILENAME_ROSTER_JSON
from .text import atomic_write_text, parse_wav_speaker_slug

_VALID_SOURCES = ("recorded", "live")


def _coerce_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = value.get("source")
    wavs = value.get("wavs")
    return {
        "name": value["name"] if isinstance(value.get("name"), str) else "",
        "source": source if source in _VALID_SOURCES else "live",
        "slug": value["slug"] if isinstance(value.get("slug"), str) else "",
        "wavs": [w for w in wavs if isinstance(w, str)] if isinstance(wavs, list) else [],
    }


def read_roster(session_dir: Path) -> dict[str, dict[str, Any]]:
    """The session's roster as `{full identity: entry}`. Missing, torn, or
    non-dict top level → `{}` so a single bad file never crashes the poll."""
    path = session_dir / FILENAME_ROSTER_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing file (OSError) or torn/garbage JSON (ValueError): the roster
        # is best-effort durable state, recovered on the next occurrence — a
        # read failure must degrade to "no roster", never propagate.
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for identity, entry in data.items():
        coerced = _coerce_entry(entry)
        if isinstance(identity, str) and coerced is not None:
            out[identity] = coerced
    return out


def record_occurrence(
    session_dir: Path,
    *,
    identity: str,
    name: str = "",
    recorded: bool,
    wav: str | None = None,
) -> None:
    """Upsert one Identity's presence in this session. Idempotent and
    merge-on-write: a reconnect or a later utterance accrues WAVs (deduped)
    without clobbering the entry, a non-empty `name` overwrites a blank one,
    and `source` only ever upgrades live → recorded (a record-off presence
    after a recording must not erase that the Identity was recorded)."""
    if not identity:
        return
    roster = read_roster(session_dir)
    entry = roster.get(identity) or {"name": "", "source": "live", "slug": "", "wavs": []}
    if name:
        entry["name"] = name
    if recorded:
        entry["source"] = "recorded"
        if wav:
            if wav not in entry["wavs"]:
                entry["wavs"].append(wav)
            if not entry["slug"]:
                slug = parse_wav_speaker_slug(wav)
                if slug:
                    entry["slug"] = slug
    roster[identity] = entry
    session_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        session_dir / FILENAME_ROSTER_JSON,
        json.dumps(roster, indent=2, ensure_ascii=False),
    )
