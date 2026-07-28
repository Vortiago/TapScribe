"""Domain error → HTTP status. ONE source of truth: the orchestrators raise
FastAPI-free domain errors (SessionBusy, NoUsableWavs, SummarizerFailed, …)
and these handlers translate them, so every batch route is just
`return await orchestrator(...)` instead of a per-route try/except ladder. A
domain error's HTTP meaning is intrinsic (busy is always 409), so it's
registered once here rather than re-mapped in each route that can raise it.

`register_domain_errors(app)` is the one call `app.py` makes; the routers never
touch the registry. Adding an orchestrator error means adding a row here, and
forgetting to falls to 500 rather than passing silently as a 200.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..batch_strip import StrippedDirUnclearable
from ..batch_summarize import NoMergedTranscript
from ..batch_transcribe import WavTooQuiet, WavUnreadable
from ..live_control import GateKindUnsupported, LiveModelUnknown, LiveStartFailed
from ..recorder import SessionBusy
from ..session_maintenance import AbsorbCollision, InvalidAbsorbRequest, SessionDeleteError
from ..session_merge import InvalidRange, NoUsableWavs
from ..session_paths import SessionNotFound, StrippedMissing, UnknownSource, WavNotFound
from ..sessions import MetaValidationError
from ..summarizers import SummarizerFailed, SummarizerUnavailable

DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    SessionBusy: 409,
    NoUsableWavs: 404,
    InvalidRange: 400,
    WavUnreadable: 422,
    WavTooQuiet: 422,
    StrippedDirUnclearable: 500,
    NoMergedTranscript: 422,
    SummarizerUnavailable: 400,
    SummarizerFailed: 502,
    SessionNotFound: 404,
    UnknownSource: 400,
    StrippedMissing: 404,
    WavNotFound: 404,
    MetaValidationError: 400,
    AbsorbCollision: 409,
    InvalidAbsorbRequest: 400,
    SessionDeleteError: 500,
    # Live-channel reconcile (live_control) — the /api/live/start route and
    # the boot auto-start both surface these; registering the concrete
    # subclasses keeps `type(exc)` lookups in `domain_error_handler` exact.
    LiveModelUnknown: 400,
    GateKindUnsupported: 400,
    LiveStartFailed: 500,
}


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate a known domain error to its status; anything unmapped falls to
    500, so a new orchestrator error can't silently slip through as a 200."""
    return JSONResponse(status_code=DOMAIN_ERROR_STATUS.get(type(exc), 500), content={"detail": str(exc)})


def register_domain_errors(app: FastAPI) -> None:
    """Attach the handler for every registered domain error type."""
    for exc_type in DOMAIN_ERROR_STATUS:
        app.add_exception_handler(exc_type, domain_error_handler)
