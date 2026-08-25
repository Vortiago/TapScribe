"""Batch diarization — split each multi-person tap in a session into Voices.

The strip/transcribe sibling: claim the session's job slot via
`recorder.jobs.run`, work on a thread, aggregate; domain errors out, the route
maps them to HTTP codes. ADR-0021.

Which taps get diarized comes from the **Roster's** per-identity `mode`, not the
live tap setting: whether a recording holds several humans is a property of the
recording, so flipping the setting next week must not change what a finished
session means.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import tapscribe.voices as voices

from .diarizers import load_diarizer
from .diarizers.base import AudioClip, DiarizationResult
from .recorder import Recorder
from .roster import read_roster, slug_owners
from .session_merge import remerge_with_stored_selection, select_session_wavs
from .session_paths import resolve_session_dir
from .sessions import read_session_transcript
from .tap_mode import TAP_MODE_MULTI
from .text import parse_wav_speaker_slug, parse_wav_start
from .wav_predecode import load_recorder_wav_as_pcm


@dataclass(frozen=True)
class DiarizeSessionRequest:
    """`source` matches `BatchSessionRequest`'s, and the pipeline passes the
    same value to both stages so they agree by construction. The absolute-time
    join tolerates either; this is about cost."""

    session: str
    source: str = "original"


@dataclass(frozen=True)
class _Target:
    """One identity's diarizable audio, in time order."""

    identity: str
    wavs: tuple[Path, ...]


@dataclass(frozen=True)
class _Plan:
    """What a run will do, and why it will skip the rest. Computed before the
    job slot is claimed, so "nothing to diarize" costs no claim and no model."""

    targets: list[_Target]
    skipped: list[dict[str, str]]
    started: datetime


async def diarize_session(recorder: Recorder, req: DiarizeSessionRequest) -> dict[str, Any]:
    """Diarize every multi-person tap in the session, then re-merge so the
    transcript already on disk picks up the new speaker keys.

    Claims the session's `JobTracker` slot — `SessionBusy` when a job is in
    flight. A session with no multi-person tap is a no-op that claims nothing
    and loads no model.
    """
    session_dir = resolve_session_dir(req.session)
    plan = await asyncio.to_thread(_plan, session_dir, req.source)
    if not plan.targets:
        return _result(req, run_id="", rows=[], plan=plan)

    # Before the claim, like `batch_summarize`: a box whose model was never
    # fetched should not hold a session's job slot to find that out, and
    # `DiarizerUnavailable` is the actionable error where `SessionBusy` is not.
    # On a thread because opening the 28 MB graph takes ~0.2 s, and the event
    # loop is serving a 500 ms poll and every live /tap frame.
    diarizer = await asyncio.to_thread(load_diarizer)
    async with recorder.jobs.run(
        req.session, kind="diarize", total=len(plan.targets), status="diarizing"
    ) as job:
        out = await diarize_session_locked(req, plan=plan, diarizer=diarizer, job=job)
        await asyncio.to_thread(_remerge, req.session, session_dir)
    return out


