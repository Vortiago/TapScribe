"""FastAPI app + HTTP / WebSocket routes.

Routes receive the running `Recorder` via `Depends(get_recorder)`, which
reads from `request.app.state.recorder`. `__main__.py` constructs the
Recorder and attaches it before uvicorn starts; tests build their own
and override `app.state.recorder` for isolation.

The big-picture route map:

  GET  /                        — dashboard HTML shell
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
import json
import logging
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config
from . import hallucinations as hallucinations_mod
from . import strip_silence as _ss
from .audio import wav_duration_s, wav_rms_dbfs
from .recorder import JobState, Recorder
from .session_merge import merge_session, select_session_wavs
from .sessions import (
    absorb_session,
    gather_sessions,
    read_session_meta,
    resolve_session_dir,
    resolve_wav,
    strip_one_wav,
    stripped_dir,
    write_session_meta,
)
from .tap_fan_out import TapFanOut
from .text import (
    MAX_CONFIG_TEXT_LEN,
    read_hotwords,
    read_live_prompt,
    read_prompt,
    write_hotwords,
    write_live_prompt,
    write_prompt,
)
from .transcribers import load_transcriber
from .transcribers.catalog import REGISTRY, available_backends
from .wav_cache import cached_transcribe, read_primary_payload, set_primary_transcript


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
    through `float()` and land in [lo, hi]; otherwise raise 400."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"{field} must be a number, got {raw!r}") from e
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
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"{field} must be an integer, got {raw!r}") from e
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

    _SILENCED = ("/api/state", "/dashboard.css", "/dashboard.js", "/web/", "/health", "/healthz")

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(auth.basic_auth_middleware)


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


@app.post("/api/new-session")
async def api_new_session(recorder: Recorder = Depends(get_recorder)):
    """Rotate the current session. Already-open /record WebSockets keep
    writing to their original folder (captured at WS open); only new
    opens land in the new folder."""
    prev, current = recorder.rotate_session()
    print(f"[tapscribe] new session pending: {recorder.session_dir} (previous: {prev})", flush=True)
    return {"ok": True, "previous": prev, "current": current, "path": str(recorder.session_dir)}


# ---------------------------------------------------------------------------
# /api/state — the dashboard's once-per-second polling endpoint
# ---------------------------------------------------------------------------


@app.get("/api/state")
async def api_state(recorder: Recorder = Depends(get_recorder)):
    active_streams = await recorder.streams.snapshot()
    jobs_snapshot = {k: asdict(v) for k, v in recorder.jobs.snapshot().items()}
    prompt = read_prompt()
    live_prompt = read_live_prompt()
    hotwords = read_hotwords()
    halluc_rules = hallucinations_mod.parse_rules()
    inputs_support = _compute_inputs_support()
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
    sessions_list = gather_sessions(
        current_session=recorder.session_start,
        jobs=jobs_snapshot,
    )
    # Powers the "· N sessions override this" footer in the default config panel.
    override_counts = {"prompt": 0, "hotwords": 0}
    for s in sessions_list:
        m = s.get("session_meta") or {}
        if m.get("prompt"):
            override_counts["prompt"] += 1
        if m.get("hotwords"):
            override_counts["hotwords"] += 1
    return {
        "current_session": recorder.session_start,
        "active": active,
        "sessions": sessions_list,
        "default_override_counts": override_counts,
        "live_feed": recorder.transcripts.snapshot(),
        "live_info": dict(recorder.live.info),
        "live_log": list(recorder.live.log)[-30:],
        "live_supports_native_vad": bool(getattr(recorder.live, "supports_native_vad", False)),
        "mlx_available": recorder.use_mlx,  # back-compat for the dashboard ribbon
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
        "hallucinations": {
            "path": str(config.HALLUCINATIONS_FILE),
            "rules": [r["raw"] for r in halluc_rules],
            "count": len(halluc_rules),
        },
    }


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

    if recorder.live.matches(
        model=model,
        language=language,
        gate_kind=gate_kind,
        conf=conf,
        gate_speech_threshold=gate_speech_threshold,
        gate_hangover_ms=gate_hangover_ms,
        gate_pre_roll_ms=gate_pre_roll_ms,
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
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(500, f"failed to write config: {e}") from e
    return {"ok": True, "key": key, "length": len(content)}


# ---------------------------------------------------------------------------
# Session housekeeping
# ---------------------------------------------------------------------------


@app.post("/api/sessions/prune-empty")
async def api_sessions_prune_empty(recorder: Recorder = Depends(get_recorder)):
    """Delete every session folder that has zero WAVs, no merged
    transcript, and no operator-set label. Skips the CURRENT session."""
    pruned: list[str] = []
    failed: list[dict[str, str]] = []
    for sd in config.RECORDINGS_DIR.glob("*"):
        if not sd.is_dir():
            continue
        if sd.name == recorder.session_start:
            continue
        if any(sd.glob("*.wav")):
            continue
        if (sd / "session-transcript.json").exists():
            continue
        meta = read_session_meta(sd.name)
        if meta.get("label"):
            continue
        try:
            shutil.rmtree(sd)
            pruned.append(sd.name)
        except OSError as e:
            failed.append({"session": sd.name, "error": str(e)})
    print(f"[tapscribe] pruned {len(pruned)} empty sessions", flush=True)
    return {"ok": True, "pruned": pruned, "count": len(pruned), "failed": failed}


@app.post("/api/sessions/{session}/strip-silence")
async def api_session_strip_silence(
    session: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
):
    """Non-destructively strip silence from every WAV in <session>/. Writes
    cleaned copies to <session>/stripped/ (originals untouched)."""
    session_dir = resolve_session_dir(session)

    body = await _json_body(req)
    # `if x is not None else default` (not `x or default`) so the operator
    # can pass 0 explicitly — e.g. pad_ms=0 disables region padding for
    # A/B comparisons. Negatives are nonsense; clamp at zero.
    min_silence_ms = max(0, int(body["min_silence_ms"])) if body.get("min_silence_ms") is not None else 500
    pad_ms = max(0, int(body["pad_ms"])) if body.get("pad_ms") is not None else 200
    threshold_db = float(body.get("threshold_db") if body.get("threshold_db") is not None else -45.0)
    speech_floor_db = float(
        body.get("speech_floor_db") if body.get("speech_floor_db") is not None else _ss.SPEECH_RMS_DBFS_FLOOR
    )
    use_silero = bool(body.get("use_silero", True))

    originals = sorted(session_dir.glob("*.wav"))
    if not originals:
        raise HTTPException(404, "no WAVs in this session to strip")

    # JobTracker.claim() encapsulates the "one job per session" rule.
    claimed = await recorder.jobs.claim(
        JobState(
            session=session,
            kind="strip",
            current=0,
            total=len(originals),
            started_at=datetime.now(UTC),
            status="stripping",
        )
    )
    if not claimed:
        raise HTTPException(409, "session is already busy (transcribe or strip in flight)")

    try:
        out_dir = stripped_dir(session)
        if out_dir.exists():
            try:
                shutil.rmtree(out_dir)
            except OSError as e:
                raise HTTPException(500, f"could not clear stripped/: {e}") from e

        started = datetime.now(UTC)

        def _run() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for src in originals:
                try:
                    results.append(
                        strip_one_wav(
                            src, out_dir, min_silence_ms, pad_ms, threshold_db, use_silero, speech_floor_db
                        )
                    )
                except Exception as e:
                    results.append({"name": src.name, "written": False, "error": str(e)})
            return results

        results = await asyncio.to_thread(_run)
        finished = datetime.now(UTC)
    finally:
        await recorder.jobs.release(session)

    written = sum(1 for r in results if r.get("written"))
    in_secs = sum(r.get("in_seconds", 0.0) for r in results)
    speech_secs = sum(r.get("speech_seconds", 0.0) for r in results)
    detectors = sorted({r.get("detector") for r in results if r.get("detector")})

    print(
        f"[tapscribe] strip-silence {session}: {written}/{len(originals)} wavs, "
        f"{speech_secs:.1f}s speech of {in_secs:.1f}s ({100 * speech_secs / max(in_secs, 1e-9):.0f}%), "
        f"detector={detectors}, took {int((finished - started).total_seconds() * 1000)} ms",
        flush=True,
    )

    return {
        "ok": True,
        "session": session,
        "files_processed": len(originals),
        "files_written": written,
        "in_seconds": round(in_secs, 2),
        "speech_seconds": round(speech_secs, 2),
        "detector": detectors[0] if len(detectors) == 1 else detectors,
        "stripped_at": finished.isoformat(),
        "took_ms": int((finished - started).total_seconds() * 1000),
        "files": results,
    }


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
        raise HTTPException(500, f"delete failed: {e}") from e
    print(f"[tapscribe] removed stripped/ from session: {session}", flush=True)
    return {"ok": True, "deleted": True}


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
        raise HTTPException(500, f"delete failed: {e}") from e
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


# ---------------------------------------------------------------------------
# WAV download + transcription
# ---------------------------------------------------------------------------


@app.get("/api/wav/{session}/{name}")
async def get_wav(session: str, name: str, source: str = "original"):
    """Download a WAV. source=stripped pulls from <session>/stripped/."""
    path = resolve_wav(session, name, source)
    dl_name = ("stripped-" + name) if source == "stripped" else name
    return FileResponse(path, media_type="audio/wav", filename=dl_name)


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
        raise HTTPException(422, str(e)) from e
    return {"ok": True, "primary": {"backend": backend, "model": model}}


def _effective_batch_prompt_hotwords(session: str) -> tuple[str | None, str | None]:
    """Override chain for batch transcribe jobs: session-meta → global
    config files. Returns (initial_prompt, hotwords), each None when
    both layers are empty so the adapter receives no value (vs. the
    empty string, which some backends would treat as a real prompt).

    Limitation: an empty session-meta override falls back to the global
    default — there's no way for a session to assert "specifically NO
    prompt, even though a global is set." If an operator needs that
    today the workaround is to clear the global prompt; a future
    sentinel value (e.g. a `null` override that's distinct from the
    empty string) could express it explicitly without touching the
    global."""
    meta = read_session_meta(session)
    prompt = (meta.get("prompt") or "").strip() or (read_prompt() or "").strip()
    hotwords = (meta.get("hotwords") or "").strip() or (read_hotwords() or "").strip()
    return (prompt or None), (hotwords or None)


@app.post("/api/transcribe")
async def api_transcribe(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await req.json()
    session = body.get("session") or ""
    name = body.get("name") or ""
    model_name = body.get("model") or "small.en"
    source = body.get("source") or "original"
    if not session or not name:
        raise HTTPException(400, "session and name are required")
    path = resolve_wav(session, name, source)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size < 64 or wav_duration_s(path) <= 0.0:
        raise HTTPException(422, "empty or unreadable WAV (size=" + str(size) + " bytes)")
    # Silence detection always reads the ORIGINAL, not the per-source file.
    original_path = config.RECORDINGS_DIR / session / name
    rms_dbfs = wav_rms_dbfs(original_path)
    if rms_dbfs < config.SILENT_RMS_DBFS_FLOOR:
        raise HTTPException(
            422,
            f"original WAV is essentially silent ({rms_dbfs:.1f} dBFS RMS, floor {config.SILENT_RMS_DBFS_FLOOR} dBFS) "
            "— Whisper would hallucinate. Remove or skip this file.",
        )

    # Per-call backend override — when the dashboard's backend chip
    # differs from the Recorder's default. Falls back to the Recorder's
    # preference if the body didn't carry one.
    backend_override = (body.get("backend") or "").strip() or recorder.backend
    transcriber = await asyncio.to_thread(load_transcriber, model_name, backend=backend_override)
    initial_prompt, hotwords = _effective_batch_prompt_hotwords(session)
    # Canary's per-call language fields ride alongside prompt/hotwords. Empty
    # string → adapter falls back to its own "en" default.
    source_lang = (body.get("source_lang") or "").strip() or None
    target_lang = (body.get("target_lang") or "").strip() or None
    rules = hallucinations_mod.parse_rules()

    cached = await asyncio.to_thread(
        cached_transcribe,
        path,
        transcriber,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        source_lang=source_lang,
        target_lang=target_lang,
        hallucination_rules=rules,
        force=True,  # explicit per-WAV transcribe always re-runs
        source=source,
    )

    # Return the freshly-written sidecar's raw JSON dict to preserve the
    # wire shape callers expect. read_primary_payload resolves whichever
    # cache layout actually landed (legacy or new-layout) without the
    # route needing to know.
    result_dict = read_primary_payload(path)
    if result_dict is None:
        raise HTTPException(500, "cached_transcribe completed but no sidecar landed on disk")
    print(
        f"[tapscribe] transcribed {name} ({source}) with {model_name} in {cached.transcribe_ms} ms",
        flush=True,
    )
    return JSONResponse(result_dict)


@app.post("/api/transcribe-session")
async def api_transcribe_session(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await req.json()
    session = body.get("session") or ""
    model_name = body.get("model") or "small.en"
    from_iso = body.get("from_iso") or None
    to_iso = body.get("to_iso") or None
    force = bool(body.get("force"))
    source = body.get("source") or "original"
    if not session:
        raise HTTPException(400, "session is required")
    session_dir = resolve_session_dir(session)

    # Phase 0: pure selection.
    try:
        selection = select_session_wavs(
            session_dir,
            from_iso=from_iso,
            to_iso=to_iso,
            source=source,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not selection.wavs:
        raise HTTPException(404, "no usable WAVs in the given range")

    backend_override = (body.get("backend") or "").strip() or recorder.backend
    transcriber = await asyncio.to_thread(load_transcriber, model_name, backend=backend_override)
    initial_prompt, hotwords = _effective_batch_prompt_hotwords(session)
    source_lang = (body.get("source_lang") or "").strip() or None
    target_lang = (body.get("target_lang") or "").strip() or None
    rules = hallucinations_mod.parse_rules()
    # No `effective_force = force or bool(prompt/hotwords_override)` here:
    # the cache match key in `cached_transcribe` now includes
    # initial_prompt_used + hotwords_used, so a meta change automatically
    # misses the cache. As a side benefit this re-runs only the WAVs
    # whose cached entry doesn't match — the old `effective_force` path
    # forced every file in the session, even ones already transcribed
    # under the new prompt by a prior /api/transcribe call.
    effective_force = force

    claimed = await recorder.jobs.claim(
        JobState(
            session=session,
            kind="transcribe",
            current=0,
            total=len(selection.wavs),
            started_at=datetime.now(UTC),
            model=model_name,
            status="running",
        )
    )
    if not claimed:
        raise HTTPException(409, "session is already busy (transcribe or strip in flight)")

    try:
        # Phase 1: ensure every selected WAV is transcribed (cache-aware).
        for idx, wav in enumerate(selection.wavs):
            await recorder.jobs.update(session, current=idx, current_file=wav.name)
            await asyncio.to_thread(
                cached_transcribe,
                wav,
                transcriber,
                initial_prompt=initial_prompt,
                hotwords=hotwords,
                source_lang=source_lang,
                target_lang=target_lang,
                hallucination_rules=rules,
                force=effective_force,
                source=selection.source,
            )

        # Phase 2: pure read-and-build merge.
        transcript = merge_session(selection)
        merged = transcript.to_dict()
        if not merged.get("model"):
            merged["model"] = model_name

        out_path = session_dir / "session-transcript.json"
        out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        (session_dir / "session-transcript.txt").write_text(transcript.plain_text, encoding="utf-8")

        return JSONResponse(merged)
    except Exception as e:
        await recorder.jobs.update(session, status="error: " + str(e))
        raise
    finally:
        await recorder.jobs.release(session)


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
            pass
        except Exception as e:  # pragma: no cover
            print(f"[tapscribe] /tap error for {utterance_id}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Dashboard assets
# ---------------------------------------------------------------------------

DASHBOARD_HTML_PATH = config.WEB_DIR / "dashboard.html"
DASHBOARD_CSS_PATH = config.WEB_DIR / "dashboard.css"
DASHBOARD_JS_DIR = config.WEB_DIR / "js"
DASHBOARD_COMPONENTS_DIR = config.WEB_DIR / "components"


def _read_dashboard_html() -> str:
    try:
        return DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "<!doctype html><html><body>"
            "<h1>Dashboard HTML missing</h1>"
            "<p>Expected at <code>" + str(DASHBOARD_HTML_PATH) + "</code>.</p>"
            "</body></html>"
        )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(_read_dashboard_html())


@app.get("/dashboard.css")
async def dashboard_css():
    if not DASHBOARD_CSS_PATH.is_file():
        raise HTTPException(404, "dashboard.css not found")
    return FileResponse(DASHBOARD_CSS_PATH, media_type="text/css")


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
