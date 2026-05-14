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
  POST /api/live-transcript     — bridge forwards settled lines here
  WS   /record?identity&name    — one WAV per connection
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
from .recorder import ActiveStream, JobState, Recorder
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
    return {
        "current_session": recorder.session_start,
        "active": [asdict(s) for s in active_streams],
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
        await asyncio.to_thread(recorder.live.stop)

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


@app.post("/api/live-transcript")
async def api_live_transcript(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Receive a settled transcript line from the bridge and append it to
    the in-memory live feed. Best-effort; failures are non-fatal."""
    try:
        body = await req.json()
    except Exception as e:
        raise HTTPException(400, "invalid JSON") from e
    raw_text = (body.get("text") or "").strip()
    # Drop Whisper meta-token leakage. Then drop residues with no letters —
    # Whisper also emits standalone "." on silent frames, and a meta-only
    # line like ". [BLANK_AUDIO] [BLANK_" cleans to ". ." which is noise.
    cleaned = clean_meta_tokens(raw_text)
    if not cleaned or not any(c.isalpha() for c in cleaned):
        return {"ok": True, "skipped": "empty-or-no-letters", "raw_chars": len(raw_text)}
    entry = {
        "ts": body.get("ts") or datetime.now(timezone.utc).isoformat(),
        "identity": body.get("identity") or "",
        "name": body.get("name") or "",
        "text": cleaned,
        "session": body.get("session") or recorder.session_start,
    }
    recorder.transcripts.append(entry)
    return {"ok": True}


@app.delete("/api/live-transcript")
async def api_live_transcript_clear(recorder: Recorder = Depends(get_recorder)):
    recorder.transcripts.clear()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Recording toggle
# ---------------------------------------------------------------------------

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

@app.websocket("/record")
async def record(ws: WebSocket):
    """One WAV per WebSocket connection. The Recorder is fetched off the
    app instance directly — WebSockets don't have Depends() injection."""
    recorder: Recorder | None = getattr(ws.app.state, "recorder", None)
    await ws.accept()
    if recorder is None:
        await ws.close(code=1011, reason="recorder not ready")
        return
    # Honor the operator's pause toggle: accept the WS so the bridge knows
    # we heard the open, then close cleanly.
    if not recorder.recording_enabled:
        await ws.close(code=1000, reason="recording paused by operator")
        return

    identity = ws.query_params.get("identity", "unknown")
    name = ws.query_params.get("name", "")
    started = datetime.now(timezone.utc)
    started_iso = started.strftime("%Y-%m-%dT%H-%M-%SZ")
    utt = uuid4().hex[:8]

    short_id = safe_name(identity)[:10] or "unknown"
    name_slug = safe_name(name) or "anon"
    fname = f"{started_iso}_{name_slug}_{short_id}_{utt}.wav"
    # Capture the session dir at WS open — rotate_session() while a WAV is
    # being recorded must NOT redirect it to the new folder.
    session_dir = recorder.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    fpath = session_dir / fname

    conn_id = utt + "-" + short_id
    await recorder.streams.register(ActiveStream(
        conn_id=conn_id, identity=identity, name=name,
        filename=fname, started_at=started, bytes_received=0,
    ))

    print(f"[tapscribe] WS open -> {fname}", flush=True)

    wf = wave.open(str(fpath), "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(16000)

    bytes_received = 0
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
                wf.writeframes(buf)
                bytes_received += len(buf)
                await recorder.streams.update_bytes(conn_id, bytes_received)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pragma: no cover
        print(f"[tapscribe] WS error for {fname}: {e}", flush=True)
    finally:
        wf.close()
        await recorder.streams.remove(conn_id)
        if bytes_received == 0:
            with suppress(OSError):
                fpath.unlink()
            print(f"[tapscribe] WS closed (empty), removed {fname}", flush=True)
        else:
            dur = bytes_received / 32000.0
            print(
                f"[tapscribe] WS closed, wrote {bytes_received} bytes ({dur:.2f}s) to {fname}",
                flush=True,
            )


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
