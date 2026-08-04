"""Sessions: the listing, the lazy bodies, and the destructive housekeeping.

  GET     /sessions                              legacy simple listing
  GET     /api/session-meta/{session}            the session's meta
  PUT     /api/session-meta/{session}            write the session's meta
  GET     /api/sessions/{session}/transcript     the full merged transcript (lazy)
  GET     /api/sessions/{session}/files          the full per-WAV listing (lazy)
  GET     /api/sessions/{session}/summary        the full persisted summary (lazy)
  GET     /api/search                            cross-session transcript search
  POST    /api/sessions/prune-empty              delete every empty session folder
  POST    /api/sessions/bulk-reclaim-audio       reclaim audio from old sessions
  POST    /api/sessions/{target}/absorb          fold another session into this one
  DELETE  /api/sessions/{session}/audio          delete audio, keep transcript + meta
  DELETE  /api/sessions/{session}                delete the folder

Not here: the strip-silence routes on this prefix
(`POST /api/sessions/{s}/strip-silence`, `DELETE /api/sessions/{s}/stripped`)
live in `routes/strip.py` with the preview they must not drift from, and the
pipeline trigger lives in `routes/tap.py` beside its tap-bearer twin.

The three lazy bodies are the companions to `/api/state`'s slim markers: the
dashboard fetches each once per content stamp instead of shipping it on every
poll tick. Every destructive route crosses `refuse_current_or_busy`
(`routes/guards.py`) and then holds the session's job slot where the walk itself
must not race a batch job.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..recorder import Recorder, SessionBusy, open_wav_names
from ..session_maintenance import (
    SessionDeleteError,
    absorb_session,
    delete_session_audio,
    prune_empty_sessions,
    reclaim_audio_older_than,
)
from ..session_paths import resolve_session_dir
from ..sessions import (
    gather_sessions,
    read_session_files,
    read_session_meta,
    read_session_summary,
    read_session_transcript,
    search_transcripts,
    write_session_meta,
)
from ..tap_registry import release_destruct, try_claim_destruct
from .body import json_body
from .deps import get_recorder
from .guards import ops_log, refuse_current_or_busy

router = APIRouter()


@router.get("/sessions")
async def list_sessions_simple(recorder: Recorder = Depends(get_recorder)):
    """Legacy simple listing."""
    active_streams = await recorder.streams.snapshot()
    return gather_sessions(
        current_session=recorder.session_start,
        jobs={k: asdict(v) for k, v in recorder.jobs.snapshot().items()},
        # Same open-WAV masking as /api/state so files_sig stays consistent
        # across the two endpoints during a recording.
        open_wavs=open_wav_names(active_streams),
    )


@router.post("/api/sessions/prune-empty")
async def api_sessions_prune_empty(recorder: Recorder = Depends(get_recorder)):
    """Delete every session folder that has zero WAVs, no merged
    transcript, and no operator-set label. Skips the CURRENT session and any
    session with a live tap materialising its folder."""
    # Synchronous on purpose, NOT `asyncio.to_thread` like this module's other
    # destructive walks — `prune_empty_sessions` owns that requirement.
    result = prune_empty_sessions(recorder.session_start)
    print(f"[tapscribe] pruned {result['count']} empty sessions", flush=True)
    return {"ok": True, **result}


@router.delete("/api/sessions/{session}/audio")
async def api_session_audio_delete(session: str, recorder: Recorder = Depends(get_recorder)):
    """Delete ALL of a session's audio (original WAVs + stripped/ + per-WAV
    transcript-cache sidecars) to reclaim disk. KEEPS the merged
    session-transcript + session-meta. Refuses the CURRENT session, any
    session with a transcribe/strip job in flight, or a session with a tap
    open on it (a live ActiveStream or an in-flight mark)."""
    await refuse_current_or_busy(recorder, session, current=session, action="delete audio from")
    resolve_session_dir(session)
    # Hold the session's job slot for the walk's duration: the pre-flight
    # above is check-then-act (a transcribe/strip could claim the freed slot
    # between the check and the thread hop and race the unlink walk), so the
    # delete claims the SAME slot the batch jobs use — a job arriving
    # mid-delete gets the standard SessionBusy 409, and vice versa. run()
    # releases on every exit path, so the surviving session's slot is always
    # freed again.
    async with recorder.jobs.run(session, kind="delete", total=1):
        # Offload the filesystem walk (many WAVs + .transcripts/ dirs) so the
        # ~1 Hz /api/state poll stays responsive — same as strip-silence.
        summary = await asyncio.to_thread(_delete_audio_worker, session)
    ops_log(
        f"deleted audio from session {session}: "
        f"{summary['wavs_deleted']} wavs, {summary['bytes_freed']} bytes freed"
    )
    return {"ok": True, **summary}


def _delete_audio_worker(session: str) -> dict:
    """Worker for `DELETE /api/sessions/{session}/audio`: claims the
    destruction guard, runs `delete_session_audio`, releases the guard.
    Raises `SessionBusy` (409) when a tap is open on `session`."""
    if not try_claim_destruct(session):
        raise SessionBusy("delete aborted: a tap is open on this session")
    try:
        return delete_session_audio(session)
    finally:
        release_destruct(session)


@router.post("/api/sessions/bulk-reclaim-audio")
async def api_bulk_reclaim_audio(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Bulk reclaim audio from old sessions. Walks the recordings archive
    and, for every session older than ``older_than_days`` with a merged
    transcript, reclaims its audio (originals + ``stripped/`` go; the
    merged transcript + meta stay).

    Body (JSON), like the sibling ``absorb`` route — NOT query params — so a
    dashboard POSTing ``{older_than_days, execute}`` can't silently arrive as
    a no-op ``execute=False`` (a bare-annotated scalar would resolve as a
    Query param and drop a JSON body):

        ``{"older_than_days": <int > 0>, "execute": <bool>}``

    ``execute`` absent/false (preview): lists eligible sessions and their
    reclaimable byte counts without deleting anything. ``execute: true``
    performs the reclaim across all eligible sessions.

    Returns ``{ok, sessions, total_bytes, failed}``. Refuses
    ``older_than_days <= 0`` (400). The current session, any session with a
    transcribe/strip job in flight, and any session with a live tap writing to
    it (an ActiveStream) are all excluded, so live/busy audio is never touched.
    """
    body = await json_body(req)
    older_than_days = body.get("older_than_days")
    if not isinstance(older_than_days, int) or isinstance(older_than_days, bool) or older_than_days <= 0:
        raise HTTPException(400, "older_than_days must be a positive integer")
    # Strict: only a literal JSON `true` triggers a real delete — `bool("false")`
    # is True, so never coerce an arbitrary body value on a destructive flag.
    execute = body.get("execute") is True

    # Exclude what the recorder-free reclaim fn can't see: a session with a
    # transcribe/strip job in flight (recorder.jobs) or a live tap writing to
    # it (recorder.streams). Deleting their WAVs mid-job would corrupt output;
    # the current session is excluded inside the fn. Mirrors the single-item
    # DELETE route's refuse_current_or_busy, but as an EXCLUSION (skip the
    # busy ones) rather than refusing the whole bulk op, and minus that
    # preflight's in-flight-mark branch: eligibility here needs a WAV older
    # than the cutoff, and a fresh-record open's WAV is stamped now(), so a
    # marked session is never eligible on the mark's account.
    busy = set(recorder.jobs.snapshot())
    busy |= {s.session for s in await recorder.streams.snapshot()}

    result = await asyncio.to_thread(
        reclaim_audio_older_than,
        recorder.session_start,
        older_than_days,
        execute=execute,
        exclude_sessions=frozenset(busy),
        busy_check=recorder.jobs.get,
    )
    return {"ok": True, **result}


