"""The tap control plane: everything a Bridge drives, plus its dashboard twins.

  WS      /tap                                    Bridge audio in, one WS per utterance
  POST    /api/new-session                        rotate + prune (dashboard, Basic auth)
  POST    /api/tap/new-session                    rotate or mint detached (tap bearer)
  POST    /api/sessions/{session}/pipeline        trigger the pipeline (dashboard)
  POST    /api/tap/sessions/{session}/pipeline    trigger the pipeline (tap bearer)
  GET     /api/tap/sessions/{session}/pipeline    poll the pipeline (tap bearer)
  PUT     /api/tap-settings                       per-identity record/live preferences
  POST    /api/recording/toggle                   the global recording pause

The two auth schemes sit side by side on purpose (ADR-0018). Routes under
`config.TAP_PREFIX + "/"` are TAP-BEARER, enforced by the auth middleware and
never by the handler (ADR-0008); their dashboard twins are Basic-auth gated. The
asymmetries only make sense when both are visible: the tap rotate is idempotent
and never prunes (deleting folders stays an operator action), and both pipeline
triggers ignore the request body entirely so no caller can choose which model
gets loaded. `_trigger_pipeline` is the shared body the twins cannot drift from.

The WS is the only endpoint a Bridge streams to; the handler gates auth, resolves
the tap's session, honours the pause toggle and pumps frames into a `TapFanOut`,
which owns everything else (ADR-0002).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from .. import auth, config
from ..batch_pipeline import PipelineRequest, start_pipeline
from ..recorder import Recorder
from ..session_maintenance import (
    prune_empty_sessions,
    session_is_empty,
)
from ..session_paths import resolve_session_dir
from ..sessions import read_session_summary
from ..tap_fan_out import TapFanOut
from .body import (
    json_body,
    parse_opt_bool,
    require_json_object_body,
)
from .deps import get_recorder

router = APIRouter()


def _rotate_and_prune(recorder: Recorder) -> dict[str, Any]:
    """Rotate to a fresh session, THEN prune now-empty sessions. Order matters:
    rotating first makes the previous (now-abandoned, empty) session eligible
    for pruning, while the freshly-minted current session is protected by
    `prune_empty_sessions`' current-session skip.

    Used by the Basic-auth dashboard `/api/new-session` only — the tap endpoint
    deliberately does NOT prune (deleting folders stays an operator action; see
    `api_tap_new_session`).

    Keep it synchronous — never offload the prune below to a thread;
    `prune_empty_sessions` owns that requirement and states why. `_open`'s half
    of the invariant is structural now (it marks the session in-flight before its
    mkdir), so the old "keep `_open` await-free" rule no longer applies. (With
    `--workers > 1` each worker has its own Recorder and the guarantee weakens;
    TapScribe runs single-worker by design.)
    """
    prev, current = recorder.rotate_session()
    prune = prune_empty_sessions(current)
    print(
        f"[tapscribe] new session {current} (previous: {prev}); pruned {prune['count']} empty",
        flush=True,
    )
    return {
        "previous": prev,
        "current": current,
        "path": str(recorder.session_dir),
        "pruned": prune,
    }


@router.post("/api/new-session")
async def api_new_session(recorder: Recorder = Depends(get_recorder)):
    """Rotate the current session and prune now-empty sessions. Already-open
    /tap WebSockets keep writing to their original folder (captured at WS
    open); only new opens land in the new folder."""
    return {"ok": True, **_rotate_and_prune(recorder)}


@router.post("/api/tap/new-session")
async def api_tap_new_session(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Bridge-initiated session rotation, authenticated by the TAP token
    (`Authorization: Bearer <token>`) — NOT dashboard Basic auth — so a browser
    bridge that holds only the tap token can start a fresh session without the
    operator switching to the dashboard. Gated by the auth middleware's
    TAP-BEARER scheme (`config.TAP_PREFIX`), not dashboard Basic auth — the
    handler carries no bearer check of its own (ADR-0008).

    Rotates ONLY — unlike the dashboard's `/api/new-session`, this does NOT
    prune empty sessions. The tap token is a deliberately lower-privilege
    credential handed to browser extensions, so deleting session folders stays
    a Basic-auth action (the dashboard's "+ new session" / "prune empty"). No
    filesystem path is derived from the request (session ids are server-minted
    UTC timestamps and the optional body carries only a boolean), so there is
    no path-injection surface here.

    With a JSON body of `{"detached": true}` the verb creates a DETACHED
    session instead: a fresh session directory is minted and returned WITHOUT
    rotating the global current session, for the bridge to direct its taps
    into via /tap?session=<id> (per-bridge isolation; see "Detached session"
    in CONTEXT.md). The legacy no-body call keeps the rotate semantics below.
    """
    # The body is optional (the legacy no-body call = rotate) but, when
    # present, must parse as a JSON object: a malformed {"detached": true}
    # falling through to the legacy branch would silently rotate the GLOBAL
    # session out from under every plain tap — reject so the bridge retries.
    body = await require_json_object_body(req, allow_empty=True)
    if body.get("detached"):
        session_id, session_dir = recorder.create_detached_session()
        print(
            f"[tapscribe] tap detached session {session_id} (current: {recorder.session_start})",
            flush=True,
        )
        return {
            "ok": True,
            "detached": True,
            "session": session_id,
            "path": str(session_dir),
            "current": recorder.session_start,
        }

    # Idempotency guard (tap path only): if the current session is empty, a
    # rotation would only churn the session-id timestamp — no-op it. The
    # dashboard button keeps always-rotate semantics.
    rotated = not session_is_empty(recorder.session_dir)
    if rotated:
        previous, current = recorder.rotate_session()
        print(f"[tapscribe] tap new session {current} (previous: {previous})", flush=True)
    else:
        previous = recorder.session_start
    return {
        "ok": True,
        "rotated": rotated,
        "previous": previous,
        "current": recorder.session_start,
        "path": str(recorder.session_dir),
    }