async def diarize_session_locked(
    req: DiarizeSessionRequest, *, plan: _Plan | None = None, diarizer=None, job=None
) -> dict[str, Any]:
    """The same work minus the claim, so the end-of-meeting pipeline runs it as
    one stage of a single `kind="pipeline"` claim. `plan` and `diarizer` are
    passed down by the claiming wrapper, which computed them BEFORE claiming;
    the pipeline lets this do both.

    Does NOT re-merge — the pipeline transcribes next and merges then.
    """
    session_dir = resolve_session_dir(req.session)
    if plan is None:
        plan = await asyncio.to_thread(_plan, session_dir, req.source)
    if not plan.targets:
        return _result(req, run_id="", rows=[], plan=plan)
    if diarizer is None:
        diarizer = await asyncio.to_thread(load_diarizer)
    if job is not None:
        await job.update(total=len(plan.targets))

    run_id = uuid4().hex[:12]
    targets = plan.targets

    def _diarize_one(target: _Target) -> dict[str, Any]:
        """One identity, written to the sidecar as it finishes so a later
        failure keeps the earlier identities' Voices."""
        # A generator, so the engine holds one clip at a time: an hour of
        # 16 kHz float32 is 230 MB, the embeddings it reduces to are 10 MB.
        clips = (
            AudioClip(load_recorder_wav_as_pcm(wav), start)
            for wav in target.wavs
            if (start := parse_wav_start(wav.name)) is not None
        )
        result: DiarizationResult = diarizer.diarize(clips)
        voices.record_voices(session_dir, identity=target.identity, run_id=run_id, spans=result.voices)
        return {
            "identity": target.identity,
            "wavs": len(target.wavs),
            "voices": len(result.voices),
            "spans": sum(len(s) for s in result.voices.values()),
            "engine": result.engine,
            "took_ms": result.took_ms,
        }

    rows: list[dict[str, Any]] = []
    for done, target in enumerate(targets):
        if job is not None:
            await job.update(current=done, current_file=target.identity)
        rows.append(await asyncio.to_thread(_diarize_one, target))
    if job is not None:
        await job.update(current=len(rows), current_file=None)
    return _result(req, run_id=run_id, rows=rows, plan=plan)


def _plan(session_dir: Path, source: str) -> _Plan:
    """Which identities to diarize, and why the rest were left out.

    Pure disk reads (the Roster plus one selection pass), so `diarize_session`
    can decide "nothing to do" without claiming a slot or loading a model.
    """
    started = datetime.now(UTC)
    roster = read_roster(session_dir)
    # Two taps under one display name mint the same WAV slug, so neither
    # identity's Voices could be joined back to it (#440). `slug_owners` is the
    # one owner of that verdict; the merge-time join reads the same answer.
    owners = slug_owners(roster)
    by_slug: dict[str, list[Path]] = {}
    for wav in select_session_wavs(session_dir, source=source).wavs:
        by_slug.setdefault(parse_wav_speaker_slug(wav.name), []).append(wav)

    targets: list[_Target] = []
    skipped: list[dict[str, str]] = []
    for identity, entry in sorted(roster.items()):
        if entry.get("mode") != TAP_MODE_MULTI:
            skipped.append({"identity": identity, "reason": "single"})
            continue
        slug = entry.get("slug") or ""
        if len(owners.get(slug, ())) != 1:
            skipped.append({"identity": identity, "reason": "ambiguous-slug"})
            continue
        wavs = by_slug.get(slug) or []
        if not wavs:
            skipped.append({"identity": identity, "reason": "no-audio"})
            continue
        # `select_session_wavs` sorts by filename, which starts with the ISO
        # stamp — so this is chronological, which is what makes Voice A whoever
        # spoke first.
        targets.append(_Target(identity=identity, wavs=tuple(wavs)))
    return _Plan(targets=targets, skipped=skipped, started=started)


def _remerge(session: str, session_dir: Path) -> bool:
    """Rebuild the merged transcript with the Voices just written.

    Speaker keys are baked in at merge time, so without this the operator
    diarizes, maps the Voices and the transcript keeps the undiarized key until
    someone re-transcribes. Absence is the norm rather than an error — diarize
    before transcribe is the pipeline's order.
    """
    previous = read_session_transcript(session)
    return remerge_with_stored_selection(session_dir, previous) if previous else False


def _result(
    req: DiarizeSessionRequest, *, run_id: str, rows: list[dict[str, Any]], plan: _Plan
) -> dict[str, Any]:
    return {
        "ok": True,
        "session": req.session,
        "source": req.source,
        "run_id": run_id,
        "identities": rows,
        "skipped": plan.skipped,
        "took_ms": int((datetime.now(UTC) - plan.started).total_seconds() * 1000),
    }