@router.post("/api/sessions/{target}/absorb")
async def api_session_absorb(
    target: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
):
    """Fold another session into this one. The named `target` keeps its
    identity (folder, label, aliases); the source session's WAVs + sidecars
    are moved in, source aliases fill any gaps in target's, and the source
    folder is deleted.

    Refuses if the source is the currently-recording session — rotate
    first if you want to absorb the live one into a previous folder.
    Refuses if either side has an in-flight transcribe / strip job, or if the
    SOURCE has a tap open on it (a live ActiveStream or an in-flight mark). The
    target may freely be the live session or have a tap open: absorb only ever
    moves source's files in, it never rewrites target's own files.
    """
    body = await json_body(req)
    source = body.get("source") or ""
    if not isinstance(source, str) or not source:
        raise HTTPException(400, "source session id required")
    if source == target:
        raise HTTPException(400, "cannot absorb a session into itself")
    await refuse_current_or_busy(
        recorder,
        target,
        source,
        current=source,
        action="absorb",
        hint="then absorb the now-previous folder into the target",
    )
    resolve_session_dir(target)
    resolve_session_dir(source)

    summary = absorb_session(target, source)
    ops_log(
        f"absorbed {source} into {target}: "
        f"{summary['wavs_moved']} wavs, {summary['stripped_moved']} stripped, "
        f"+{len(summary['aliases_added'])} aliases"
    )
    return {"ok": True, **summary}


