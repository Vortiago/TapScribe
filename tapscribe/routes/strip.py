"""Strip silence: preview a cut, commit it, read it back, throw it away.

  POST    /api/sessions/{session}/strip-silence      commit: write stripped/ region clips
  GET     /api/wav/{session}/{name}/strip-preview    what a commit WOULD cut, no writes
  GET     /api/wav/{session}/{name}/strip-meta       the committed cut for one original
  DELETE  /api/sessions/{session}/stripped           remove stripped/ so it can be regenerated

Grouped by the domain concern rather than the URL prefix (ADR-0018): these four
span `/api/sessions/*` and `/api/wav/*`, and keeping them together is what lets
`_parse_strip_knob_overrides` stay module-private. It is the ONE owner of the
knob names, their bounds, and the only-forward-explicit contract, so the preview
plans with exactly the knobs a commit would use. Splitting these by prefix would
turn that into a shared helper two modules import, which is the drift its own
docstring warns about.
"""

from __future__ import annotations

import asyncio
import shutil
import wave
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..batch_strip import StripSessionRequest, strip_session
from ..recorder import Recorder
from ..session_paths import (
    resolve_session_dir,
    resolve_wav,
    stripped_dir,
)
from ..sessions import read_wav_strip_meta
from ..strip_silence import plan_strip_regions, read_wav_int16
from .body import (
    json_body,
    parse_bounded_float,
    parse_bounded_int,
)
from .deps import get_recorder

router = APIRouter()


def _parse_strip_knob_overrides(min_silence_ms: Any, pad_ms: Any, speech_floor_db: Any) -> dict[str, Any]:
    """Range-bound the strip-silence knobs and return only the explicitly
    provided ones, ready to splat into StripSessionRequest (which owns the
    DEFAULTS). One owner for the names + bounds + only-forward-explicit
    contract, shared by the strip route (body knobs) and the strip-preview
    route (query knobs) so the two can never drift — the preview must plan
    with exactly the knobs a commit would use. The `is not None` checks keep
    an explicit 0 (e.g. pad_ms=0 to disable region padding for A/B) from
    silently falling back to the default; out-of-range values → 400 instead
    of a 500 from int()/float()."""
    overrides: dict[str, Any] = {}
    bounded_min_silence = parse_bounded_int(min_silence_ms, "min_silence_ms", lo=100, hi=600_000)
    if bounded_min_silence is not None:
        overrides["min_silence_ms"] = bounded_min_silence
    bounded_pad = parse_bounded_int(pad_ms, "pad_ms", lo=0, hi=5_000)
    if bounded_pad is not None:
        overrides["pad_ms"] = bounded_pad
    bounded_floor = parse_bounded_float(speech_floor_db, "speech_floor_db", lo=-120.0, hi=0.0)
    if bounded_floor is not None:
        overrides["speech_floor_db"] = bounded_floor
    return overrides


@router.post("/api/sessions/{session}/strip-silence")
async def api_session_strip_silence(
    session: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
):
    """Non-destructively strip silence from every WAV in <session>/. Thin
    HTTP shim over `batch_strip.strip_session` — parse + range-bound the knobs;
    the registered domain-error handlers map failures to status codes."""
    body = await json_body(req)
    overrides = _parse_strip_knob_overrides(
        body.get("min_silence_ms"), body.get("pad_ms"), body.get("speech_floor_db")
    )
    return await strip_session(recorder, StripSessionRequest(session=session, **overrides))


