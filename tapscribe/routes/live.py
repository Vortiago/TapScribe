"""Live channel control and its transcript feed.

  POST    /api/live/start       reconcile the channel toward a desired state
  POST    /api/live/stop        stop the WhisperLiveKit child
  GET     /api/live/log         the full log tail (on demand, not per poll)
  DELETE  /api/live-transcript  clear the live transcript feed

Boundary parsing lives here; the transition itself (family swap, catalog
allowlist, restart choreography) is `live_control`'s, so a rejected request
cannot disturb a running channel. There is no POST counterpart to the transcript
clear: settled lines arrive through the Recorder's WlK relay (ADR-0002).
"""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..live_control import (
    DesiredLiveState,
    apply_live,
    plan_live,
)
from ..recorder import Recorder
from .body import (
    json_body,
    parse_bounded_float,
    parse_bounded_int,
    parse_opt_bool,
    parse_opt_str,
)
from .deps import get_recorder

router = APIRouter()


@router.post("/api/live/start")
async def api_live_start(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Reconcile the live channel toward the requested model / language /
    gate config. Boundary parsing + numeric bounds happen here (the HTTP
    edge); the domain transition — family swap, catalog allowlist, restart
    choreography — lives in `live_control`, so this route is a thin shim and
    a rejected request cannot disturb a running channel (#334: `plan_live`
    is pure and raises before `apply_live` touches anything).

    `apply_live` spawn/stop is synchronous and can block for several
    seconds, so it is offloaded to a worker thread to keep /api/state
    polling responsive.
    """
    body = await json_body(req)
    # Boundary parsing FIRST — non-strings and out-of-range numbers 400 at
    # the HTTP edge (CodeQL treats Request.json() as untrusted; the
    # dashboard's min/max attrs are client-side hints only) while building
    # the DesiredLiveState, before any domain logic runs. Nothing downstream
    # can mutate on a rejected request.
    desired = DesiredLiveState(
        model=parse_opt_str(body.get("model"), "model"),
        language=parse_opt_str(body.get("language"), "language"),
        gate_kind=parse_opt_str(body.get("gate_kind"), "gate_kind"),
        conf=parse_opt_bool(body.get("confidence_validation"), "confidence_validation"),
        gate_speech_threshold=parse_bounded_float(
            body.get("gate_speech_threshold"), "gate_speech_threshold", lo=0.0, hi=1.0
        ),
        gate_hangover_ms=parse_bounded_int(body.get("gate_hangover_ms"), "gate_hangover_ms", lo=0, hi=10_000),
        gate_pre_roll_ms=parse_bounded_int(body.get("gate_pre_roll_ms"), "gate_pre_roll_ms", lo=0, hi=5_000),
        gate_min_speech_ms=parse_bounded_int(
            body.get("gate_min_speech_ms"), "gate_min_speech_ms", lo=0, hi=5_000
        ),
    )
    # Pure: validates (raising a LiveReconcileError the domain-error handler
    # maps) and decides the transition without touching the running channel.
    plan = plan_live(recorder.live, desired, use_mlx=recorder.use_mlx)
    return await asyncio.to_thread(
        apply_live, recorder.live, plan, set_live=lambda ch: setattr(recorder, "live", ch)
    )


@router.post("/api/live/stop")
async def api_live_stop(recorder: Recorder = Depends(get_recorder)):
    ok, msg = await asyncio.to_thread(recorder.live.stop)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg, "state": recorder.live.info["state"]}


@router.get("/api/live/log")
async def api_live_log(recorder: Recorder = Depends(get_recorder)):
    """Full WhisperLiveKit log tail (up to `live.LOG_TAIL_LINES` lines) — the
    source for the dashboard's log dialog. /api/state only sends a
    `live.LOG_PREVIEW_LINES` preview so the poll stays cheap; this endpoint is
    requested on demand when the operator opens the dialog."""
    return {
        "log": list(recorder.live.log),
        "state": recorder.live.info.get("state", ""),
    }


@router.delete("/api/live-transcript")
async def api_live_transcript_clear(recorder: Recorder = Depends(get_recorder)):
    """Clear the live transcript feed (the dashboard's "clear" button).

    Note: there is no POST counterpart anymore. Bridges send audio to /tap;
    settled lines are consumed by the Recorder's internal WlK relay and
    appended to recorder.transcripts directly. See ADR-0002.
    """
    recorder.transcripts.clear()
    return {"ok": True}
