"""FastAPI app + HTTP / WebSocket routes.

Routes receive the running `Recorder` via `Depends(get_recorder)`, which
reads from `request.app.state.recorder`. `__main__.py` constructs the
Recorder and attaches it before uvicorn starts; tests build their own
and override `app.state.recorder` for isolation.

The big-picture route map:

  GET  /                        — the Stages dashboard HTML shell (next.html)
  GET  /api/state               — sessions + active streams + live channel
  POST /api/transcribe          — batch transcribe one WAV
  POST /api/transcribe-session  — merge per-WAV transcripts into a session
  POST /api/live/start          — start / restart whisperlivekit-server
  POST /api/live/stop           — stop whisperlivekit-server
  DELETE /api/live-transcript   — clear the live transcripts feed (dashboard "clear")
  WS   /tap?identity&name       — Bridge audio in (one WS per utterance);
                                  Recorder fans out to WAV + WlK relay (ADR-0002).
                                  Auth: Sec-WebSocket-Protocol "tapscribe.v1.tap.<token>"
                                  when AUTH_ENABLED; gate is in the route handler.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, config
from . import hallucinations as hallucinations_mod
from .audio import compute_peaks
from .batch_strip import StrippedDirUnclearable, StripSessionRequest, strip_session
from .batch_summarize import NoMergedTranscript, SummarizeSessionRequest, summarize_session
from .batch_transcribe import (
    BatchOneRequest,
    BatchSessionRequest,
    WavTooQuiet,
    WavUnreadable,
    transcribe_one,
    transcribe_session,
)
from .recorder import Recorder, SessionBusy
from .session_maintenance import (
    absorb_session,
    delete_session_audio,
    delete_session_wav,
    prune_empty_sessions,
    session_is_empty,
)
from .session_merge import InvalidRange, NoUsableWavs
from .session_paths import resolve_session_dir, resolve_wav, stripped_dir
from .sessions import (
    gather_sessions,
    read_session_meta,
    read_session_summary,
    read_session_transcript,
    read_wav_strip_meta,
    read_wav_transcript,
    write_session_meta,
)
from .summarizers import SummarizerFailed, SummarizerUnavailable, summary_model_catalog
from .summarizers.catalog import _MAX_TOKENS_BOUNDS
from .tap_fan_out import TapFanOut
from .text import (
    MAX_CONFIG_TEXT_LEN,
    read_hotwords,
    read_live_model,
    read_live_prompt,
    read_prompt,
    write_hotwords,
    write_live_model,
    write_live_prompt,
    write_prompt,
)
from .transcribers import evict_idle_now, run_on_model_thread
from .transcribers.catalog import REGISTRY, available_backends
from .wav_cache import set_primary_transcript


def _available_backends_snapshot() -> frozenset[str]:
    """`available_backends()` returns the cached set; expose as plain set
    of strings for the JSON serialiser."""
    return frozenset(str(k) for k in available_backends())


def _compute_inputs_support() -> dict[str, bool]:
    """Derive per-context support flags for the dashboard editors.

    The dashboard hides each editor when no installed model in that
    context declares the corresponding input. We compute this from the
    registry (`ModelEntry.inputs`) so adding a future Voxtral prompt
    field (or removing one) automatically updates the UI gating with
    no manual flag-flipping.

    `live_hotwords` is intentionally not exposed: WhisperLiveKit's CLI
    has no --hotwords flag (see `build_live_cmd`), so even though
    Whisper-family entries declare hotwords in `WHISPER_INPUTS`, the
    live channel can't currently consume them.
    """

    def _any_installed_has(context: str, input_name: str) -> bool:
        for entry in REGISTRY.for_context(context, only_installed=True):  # type: ignore[arg-type]
            for inp in entry.inputs:
                if inp.name == input_name:
                    return True
        return False

    return {
        "live_prompt": _any_installed_has("live", "initial_prompt"),
        "batch_prompt": _any_installed_has("batch", "initial_prompt"),
        "batch_hotwords": _any_installed_has("batch", "hotwords"),
    }


# Map of config key (URL segment) → writer. Keeps the PUT handler one
# branch deep and makes the supported keys easy to grep for.
_CONFIG_WRITERS = {
    "prompt": write_prompt,
    "live-prompt": write_live_prompt,
    "live-model": write_live_model,
    "hotwords": write_hotwords,
}


# ---------------------------------------------------------------------------
# Dependency injection — every route reads the Recorder via Depends
# ---------------------------------------------------------------------------


def get_recorder(request: Request) -> Recorder:
    """FastAPI dependency that returns the singleton Recorder attached to
    the app instance. Tests override this via `app.dependency_overrides[
    get_recorder] = lambda: my_recorder` for per-test isolation."""
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        raise HTTPException(503, "Recorder not attached to app.state")
    return recorder


async def _json_body(req: Request) -> dict[str, Any]:
    """Return the request body parsed as a dict, or {} on any failure.
    Routes that want to *require* a JSON object body call this then
    branch on emptiness; routes that treat the body as optional just use
    the dict directly."""
    try:
        body = await req.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _parse_bounded_float(raw, field: str, *, lo: float, hi: float) -> float | None:
    """Parse an optional numeric body field with range enforcement.
    None / missing → returned unchanged so the downstream "field not
    supplied" semantics still work. Anything else must round-trip
    through `float()`, be finite, and land in [lo, hi]; otherwise
    raise 400. The explicit finite check matters because
    `lo <= NaN <= hi` is always False AND `NaN` happily survives
    `float()` — without the check a `{"gate_speech_threshold": NaN}`
    payload would slip past with a confusing "must be in […]" error
    that names NaN as the offending value."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        # `from None` — CodeQL py/stack-trace-exposure: chain adds nothing
        # the detail message doesn't already convey.
        raise HTTPException(400, f"{field} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise HTTPException(400, f"{field} must be a finite number, got {value}")
    if not (lo <= value <= hi):
        raise HTTPException(400, f"{field} must be in [{lo}, {hi}], got {value}")
    return value


def _parse_bounded_int(raw, field: str, *, lo: int, hi: int) -> int | None:
    if raw is None:
        return None
    try:
        # Accept JSON numerics (which arrive as float in some clients)
        # by routing through float→int — rejects "3.5" implicitly.
        value = int(raw)
    except (TypeError, ValueError):
        # `from None` — see _parse_bounded_float for the rationale.
        raise HTTPException(400, f"{field} must be an integer, got {raw!r}") from None
    if not (lo <= value <= hi):
        raise HTTPException(400, f"{field} must be in [{lo}, {hi}], got {value}")
    return value


# ---------------------------------------------------------------------------
# Logging — silence the per-second poll spam
# ---------------------------------------------------------------------------


class _SuppressPollAccess(logging.Filter):
    """Drop uvicorn access logs for the dashboard's per-second poll
    endpoints so the terminal isn't flooded. Real activity (POST
    /api/transcribe, DELETE /api/sessions/..., websocket records) still
    surfaces."""

    _SILENCED = ("/api/state", "/dashboard.css", "/next.css", "/web/", "/health", "/healthz")

    def filter(self, record):
        msg = record.getMessage()
        for needle in self._SILENCED:
            if needle in msg:
                return False
        return True


# ---------------------------------------------------------------------------
# Lifespan: install log filter + (optionally) auto-start the live channel
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Install poll-spam filter on the uvicorn access logger. We do this in
    # the lifespan (not module load) because uvicorn replaces the access
    # logger's handlers via dictConfig() during its own boot — anything we
    # add before uvicorn.run() would be dropped.
    logging.getLogger("uvicorn.access").addFilter(_SuppressPollAccess())

    # JSON logging is opt-in via --log-json (set on app.state in __main__).
    # Same dictConfig race as the poll filter — apply here, not at import.
    if getattr(app.state, "log_json", False):
        from .logging_setup import install_json_logging

        install_json_logging()

    recorder: Recorder | None = getattr(app.state, "recorder", None)
    if recorder is not None and config.AUTO_START_LIVE:
        ok, msg = recorder.live.start()
        if not ok:
            print(f"[tapscribe] live auto-start skipped: {msg}", flush=True)
    try:
        yield
    finally:
        if recorder is not None:
            recorder.live.stop(timeout=3.0)


app = FastAPI(title="TapScribe recorder", lifespan=_lifespan)
# Compress responses (the dashboard polls /api/state ~1-2×/s; even the slimmed
# listing is highly compressible JSON). minimum_size skips tiny bodies where the
# gzip header would cost more than it saves.
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(auth.basic_auth_middleware)


# ---------------------------------------------------------------------------
# Domain error → HTTP status. ONE source of truth: the orchestrators raise
# FastAPI-free domain errors (SessionBusy, NoUsableWavs, SummarizerFailed, …)
# and these handlers translate them, so every batch route is just
# `return await orchestrator(...)` instead of a per-route try/except ladder. A
# domain error's HTTP meaning is intrinsic (busy is always 409), so it's
# registered once here rather than re-mapped in each route that can raise it.
# ---------------------------------------------------------------------------

_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    SessionBusy: 409,
    NoUsableWavs: 404,
    InvalidRange: 400,
    WavUnreadable: 422,
    WavTooQuiet: 422,
    StrippedDirUnclearable: 500,
    NoMergedTranscript: 422,
    SummarizerUnavailable: 400,
    SummarizerFailed: 502,
}


