"""Batch summarize — turn a session's merged transcript into a summary.

The post-transcription sibling of `batch_transcribe` / `batch_strip`: same
orchestrator shape (resolve inputs, bracket the session's job slot via
`recorder.jobs.run`, run the work off the event loop) and the same FastAPI-free
contract — domain errors out, the route maps them to HTTP codes. `SessionBusy`
lives in `tapscribe.recorder` next to JobTracker (the cm raises it); the "one
heavy job per session" rule has four claimants (transcribe / strip / summarize
via `run`, plus the end-of-meeting pipeline, which hand-rolls the claim because
claim and release live in different call frames — see `batch_pipeline`).

All three summarizer sources are wired: `load_summarizer` dispatches
command (#82) / local (#86) / api (#85). The summary is persisted to
session-summary.json next to the merged transcript and read back lazily (#83),
and the operator's saved defaults resolve through `effective_summarizer_config`
below (#84) — session-meta override › global summarizer.json › built-ins.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .recorder import Recorder
from .sessions import (
    known_names_for_session,
    read_session_meta,
    read_session_transcript,
    write_session_summary,
)
from .summarizers import DEFAULT_SUMMARY_PROMPT, SummarizerError, load_summarizer
from .text import read_summarizer_config


class NoMergedTranscript(SummarizerError):
    """The session has no merged transcript (or an empty one) to summarize —
    the operator must transcribe it first. Distinct from `SessionBusy`; routes
    map this to 422 and the view surfaces it as 'transcribe first'."""


@dataclass(frozen=True)
class SummarizeSessionRequest:
    """Inputs for summarizing a session. The operator's saved defaults
    (source / command / model / prompt, #84) live in `config/summarizer.json`
    and are resolved via `effective_summarizer_config`; direct callers may
    still pass source / command / prompt explicitly per request.

    The knob defaults live HERE (the same convention as `StripSessionRequest`)
    and deliberately MIRROR `effective_summarizer_config`'s built-ins, so
    `SummarizeSessionRequest(session=…)` names the same source the routed and
    pipeline paths resolve to. `source="command"` used to be the default, which
    made that bare invocation raise `SummarizerUnavailable` before any work —
    the command source needs a non-empty `command` and there is no default one."""

    session: str
    # NOTE (#84): the routed/pipeline paths always pass explicit values resolved
    # via `effective_summarizer_config`; these dataclass defaults only apply to
    # direct callers, and match that resolver's built-ins.
    source: str = "local"
    command: str = ""
    model: str = ""  # local/api source: which model to use (empty = default)
    max_tokens: int | None = None  # local source: OUTPUT cap; api: omit when None
    prompt: str = DEFAULT_SUMMARY_PROMPT
    base_url: str = ""  # api source: endpoint base URL
    api_key: str = ""  # api source: write-only bearer token


def effective_summarizer_config(session: str) -> dict[str, Any]:
    """Resolve the summarizer config for `session` (#84): the session-meta
    override (source + prompt — empty falls back, a session can't assert "no
    prompt") over the global summarizer default over built-ins ("local", the
    bundled offline source, and `DEFAULT_SUMMARY_PROMPT`). The per-source
    fields (command / model / max_tokens) come from the global layer only —
    the per-session override is deliberately just source + prompt.

    `batch_transcribe._effective_prompt_hotwords`'s sibling; the summarize
    route uses it for body fields the caller omitted, the end-of-meeting
    pipeline for everything (the tap trigger carries no summarizer fields by
    design — operator defaults only)."""
    meta = read_session_meta(session)
    g = read_summarizer_config()
    return {
        "source": (meta.get("summary_source") or "").strip() or (g["source"] or "").strip() or "local",
        "prompt": (meta.get("summary_prompt") or "").strip()
        or (g["prompt"] or "").strip()
        or DEFAULT_SUMMARY_PROMPT,
        "command": g["command"],
        "model": g["model"],
        "max_tokens": g["max_tokens"],
        "base_url": g["base_url"],
        "api_key": g["api_key"],
    }


async def summarize_session(recorder: Recorder, req: SummarizeSessionRequest) -> dict[str, Any]:
    """Summarize the session's merged transcript with the chosen `Summarizer`.

    Reads `session-transcript.json` (raising `NoMergedTranscript` when it's
    absent or its `plain_text` is empty), builds the `Summarizer` via the
    factory (raising `SummarizerUnavailable` for a misconfigured source — BEFORE
    the disk read + slot claim, so a bad command fails fast for free), then
    brackets the run in `recorder.jobs.run(..., kind="summarize")` — which
    raises `SessionBusy` when a transcribe / strip / summarize is already in
    flight (releasing nothing) and releases the slot on every other exit path. Persists the summary to session-summary.json (atomic, one current summary
    per session) before returning it."""
    # Build the summarizer FIRST: it's pure (shlex parse + non-empty check, no
    # I/O), so a misconfigured command (empty template, unknown source) fails
    # fast — before the merged-transcript disk read, and without ever touching
    # the JobTracker slot.
    summarizer = load_summarizer(
        source=req.source,
        command=req.command,
        model=req.model,
        max_tokens=req.max_tokens,
        base_url=req.base_url,
        api_key=req.api_key,
    )

    merged = await asyncio.to_thread(read_session_transcript, req.session)
    text = (merged or {}).get("plain_text") or ""
    if not text.strip():
        raise NoMergedTranscript(
            "this session has no merged transcript yet — transcribe it first, then summarize"
        )

    async with recorder.jobs.run(req.session, kind="summarize", total=1, status="summarizing"):
        persisted = await summarize_session_locked(req, summarizer=summarizer, merged=merged)

    print(
        f"[tapscribe] summarize {req.session}: source={persisted['source']} "
        f"chars={len(persisted['summary'])} took {persisted['took_ms']} ms",
        flush=True,
    )
    return {"ok": True, "session": req.session, **persisted}


async def summarize_session_locked(
    req: SummarizeSessionRequest, *, summarizer: Any, merged: dict[str, Any] | None
) -> dict[str, Any]:
    """The summarize core: run the prepared `Summarizer` over the merged
    transcript's text and persist session-summary.json. Assumes the caller
    already holds the session's job slot — claims and releases NOTHING, so
    the end-of-meeting pipeline can run it as one stage of a single
    `kind="pipeline"` claim. Returns the persisted dict (result mapping +
    `summarized_at` + the source transcript's `transcribed_at`)."""
    text = (merged or {}).get("plain_text") or ""
    # Hint the summarizer with the known-people names (this session's participants
    # first, then people the registry learned across previous meetings) so it can
    # map the transcript's lossy speaker slugs + ASR-mangled spoken names back to
    # the canonical spellings. Best-effort: a read failure degrades to no hint.
    # Both callers reach the summarizer through here (the /summarize route AND the
    # end-of-meeting pipeline), so wiring it in the locked core covers both.
    names = await asyncio.to_thread(known_names_for_session, req.session)
    result = await asyncio.to_thread(summarizer.summarize, text, prompt=req.prompt, names=names)
    # Persist next to the merged transcript (#83) — only after a successful
    # run, so a failed re-generate can't clobber the stored summary. The
    # `summarized_at` stamp is the slim listing marker's re-fetch signal.
    persisted = {
        **result.to_mapping(),
        "summarized_at": datetime.now(UTC).isoformat(),
        # Carry the source transcript's stamp (#94) so the Summary view can flag
        # a summary that predates a later re-transcribe. `merged` is the
        # session-transcript.json dict the caller read.
        "transcribed_at": (merged or {}).get("transcribed_at"),
    }
    await asyncio.to_thread(write_session_summary, req.session, persisted)
    return persisted
