"""The browser first-run / manage-models surface.

  GET   /api/setup/state    catalog-driven install state for this host
  POST  /api/setup/install  run the picker, stream progress as SSE

Read and execution are deliberately separate routes: the state read is cheap and
idempotent, the install spawns the dependency-free install picker as a
subprocess (ADR-0015) and streams one SSE event per output line. Concurrent
installs are refused via a slot claimed on `app.state`.
"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse

from ..runtime_probe import refresh_backend_probes
from ..setup_install import InstallSelectionError, run_install, sse, validate_live, validate_selection
from ..setup_state import build_setup_state
from .body import json_body

router = APIRouter()


@router.get("/api/setup/state")
async def api_setup_state():
    """Catalog-driven setup state for the browser first-run / manage-models
    surface. Read-only; install *execution* is separate.

    Response shape:
      {
        "first_run": bool,                  # no transcription backend installed yet
        "available_backends": ["cpu", ...], # what this host can run
        "families": [ {family, label, size_hint, live, batch,
                       installed, backends, models}, ... ]
      }
    """
    return build_setup_state()


@router.post("/api/setup/install")
async def api_setup_install(request: Request):
    """Install the selected model families and stream progress as Server-Sent
    Events. Body: ``{"families": {"<family>": "<mlx|cuda|cpu>", ...}, "live":
    <bool, default true>}``. `live` is the WhisperLiveKit live-caption channel
    opt-out (#374) — it stays ON by default so an existing client that
    predates the flag still gets the live channel it always got.

    Delegates the actual pip work to the dependency-free install picker
    (`tapscribe/install_picker.py --non-interactive`) against a selection written
    from the validated request, streaming one SSE `data:` event per output line
    then a terminal `done`/`error`. On success the backend probes are refreshed
    so `/api/models` + `/api/setup/state` reflect the new install without a
    restart. Concurrent installs are refused (409)."""
    body = await json_body(request)
    try:
        selection = validate_selection(body.get("families", {}))
        live = validate_live(body.get("live"))
    except InstallSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc

    if getattr(request.app.state, "setup_install_active", False):
        raise HTTPException(409, "an install is already running")
    # Claim the slot synchronously here — there's no await between the guard
    # above and this set, so two near-simultaneous requests can't both pass
    # before the (lazily-started) stream would set it. Cleared in finally.
    request.app.state.setup_install_active = True

    async def events():
        try:
            async for ev in run_install(
                selection,
                live=live,
                # Set by `python -m tapscribe --install-spec` (the Bundle's
                # the tray passes its wheel); absent in a checkout.
                install_spec=getattr(request.app.state, "install_spec", None),
                on_success=refresh_backend_probes,
            ):
                yield sse(ev)
        finally:
            request.app.state.setup_install_active = False

    # no-cache + no proxy buffering so events arrive as they're produced
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)