async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate a known domain error to its status; anything unmapped falls to
    500, so a new orchestrator error can't silently slip through as a 200."""
    return JSONResponse(status_code=_DOMAIN_ERROR_STATUS.get(type(exc), 500), content={"detail": str(exc)})


for _exc_type in _DOMAIN_ERROR_STATUS:
    app.add_exception_handler(_exc_type, _domain_error_handler)


# ---------------------------------------------------------------------------
# Health + simple listings
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(recorder: Recorder = Depends(get_recorder)):
    return {"status": "ok", "session_dir": str(recorder.session_dir)}


@app.get("/healthz")
async def healthz(recorder: Recorder = Depends(get_recorder)):
    """Liveness + readiness probe for monitoring (k8s, systemd watchdog,
    plain curl). No auth required — must also be exempt in
    `config.AUTH_EXEMPT_ROUTES`. The richer shape vs `/health` lets an
    operator alert on meaningful state (recording paused unexpectedly,
    live channel stuck in `error`) without scraping `/api/state`."""
    from . import __version__

    active_streams = await recorder.streams.snapshot()
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "recording_enabled": recorder.recording_enabled,
            "live_channel_state": recorder.live.info.get("state", ""),
            "active_taps": len(active_streams),
        }
    )


@app.get("/sessions")
async def list_sessions_simple(recorder: Recorder = Depends(get_recorder)):
    """Legacy simple listing."""
    return gather_sessions(
        current_session=recorder.session_start,
        jobs={k: asdict(v) for k, v in recorder.jobs.snapshot().items()},
    )