@router.delete("/api/sessions/{session}/stripped")
async def api_session_stripped_delete(session: str, recorder: Recorder = Depends(get_recorder)):
    """Remove a session's stripped/ folder so it can be regenerated.

    Deliberately NOT the full `refuse_current_or_busy`: stripped/ is a
    derived artefact, so clearing the CURRENT session's copy — including
    while a tap is writing ORIGINALS into that session — is legitimate, and
    only the busy-job guard applies. `batch_strip.strip_session` holds the
    session's job slot for its whole run and is writing region clips into
    exactly this directory, so an rmtree underneath it would delete half of
    what it just wrote and leave the strip-meta describing clips that are
    gone."""
    # `jobs.run` IS the busy guard (SessionBusy → 409) and holds the slot for
    # the walk, so a strip can't claim it mid-rmtree. The path work is inside
    # the block for the same reason.
    async with recorder.jobs.run(session, kind="delete", total=1):
        resolve_session_dir(session)
        d = stripped_dir(session)
        if not d.is_dir():
            return {"ok": True, "deleted": False, "reason": "no stripped/ folder"}
        try:
            await asyncio.to_thread(shutil.rmtree, d)
        except FileNotFoundError:
            # Someone cleared stripped/ between the is_dir() above and here
            # (a second dashboard tab, an absorb). The caller's goal — no
            # stripped/ folder — already holds, so report it as a no-op
            # rather than 500ing on a check-then-act window.
            return {"ok": True, "deleted": False, "reason": "no stripped/ folder"}
        except OSError as e:
            raise HTTPException(500, f"delete failed: {e}") from None
    print(f"[tapscribe] removed stripped/ from session: {session}", flush=True)
    return {"ok": True, "deleted": True}


@router.get("/api/wav/{session}/{name}/strip-meta")
async def api_wav_strip_meta(session: str, name: str):
    """The committed strip-silence cut for one ORIGINAL wav (or null when the
    session was never stripped or this wav produced no regions). Lazy
    companion to /api/state, same contract as the transcript sidecar route:
    resolve_wav path-safety inside the reader, disk read off the event loop."""
    return await asyncio.to_thread(read_wav_strip_meta, session, name)


@router.get("/api/wav/{session}/{name}/strip-preview")
async def api_wav_strip_preview(
    session: str,
    name: str,
    min_silence_ms: int | None = None,
    pad_ms: int | None = None,
    speech_floor_db: float | None = None,
    source: str = "original",
):
    """What ✂ strip WOULD cut for one WAV at the given knobs — the live
    waveform overlay's data source (#89). Runs the REAL detector through the
    same plan the splitter writes from, with NO disk writes. Shares the
    strip route's knob bounds (out-of-range → 400) and get_wav's
    path-sanitiser; omitted knobs fall back to StripSessionRequest's
    defaults, mirroring the strip route's only-forward-explicit contract."""
    overrides = _parse_strip_knob_overrides(min_silence_ms, pad_ms, speech_floor_db)
    knobs = StripSessionRequest(session=session, **overrides)
    path = resolve_wav(session, name, source)

    def _plan() -> dict[str, Any]:
        samples = read_wav_int16(path)
        plan = plan_strip_regions(
            samples,
            min_silence_ms=knobs.min_silence_ms,
            pad_ms=knobs.pad_ms,
            speech_floor_db=knobs.speech_floor_db,
        )
        return {
            "spans": plan.spans,
            "in_seconds": plan.in_seconds,
            "speech_seconds": plan.speech_seconds,
            "segments": len(plan.regions),
            "segments_filtered_below_floor": plan.segments_filtered_below_floor,
            "silent": plan.silent,
            "rms_dbfs": round(plan.rms_dbfs, 1),
            "reason": plan.reason,
            "detector": plan.detector,
            "knobs": {
                "min_silence_ms": knobs.min_silence_ms,
                "pad_ms": knobs.pad_ms,
                "speech_floor_db": knobs.speech_floor_db,
            },
        }

    try:
        return await asyncio.to_thread(_plan)
    except (ValueError, wave.Error, EOFError, OSError) as e:
        # read_wav_int16 rejects non-recorder formats with ValueError and
        # surfaces corrupt/truncated/vanished files as wave.Error/EOFError/
        # OSError — every "this WAV can't be planned" case → 422
        # unprocessable, the same outcome the peaks route reaches via
        # compute_peaks's RuntimeError wrapping.
        raise HTTPException(422, str(e)) from e