async def _trigger_pipeline(recorder: Recorder, session: str) -> dict[str, Any]:
    """Shared body of the two pipeline-trigger twins (tap-bearer and dashboard
    Basic auth): cross the path-safety seam, hand `start_pipeline` a
    session-only `PipelineRequest` (the batch model, backend and summarizer
    resolve from operator config, never the request body), and return the
    running ack. The two routes keep their own decorators and auth boundaries;
    only this fire-and-forget body is shared so they cannot drift."""
    resolve_session_dir(session)  # path-safety seam; 404s unknown/traversal ids
    await start_pipeline(recorder, PipelineRequest(session=session))
    return {"ok": True, "session": session, "state": "running"}


@router.post("/api/tap/sessions/{session}/pipeline", status_code=202)
async def api_tap_pipeline_trigger(session: str, recorder: Recorder = Depends(get_recorder)):
    """Bridge-initiated end-of-meeting pipeline: strip → transcribe →
    summarize the session as ONE session job. Tap-bearer authenticated by the
    auth middleware's TAP-BEARER scheme (`config.TAP_PREFIX`); the handler
    carries no bearer check of its own (ADR-0008).

    Fire-and-forget: the job slot is claimed before this returns (so a
    concurrent trigger or manual transcribe gets a deterministic 409 via
    `SessionBusy`) and the chain runs in the background — poll the GET twin
    for progress and the result.

    The request body is IGNORED entirely, never parsed: the pipeline resolves
    the batch model, backend, and summarizer from operator-side configuration
    (`PipelineRequest` carries only the session), so a tap-token holder can
    never choose which model gets loaded or downloaded."""
    return await _trigger_pipeline(recorder, session)


@router.post("/api/sessions/{session}/pipeline", status_code=202)
async def api_dashboard_pipeline_trigger(session: str, recorder: Recorder = Depends(get_recorder)):
    """Trigger the end-of-meeting pipeline from the dashboard (Basic auth).

    A thin shim over `start_pipeline` — fire-and-forget, 202, body ignored.
    Resolves the batch model, backend, and summarizer from operator-side
    configuration; a dashboard operator can no more pick a model than the
    tap caller can. The request body is IGNORED entirely, never parsed."""
    return await _trigger_pipeline(recorder, session)


