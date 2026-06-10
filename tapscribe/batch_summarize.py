"""Batch summarize — turn a session's merged transcript into a summary.

The post-transcription sibling of `batch_transcribe` / `batch_strip`: same
orchestrator shape (resolve inputs, bracket the session's job slot via
`recorder.jobs.run`, run the work off the event loop) and the same FastAPI-free
contract — domain errors out, the route maps them to HTTP codes. `SessionBusy`
lives in `tapscribe.recorder` next to JobTracker (the cm raises it); the "one
heavy job per session" rule now has three claimants (transcribe / strip /
summarize), all going through `run`.

This is the tracer-bullet slice (#82): the **Command** source only, no
persistence (the summary is returned to the caller and lost on reload) and no
saved config (source / command / prompt arrive per request). Persistence (#83)
and the global-default + per-session-override config (#84) layer on later
without changing this seam.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .recorder import Recorder
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
    model: str = ""  # local source: which catalog model to load (empty = default)
    max_tokens: int | None = None  # local source: OUTPUT cap (None = env default)
    prompt: str = DEFAULT_SUMMARY_PROMPT


async def summarize_session(recorder: Recorder, req: SummarizeSessionRequest) -> dict[str, Any]:
    """Summarize the session's merged transcript with the chosen `Summarizer`.

    Reads `session-transcript.json` (raising `NoMergedTranscript` when it's
    absent or its `plain_text` is empty), builds the `Summarizer` via the
    factory (raising `SummarizerUnavailable` for a misconfigured source — BEFORE
    the disk read + slot claim, so a bad command fails fast for free), then
    brackets the run in `recorder.jobs.run(..., kind="summarize")` — which
    raises `SessionBusy` when a transcribe / strip / summarize is already in
    flight (releasing nothing) and releases the slot on every other exit path.
    Returns the summary dict."""
    # Build the summarizer FIRST: it's pure (shlex parse + non-empty check, no
    # I/O), so a misconfigured command (empty template, unknown source) fails
    # fast — before the merged-transcript disk read, and without ever touching
    # the JobTracker slot.
    summarizer = load_summarizer(
        source=req.source, command=req.command, model=req.model, max_tokens=req.max_tokens
    )

    merged = await asyncio.to_thread(read_session_transcript, req.session)
    text = (merged or {}).get("plain_text") or ""
    if not text.strip():
        raise NoMergedTranscript(
            "this session has no merged transcript yet — transcribe it first, then summarize"
        )

    async with recorder.jobs.run(req.session, kind="summarize", total=1, status="summarizing"):
        result = await asyncio.to_thread(summarizer.summarize, text, prompt=req.prompt)

    print(
        f"[tapscribe] summarize {req.session}: source={result.source} "
        f"chars={len(result.summary)} took {result.took_ms} ms",
        flush=True,
    )
    return {"ok": True, "session": req.session, **result.to_mapping()}
