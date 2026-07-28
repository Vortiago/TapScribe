"""One WAV: download it, read its transcript, draw it, delete it, pick a primary.

  GET     /api/wav/{session}/{name}             download (source=original|stripped)
  GET     /api/wav/{session}/{name}/transcript  the full primary cached transcript
  GET     /api/wav/{session}/{name}/peaks       fixed-size waveform downsample
  DELETE  /api/wav/{session}/{name}             delete the WAV + its sidecars
  PUT     /api/wav/{session}/{name}/primary     point _primary at a (backend, model)

Every route here crosses `session_paths.resolve_wav`, which owns the two-layer
path guard and rejects an unknown `source`. The strip-silence pair on this same
URL prefix (strip-preview, strip-meta) lives in `routes/strip.py` with the knob
parser they share with the commit route.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import FileResponse

from ..audio import compute_peaks
from ..recorder import Recorder
from ..session_maintenance import delete_session_wav
from ..session_paths import (
    resolve_session_dir,
    resolve_wav,
)
from ..sessions import read_wav_transcript
from ..wav_cache import set_primary_transcript
from .body import json_body
from .deps import get_recorder
from .guards import refuse_current_or_busy

router = APIRouter()


@router.get("/api/wav/{session}/{name}")
async def get_wav(session: str, name: str, source: str = "original"):
    """Download a WAV. source=stripped pulls from <session>/stripped/."""
    path = resolve_wav(session, name, source)
    dl_name = ("stripped-" + name) if source == "stripped" else name
    return FileResponse(path, media_type="audio/wav", filename=dl_name)


@router.get("/api/wav/{session}/{name}/transcript")
async def api_wav_transcript(session: str, name: str, source: str = "original"):
    """The FULL primary cached transcript for one WAV (or null when none).

    Lazy companion to `/api/state`, whose per-WAV `transcript` is now a slim
    marker. The dashboard fetches this when a WAV row is expanded and caches
    it per (session, name, source, transcribed_at). Mirrors `get_wav`'s
    path-safety (resolve_wav validates session/name/source) and offloads the
    disk read with to_thread."""
    return await asyncio.to_thread(read_wav_transcript, session, name, source)


# Waveform downsample resolution. The route CLAMPS the operator-supplied bins
# into this band rather than 422-ing — a fixed payload size is the whole point,
# and the dashboard never needs more than a few thousand bars on screen.
_PEAKS_BINS_DEFAULT = 800


_PEAKS_BINS_MIN = 16


_PEAKS_BINS_MAX = 2000


@router.get("/api/wav/{session}/{name}/peaks")
async def api_wav_peaks(
    session: str,
    name: str,
    bins: int = _PEAKS_BINS_DEFAULT,
    source: str = "original",
):
    """Server-computed waveform peaks for one WAV — a fixed-size downsample
    (the foundation the later cut overlay draws on). Mirrors get_wav's
    path-safety (resolve_wav validates session/name/source under
    RECORDINGS_DIR — including rejecting an unknown `source`), clamps `bins`
    to a sane band, and offloads the O(samples) read off the event loop. The
    payload is `bins` floats regardless of recording length."""
    bins = max(_PEAKS_BINS_MIN, min(_PEAKS_BINS_MAX, bins))
    path = resolve_wav(session, name, source)
    try:
        peaks = await asyncio.to_thread(compute_peaks, path, bins=bins)
    except RuntimeError as e:
        # The WAV exists (resolve_wav 404'd a missing one) but isn't decodable
        # as peaks → 422 unprocessable. compute_peaks is a low-level audio
        # helper with no domain-error type, so we map its RuntimeError at the
        # call site rather than via the batch-orchestrator domain-error handler
        # (a deliberately separate layer, not an unfinished unification).
        raise HTTPException(422, str(e)) from e
    return asdict(peaks)


@router.delete("/api/wav/{session}/{name}")
async def api_wav_delete(
    session: str,
    name: str,
    source: str = "original",
    recorder: Recorder = Depends(get_recorder),
):
    """Delete one WAV + its transcript-cache sidecars. source=stripped
    targets a region under <session>/stripped/. No region cascade — see
    `delete_session_wav`. Refuses the CURRENT session, any session with
    a transcribe/strip job in flight, or a session with a live tap writing
    to it. An unknown `source` is rejected (400) by `resolve_source_dir` —
    the path seam owns that check; `source` itself is never a path
    component (only compared against the two literals)."""
    await refuse_current_or_busy(recorder, session, current=session, action="delete WAVs from")
    resolve_session_dir(session)
    summary = await asyncio.to_thread(delete_session_wav, session, name, source)
    print(
        f"[tapscribe] deleted wav {name} ({source}) from session {session}: "
        f"{summary['bytes_freed']} bytes freed",
        flush=True,
    )
    return {"ok": True, **summary}


@router.put("/api/wav/{session}/{name}/primary")
async def api_set_primary(
    session: str,
    name: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),  # noqa: ARG001
):
    """Point the primary cached transcript at the given (backend, model).
    Used by the per-WAV picker UI to flip which transcript merge_session
    and the dashboard surface, without re-running anything.

    Body: `{"backend": "faster-whisper", "model": "small.en", "source"?: "original"|"stripped"}`.
    """
    body = await json_body(req)
    backend = body.get("backend")
    model = body.get("model")
    if not isinstance(backend, str) or not backend:
        raise HTTPException(400, "backend required")
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "model required")
    source = body.get("source") or "original"
    path = resolve_wav(session, name, source)
    try:
        await asyncio.to_thread(set_primary_transcript, path, backend=backend, model=model)
    except FileNotFoundError as e:
        raise HTTPException(422, str(e)) from None
    return {"ok": True, "primary": {"backend": backend, "model": model}}
