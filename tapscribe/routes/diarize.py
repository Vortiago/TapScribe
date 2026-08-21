"""Diarization: run it over a session, read back the Voices it found.

  POST  /api/sessions/{session}/diarize   split every multi-person tap into Voices
  GET   /api/sessions/{session}/voices    the Voices, per identity, with their mapping

The Voice→Person mapping PUT is deliberately NOT here — it can create a Person,
and `people.json` is mutated in exactly two places (`routes/people.py` and the
`/api/state` sync, both on the event loop, so they cannot race). A third writer
would break that invariant to satisfy a URL prefix; ADR-0018 groups by concern.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..batch_diarize import DiarizeSessionRequest, diarize_session
from ..recorder import Recorder
from ..roster import read_roster
from ..session_paths import resolve_session_dir
from ..text import parse_iso, voice_key
from ..voices import read_voices, voices_sig
from .body import json_body
from .deps import get_recorder

router = APIRouter()


@router.post("/api/sessions/{session}/diarize")
async def api_session_diarize(
    session: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
) -> dict[str, Any]:
    """Split every multi-person tap in the session into Voices. Thin HTTP shim
    over `batch_diarize.diarize_session`; the registered domain-error handlers
    map failures to status codes.

    `source` picks original or stripped audio, matching the transcribe body.
    Anything else falls back to `original` rather than 400ing: the field is a
    cost hint, and the absolute-time join works either way.
    """
    body = await json_body(req)
    source = body.get("source") if body.get("source") in ("original", "stripped") else "original"
    return await diarize_session(recorder, DiarizeSessionRequest(session=session, source=source))


@router.get("/api/sessions/{session}/voices")
async def api_session_voices(session: str) -> dict[str, Any]:
    """The Voices a diarization run found — the lazy body the Transcript stage's
    voicemap renders, keyed on `voices_sig` from the poll.

    Span COUNTS and total seconds, never the spans themselves: a long meeting is
    thousands of them and the panel shows one row per Voice.

    Deliberately NOT the operator's mapping. This body only changes when a
    diarize runs, which is what makes `voices_sig` a valid key; a mapping changes
    on a click, so carrying it here would make the body WRONG between the click
    and the next re-diarize. The mapping rides `/api/state`'s `session_meta`,
    which is 500 ms fresh — one fact, one owner (CLAUDE.md's signature hygiene).
    """
    return await asyncio.to_thread(_voices_view, session)


def _voices_view(session: str) -> dict[str, Any]:
    session_dir = resolve_session_dir(session)
    sidecar = read_voices(session_dir)
    roster = read_roster(session_dir)

    identities = []
    for identity, entry in sorted(sidecar.items()):
        rows = []
        for label, voice in sorted(entry["voices"].items()):
            rows.append(
                {
                    "key": voice_key(identity, label),
                    "label": label,
                    "spans": len(voice["spans"]),
                    "seconds": round(_speaking_seconds(voice["spans"]), 2),
                }
            )
        identities.append(
            {
                "identity": identity,
                "name": (roster.get(identity) or {}).get("name") or identity,
                "run_id": entry["run_id"],
                "voices": rows,
            }
        )
    return {
        "session": session,
        "voices_sig": voices_sig({i: e["run_id"] for i, e in sidecar.items()}),
        "identities": identities,
    }


def _speaking_seconds(spans: list[dict[str, str]]) -> float:
    total = 0.0
    for span in spans:
        start, end = parse_iso(span["start"]), parse_iso(span["end"])
        if start and end:
            total += max(0.0, (end - start).total_seconds())
    return total