@router.delete("/api/sessions/{session}")
async def api_session_delete(session: str, recorder: Recorder = Depends(get_recorder)):
    """Recursively delete a recordings folder. Refuses the CURRENT session,
    any session with a transcribe/strip job in flight, or a session with a tap
    open on it (a live ActiveStream or an in-flight mark). `rmtree`-ing the
    folder out from under a running job thread or an open tap WS would crash
    it mid-write (the same guard the sibling /audio and /absorb endpoints
    enforce)."""
    await refuse_current_or_busy(recorder, session, current=session, action="delete")
    session_dir = resolve_session_dir(session)
    # Same hold-for-the-walk bracket as the sibling /audio delete, for the two
    # reasons spelled out there. Route-specific: `jobs.run` releases on EVERY
    # exit path, so it also subsumes the hand-rolled `jobs.release` this route
    # used to do on the success path only.
    async with recorder.jobs.run(session, kind="delete", total=1):
        try:
            await asyncio.to_thread(_delete_session_worker, session_dir, session)
        except OSError as e:
            raise SessionDeleteError(f"delete failed: {e}") from None
    ops_log(f"deleted session: {session_dir}")
    return {"ok": True, "deleted": session}


def _delete_session_worker(session_dir: Path, session: str) -> str:
    """Worker for `DELETE /api/sessions/{session}`: claims the destruction
    guard, runs `rmtree`, releases the guard. Raises `SessionBusy` (409)
    when a tap is open on `session`."""
    if not try_claim_destruct(session):
        raise SessionBusy("delete aborted: a tap is open on this session")
    try:
        shutil.rmtree(session_dir)
        return session
    finally:
        release_destruct(session)


@router.get("/api/session-meta/{session}")
async def api_session_meta_get(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    resolve_session_dir(session)
    return read_session_meta(session)


@router.put("/api/session-meta/{session}")
async def api_session_meta_put(session: str, req: Request, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    resolve_session_dir(session)
    write_session_meta(session, await json_body(req))
    return {"ok": True, "meta": read_session_meta(session)}


@router.get("/api/sessions/{session}/transcript")
async def api_session_transcript(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """The FULL merged session-transcript.json (or null when none).

    Lazy companion to `/api/state`, whose `session_transcript` is now a slim
    marker. The dashboard fetches this once per (session, transcribed_at) when
    a session is opened and caches it client-side, so the heavy segments[] /
    plain_text / suppressed[] body crosses the wire on open, not every poll.
    The disk read is offloaded with to_thread like the rest of the poll path."""
    return await asyncio.to_thread(read_session_transcript, session)


@router.get("/api/sessions/{session}/files")
async def api_session_files(session: str, recorder: Recorder = Depends(get_recorder)):
    """The FULL per-session WAV listing (originals + their stripped region
    clips), the `files[]` array `/api/state` no longer embeds.

    Lazy companion to `/api/state`, which now carries only `wav_count`,
    `total_bytes`, `total_duration_s` and a `files_sig`. The dashboard fetches
    this once per (session, files_sig) when a session is opened and caches it
    client-side, so a huge session's per-WAV array crosses the wire on open +
    on change — not on every poll. `resolve_session_dir` (inside
    `read_session_files`) validates the id against path traversal; the disk walk
    is offloaded with to_thread like the rest of the poll path.

    Each descriptor carries `open` for the WAVs a tap is writing into THIS
    session right now: an open WAV's RIFF header is patched only at tap close,
    so the dashboard's Player refuses it (ADR-0017). Scoped by session because
    two sessions can hold the same filename — unlike `/api/state`'s masking
    set, where an over-broad match would only affect refetch cadence."""
    active_streams = await recorder.streams.snapshot()
    return await asyncio.to_thread(
        read_session_files, session, open_wav_names(active_streams, session=session)
    )


@router.get("/api/sessions/{session}/summary")
async def api_session_summary(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """The FULL persisted session summary (or null when none).

    Lazy companion to `/api/state`, whose `session_summary` is a slim marker
    (summarized_at + source + model). The dashboard fetches this once per
    (session, summarized_at) when the Summary stage is opened and caches it
    client-side. The disk read is offloaded with to_thread like the rest of
    the poll path."""
    return await asyncio.to_thread(read_session_summary, session)


@router.get("/api/search")
async def api_search(q: str = "", recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """Cross-session transcript-content search.

    Scans every session's merged transcript for a query term (case-
    insensitive) and returns one hit per matching session:
    ``{session, label, snippet, count}``.

    Basic-auth (not tap-bearer, not exempt). The scan runs off the event
    loop via ``asyncio.to_thread`` so it doesn't block /api/state polling.
    """
    return await asyncio.to_thread(search_transcripts, q)