def _rotate_and_prune(recorder: Recorder) -> dict[str, Any]:
    """Rotate to a fresh session, THEN prune now-empty sessions. Order matters:
    rotating first makes the previous (now-abandoned, empty) session eligible
    for pruning, while the freshly-minted current session is protected by
    `prune_empty_sessions`' current-session skip.

    Used by the Basic-auth dashboard `/api/new-session` only — the tap endpoint
    deliberately does NOT prune (deleting folders stays an operator action; see
    `api_tap_new_session`).

    Prune runs synchronously (not offloaded to a thread): single-threaded
    asyncio then guarantees no `/tap` upload can interleave and have its
    just-created session folder deleted mid-walk. Keep it synchronous — and
    `TapFanOut._open` await-free between mkdir and wave-open — or that race
    reopens. (With `--workers > 1` each worker has its own Recorder and the
    guarantee weakens; TapScribe runs single-worker by design.)
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


@app.post("/api/new-session")
async def api_new_session(recorder: Recorder = Depends(get_recorder)):
    """Rotate the current session and prune now-empty sessions. Already-open
    /tap WebSockets keep writing to their original folder (captured at WS
    open); only new opens land in the new folder."""
    return {"ok": True, **_rotate_and_prune(recorder)}


@app.post("/api/tap/new-session")
async def api_tap_new_session(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Bridge-initiated session rotation, authenticated by the TAP token
    (`Authorization: Bearer <token>`) — NOT dashboard Basic auth — so a browser
    bridge that holds only the tap token can start a fresh session without the
    operator switching to the dashboard. Exempt from the Basic-auth middleware
    (`config.AUTH_EXEMPT_ROUTES`); the bearer check below is the gate.

    Rotates ONLY — unlike the dashboard's `/api/new-session`, this does NOT
    prune empty sessions. The tap token is a deliberately lower-privilege
    credential handed to browser extensions, so deleting session folders stays
    a Basic-auth action (the dashboard's "+ new session" / "prune empty"). No
    filesystem path is derived from the request (the new session id is a
    server-minted UTC timestamp), so there is no path-injection surface here.
    """
    if config.AUTH_ENABLED and not auth.check_tap_bearer(
        req.headers.get("authorization"), recorder.tap.value
    ):
        return JSONResponse({"detail": "invalid tap token"}, status_code=401)

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


# ---------------------------------------------------------------------------
# /api/state — the dashboard's once-per-second polling endpoint
# ---------------------------------------------------------------------------


def _build_state_blob(current_session: str, jobs_snapshot: dict[str, Any]) -> dict[str, Any]:
    """The blocking, disk-bound half of /api/state: walk every session +
    WAV (gather_sessions) and read the editable config files. Pulled into
    one function so api_state can run it on a worker thread — left inline
    it would block the single event loop for the duration of the walk and
    serialise the operator's click POSTs behind the poll."""
    return {
        "sessions": gather_sessions(current_session=current_session, jobs=jobs_snapshot),
        "prompt": read_prompt(),
        "live_prompt": read_live_prompt(),
        "live_model_default": read_live_model(),
        "hotwords": read_hotwords(),
        "halluc_rules": hallucinations_mod.parse_rules(),
        "inputs_support": _compute_inputs_support(),
    }


@app.get("/api/state")
async def api_state(req: Request, recorder: Recorder = Depends(get_recorder)):
    active_streams = await recorder.streams.snapshot()
    jobs_snapshot = {k: asdict(v) for k, v in recorder.jobs.snapshot().items()}
    blob = await asyncio.to_thread(_build_state_blob, recorder.session_start, jobs_snapshot)
    prompt = blob["prompt"]
    live_prompt = blob["live_prompt"]
    hotwords = blob["hotwords"]
    halluc_rules = blob["halluc_rules"]
    inputs_support = blob["inputs_support"]
    # The per-row rec/live toggles control the per-identity preference,
    # not the in-flight WS snapshot — so the button state needs to track
    # the current preference, otherwise clicks land server-side but the
    # UI never flips and looks like the buttons do nothing.
    active = []
    for s in active_streams:
        row = asdict(s)
        pref = recorder.tap_settings.get(s.identity)
        row["record"] = pref.record
        row["live"] = pref.live
        active.append(row)
    sessions_list = blob["sessions"]
    # Powers the "· N sessions override this" footer in the default config panel.
    override_counts = {"prompt": 0, "hotwords": 0}
    for s in sessions_list:
        m = s.get("session_meta") or {}
        if m.get("prompt"):
            override_counts["prompt"] += 1
        if m.get("hotwords"):
            override_counts["hotwords"] += 1
    payload = {
        "current_session": recorder.session_start,
        "active": active,
        "sessions": sessions_list,
        "default_override_counts": override_counts,
        "live_feed": recorder.transcripts.snapshot(),
        "live_info": dict(recorder.live.info),
        "live_log": list(recorder.live.log)[-30:],
        "live_supports_native_vad": bool(getattr(recorder.live, "supports_native_vad", False)),
        "backend": recorder.backend,
        "available_backends": sorted(_available_backends_snapshot()),
        "recording_enabled": recorder.recording_enabled,
        "prompt": {
            "path": str(config.PROMPT_FILE),
            "content": prompt,
            "length": len(prompt),
        },
        "live_prompt": {
            "path": str(config.LIVE_PROMPT_FILE),
            "content": live_prompt,
            "length": len(live_prompt),
        },
        "hotwords": {
            "path": str(config.HOTWORDS_FILE),
            "content": hotwords,
            "length": len(hotwords),
        },
        "inputs_support": inputs_support,
        "live_model_default": blob["live_model_default"],
        "hallucinations": {
            "path": str(config.HALLUCINATIONS_FILE),
            "rules": [r["raw"] for r in halluc_rules],
            "count": len(halluc_rules),
        },
    }
    # Conditional GET: hash the (compact) body into a weak ETag and answer 304
    # when the dashboard's If-None-Match still matches. The poll fires every
    # ~0.5-1s; at idle the payload is byte-identical, so the client reuses its
    # cached state and skips the parse + state-object allocation. Weak ETag
    # (W/) because GZipMiddleware re-encodes the body — the validator is over
    # the semantic content, not the on-wire bytes. During capture the payload
    # changes each tick, so this only short-circuits genuine no-ops.
    body = json.dumps(jsonable_encoder(payload), separators=(",", ":")).encode("utf-8")
    etag = 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if req.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