@router.get("/api/tap/sessions/{session}/pipeline")
async def api_tap_pipeline_poll(session: str, recorder: Recorder = Depends(get_recorder)):
    """Poll the end-of-meeting pipeline. Tap-bearer authenticated by the auth
    middleware's TAP-BEARER scheme (`config.TAP_PREFIX`), like its POST twin;
    the handler carries no bearer check of its own (ADR-0008).

    Returns stage progress while running (from the live job snapshot), the
    persisted summary when done, the failing stage's domain error when failed.
    `state: "idle"` when this session has no pipeline record and no persisted
    summary.

    The done branch reads session-summary.json rather than process memory,
    so a Bridge that polls across a Recorder restart still gets its summary
    (`recorder.pipelines` is in-memory only and rebuilt empty at boot)."""
    resolve_session_dir(session)  # path-safety seam; 404s unknown/traversal ids

    record = recorder.pipelines.get(session)
    if record is not None and record.state == "running":
        out: dict = {
            "ok": True,
            "session": session,
            "state": "running",
            "started_at": record.started_at.isoformat() if record.started_at else None,
        }
        job = recorder.jobs.get(session)
        if job is not None and job.kind == "pipeline":
            out.update(
                stage=job.stage,
                status=job.status,
                current=job.current,
                total=job.total,
                current_file=job.current_file,
            )
        return out
    if record is not None and record.state == "failed":
        return {
            "ok": True,
            "session": session,
            "state": "failed",
            "stage": record.stage,
            "error": record.error,
            "error_kind": record.error_kind,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        }
    summary = await asyncio.to_thread(read_session_summary, session)
    if record is not None and record.state == "done":
        return {
            "ok": True,
            "session": session,
            "state": "done",
            "summary": summary,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        }
    if summary is not None:
        # No record (Recorder restarted since the run) but a persisted summary
        # exists — that IS the done answer the polling Bridge is waiting for.
        return {"ok": True, "session": session, "state": "done", "summary": summary}
    return {"ok": True, "session": session, "state": "idle"}


