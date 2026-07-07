"""End-of-meeting pipeline — strip → transcribe → summarize as ONE session job.

The fourth orchestrator sibling (`batch_strip` / `batch_transcribe` /
`batch_summarize`): same FastAPI-free contract, but instead of owning a work
loop it chains the three siblings' `*_locked` cores under a single
`kind="pipeline"` JobTracker claim, so the "one heavy job per session" rule
holds across the whole chain — a concurrent trigger or a manual transcribe
gets `SessionBusy` (409) for the chain's full duration.

Everything is resolved OPERATOR-SIDE: the batch model from batch-model.txt
(falling back to the bundled default), the backend from the Recorder's launch
preference, the summarizer from the local-source catalog default. The
tap-token trigger carries no model / backend / summarizer / prompt fields by
design, so an untrusted request body can never reach a model loader or a Hub
download.

`start_pipeline` is fire-and-forget with a deterministic busy verdict: it
claims the job slot IN THE REQUEST PATH (raising `SessionBusy` before any
background work starts) and hands the claimed slot to a background task that
updates stage progress, records the outcome in `recorder.pipelines`, and
releases. This is the one caller that hand-rolls the claim/release ritual
`JobTracker.run` exists to absorb — `run` brackets a single block, but here
the claim happens in the request handler and the release in the task, two
different call frames. The foreign-claim guard stays structural: the release
only ever runs in the task spawned after OUR claim succeeded.

Stage failures abort the chain and are recorded — failing stage + the domain
error's message — never half-swallowed; the poll endpoint surfaces them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .batch_strip import StripSessionRequest, strip_session_locked
from .batch_summarize import (
    NoMergedTranscript,
    SummarizeSessionRequest,
    effective_summarizer_config,
    summarize_session_locked,
)
from .batch_transcribe import BatchSessionRequest, resolve_batch_model, transcribe_session_locked
from .recorder import JobState, Recorder, SessionBusy
from .session_merge import InvalidRange, NoUsableWavs, select_session_wavs
from .session_paths import resolve_session_dir
from .sessions import read_session_transcript
from .summarizers import load_summarizer


@dataclass(frozen=True)
class PipelineRequest:
    """Inputs for an end-of-meeting pipeline run. Deliberately just the
    session: every other knob (model, backend, summarizer, prompt) resolves
    from operator-side configuration, never from the (tap-token) caller."""

    session: str


# Strong references to in-flight pipeline tasks. asyncio only holds weak refs
# to tasks; without this a GC mid-meeting could silently drop the chain.
_RUNNING: set[asyncio.Task] = set()


async def start_pipeline(recorder: Recorder, req: PipelineRequest) -> asyncio.Task:
    """Claim the session's job slot and kick off the chain in the background.

    Raises `SessionBusy` (the route's 409) right here in the request path
    when the session already has a job — the claim is the race arbiter, so
    two concurrent triggers get one deterministic winner. On success the
    returned task runs the chain; callers that just want fire-and-forget
    (the trigger route) ignore it, tests await it."""
    model = resolve_batch_model()
    claimed = await recorder.jobs.claim(
        JobState(
            session=req.session,
            kind="pipeline",
            current=0,
            total=0,
            started_at=datetime.now(UTC),
            status="stripping",
            stage="strip",
            model=model,
        )
    )
    if not claimed:
        raise SessionBusy(f"session {req.session!r} already has a job in flight")

    recorder.pipelines.begin(req.session)
    task = asyncio.create_task(_run_claimed(recorder, req, model=model, backend=recorder.backend))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


async def _run_claimed(recorder: Recorder, req: PipelineRequest, *, model: str, backend: str) -> None:
    """Drive the three stages under the already-claimed slot, record the
    outcome, release. Never raises: a stage failure (any exception) aborts
    the chain and lands in `recorder.pipelines` as failed-at-stage — the
    poll endpoint's contract — plus a log line for the operator."""
    job = recorder.jobs.handle(req.session)
    stage = "strip"
    try:
        await job.update(stage="strip", status="stripping", current=0, current_file=None)
        await run_strip_stage(req, job=job)

        stage = "transcribe"
        await job.update(stage="transcribe", status="transcribing", current=0, current_file=None)
        await run_transcribe_stage(req, job=job, model=model, backend=backend)

        stage = "summarize"
        await job.update(stage="summarize", status="summarizing", total=1, current=0, current_file=None)
        await run_summarize_stage(req, job=job)
    except Exception as e:
        recorder.pipelines.finish_failed(req.session, stage=stage, error=str(e), error_kind=type(e).__name__)
        print(f"[tapscribe] pipeline {req.session}: FAILED at {stage}: {e}", flush=True)
    else:
        recorder.pipelines.finish_done(req.session)
        print(f"[tapscribe] pipeline {req.session}: done (strip → transcribe → summarize)", flush=True)
    finally:
        await recorder.jobs.release(req.session)


# ---------------------------------------------------------------------------
# Stages — each is one sibling orchestrator's pre-checks + its *_locked core.
# Module-level so tests fake whole stages the same way the transcribe suite
# fakes load_transcriber (patch it in tapscribe.transcribers, where
# lease_transcriber resolves it at call time).
# ---------------------------------------------------------------------------


async def run_strip_stage(req: PipelineRequest, *, job) -> dict[str, Any]:
    """`strip_session` minus the claim: glob the originals (NoUsableWavs when
    the session has none) and drive the strip core."""
    session_dir = resolve_session_dir(req.session)
    originals = sorted(session_dir.glob("*.wav"))
    if not originals:
        raise NoUsableWavs("no WAVs in this session to strip")
    await job.update(total=len(originals))
    return await strip_session_locked(StripSessionRequest(session=req.session), originals=originals)


async def run_transcribe_stage(req: PipelineRequest, *, job, model: str, backend: str) -> dict[str, Any]:
    """`transcribe_session` minus the claim, pinned to the stripped output of
    the stage before. A session whose strip found zero speech has no
    stripped/ dir at all, so the selection comes back empty → `NoUsableWavs`
    — the correct verdict for a meeting with nothing usable in it."""
    session_dir = resolve_session_dir(req.session)
    try:
        selection = select_session_wavs(session_dir, from_iso=None, to_iso=None, source="stripped")
    except ValueError as e:
        raise InvalidRange(str(e)) from e
    if not selection.wavs:
        raise NoUsableWavs("no usable WAVs after stripping — no speech detected in this session")
    treq = BatchSessionRequest(
        session=req.session,
        source="stripped",
        model=model,
        backend=backend,
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
        target_lang=None,
    )
    await job.update(total=len(selection.wavs))
    return await transcribe_session_locked(treq, selection=selection, job=job)


async def run_summarize_stage(req: PipelineRequest, *, job) -> dict[str, Any]:  # noqa: ARG001 — job kept for stage-signature symmetry; this stage has no inner progress loop
    """`summarize_session` minus the claim, over the merged transcript the
    stage before just wrote. The config resolves the full #84 chain —
    session-meta override → global summarizer default → built-ins (the
    bundled local source) — the same "operator defaults only" contract as
    the transcribe stage's model/prompt; the tap trigger never carries
    summarizer fields. Every resolved value was operator-side validated at
    write time, and `load_summarizer` re-checks the model allowlist."""
    cfg = await asyncio.to_thread(effective_summarizer_config, req.session)
    sreq = SummarizeSessionRequest(session=req.session, **cfg)
    summarizer = load_summarizer(
        source=sreq.source,
        command=sreq.command,
        model=sreq.model,
        max_tokens=sreq.max_tokens,
        base_url=sreq.base_url,
        api_key=sreq.api_key,
    )
    merged = await asyncio.to_thread(read_session_transcript, req.session)
    if not ((merged or {}).get("plain_text") or "").strip():
        raise NoMergedTranscript("the transcribe stage produced no merged transcript text to summarize")
    return await summarize_session_locked(sreq, summarizer=summarizer, merged=merged)
