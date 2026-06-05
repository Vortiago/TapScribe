"""Batch summarize — turn a session's merged transcript into a summary.

The post-transcription sibling of `batch_transcribe` / `batch_strip`: same
orchestrator shape (resolve inputs, claim the session's `JobTracker` slot, run
the work off the event loop, release on every exit path) and the same
FastAPI-free contract — domain errors out, the route maps them to HTTP codes.
`SessionBusy` is shared with `batch_transcribe` because it's a JobTracker
concern, not a transcription-specific one; the "one heavy job per session" rule
now has three claimants (transcribe / strip / summarize).

This is the tracer-bullet slice (#82): the **Command** source only, no
persistence (the summary is returned to the caller and lost on reload) and no
saved config (source / command / prompt arrive per request). Persistence (#83)
and the global-default + per-session-override config (#84) layer on later
without changing this seam.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .batch_transcribe import SessionBusy
from .recorder import JobState, Recorder
from .sessions import read_session_transcript
from .summarizers import DEFAULT_SUMMARY_PROMPT, SummarizerError, load_summarizer


class NoMergedTranscript(SummarizerError):
    """The session has no merged transcript (or an empty one) to summarize —
    the operator must transcribe it first. Distinct from `SessionBusy`; routes
    map this to 422 and the view surfaces it as 'transcribe first'."""


@dataclass(frozen=True)
class SummarizeSessionRequest:
    """Inputs for summarizing a session. For the tracer-bullet slice the
    source / command / prompt arrive per request (no saved config yet); the
    prompt default lives HERE so `SummarizeSessionRequest(session=…)` is the
    canonical invocation — the same convention as `StripSessionRequest`'s knob
    defaults."""

    session: str
    source: str = "command"
    command: str = ""
    prompt: str = DEFAULT_SUMMARY_PROMPT


async def summarize_session(recorder: Recorder, req: SummarizeSessionRequest) -> dict[str, Any]:
    """Summarize the session's merged transcript with the chosen `Summarizer`.

    Reads `session-transcript.json` (raising `NoMergedTranscript` when it's
    absent or its `plain_text` is empty), builds the `Summarizer` via the
    factory (raising `SummarizerUnavailable` for a misconfigured source — BEFORE
    claiming, so a bad command doesn't claim+release a slot for nothing), claims
    the session's `JobTracker` slot under the new `summarize` kind (raising
    `SessionBusy` when a transcribe / strip / summarize is already in flight —
    WITHOUT releasing the foreign claim), runs the summarizer off the event
    loop, and returns the summary dict. The slot is released on every exit path
    via the `finally`."""
    # Build the summarizer FIRST: it's pure (shlex parse + non-empty check, no
    # I/O), so a misconfigured command (empty template, unknown source) fails
    # fast — before the merged-transcript disk read, and without ever touching
    # the JobTracker slot.
    summarizer = load_summarizer(source=req.source, command=req.command)

    merged = await asyncio.to_thread(read_session_transcript, req.session)
    text = (merged or {}).get("plain_text") or ""
    if not text.strip():
        raise NoMergedTranscript(
            "this session has no merged transcript yet — transcribe it first, then summarize"
        )

    claimed = await recorder.jobs.claim(
        JobState(
            session=req.session,
            kind="summarize",
            current=0,
            total=1,
            started_at=datetime.now(UTC),
            status="summarizing",
        )
    )
    if not claimed:
        raise SessionBusy("session is already busy (transcribe, strip, or summarize in flight)")

    try:
        result = await asyncio.to_thread(summarizer.summarize, text, prompt=req.prompt)
    finally:
        await recorder.jobs.release(req.session)

    print(
        f"[tapscribe] summarize {req.session}: source={result.source} "
        f"chars={len(result.summary)} took {result.took_ms} ms",
        flush=True,
    )
    return {"ok": True, "session": req.session, **result.to_mapping()}
