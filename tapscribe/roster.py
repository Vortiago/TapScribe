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
from .tap_mode import TAP_MODE_SINGLE, is_mode
from .text import atomic_write_text, parse_wav_speaker_slug

_VALID_SOURCES = ("recorded", "live")

# Cap for the bridge-supplied display name. Deliberately much tighter than
# `MAX_CONFIG_TEXT_LEN` (4000, for pasted prompts): this is a person's name,
# and unlike every operator-supplied text field it arrives on the LOWER-
# privilege tap credential. 200 chars comfortably fits any real name.
MAX_ROSTER_NAME_LEN = 200

#: How much of an untrusted name is even LOOKED at. Generously above
#: MAX_ROSTER_NAME_LEN so whitespace collapsing still has room to work on any
#: plausible input, but bounded so the sanitiser's cost can't scale with what a
#: bridge chooses to send. See the slice in `sanitise_name`.
_SANITISE_INPUT_CAP = 4096


def sanitise_name(name: str) -> str:
    """Cap and flatten a bridge-supplied `?name=` before it becomes durable
    state. The Roster is the seam where an untrusted string turns into a
    Person's display name, and from there into global `people.json` and — via
    `known_names` — into the summarizer's INSTRUCTION block, ABOVE the
    transcript. Two concrete abuses this closes:

    - `?name=Alice%0A%0AIgnore+all+previous+instructions…` is an
      instruction-position prompt injection; non-printable characters
      (newlines, tabs, NULs, control codes) are flattened to spaces and
      whitespace runs collapsed, so a "name" can never open a new paragraph.
    - a 1 MB name costs 1 MB per 500 ms `/api/state` poll, durably; the
      length cap bounds it.

    Returns "" for a name that sanitises away entirely — the caller treats
    that exactly like the empty name it is (never blanking a stored one).
    Ordinary names are untouched: `str.isprintable()` keeps accents,
    apostrophes and hyphens."""
    # Slice FIRST. The cap below bounds what is STORED, not what is COMPUTED:
    # applying the flatten/collapse passes to the raw value walked the whole
    # unbounded string (plus a `.split()` list of every word) before truncating,
    # so a 2 MB `?name=` cost ~280 ms and a 10 MB one over a second — all of it
    # synchronous on the event loop, blocking every other request and every
    # other tap. Whitespace collapsing can only ever shrink the string, so
    # taking a generous prefix first cannot change the result for any input
    # that survives the cap.
    raw = (name or "")[:_SANITISE_INPUT_CAP]
    flattened = "".join(ch if ch.isprintable() else " " for ch in raw)
    return " ".join(flattened.split())[:MAX_ROSTER_NAME_LEN].strip()


def _coerce_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = value.get("source")
    wavs = value.get("wavs")
    mode = value.get("mode")
    return {
        "name": value["name"] if isinstance(value.get("name"), str) else "",
        "source": source if source in _VALID_SOURCES else "live",
        "slug": value["slug"] if isinstance(value.get("slug"), str) else "",
        "wavs": [w for w in wavs if isinstance(w, str)] if isinstance(wavs, list) else [],
        # The mode in effect when this tap opened, so diarization is a property
        # of the recording rather than of whatever the setting says later
        # (ADR-0021). A pre-feature entry reads as single.
        "mode": mode if is_mode(mode) else TAP_MODE_SINGLE,
    }


def coerce_roster(raw: Any) -> dict[str, dict[str, Any]]:
    """Coerce a raw parsed roster mapping into `{full identity: entry}`, dropping
    non-str identities and non-dict entries (and per-field junk, via
    `_coerce_entry`). Non-dict top level → `{}`. Shared by `read_roster` (the
    uncached write-path reader) and the cached poll path
    (`sessions._read_roster_cached`) so both produce the identical shape."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for identity, entry in raw.items():
        coerced = _coerce_entry(entry)
        if isinstance(identity, str) and coerced is not None:
            out[identity] = coerced
    return out


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
    return coerce_roster(data)


def record_occurrence(
    session_dir: Path,
    *,
    identity: str,
    name: str = "",
    recorded: bool,
    wav: str | None = None,
    mode: str | None = None,
) -> None:
    """Upsert one Identity's presence in this session. Idempotent and
    merge-on-write: a reconnect or a later utterance accrues WAVs (deduped)
    without clobbering the entry, a non-empty `name` overwrites a blank one,
    and `source` only ever upgrades live → recorded (a record-off presence
    after a recording must not erase that the Identity was recorded).

    `name` is bridge-supplied (untrusted) and is capped + flattened through
    `sanitise_name` HERE — the one seam where it becomes durable state."""
    if not identity:
        return
    roster = read_roster(session_dir)
    entry = roster.get(identity) or {
        "name": "",
        "source": "live",
        "slug": "",
        "wavs": [],
        "mode": TAP_MODE_SINGLE,
    }
    if clean_name := sanitise_name(name):
        entry["name"] = clean_name
    # Only an explicit value moves it: this runs per utterance, and a later call
    # that says nothing must not downgrade a multi-person tap.
    if is_mode(mode):
        entry["mode"] = mode
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