@router.put("/api/tap-settings")
async def api_tap_settings_put(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Set per-identity record/live preferences. Body:
    `{"identity": "...", "record"?: bool, "live"?: bool}`. Either field
    may be omitted to leave that preference unchanged. Takes effect on
    the NEXT /tap WebSocket open for this identity — already-open WSes
    finish their current utterance on the bridge's normal close."""
    body = await json_body(req)
    identity = body.get("identity")
    if not isinstance(identity, str) or not identity:
        raise HTTPException(400, "identity required")
    # parse_opt_bool, not bool(): see api_recording_toggle — "false" from a
    # client that stringifies its flags must 400, never silently mean True.
    setting = recorder.tap_settings.set(
        identity,
        record=parse_opt_bool(body.get("record"), "record"),
        live=parse_opt_bool(body.get("live"), "live"),
    )
    return {
        "ok": True,
        "identity": identity,
        "record": setting.record,
        "live": setting.live,
    }


@router.post("/api/recording/toggle")
async def api_recording_toggle(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Flip recorder.recording_enabled. Optional body {"enabled": bool} to
    set explicitly; without a body, just toggles. New /tap WSes are
    accepted then immediately closed when disabled — already-open WAVs
    continue to record their current utterance, which finalises cleanly
    on the bridge's normal trackMuted close."""
    body = await json_body(req)
    if "enabled" in body:
        # parse_opt_bool, not bool(): a client that stringifies its flags would
        # otherwise turn {"enabled": "false"} into ENABLED (bool("false") is
        # True) and keep recording every participant after the operator asked
        # to pause — a wrong-direction privacy bug, not just a bad request.
        enabled = recorder.toggle_recording(enabled=parse_opt_bool(body["enabled"], "enabled"))
    else:
        enabled = recorder.toggle_recording()
    print(f"[tapscribe] recording {'enabled' if enabled else 'paused'}", flush=True)
    return {"ok": True, "enabled": enabled}


@router.websocket("/tap")
async def tap(ws: WebSocket):
    """The Bridge's only endpoint. One WS per utterance.

    The route's job is small: gate auth, resolve the tap's session
    (?session=<id> → that detached session, validated against the
    path-safety seam with the upgrade refused on an unknown id; absent →
    the global current session), accept the upgrade, honour the
    recording-paused toggle, build a TapFanOut, and pump PCM frames into
    it. The fan-out owns WAV writing, UtteranceIndex bookkeeping,
    ActiveStream registration, and the WlKRelay (per ADR-0002).

    Resume: the bridge passes a stable `utterance_id` per unmuted speech
    segment and keeps it across reconnects. The fan-out's `open()`
    consults UtteranceIndex.try_resume and appends to the existing WAV
    when applicable — a network blip mid-utterance no longer fragments
    the recording.

    Graceful degradation (per ADR-0002): if WlK isn't running or the
    relay's connection fails mid-stream, WAV recording continues
    unaffected; the operator sees the live-channel state on the
    dashboard.
    """
    recorder: Recorder | None = getattr(ws.app.state, "recorder", None)
    if recorder is None:
        # Refuse the upgrade before accept so the bridge sees a hard fail
        # rather than an empty open-then-close.
        await ws.close(code=1011, reason="recorder not ready")
        return

    # Auth gate: when AUTH_ENABLED, the bridge must offer a subprotocol of the
    # form `auth.TAP_SUBPROTOCOL_PREFIX + <token>` whose token matches
    # recorder.tap.value. Named rather than spelled out: this module is INSIDE
    # the wire contract's source, so a literal here would be a fifth copy that
    # tools/stamp_tap_wire.py deliberately never writes (#356, ADR-0019).
    # We accept-with-subprotocol on match (browsers require the server to
    # echo one of the offered values), and refuse the upgrade on mismatch.
    accept_subprotocol: str | None = None
    if config.AUTH_ENABLED:
        offered = ws.scope.get("subprotocols") or []
        accept_subprotocol = auth.pick_tap_subprotocol(offered, recorder.tap.value)
        if accept_subprotocol is None:
            await ws.close(code=4401, reason="missing or invalid tap token")
            return

    # Detached-session routing: ?session=<id> pins this tap to an existing
    # session, resolved through the canonical path-safety seam
    # (session_paths.resolve_session_dir). Absent → the recorder's current
    # session. Affiliation is snapshotted here at WS open — like the
    # per-identity record/live prefs below — so a rotation never re-homes
    # an open tap.
    session_param = ws.query_params.get("session")
    if session_param is not None:
        tap_session_dir = resolve_session_dir(session_param)
        tap_session = tap_session_dir.name
    else:
        tap_session = recorder.session_start
        tap_session_dir = recorder.session_dir

    await ws.accept(subprotocol=accept_subprotocol)

    # Honor the operator's pause toggle: accept the WS so the bridge knows
    # we heard the open, then close cleanly.
    if not recorder.recording_enabled:
        await ws.close(code=1000, reason="recording paused by operator")
        return

    identity = ws.query_params.get("identity", "unknown")
    name = ws.query_params.get("name", "")
    utterance_id = ws.query_params.get("utterance_id") or uuid4().hex

    # Per-identity record/live preferences. Snapshotted at WS open —
    # toggling mid-utterance takes effect on the NEXT /tap WS for this
    # identity. Same semantics as the global pause toggle so an operator
    # who flips a switch never has a half-written WAV finalised under
    # them.
    tap_pref = recorder.tap_settings.get(identity)

    async with await TapFanOut.open(
        recorder,
        identity=identity,
        name=name,
        utterance_id=utterance_id,
        do_record=tap_pref.record,
        do_live=tap_pref.live,
        session=tap_session,
        session_dir=tap_session_dir,
    ) as fan_out:
        try:
            while True:
                msg = await ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    break
                if t != "websocket.receive":
                    continue
                buf = msg.get("bytes")
                if buf:
                    await fan_out.write_frame(buf)
        except WebSocketDisconnect:
            # The Bridge closing the /tap WS (end of utterance, or a network
            # drop) raises this — it's the normal termination path, not an
            # error. Swallow it and let the TapFanOut context manager finalize
            # the WAV on exit; nothing is lost.
            pass
        except Exception as e:  # pragma: no cover
            print(f"[tapscribe] /tap error for {utterance_id}: {e}", flush=True)
