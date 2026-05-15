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
import wave
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import datetime, timezone
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

from . import auth, config
from . import hallucinations as hallucinations_mod
from . import strip_silence as _ss
from .audio import wav_duration_s, wav_rms_dbfs
from .live_relay import WlKRelay
from .recorder import ActiveStream, JobState, Recorder, UtteranceRecord
from .session_merge import merge_session, select_session_wavs
from .sessions import (
    gather_sessions,
    read_session_meta,
    resolve_source_dir,
    strip_one_wav,
    stripped_dir,
    write_session_meta,
)
from .text import clean_meta_tokens, read_hotwords, read_prompt, safe_name
from .transcribers import load_transcriber
from .wav_cache import cached_transcribe

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


# ---------------------------------------------------------------------------
# Logging — silence the per-second poll spam
# ---------------------------------------------------------------------------

class _SuppressPollAccess(logging.Filter):
    """Drop uvicorn access logs for the dashboard's per-second poll
    endpoints so the terminal isn't flooded. Real activity (POST
    /api/transcribe, DELETE /api/sessions/..., websocket records) still
    surfaces."""
    _SILENCED = ("/api/state", "/dashboard.css", "/dashboard.js", "/web/", "/health")

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
    hotwords = read_hotwords()
    halluc_rules = hallucinations_mod.parse_rules()
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
    return {
        "current_session": recorder.session_start,
        "active": active,
        "sessions": gather_sessions(
            current_session=recorder.session_start,
            jobs=jobs_snapshot,
        ),
        "live_feed": recorder.transcripts.snapshot(),
        "live_info": dict(recorder.live.info),
        "live_log": list(recorder.live.log)[-30:],
        "mlx_available": recorder.use_mlx,
        "recording_enabled": recorder.recording_enabled,
        "prompt": {
            "path": str(config.PROMPT_FILE),
            "content": prompt,
            "length": len(prompt),
        },
        "hotwords": {
            "path": str(config.HOTWORDS_FILE),
            "content": hotwords,
            "length": len(hotwords),
        },
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
    try:
        body = await req.json()
    except Exception:
        body = {}
    model = (body.get("model") or "").strip() or None
    language = (body.get("language") or "").strip() or None
    vac = body.get("vac")
    conf = body.get("confidence_validation")

    # vac/confidence_validation are LiveConfig fields — replace wholesale.
    if vac is not None or conf is not None:
        from dataclasses import replace as _replace
        recorder.live.config = _replace(
            recorder.live.config,
            vac=bool(vac) if vac is not None else recorder.live.config.vac,
            confidence_validation=bool(conf) if conf is not None else recorder.live.config.confidence_validation,
        )

    if recorder.live.running():
        same_model = (not model) or model == recorder.live.config.model
        same_lang = (not language) or language == recorder.live.config.language
        # vac/confidence_validation changes always require a restart since
        # they're CLI flags on the spawned child.
        same_quality = vac is None and conf is None
        if same_model and same_lang and same_quality:
            return {"ok": True, "msg": "already running with requested config", "state": recorder.live.info["state"]}

    # Reflect the upcoming transition in `info` *before* we start tearing
    # down the old child or fetching weights. Without this, dashboards
    # polling /api/state during the stop→start window (or during an HF
    # download inside start()) would render state="stopped" with the
    # *previous* model selection — making it look like the user's pick
    # was discarded.
    recorder.live.info["state"] = "starting"
    recorder.live.info["last_error"] = ""
    if model is not None:
        recorder.live.info["model"] = model
    if language is not None:
        recorder.live.info["language"] = language

    if recorder.live.running():
        await asyncio.to_thread(recorder.live.stop)
        # stop() sets state="stopped"; restore the transitional state so
        # the dashboard stays on "starting" with the new model.
        recorder.live.info["state"] = "starting"
        if model is not None:
            recorder.live.info["model"] = model

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
    try:
        body = await req.json()
    except Exception as e:
        raise HTTPException(400, "invalid JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(400, "expected an object body")
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
    body: dict[str, Any] = {}
    try:
        parsed = await req.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        pass
    if "enabled" in body:
        enabled = recorder.toggle_recording(enabled=bool(body["enabled"]))
    else:
        enabled = recorder.toggle_recording()
    print(f"[tapscribe] recording {'enabled' if enabled else 'paused'}", flush=True)
    return {"ok": True, "enabled": enabled}


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
    session: str, req: Request, recorder: Recorder = Depends(get_recorder),
):
    """Non-destructively strip silence from every WAV in <session>/. Writes
    cleaned copies to <session>/stripped/ (originals untouched)."""
    session_dir = config.RECORDINGS_DIR / session
    if not session_dir.is_dir():
        raise HTTPException(404, "session not found")
    if config.RECORDINGS_DIR.resolve() not in session_dir.resolve().parents:
        raise HTTPException(404, "session not found")

    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    min_silence_ms = int(body.get("min_silence_ms") or 500)
    pad_ms = int(body.get("pad_ms") or 200)
    threshold_db = float(body.get("threshold_db") or -45.0)
    speech_floor_db = float(body.get("speech_floor_db") if body.get("speech_floor_db") is not None else _ss.SPEECH_RMS_DBFS_FLOOR)
    use_silero = bool(body.get("use_silero", True))

    originals = sorted(session_dir.glob("*.wav"))
    if not originals:
        raise HTTPException(404, "no WAVs in this session to strip")

    # JobTracker.claim() encapsulates the "one job per session" rule.
    claimed = await recorder.jobs.claim(JobState(
        session=session, kind="strip", current=0, total=len(originals),
        started_at=datetime.now(timezone.utc), status="stripping",
    ))
    if not claimed:
        raise HTTPException(409, "session is already busy (transcribe or strip in flight)")

    try:
        out_dir = stripped_dir(session)
        if out_dir.exists():
            try:
                shutil.rmtree(out_dir)
            except OSError as e:
                raise HTTPException(500, f"could not clear stripped/: {e}") from e

        started = datetime.now(timezone.utc)

        def _run() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for src in originals:
                dst = out_dir / src.name
                try:
                    results.append(strip_one_wav(src, dst, min_silence_ms, pad_ms, threshold_db, use_silero, speech_floor_db))
                except Exception as e:
                    results.append({"name": src.name, "written": False, "error": str(e)})
            return results

        results = await asyncio.to_thread(_run)
        finished = datetime.now(timezone.utc)
    finally:
        await recorder.jobs.release(session)

    written = sum(1 for r in results if r.get("written"))
    in_secs = sum(r.get("in_seconds", 0.0) for r in results)
    speech_secs = sum(r.get("speech_seconds", 0.0) for r in results)
    detectors = sorted({r.get("detector") for r in results if r.get("detector")})

    print(
        f"[tapscribe] strip-silence {session}: {written}/{len(originals)} wavs, "
        f"{speech_secs:.1f}s speech of {in_secs:.1f}s ({100*speech_secs/max(in_secs,1e-9):.0f}%), "
        f"detector={detectors}, took {int((finished-started).total_seconds()*1000)} ms",
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
    session_dir = config.RECORDINGS_DIR / session
    if not session_dir.is_dir():
        raise HTTPException(404, "session not found")
    if config.RECORDINGS_DIR.resolve() not in session_dir.resolve().parents:
        raise HTTPException(404, "session not found")
    d = stripped_dir(session)
    if not d.is_dir():
        return {"ok": True, "deleted": False, "reason": "no stripped/ folder"}
    try:
        shutil.rmtree(d)
    except OSError as e:
        raise HTTPException(500, f"delete failed: {e}") from e
    print(f"[tapscribe] removed stripped/ from session: {session}", flush=True)
    return {"ok": True, "deleted": True}


@app.delete("/api/sessions/{session}")
async def api_session_delete(session: str, recorder: Recorder = Depends(get_recorder)):
    """Recursively delete a recordings folder. Refuses the CURRENT session."""
    if session == recorder.session_start:
        raise HTTPException(409, "cannot delete the current session — rotate to a new one first")
    session_dir = config.RECORDINGS_DIR / session
    if not session_dir.is_dir():
        raise HTTPException(404, "session not found")
    if config.RECORDINGS_DIR.resolve() not in session_dir.resolve().parents:
        raise HTTPException(404, "session not found")
    try:
        shutil.rmtree(session_dir)
    except OSError as e:
        raise HTTPException(500, f"delete failed: {e}") from e
    await recorder.jobs.release(session)
    print(f"[tapscribe] deleted session: {session_dir}", flush=True)
    return {"ok": True, "deleted": session}


@app.get("/api/session-meta/{session}")
async def api_session_meta_get(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    session_dir = config.RECORDINGS_DIR / session
    if not session_dir.is_dir():
        raise HTTPException(404, "session not found")
    if config.RECORDINGS_DIR.resolve() not in session_dir.resolve().parents:
        raise HTTPException(404, "session not found")
    return read_session_meta(session)


@app.put("/api/session-meta/{session}")
async def api_session_meta_put(session: str, req: Request, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    session_dir = config.RECORDINGS_DIR / session
    if not session_dir.is_dir():
        raise HTTPException(404, "session not found")
    if config.RECORDINGS_DIR.resolve() not in session_dir.resolve().parents:
        raise HTTPException(404, "session not found")
    try:
        body = await req.json()
    except Exception as e:
        raise HTTPException(400, "invalid JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(400, "expected an object body")
    write_session_meta(session, body)
    return {"ok": True, "meta": read_session_meta(session)}


# ---------------------------------------------------------------------------
# WAV download + transcription
# ---------------------------------------------------------------------------

@app.get("/api/wav/{session}/{name}")
async def get_wav(session: str, name: str, source: str = "original"):
    """Download a WAV. source=stripped pulls from <session>/stripped/."""
    if source == "stripped":
        path = stripped_dir(session) / name
        dl_name = "stripped-" + name
    else:
        path = config.RECORDINGS_DIR / session / name
        dl_name = name
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise HTTPException(404, "not found")
    if config.RECORDINGS_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="audio/wav", filename=dl_name)


@app.post("/api/transcribe")
async def api_transcribe(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await req.json()
    session = body.get("session") or ""
    name = body.get("name") or ""
    model_name = body.get("model") or "small.en"
    prompt_override = body.get("prompt") or ""
    hotwords_override = body.get("hotwords") or ""
    source = body.get("source") or "original"
    if not session or not name:
        raise HTTPException(400, "session and name are required")
    source_dir = resolve_source_dir(session, source)
    path = source_dir / name
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise HTTPException(404, "not found")
    if config.RECORDINGS_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(404, "not found")
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
            "— Whisper would hallucinate. Remove or skip this file."
        )

    transcriber = await asyncio.to_thread(load_transcriber, model_name, use_mlx=recorder.use_mlx)
    initial_prompt = (prompt_override or "").strip() or (read_prompt() or None)
    hotwords = (hotwords_override or "").strip() or (read_hotwords() or None)
    rules = hallucinations_mod.parse_rules()

    cached = await asyncio.to_thread(
        cached_transcribe, path, transcriber,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        hallucination_rules=rules,
        force=True,  # explicit per-WAV transcribe always re-runs
        source=source,
    )

    # Read the sidecar back as a dict to preserve the wire shape callers expect.
    result_dict = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    print(f"[tapscribe] transcribed {name} ({source}) with {model_name} in {cached.transcribe_ms} ms", flush=True)
    return JSONResponse(result_dict)


@app.post("/api/transcribe-session")
async def api_transcribe_session(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await req.json()
    session = body.get("session") or ""
    model_name = body.get("model") or "small.en"
    from_iso = body.get("from_iso") or None
    to_iso = body.get("to_iso") or None
    force = bool(body.get("force"))
    prompt_override = body.get("prompt") or ""
    hotwords_override = body.get("hotwords") or ""
    source = body.get("source") or "original"
    if not session:
        raise HTTPException(400, "session is required")

    session_dir = config.RECORDINGS_DIR / session
    if not session_dir.is_dir():
        raise HTTPException(404, "session not found")
    if (
        config.RECORDINGS_DIR.resolve() not in session_dir.resolve().parents
        and session_dir.resolve() != config.RECORDINGS_DIR.resolve() / session
    ):
        raise HTTPException(404, "not found")

    # Phase 0: pure selection.
    try:
        selection = select_session_wavs(
            session_dir, from_iso=from_iso, to_iso=to_iso, source=source,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not selection.wavs:
        raise HTTPException(404, "no usable WAVs in the given range")

    transcriber = await asyncio.to_thread(load_transcriber, model_name, use_mlx=recorder.use_mlx)
    initial_prompt = (prompt_override or "").strip() or (read_prompt() or None)
    hotwords = (hotwords_override or "").strip() or (read_hotwords() or None)
    rules = hallucinations_mod.parse_rules()
    effective_force = force or bool(prompt_override.strip() or hotwords_override.strip())

    claimed = await recorder.jobs.claim(JobState(
        session=session, kind="transcribe", current=0, total=len(selection.wavs),
        started_at=datetime.now(timezone.utc), model=model_name, status="running",
    ))
    if not claimed:
        raise HTTPException(409, "session is already busy (transcribe or strip in flight)")

    try:
        # Phase 1: ensure every selected WAV is transcribed (cache-aware).
        for idx, wav in enumerate(selection.wavs):
            await recorder.jobs.update(session, current=idx, current_file=wav.name)
            await asyncio.to_thread(
                cached_transcribe, wav, transcriber,
                initial_prompt=initial_prompt,
                hotwords=hotwords,
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
# WebSocket: record one WAV per connection
# ---------------------------------------------------------------------------

def _reopen_wav_for_append(path) -> wave.Wave_write:
    """Reopen an existing 16 kHz mono int16 WAV so subsequent
    writeframes() append after the existing audio. We read the existing
    frames, then rewrite them via Wave_write so the resulting file is
    structurally valid both before and after the resumed segment is
    appended."""
    with wave.open(str(path), "rb") as r:
        existing = r.readframes(r.getnframes())
    wf = wave.open(str(path), "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(16000)
    if existing:
        wf.writeframes(existing)
    return wf


@app.websocket("/tap")
async def tap(ws: WebSocket):
    """The Bridge's only endpoint. One WS per utterance.

    Behaviour: every received PCM frame is BOTH written to a WAV on disk
    AND relayed to the Recorder's supervised WhisperLiveKit child for
    live captioning. Settled lines from WlK land directly in
    `recorder.transcripts` attributed to this WS's identity / name —
    Bridges never see the WlK protocol.

    Resume: the bridge passes a stable `utterance_id` per unmuted speech
    segment and keeps it across reconnects. If a recent record exists
    for that id (same identity, file still on disk, not currently open),
    we append to the same WAV instead of opening a new one. This means a
    network blip or recorder restart mid-utterance no longer fragments
    the recording.

    Graceful degradation (per ADR-0002): if WlK isn't running or the
    relay's connection fails mid-stream, WAV recording continues
    unaffected; the operator can see the live-channel state on the
    dashboard.
    """
    recorder: Recorder | None = getattr(ws.app.state, "recorder", None)
    if recorder is None:
        # Refuse the upgrade before accept so the bridge sees a hard fail
        # rather than an empty open-then-close.
        await ws.close(code=1011, reason="recorder not ready")
        return

    # Auth gate: when AUTH_ENABLED, the bridge must offer a subprotocol of
    # the form "tapscribe.v1.tap.<token>" whose token matches recorder.tap.token.
    # We accept-with-subprotocol on match (browsers require the server to
    # echo one of the offered values), and refuse the upgrade on mismatch.
    accept_subprotocol: str | None = None
    if config.AUTH_ENABLED:
        offered = ws.scope.get("subprotocols") or []
        accept_subprotocol = auth.pick_tap_subprotocol(offered, recorder.tap.token)
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
    do_record = tap_pref.record
    do_live = tap_pref.live

    wf: wave.Wave_write | None = None
    record: UtteranceRecord | None = None
    fpath = None
    fname = ""
    bytes_received = 0
    started_at = datetime.now(timezone.utc)

    if do_record:
        resumed = recorder.utterances.try_resume(utterance_id, identity=identity)
        if resumed is not None:
            fpath = resumed.path
            fname = resumed.filename
            record = resumed
            if name and name != record.name:
                record.name = name
            wf = _reopen_wav_for_append(fpath)
            bytes_received = resumed.bytes_received
            started_at = resumed.started_at
            print(
                f"[tapscribe] /tap resume -> {fname} "
                f"(prior {bytes_received} bytes)",
                flush=True,
            )
        else:
            started_iso = started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
            short_id = safe_name(identity)[:10] or "unknown"
            name_slug = safe_name(name) or "anon"
            # Filename uses a fresh local uuid for uniqueness; the bridge's
            # utterance_id lives in the index, not in the path. This avoids
            # two distinct utterances colliding on disk when they happen to
            # reuse the same utterance_id within the same wall-clock second
            # (e.g. an expired-and-restarted utterance).
            fname = f"{started_iso}_{name_slug}_{short_id}_{uuid4().hex[:8]}.wav"
            # Capture the session dir at WS open — rotate_session() while a WAV
            # is being recorded must NOT redirect it to the new folder.
            session_dir = recorder.session_dir
            session_dir.mkdir(parents=True, exist_ok=True)
            fpath = session_dir / fname
            record = UtteranceRecord(
                utterance_id=utterance_id,
                identity=identity,
                name=name,
                filename=fname,
                path=fpath,
                started_at=started_at,
            )
            recorder.utterances.register_new(record)
            wf = wave.open(str(fpath), "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            print(f"[tapscribe] /tap open -> {fname}", flush=True)
    else:
        # record=False: don't open a WAV or touch the utterance index;
        # frames flow to the relay (if live=True) and the active-stream
        # row stays visible so the operator can flip recording back on.
        fname = "(record off)"
        print(f"[tapscribe] /tap open (record off) for {identity}", flush=True)

    conn_id = utterance_id[:8] + "-" + (safe_name(identity)[:10] or "unknown")
    await recorder.streams.register(ActiveStream(
        conn_id=conn_id, identity=identity, name=name,
        filename=fname, started_at=started_at,
        bytes_received=bytes_received,
        record=do_record, live=do_live,
    ))

    # Set up the WlK relay. Per ADR-0002, this is per-WS so settled
    # lines stay attributed to one speaker. The on-settled-line callback
    # cleans Whisper meta-tokens and skips letterless residues, then
    # appends to the live transcripts feed.
    def _on_settled_line(text: str) -> None:
        cleaned = clean_meta_tokens(text)
        if not cleaned or not any(c.isalpha() for c in cleaned):
            return
        recorder.transcripts.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "identity": identity,
            "name": name,
            "text": cleaned,
            "session": recorder.session_start,
        })

    relay: WlKRelay | None = None
    relay_alive = False
    if do_live and recorder.live.running():
        candidate = WlKRelay(
            host=recorder.live.config.host,
            port=recorder.live.config.port,
            language=recorder.live.config.language,
            on_settled_line=_on_settled_line,
        )
        if await candidate.connect():
            relay = candidate
            relay_alive = True

    try:
        while True:
            msg = await ws.receive()
            t = msg.get("type")
            if t == "websocket.disconnect":
                break
            if t != "websocket.receive":
                continue
            if msg.get("bytes"):
                buf = msg["bytes"]
                if wf is not None:
                    wf.writeframes(buf)
                    bytes_received += len(buf)
                    await recorder.streams.update_bytes(conn_id, bytes_received)
                # Best-effort relay. If the relay reports closed/dead,
                # stop trying for the rest of this WS — recording
                # continues unaffected.
                if relay_alive:
                    if not await relay.send(buf):
                        relay_alive = False
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pragma: no cover
        print(f"[tapscribe] /tap error for {fname}: {e}", flush=True)
    finally:
        # Sync cleanup first — these must complete even if the task is
        # being cancelled (TestClient does that; some ASGI servers do
        # under shutdown). Any `await` after this point can raise
        # CancelledError and skip the rest of the block.
        if wf is not None:
            wf.close()
            kept = bytes_received > 0
            if not kept:
                with suppress(OSError):
                    fpath.unlink()
                print(f"[tapscribe] /tap closed (empty), removed {fname}", flush=True)
            else:
                dur = bytes_received / 32000.0
                print(
                    f"[tapscribe] /tap closed, wrote {bytes_received} bytes ({dur:.2f}s) to {fname}",
                    flush=True,
                )
            recorder.utterances.release(
                utterance_id, bytes_received=bytes_received, kept=kept,
            )
        else:
            print(f"[tapscribe] /tap closed (record off) for {identity}", flush=True)
        if relay is not None:
            await relay.close()  # drains tail captions per Q2
        await recorder.streams.remove(conn_id)


# ---------------------------------------------------------------------------
# Dashboard assets
# ---------------------------------------------------------------------------

DASHBOARD_HTML_PATH = config.WEB_DIR / "dashboard.html"
DASHBOARD_CSS_PATH = config.WEB_DIR / "dashboard.css"
DASHBOARD_JS_DIR = config.WEB_DIR / "js"


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


@app.get("/web/js/{name}")
async def dashboard_js_module(name: str):
    """Serve dashboard JS modules. Restricted to .js files under web/js/."""
    if not name.endswith(".js") or "/" in name or "\\" in name:
        raise HTTPException(404, "not found")
    path = DASHBOARD_JS_DIR / name
    if not path.is_file():
        raise HTTPException(404, "not found")
    if DASHBOARD_JS_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="application/javascript")