# ---------------------------------------------------------------------------
# Live channel control
# ---------------------------------------------------------------------------


@app.post("/api/live/start")
async def api_live_start(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Start the live channel (whisperlivekit-server). If already running
    with a different model/language, restarts it; if already running with
    the same config, no-op.

    Spawn/stop are synchronous and can block for several seconds — so we
    offload to a worker thread to keep /api/state polling responsive.
    """
    body = await _json_body(req)
    model = (body.get("model") or "").strip() or None
    language = (body.get("language") or "").strip() or None
    conf = body.get("confidence_validation")

    # Boundary validation. CodeQL treats Request.json() as untrusted
    # input; the dashboard's HTML min/max attributes are only client-
    # side hints. Anything that fails the checks here returns 400 —
    # don't let it surface deeper as a ValueError 500.
    gate_kind_raw = body.get("gate_kind")
    gate_kind = (gate_kind_raw or "").strip() or None
    if gate_kind is not None and gate_kind not in ("tapscribe", "backend"):
        raise HTTPException(400, f"gate_kind must be 'tapscribe' or 'backend', got {gate_kind!r}")
    if gate_kind == "backend" and not getattr(recorder.live, "supports_native_vad", False):
        # Stale-dashboard guard: a future Parakeet / Canary live channel
        # has no native VAD, so "backend" gating would silently leave
        # no gate at all. UI auto-greys this, but old clients won't.
        raise HTTPException(
            400,
            "current live channel has no native VAD; gate_kind='backend' is not supported",
        )

    gate_speech_threshold = _parse_bounded_float(
        body.get("gate_speech_threshold"), "gate_speech_threshold", lo=0.0, hi=1.0
    )
    gate_hangover_ms = _parse_bounded_int(body.get("gate_hangover_ms"), "gate_hangover_ms", lo=0, hi=10_000)
    gate_pre_roll_ms = _parse_bounded_int(body.get("gate_pre_roll_ms"), "gate_pre_roll_ms", lo=0, hi=5_000)
    gate_min_speech_ms = _parse_bounded_int(
        body.get("gate_min_speech_ms"), "gate_min_speech_ms", lo=0, hi=5_000
    )

    if recorder.live.matches(
        model=model,
        language=language,
        gate_kind=gate_kind,
        conf=conf,
        gate_speech_threshold=gate_speech_threshold,
        gate_hangover_ms=gate_hangover_ms,
        gate_pre_roll_ms=gate_pre_roll_ms,
        gate_min_speech_ms=gate_min_speech_ms,
    ):
        return {
            "ok": True,
            "msg": "already running with requested config",
            "state": recorder.live.info["state"],
        }

    # Announce the transition (replaces gate config + conf in LiveConfig,
    # flips info to "starting" with the new model/language) BEFORE we
    # tear down the old child or fetch weights — otherwise dashboards
    # polling /api/state during the stop→start window would render the
    # previous selection.
    recorder.live.begin_transition(
        model=model,
        language=language,
        gate_kind=gate_kind,
        conf=conf,
        gate_speech_threshold=gate_speech_threshold,
        gate_hangover_ms=gate_hangover_ms,
        gate_pre_roll_ms=gate_pre_roll_ms,
        gate_min_speech_ms=gate_min_speech_ms,
    )

    if recorder.live.running():
        await asyncio.to_thread(recorder.live.stop)
        # stop() sets state="stopped"; re-announce so the dashboard stays
        # on "starting" with the new model.
        recorder.live.begin_transition(model=model, language=language)

    ok, msg = await asyncio.to_thread(recorder.live.start, model=model, language=language)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg, "state": recorder.live.info["state"]}


@app.post("/api/live/stop")
async def api_live_stop(recorder: Recorder = Depends(get_recorder)):
    ok, msg = await asyncio.to_thread(recorder.live.stop)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg, "state": recorder.live.info["state"]}


@app.get("/api/live/log")
async def api_live_log(recorder: Recorder = Depends(get_recorder)):
    """Full WhisperLiveKit log tail (up to 200 lines) — the source for
    the dashboard's log dialog. /api/state only sends a small preview
    so the once-per-second poll stays cheap; this endpoint is requested
    on demand when the operator opens the dialog."""
    return {
        "log": list(recorder.live.log),
        "state": recorder.live.info.get("state", ""),
    }


@app.get("/api/models")
async def api_models(context: str = "batch"):
    """List every model the registry knows about, filtered by context.

    Drives the dashboard's batch + live model pickers (each calls with
    `?context=batch` and `?context=live` respectively). The response also
    includes the operator's available backends so the UI can gray out
    backend chips for kinds that aren't installed on this machine.

    Response shape:
      {
        "context": "batch" | "live",
        "available_backends": ["cpu", "cuda", ...],
        "models": [ {model_id, family, display_name, description,
                     languages, contexts, backends, inputs, available}, ... ]
      }
    """
    if context not in ("batch", "live"):
        raise HTTPException(400, f"context must be 'batch' or 'live' (got {context!r})")
    # `only_installed` filters out families whose adapter packages weren't
    # selected at install time (the picker in tools/install_picker.py only
    # pulls in extras the operator ticks). Without this filter, the
    # dashboard would advertise Parakeet/Canary even on machines that
    # skipped the NeMo install — and the operator would only find out by
    # clicking and hitting the lazy-import error.
    entries = REGISTRY.for_context(context, only_installed=True)  # type: ignore[arg-type]
    return {
        "context": context,
        "available_backends": sorted(_available_backends_snapshot()),
        "models": [e.to_mapping() for e in entries],
    }


@app.delete("/api/models/cache")
async def api_models_cache_clear(recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """Evict every idle (not-in-use) transcription model from the in-process
    cache, freeing its weights + pooled GPU memory now.

    Batch models are unloaded automatically per the TAPSCRIBE_MODEL_IDLE_TTL_S
    policy (default: immediately after each job). This endpoint is the manual
    lever for operators who set a keep-warm TTL (or disabled eviction) and
    want to reclaim RAM/VRAM on demand. An in-flight transcribe keeps its
    model, so clicking this can't yank a model out from under a running job.
    The live channel runs in its own subprocess and is unaffected — stop it
    via /api/live/stop to reclaim that memory."""
    # On the dedicated model thread: eviction calls mlx.core.clear_cache(),
    # which (like every MLX op) must run on the thread that holds the Metal
    # stream — see run_on_model_thread.
    freed = await run_on_model_thread(evict_idle_now)
    print(f"[tapscribe] evicted {freed} idle transcription model(s) from cache", flush=True)
    return {"ok": True, "evicted": freed}


@app.delete("/api/live-transcript")
async def api_live_transcript_clear(recorder: Recorder = Depends(get_recorder)):
    """Clear the live transcript feed (the dashboard's "clear" button).

    Note: there is no POST counterpart anymore. Bridges send audio to /tap;
    settled lines are consumed by the Recorder's internal WlK relay and
    appended to recorder.transcripts directly. See ADR-0002.
    """
    recorder.transcripts.clear()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Recording toggle
# ---------------------------------------------------------------------------


@app.put("/api/tap-settings")
async def api_tap_settings_put(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Set per-identity record/live preferences. Body:
    `{"identity": "...", "record"?: bool, "live"?: bool}`. Either field
    may be omitted to leave that preference unchanged. Takes effect on
    the NEXT /tap WebSocket open for this identity — already-open WSes
    finish their current utterance on the bridge's normal close."""
    body = await _json_body(req)
    identity = body.get("identity")
    if not isinstance(identity, str) or not identity:
        raise HTTPException(400, "identity required")
    record = body.get("record")
    live = body.get("live")
    setting = recorder.tap_settings.set(
        identity,
        record=bool(record) if record is not None else None,
        live=bool(live) if live is not None else None,
    )
    return {
        "ok": True,
        "identity": identity,
        "record": setting.record,
        "live": setting.live,
    }


@app.post("/api/recording/toggle")
async def api_recording_toggle(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Flip recorder.recording_enabled. Optional body {"enabled": bool} to
    set explicitly; without a body, just toggles. New /record WSes are
    accepted then immediately closed when disabled — already-open WAVs
    continue to record their current utterance, which finalises cleanly
    on the bridge's normal trackMuted close."""
    body = await _json_body(req)
    if "enabled" in body:
        enabled = recorder.toggle_recording(enabled=bool(body["enabled"]))
    else:
        enabled = recorder.toggle_recording()
    print(f"[tapscribe] recording {'enabled' if enabled else 'paused'}", flush=True)
    return {"ok": True, "enabled": enabled}


# ---------------------------------------------------------------------------
# Editable config — dashboard "save" buttons for prompt / live-prompt /
# hotwords. Atomic via tempfile + rename inside the writer helpers.
# ---------------------------------------------------------------------------


@app.put("/api/config/{key}")
async def api_config_put(key: str, req: Request):
    writer = _CONFIG_WRITERS.get(key)
    if writer is None:
        raise HTTPException(404, f"unknown config key: {key!r}")
    body = await _json_body(req)
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(400, "content must be a string")
    if len(content) > MAX_CONFIG_TEXT_LEN:
        raise HTTPException(
            400,
            f"content exceeds {MAX_CONFIG_TEXT_LEN}-char cap (got {len(content)})",
        )
    try:
        writer(content)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(500, f"failed to write config: {e}") from None
    return {"ok": True, "key": key, "length": len(content)}


# ---------------------------------------------------------------------------
# Session housekeeping
# ---------------------------------------------------------------------------


@app.post("/api/sessions/prune-empty")
async def api_sessions_prune_empty(recorder: Recorder = Depends(get_recorder)):
    """Delete every session folder that has zero WAVs, no merged
    transcript, and no operator-set label. Skips the CURRENT session."""
    result = prune_empty_sessions(recorder.session_start)
    print(f"[tapscribe] pruned {result['count']} empty sessions", flush=True)
    return {"ok": True, **result}


@app.post("/api/sessions/{session}/strip-silence")
async def api_session_strip_silence(
    session: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
):
    """Non-destructively strip silence from every WAV in <session>/. Thin
    HTTP shim over `batch_strip.strip_session` — parse + range-bound the knobs;
    the registered domain-error handlers map failures to status codes."""
    body = await _json_body(req)
    # Range-bound everything that hits the silero detector so a malformed
    # dashboard POST returns 400 instead of a 500 from int()/float().
    # Only explicitly-provided knobs are forwarded — StripSessionRequest
    # owns the defaults — and the `is not None` checks keep an explicit 0
    # (e.g. pad_ms=0 to disable region padding for A/B) from silently
    # falling back to the default.
    overrides: dict[str, Any] = {}
    min_silence_ms = _parse_bounded_int(body.get("min_silence_ms"), "min_silence_ms", lo=100, hi=600_000)
    if min_silence_ms is not None:
        overrides["min_silence_ms"] = min_silence_ms
    pad_ms = _parse_bounded_int(body.get("pad_ms"), "pad_ms", lo=0, hi=5_000)
    if pad_ms is not None:
        overrides["pad_ms"] = pad_ms
    speech_floor_db = _parse_bounded_float(body.get("speech_floor_db"), "speech_floor_db", lo=-120.0, hi=0.0)
    if speech_floor_db is not None:
        overrides["speech_floor_db"] = speech_floor_db

    return await strip_session(recorder, StripSessionRequest(session=session, **overrides))


@app.post("/api/sessions/{session}/summarize")
async def api_session_summarize(
    session: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
):
    """Summarize a session's merged transcript. Thin HTTP shim over
    `batch_summarize.summarize_session` — parse the body; the registered
    domain-error handlers map failures to status codes. For this slice the
    source / command / prompt arrive in the body (no saved config yet); the
    Local (bundled, offline — #86) and Command (#82) sources are wired, while
    the API source (#85) still maps to a clear 400."""
    body = await _json_body(req)
    # Forward only explicitly-provided fields and let SummarizeSessionRequest own
    # the defaults (source="command", prompt=DEFAULT_SUMMARY_PROMPT) — the same
    # "value object owns the defaults" contract as the strip-silence route.
    overrides: dict[str, Any] = {}
    source = body.get("source")
    if isinstance(source, str) and source.strip():
        overrides["source"] = source.strip()
    command = body.get("command")
    if isinstance(command, str):
        overrides["command"] = command.strip()
    model = body.get("model")
    if isinstance(model, str) and model.strip():
        overrides["model"] = model.strip()
    # max_tokens: parse + bounds-check exactly like the other numeric body knobs
    # (gate / strip-silence) — a clear 400 for out-of-range, None when omitted.
    # The adapter also clamps as a final safety net for non-route callers.
    max_tokens = _parse_bounded_int(
        body.get("max_tokens"), "max_tokens", lo=_MAX_TOKENS_BOUNDS[0], hi=_MAX_TOKENS_BOUNDS[1]
    )
    if max_tokens is not None:
        overrides["max_tokens"] = max_tokens
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        overrides["prompt"] = prompt
    return await summarize_session(recorder, SummarizeSessionRequest(session=session, **overrides))


@app.get("/api/summarize/models")
async def api_summarize_models():
    """List the local summarizer's selectable models for THIS machine's backend.

    Drives the Summary view's model dropdown. The backend is hardware-routed
    (MLX on Apple Silicon, GGUF/CPU elsewhere — the same probe the summarizer
    uses), so a Mac sees the MLX catalog and a Linux/CUDA box sees the GGUF one.
    The catalog is also the allowlist the local source validates a picked model
    against, so the dropdown can only ever offer loadable choices.

    Response: `{ "backend", "default", "models": [{repo_id, label, approx_gb,
    context_tokens, note, is_default}, ...] }`."""
    return summary_model_catalog()


@app.delete("/api/sessions/{session}/stripped")
async def api_session_stripped_delete(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """Remove a session's stripped/ folder so it can be regenerated."""
    resolve_session_dir(session)
    d = stripped_dir(session)
    if not d.is_dir():
        return {"ok": True, "deleted": False, "reason": "no stripped/ folder"}
    try:
        shutil.rmtree(d)
    except OSError as e:
        raise HTTPException(500, f"delete failed: {e}") from None
    print(f"[tapscribe] removed stripped/ from session: {session}", flush=True)
    return {"ok": True, "deleted": True}


@app.delete("/api/sessions/{session}/audio")
async def api_session_audio_delete(session: str, recorder: Recorder = Depends(get_recorder)):
    """Delete ALL of a session's audio (original WAVs + stripped/ + per-WAV
    transcript-cache sidecars) to reclaim disk. KEEPS the merged
    session-transcript + session-meta. Refuses the CURRENT session and any
    session with a transcribe/strip job in flight."""
    if session == recorder.session_start:
        raise HTTPException(409, "cannot delete audio from the current session — rotate to a new one first")
    resolve_session_dir(session)
    if recorder.jobs.get(session) is not None:
        raise HTTPException(409, "a transcribe or strip job is in flight on this session")
    # Offload the filesystem walk (many WAVs + .transcripts/ dirs) so the
    # ~1 Hz /api/state poll stays responsive — same as strip-silence.
    summary = await asyncio.to_thread(delete_session_audio, session)
    # NB: do NOT release jobs here — unlike whole-session delete, the
    # session survives, and the guard above already ensures none is running.
    print(
        f"[tapscribe] deleted audio from session {session}: "
        f"{summary['wavs_deleted']} wavs, {summary['bytes_freed']} bytes freed",
        flush=True,
    )
    return {"ok": True, **summary}


@app.post("/api/sessions/{target}/absorb")
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
    Refuses if either side has an in-flight transcribe / strip job.
    """
    body = await _json_body(req)
    source = body.get("source") or ""
    if not isinstance(source, str) or not source:
        raise HTTPException(400, "source session id required")
    if source == target:
        raise HTTPException(400, "cannot absorb a session into itself")
    if source == recorder.session_start:
        raise HTTPException(
            409,
            "cannot absorb the current session — rotate to a new one first, "
            "then absorb the now-previous folder into the target",
        )
    # Both sides must exist before we even look at jobs.
    resolve_session_dir(target)
    resolve_session_dir(source)
    if recorder.jobs.get(target) is not None or recorder.jobs.get(source) is not None:
        raise HTTPException(409, "a transcribe or strip job is in flight on one of these sessions")

    summary = absorb_session(target, source)
    print(
        f"[tapscribe] absorbed {source} into {target}: "
        f"{summary['wavs_moved']} wavs, {summary['stripped_moved']} stripped, "
        f"+{len(summary['aliases_added'])} aliases",
        flush=True,
    )
    return {"ok": True, **summary}


@app.delete("/api/sessions/{session}")
async def api_session_delete(session: str, recorder: Recorder = Depends(get_recorder)):
    """Recursively delete a recordings folder. Refuses the CURRENT session."""
    if session == recorder.session_start:
        raise HTTPException(409, "cannot delete the current session — rotate to a new one first")
    session_dir = resolve_session_dir(session)
    try:
        shutil.rmtree(session_dir)
    except OSError as e:
        raise HTTPException(500, f"delete failed: {e}") from None
    await recorder.jobs.release(session)
    print(f"[tapscribe] deleted session: {session_dir}", flush=True)
    return {"ok": True, "deleted": session}


@app.get("/api/session-meta/{session}")
async def api_session_meta_get(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    resolve_session_dir(session)
    return read_session_meta(session)


@app.put("/api/session-meta/{session}")
async def api_session_meta_put(session: str, req: Request, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    resolve_session_dir(session)
    write_session_meta(session, await _json_body(req))
    return {"ok": True, "meta": read_session_meta(session)}


@app.get("/api/sessions/{session}/transcript")
async def api_session_transcript(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """The FULL merged session-transcript.json (or null when none).

    Lazy companion to `/api/state`, whose `session_transcript` is now a slim
    marker. The dashboard fetches this once per (session, transcribed_at) when
    a session is opened and caches it client-side, so the heavy segments[] /
    plain_text / suppressed[] body crosses the wire on open, not every poll.
    The disk read is offloaded with to_thread like the rest of the poll path."""
    return await asyncio.to_thread(read_session_transcript, session)


@app.get("/api/sessions/{session}/summary")
async def api_session_summary(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """The FULL persisted session summary (or null when none).

    Lazy companion to `/api/state`, whose `session_summary` is a slim marker
    (summarized_at + source + model). The dashboard fetches this once per
    (session, summarized_at) when the Summary stage is opened and caches it
    client-side. The disk read is offloaded with to_thread like the rest of
    the poll path."""
    return await asyncio.to_thread(read_session_summary, session)


# ---------------------------------------------------------------------------
# WAV download + transcription
# ---------------------------------------------------------------------------


@app.get("/api/wav/{session}/{name}")
async def get_wav(session: str, name: str, source: str = "original"):
    """Download a WAV. source=stripped pulls from <session>/stripped/."""
    path = resolve_wav(session, name, source)
    dl_name = ("stripped-" + name) if source == "stripped" else name
    return FileResponse(path, media_type="audio/wav", filename=dl_name)


@app.get("/api/wav/{session}/{name}/transcript")
async def api_wav_transcript(session: str, name: str, source: str = "original"):
    """The FULL primary cached transcript for one WAV (or null when none).

    Lazy companion to `/api/state`, whose per-WAV `transcript` is now a slim
    marker. The dashboard fetches this when a WAV row is expanded and caches
    it per (session, name, source, transcribed_at). Mirrors `get_wav`'s
    path-safety (resolve_wav validates session/name/source) and offloads the
    disk read with to_thread."""
    if source not in ("original", "stripped"):
        raise HTTPException(400, f"source must be 'original' or 'stripped', got {source!r}")
    return await asyncio.to_thread(read_wav_transcript, session, name, source)


@app.get("/api/wav/{session}/{name}/strip-meta")
async def api_wav_strip_meta(session: str, name: str):
    """The committed strip-silence cut for one ORIGINAL wav (or null when the
    session was never stripped or this wav produced no regions). Lazy
    companion to /api/state, same contract as the transcript sidecar route:
    resolve_wav path-safety inside the reader, disk read off the event loop."""
    return await asyncio.to_thread(read_wav_strip_meta, session, name)


# Waveform downsample resolution. The route CLAMPS the operator-supplied bins
# into this band rather than 422-ing — a fixed payload size is the whole point,
# and the dashboard never needs more than a few thousand bars on screen.
_PEAKS_BINS_DEFAULT = 800
_PEAKS_BINS_MIN = 16
_PEAKS_BINS_MAX = 2000


@app.get("/api/wav/{session}/{name}/peaks")
async def api_wav_peaks(
    session: str,
    name: str,
    bins: int = _PEAKS_BINS_DEFAULT,
    source: str = "original",
):
    """Server-computed waveform peaks for one WAV — a fixed-size downsample
    (the foundation the later cut overlay draws on). Mirrors get_wav's
    path-safety (resolve_wav validates session/name/source under
    RECORDINGS_DIR), whitelists `source`, clamps `bins` to a sane band, and
    offloads the O(samples) read off the event loop. The payload is `bins`
    floats regardless of recording length."""
    if source not in ("original", "stripped"):
        raise HTTPException(400, f"source must be 'original' or 'stripped', got {source!r}")
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


@app.delete("/api/wav/{session}/{name}")
async def api_wav_delete(
    session: str,
    name: str,
    source: str = "original",
    recorder: Recorder = Depends(get_recorder),
):
    """Delete one WAV + its transcript-cache sidecars. source=stripped
    targets a region under <session>/stripped/. No region cascade — see
    `delete_session_wav`. Refuses the CURRENT session and any session with
    a transcribe/strip job in flight."""
    # Whitelist the query param before it flows into resolve_wav — CodeQL
    # treats query params as untrusted (mirrors the source checks elsewhere).
    if source not in ("original", "stripped"):
        raise HTTPException(400, f"source must be 'original' or 'stripped', got {source!r}")
    if session == recorder.session_start:
        raise HTTPException(409, "cannot delete WAVs from the current session — rotate to a new one first")
    resolve_session_dir(session)
    if recorder.jobs.get(session) is not None:
        raise HTTPException(409, "a transcribe or strip job is in flight on this session")
    summary = await asyncio.to_thread(delete_session_wav, session, name, source)
    print(
        f"[tapscribe] deleted wav {name} ({source}) from session {session}: "
        f"{summary['bytes_freed']} bytes freed",
        flush=True,
    )
    return {"ok": True, **summary}


@app.put("/api/wav/{session}/{name}/primary")
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
    body = await _json_body(req)
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


@app.post("/api/transcribe")
async def api_transcribe(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await req.json()
    session = body.get("session") or ""
    name = body.get("name") or ""
    if not session or not name:
        raise HTTPException(400, "session and name are required")
    source = body.get("source") or "original"
    if source not in ("original", "stripped"):
        raise HTTPException(400, f"source must be 'original' or 'stripped', got {source!r}")
    request = BatchOneRequest(
        session=session,
        name=name,
        source=source,
        model=body.get("model") or "small.en",
        # Per-call backend override — falls back to the Recorder's
        # preference when the body didn't carry one.
        backend=(body.get("backend") or "").strip() or recorder.backend,
        # Canary's per-call language fields ride alongside prompt/hotwords.
        # Empty → adapter falls back to its own default.
        source_lang=(body.get("source_lang") or "").strip() or None,
        target_lang=(body.get("target_lang") or "").strip() or None,
    )
    payload = await transcribe_one(recorder, request)
    print(
        f"[tapscribe] transcribed {request.name} ({request.source}) with {request.model}",
        flush=True,
    )
    return JSONResponse(payload)


@app.post("/api/transcribe-session")
async def api_transcribe_session(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await req.json()
    session = body.get("session") or ""
    if not session:
        raise HTTPException(400, "session is required")
    source = body.get("source") or "original"
    if source not in ("original", "stripped"):
        raise HTTPException(400, f"source must be 'original' or 'stripped', got {source!r}")
    request = BatchSessionRequest(
        session=session,
        source=source,
        model=body.get("model") or "small.en",
        backend=(body.get("backend") or "").strip() or recorder.backend,
        from_iso=body.get("from_iso") or None,
        to_iso=body.get("to_iso") or None,
        force=bool(body.get("force")),
        source_lang=(body.get("source_lang") or "").strip() or None,
        target_lang=(body.get("target_lang") or "").strip() or None,
    )
    return JSONResponse(await transcribe_session(recorder, request))


# ---------------------------------------------------------------------------
# WebSocket: one Bridge utterance per connection
# ---------------------------------------------------------------------------


@app.websocket("/tap")
async def tap(ws: WebSocket):
    """The Bridge's only endpoint. One WS per utterance.

    The route's job is small: gate auth, accept the upgrade, honour the
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

    # Auth gate: when AUTH_ENABLED, the bridge must offer a subprotocol of
    # the form "tapscribe.v1.tap.<token>" whose token matches recorder.tap.value.
    # We accept-with-subprotocol on match (browsers require the server to
    # echo one of the offered values), and refuse the upgrade on mismatch.
    accept_subprotocol: str | None = None
    if config.AUTH_ENABLED:
        offered = ws.scope.get("subprotocols") or []
        accept_subprotocol = auth.pick_tap_subprotocol(offered, recorder.tap.value)
        if accept_subprotocol is None:
            await ws.close(code=4401, reason="missing or invalid tap token")
            return

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


# ---------------------------------------------------------------------------
# Dashboard assets
# ---------------------------------------------------------------------------

# The Stages dashboard ("/next" during its incubation; promoted to "/" once
# the classic dashboard was retired). The shell is next.html; it layers
# next.css on top of dashboard.css (the shared design tokens + primitives),
# and loads everything else through the /web/... mounts below.
DASHBOARD_CSS_PATH = config.WEB_DIR / "dashboard.css"
DASHBOARD_JS_DIR = config.WEB_DIR / "js"
DASHBOARD_COMPONENTS_DIR = config.WEB_DIR / "components"
NEXT_HTML_PATH = config.WEB_DIR / "next.html"
NEXT_CSS_PATH = config.WEB_DIR / "next.css"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    try:
        return HTMLResponse(NEXT_HTML_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(
            "<!doctype html><html><body>"
            "<h1>Dashboard HTML missing</h1>"
            "<p>Expected at <code>" + str(NEXT_HTML_PATH) + "</code>.</p>"
            "</body></html>"
        )


@app.get("/dashboard.css")
async def dashboard_css():
    if not DASHBOARD_CSS_PATH.is_file():
        raise HTTPException(404, "dashboard.css not found")
    return FileResponse(DASHBOARD_CSS_PATH, media_type="text/css")


@app.get("/next.css")
async def next_css():
    if not NEXT_CSS_PATH.is_file():
        raise HTTPException(404, "next.css not found")
    return FileResponse(NEXT_CSS_PATH, media_type="text/css")


# Dashboard JS modules and HTML component templates. StaticFiles handles
# path-traversal protection and content-type detection.
if DASHBOARD_JS_DIR.is_dir():
    app.mount("/web/js", StaticFiles(directory=str(DASHBOARD_JS_DIR)), name="web_js")
if DASHBOARD_COMPONENTS_DIR.is_dir():
    app.mount(
        "/web/components",
        StaticFiles(directory=str(DASHBOARD_COMPONENTS_DIR)),
        name="web_components",
    )
