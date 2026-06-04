"""Batch strip-silence — drive the splitter across every WAV in a session.

The strip-silence sibling of `batch_transcribe`: same orchestrator shape
(claim the session's JobTracker slot, loop the WAVs on a worker thread,
aggregate, release), same FastAPI-free contract — domain errors out, the
route maps them to HTTP codes. `SessionBusy` / `NoUsableWavs` are shared
with `batch_transcribe` because they're JobTracker / selection semantics,
not transcription-specific; the "one transcribe/strip job per session"
rule has exactly these two claimants.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .batch_transcribe import BatchTranscribeError, NoUsableWavs, SessionBusy
from .recorder import JobState, Recorder
from .sessions import resolve_session_dir, strip_one_wav, stripped_dir
from .strip_silence import SPEECH_RMS_DBFS_FLOOR


class StrippedDirUnclearable(BatchTranscribeError):
    """`<session>/stripped/` exists but couldn't be removed before the
    re-strip — typically a file lock (Windows) or permissions. Raised
    before any WAV is touched, so nothing was modified. Routes map this
    to a 500."""


@dataclass(frozen=True)
class StripSessionRequest:
    """Inputs for stripping a session. The knob defaults live HERE (the
    dashboard's strip-silence sliders mirror them); the route forwards
    only explicitly-provided values, so `StripSessionRequest(session=…)`
    is the canonical default invocation."""

    session: str
    min_silence_ms: int = 500
    pad_ms: int = 200
    speech_floor_db: float = SPEECH_RMS_DBFS_FLOOR


async def strip_session(recorder: Recorder, req: StripSessionRequest) -> dict[str, Any]:
    """Non-destructively strip silence from every WAV in the session:
    cleaned copies land in `<session>/stripped/` (originals untouched),
    replacing any previous stripped output.

    Claims the session's `JobTracker` slot — raises `SessionBusy` when a
    transcribe/strip is already in flight, `NoUsableWavs` when the
    session has no originals. Per-WAV splitter failures don't abort the
    loop; they land in the result's `files` list as
    `{"written": False, "error": …}` rows so one corrupt WAV can't sink
    the rest of the session."""
    session_dir = resolve_session_dir(req.session)
    originals = sorted(session_dir.glob("*.wav"))
    if not originals:
        raise NoUsableWavs("no WAVs in this session to strip")

    # JobTracker.claim() encapsulates the "one job per session" rule.
    claimed = await recorder.jobs.claim(
        JobState(
            session=req.session,
            kind="strip",
            current=0,
            total=len(originals),
            started_at=datetime.now(UTC),
            status="stripping",
        )
    )
    if not claimed:
        raise SessionBusy("session is already busy (transcribe or strip in flight)")

    try:
        out_dir = stripped_dir(req.session)
        if out_dir.exists():
            try:
                shutil.rmtree(out_dir)
            except OSError as e:
                raise StrippedDirUnclearable(f"could not clear stripped/: {e}") from None

        started = datetime.now(UTC)

        def _run() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for src in originals:
                try:
                    results.append(
                        strip_one_wav(src, out_dir, req.min_silence_ms, req.pad_ms, req.speech_floor_db)
                    )
                except Exception as e:
                    results.append({"name": src.name, "written": False, "error": str(e)})
            return results

        results = await asyncio.to_thread(_run)
        finished = datetime.now(UTC)
    finally:
        await recorder.jobs.release(req.session)

    written = sum(1 for r in results if r.get("written"))
    in_secs = sum(r.get("in_seconds", 0.0) for r in results)
    speech_secs = sum(r.get("speech_seconds", 0.0) for r in results)
    detectors = sorted({r.get("detector") for r in results if r.get("detector")})

    print(
        f"[tapscribe] strip-silence {req.session}: {written}/{len(originals)} wavs, "
        f"{speech_secs:.1f}s speech of {in_secs:.1f}s ({100 * speech_secs / max(in_secs, 1e-9):.0f}%), "
        f"detector={detectors}, took {int((finished - started).total_seconds() * 1000)} ms",
        flush=True,
    )

    return {
        "ok": True,
        "session": req.session,
        "files_processed": len(originals),
        "files_written": written,
        "in_seconds": round(in_secs, 2),
        "speech_seconds": round(speech_secs, 2),
        "detector": detectors[0] if len(detectors) == 1 else detectors,
        "stripped_at": finished.isoformat(),
        "took_ms": int((finished - started).total_seconds() * 1000),
        "files": results,
    }
