"""Batch transcription triggers.

  POST  /api/transcribe          one WAV
  POST  /api/transcribe-session  a session (range-selectable), merged

Thin shims over `batch_transcribe`: parse the body, resolve the operator's
generalist when no model was named (ADR-0011), and let the registered
domain-error handlers map failures. The one local rule is
`_translating_registry_rejection`: the lazy resolve inside `load_transcriber`
raises plain KeyError / RuntimeError, and re-resolving against the catalog is
what tells a bad model id (400) from an unrelated failure on a good one (500).
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse

from ..batch_transcribe import (
    BatchOneRequest,
    BatchSessionRequest,
    resolve_batch_model,
    transcribe_one,
    transcribe_session,
)
from ..recorder import Recorder
from ..transcribers.catalog import REGISTRY
from .body import json_body
from .deps import get_recorder

router = APIRouter()


def _translate_registry_rejection(model: str, backend: str) -> None:
    """Translate the catalog's rejection of `(model, backend)` into a 400 — or
    return, leaving the caller's exception to propagate as the 500 it is.

    The lazy resolve inside `load_transcriber` raises plain KeyError /
    RuntimeError, neither a domain error; re-resolving here is what tells a bad
    model id apart from an unrelated failure on a good one."""
    try:
        REGISTRY.resolve(model, preference=backend)  # type: ignore[arg-type]
    except KeyError:
        raise HTTPException(400, f"unknown model {model!r} — not in the catalog") from None
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from None


@contextmanager
def _translating_registry_rejection(model: str, backend: str):
    """Wrap a batch transcribe so a catalog rejection of `(model, backend)`
    surfaces as a 400 (see `_translate_registry_rejection`) instead of a 500.
    One copy, so a third transcribe route can't become a third paste."""
    try:
        yield
    except (KeyError, RuntimeError):
        _translate_registry_rejection(model, backend)
        raise


@router.post("/api/transcribe")
async def api_transcribe(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await json_body(req)
    session = body.get("session") or ""
    name = body.get("name") or ""
    if not session or not name:
        raise HTTPException(400, "session and name are required")
    source = body.get("source") or "original"
    request = BatchOneRequest(
        session=session,
        name=name,
        source=source,
        # No model in the body → resolve the operator's generalist (batch-model.txt).
        # The Transcript page declares languages, not a model (ADR-0011); an explicit
        # per-call model is still honoured (CLI / future callers).
        model=body.get("model") or resolve_batch_model(),
        # Per-call backend override — falls back to the Recorder's
        # preference when the body didn't carry one.
        backend=(body.get("backend") or "").strip() or recorder.backend,
        # Per-call language pin rides alongside prompt/hotwords. Empty →
        # the session's candidate languages decide (ADR-0010/0011).
        source_lang=(body.get("source_lang") or "").strip() or None,
    )
    with _translating_registry_rejection(request.model, request.backend):
        payload = await transcribe_one(recorder, request)
    print(
        f"[tapscribe] transcribed {request.name} ({request.source}) with {request.model}",
        flush=True,
    )
    return JSONResponse(payload)


@router.post("/api/transcribe-session")
async def api_transcribe_session(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await json_body(req)
    session = body.get("session") or ""
    if not session:
        raise HTTPException(400, "session is required")
    source = body.get("source") or "original"
    request = BatchSessionRequest(
        session=session,
        source=source,
        # No model in the body → the operator's generalist (batch-model.txt); the
        # candidate languages (session-meta) drive which specialists join (ADR-0011).
        model=body.get("model") or resolve_batch_model(),
        backend=(body.get("backend") or "").strip() or recorder.backend,
        from_iso=body.get("from_iso") or None,
        to_iso=body.get("to_iso") or None,
        force=bool(body.get("force")),
        source_lang=(body.get("source_lang") or "").strip() or None,
    )
    with _translating_registry_rejection(request.model, request.backend):
        return JSONResponse(await transcribe_session(recorder, request))
