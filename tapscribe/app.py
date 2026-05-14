"""FastAPI app + HTTP / WebSocket routes.

This is the orchestration layer: every route is thin and delegates to the
focused helper modules. The big-picture flow:

  GET  /                        — dashboard HTML shell
  GET  /api/state               — sessions + active streams + live channel
  POST /api/transcribe          — batch transcribe one WAV
  POST /api/transcribe-session  — merge per-WAV transcripts into a session
  POST /api/live/start          — start / restart whisperlivekit-server
  POST /api/live/stop           — stop whisperlivekit-server
  POST /api/live-transcript     — bridge extension forwards settled lines here
  WS   /record?identity&name    — one WAV per connection
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import wave
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import auth, config, live
from . import hallucinations as hallucinations_mod
from . import strip_silence as _ss
from .audio import wav_duration_s, wav_rms_dbfs
from .sessions import (
    ACTIVE,
    ACTIVE_LOCK,
    SESSION_PROGRESS,
    SESSION_PROGRESS_LOCK,
    gather_sessions,
    read_session_meta,
    resolve_source_dir,
    strip_one_wav,
    stripped_dir,
    write_session_meta,
)
from .text import (
    clean_meta_tokens,
    parse_wav_speaker_slug,
    parse_wav_start,
    read_hotwords,
    read_prompt,
    safe_name,
)
from .transcribers import TranscriptionResult, load_transcriber


def _result_envelope(
    result: TranscriptionResult,
    *,
    path: Path,
    source: str,
    started: datetime,
    finished: datetime,
) -> dict[str, Any]:
    """Wrap a `TranscriptionResult` with the per-WAV write-time metadata
    that the JSON cache + dashboard expect. The result's own fields are
    flattened into the same dict for backward-compat — until Land 2's
    `CachedTranscription` formalises this envelope."""
    wav_start = parse_wav_start(path.name)
    envelope: dict[str, Any] = {
        "transcriber": result.transcriber,
        "device": result.device,
        "model": result.model,
        "language": result.language,
        "language_probability": result.language_probability,
        "duration": result.duration,
        "segments": [_segment_to_dict(s) for s in result.segments],
        "text": result.text,
        "initial_prompt_used": result.initial_prompt_used,
        "hotwords_used": result.hotwords_used,
        "quality_settings": result.quality_settings,
        "suppressed_hallucinations": [_segment_to_dict(s) for s in result.suppressed_hallucinations],
        "transcribed_at": finished.isoformat(),
        "transcribe_ms": int((finished - started).total_seconds() * 1000),
        "source": source,
        "speaker_name": parse_wav_speaker_slug(path.name),
    }
    if wav_start is not None:
        envelope["wav_start"] = wav_start.isoformat()
    return envelope


def _segment_to_dict(seg) -> dict[str, Any]:
    out: dict[str, Any] = {"start": seg.start, "end": seg.end, "text": seg.text}
    if seg.avg_logprob is not None:
        out["avg_logprob"] = seg.avg_logprob
    if seg.words is not None:
        out["words"] = [
            {"start": w.start, "end": w.end, "word": w.word, "prob": w.prob}
            for w in seg.words
        ]
    if seg.matched_rule is not None:
        out["matched_rule"] = seg.matched_rule
    return out

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
async def _lifespan(_app: FastAPI):
    # Install poll-spam filter on the uvicorn access logger. We do this in
    # the lifespan (not module load) because uvicorn replaces the access
    # logger's handlers via dictConfig() during its own boot — anything we
    # add before uvicorn.run() would be dropped.
    logging.getLogger("uvicorn.access").addFilter(_SuppressPollAccess())

    if config.AUTO_START_LIVE:
        ok, msg = live.start_live_proc()
        if not ok:
            print(f"[tapscribe] live auto-start skipped: {msg}", flush=True)
    try:
        yield
    finally:
        live.stop_live_proc(timeout=3.0)


app = FastAPI(title="TapScribe recorder", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(auth.basic_auth_middleware)


# Live transcript feed (chronological, capped). Pushed to by the bridge
# extension via POST /api/live-transcript; surfaced by /api/state.
LIVE_FEED: deque[dict[str, Any]] = deque(maxlen=200)


# ---------------------------------------------------------------------------
# Health + simple listings
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "session_dir": str(config.SESSION_DIR)}


@app.get("/sessions")
async def list_sessions_simple():
    """Legacy simple listing."""
    return gather_sessions()


@app.post("/api/new-session")
async def api_new_session():
    """Rotate the current session.

    Every recorder WebSocket captures config.SESSION_DIR at open time as a
    local variable, so in-flight recordings keep writing to their original
    folder. Only WebSockets opened AFTER this call land in the new folder.
    """
    prev, current = config.rotate_session()
    print(f"[tapscribe] new session pending: {config.SESSION_DIR} (previous: {prev})", flush=True)
    return {"ok": True, "previous": prev, "current": current, "path": str(config.SESSION_DIR)}


# ---------------------------------------------------------------------------
# /api/state — the dashboard's once-per-second polling endpoint
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def api_state():
    async with ACTIVE_LOCK:
        active = list(ACTIVE.values())
    prompt = read_prompt()
    hotwords = read_hotwords()
    halluc_rules = hallucinations_mod.parse_rules()
    return {
        "current_session": config.SESSION_START,
        "active": active,
        "sessions": gather_sessions(),
        "live_feed": list(LIVE_FEED),
        "live_info": dict(live.LIVE_INFO),
        "live_log": list(live.LIVE_LOG)[-30:],
        "mlx_available": config.USE_MLX,
        "recording_enabled": config.RECORDING_ENABLED,
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
async def api_live_start(req: Request):
    """Start the live channel (whisperlivekit-server). If already running
    with a different model/language, restarts it. If already running with
    the same config, no-op. Body (all optional): {"model": str, "language":
    str, "vac": bool, "confidence_validation": bool}.

    stop_live_proc and start_live_proc are synchronous and can block for
    several seconds (subprocess wait, model download, etc.). Running them
    on the event-loop thread would freeze /api/state polling — so we
    offload to a worker thread.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    model = (body.get("model") or "").strip() or None
    language = (body.get("language") or "").strip() or None
    vac = body.get("vac")
    conf = body.get("confidence_validation")
    if vac is not None:
        live.LIVE_CONFIG["vac"] = bool(vac)
    if conf is not None:
        live.LIVE_CONFIG["confidence_validation"] = bool(conf)

    if live.live_running():
        same_model = (not model) or model == live.LIVE_CONFIG.get("model")
        same_lang = (not language) or language == live.LIVE_CONFIG.get("language")
        # vac/confidence_validation changes always require a restart since
        # they're CLI flags on the spawned child.
        same_quality = vac is None and conf is None
        if same_model and same_lang and same_quality:
            return {"ok": True, "msg": "already running with requested config", "state": live.LIVE_INFO["state"]}
        await asyncio.to_thread(live.stop_live_proc)

    ok, msg = await asyncio.to_thread(live.start_live_proc, model, language)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg, "state": live.LIVE_INFO["state"]}


@app.post("/api/live/stop")
async def api_live_stop():
    ok, msg = await asyncio.to_thread(live.stop_live_proc)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "msg": msg, "state": live.LIVE_INFO["state"]}


@app.post("/api/live-transcript")
async def api_live_transcript(req: Request):
    """Receive a settled transcript line from the bridge extension and
    append it to the in-memory live feed. Best-effort; failures are
    non-fatal for the caller."""
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
        "session": body.get("session") or config.SESSION_START,
    }
    LIVE_FEED.append(entry)
    return {"ok": True}


@app.delete("/api/live-transcript")
async def api_live_transcript_clear():
    LIVE_FEED.clear()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Recording toggle
# ---------------------------------------------------------------------------

@app.post("/api/recording/toggle")
async def api_recording_toggle(req: Request):
    """Flip config.RECORDING_ENABLED. Optional body {"enabled": bool} to set
    explicitly; without a body, just toggles. New /record WSes are accepted
    then immediately closed when disabled — already-open WAVs continue to
    record their current utterance, which finalises cleanly on the
    extension's normal trackMuted close."""
    body: dict[str, Any] = {}
    try:
        parsed = await req.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        pass
    if "enabled" in body:
        config.RECORDING_ENABLED = bool(body["enabled"])
    else:
        config.RECORDING_ENABLED = not config.RECORDING_ENABLED
    print(f"[tapscribe] recording {'enabled' if config.RECORDING_ENABLED else 'paused'}", flush=True)
    return {"ok": True, "enabled": config.RECORDING_ENABLED}


# ---------------------------------------------------------------------------
# Session housekeeping
# ---------------------------------------------------------------------------

@app.post("/api/sessions/prune-empty")
async def api_sessions_prune_empty():
    """Delete every session folder that has zero WAVs, no merged transcript,
    and no operator-set label. Skips the CURRENT session. Idempotent."""
    pruned: list[str] = []
    failed: list[dict[str, str]] = []
    for sd in config.RECORDINGS_DIR.glob("*"):
        if not sd.is_dir():
            continue
        if sd.name == config.SESSION_START:
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
async def api_session_strip_silence(session: str, req: Request):
    """Non-destructively strip silence from every WAV in <session>/. Writes
    cleaned copies to <session>/stripped/ (originals untouched). Re-running
    overwrites the stripped/ folder so it's idempotent under different
    detector parameters."""
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

    # Mutual exclusion against concurrent transcribe-session AND concurrent
    # strip: both contend over <session>/stripped/ and the per-WAV JSON
    # cache. We claim a sentinel under SESSION_PROGRESS_LOCK so a transcribe
    # that starts between our check-and-go refuses on the same key.
    async with SESSION_PROGRESS_LOCK:
        if session in SESSION_PROGRESS:
            raise HTTPException(409, "session is already busy (transcribe or strip in flight)")
        SESSION_PROGRESS[session] = {
            "current": 0,
            "total": len(originals),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "stripping",
        }

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
        async with SESSION_PROGRESS_LOCK:
            SESSION_PROGRESS.pop(session, None)

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
async def api_session_stripped_delete(session: str):
    """Remove a session's stripped/ folder so it can be regenerated. The
    originals are never touched."""
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
async def api_session_delete(session: str):
    """Recursively delete a recordings folder. Refuses the CURRENT session."""
    if session == config.SESSION_START:
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
    async with SESSION_PROGRESS_LOCK:
        SESSION_PROGRESS.pop(session, None)
    print(f"[tapscribe] deleted session: {session_dir}", flush=True)
    return {"ok": True, "deleted": session}


@app.get("/api/session-meta/{session}")
async def api_session_meta_get(session: str):
    session_dir = config.RECORDINGS_DIR / session
    if not session_dir.is_dir():
        raise HTTPException(404, "session not found")
    if config.RECORDINGS_DIR.resolve() not in session_dir.resolve().parents:
        raise HTTPException(404, "session not found")
    return read_session_meta(session)


@app.put("/api/session-meta/{session}")
async def api_session_meta_put(session: str, req: Request):
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
async def api_transcribe(req: Request):
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
    # When source=stripped, the stripped sibling's RMS is misleadingly high
    # — silero may have false-positive'd on a brief noise burst, and that
    # tiny region is now the entire stripped audio.
    original_path = config.RECORDINGS_DIR / session / name
    rms_dbfs = wav_rms_dbfs(original_path)
    if rms_dbfs < config.SILENT_RMS_DBFS_FLOOR:
        raise HTTPException(
            422,
            f"original WAV is essentially silent ({rms_dbfs:.1f} dBFS RMS, floor {config.SILENT_RMS_DBFS_FLOOR} dBFS) "
            "— Whisper would hallucinate. Remove or skip this file."
        )

    started = datetime.now(timezone.utc)
    transcriber = await asyncio.to_thread(load_transcriber, model_name, use_mlx=config.USE_MLX)
    # Caller resolves overrides vs files (A2 policy split — Transcriber is
    # policy-free).
    initial_prompt = (prompt_override or "").strip() or (read_prompt() or None)
    hotwords = (hotwords_override or "").strip() or (read_hotwords() or None)
    rules = hallucinations_mod.parse_rules()

    raw = await asyncio.to_thread(
        transcriber.transcribe,
        path,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
    )
    filtered = hallucinations_mod.apply(raw, rules=rules)

    finished = datetime.now(timezone.utc)
    result_dict = _result_envelope(
        filtered,
        path=path,
        source=source,
        started=started,
        finished=finished,
    )

    out_path = path.with_suffix(".json")
    out_path.write_text(json.dumps(result_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[tapscribe] transcribed {name} ({source}) with {model_name} in {result_dict['transcribe_ms']} ms", flush=True)
    return JSONResponse(result_dict)


@app.post("/api/transcribe-session")
async def api_transcribe_session(req: Request):
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

    wav_dir = resolve_source_dir(session, source)

    def parse_optional(iso: str | None) -> datetime | None:
        if not iso:
            return None
        s = iso.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError as e:
            raise HTTPException(400, f"bad iso timestamp: {iso} ({e})") from e

    from_dt = parse_optional(from_iso)
    to_dt = parse_optional(to_iso)

    selected: list[tuple[Path, datetime]] = []
    skipped_bad: list[str] = []
    skipped_silent: list[str] = []
    for w in sorted(wav_dir.glob("*.wav")):
        wav_start = parse_wav_start(w.name)
        if wav_start is None:
            continue
        try:
            size = w.stat().st_size
        except OSError:
            size = 0
        if size < 64 or wav_duration_s(w) <= 0.0:
            skipped_bad.append(w.name)
            continue
        original_path = config.RECORDINGS_DIR / session / w.name
        if wav_rms_dbfs(original_path) < config.SILENT_RMS_DBFS_FLOOR:
            skipped_silent.append(w.name)
            continue
        if from_dt and wav_start < from_dt:
            continue
        if to_dt and wav_start > to_dt:
            continue
        selected.append((w, wav_start))

    if skipped_bad:
        print(f"[tapscribe] transcribe-session: skipped {len(skipped_bad)} empty/corrupt WAVs", flush=True)
    if skipped_silent:
        print(f"[tapscribe] transcribe-session: skipped {len(skipped_silent)} silent WAVs", flush=True)

    if not selected:
        raise HTTPException(404, "no usable WAVs in the given range")

    transcriber = await asyncio.to_thread(load_transcriber, model_name, use_mlx=config.USE_MLX)
    # Caller-resolved overrides — Transcriber is policy-free (A2).
    initial_prompt = (prompt_override or "").strip() or (read_prompt() or None)
    hotwords = (hotwords_override or "").strip() or (read_hotwords() or None)
    rules = hallucinations_mod.parse_rules()
    effective_force = force or bool(prompt_override.strip() or hotwords_override.strip())

    async with SESSION_PROGRESS_LOCK:
        SESSION_PROGRESS[session] = {
            "current": 0,
            "total": len(selected),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "model": model_name,
            "status": "running",
        }

    all_segments: list[dict[str, Any]] = []
    all_suppressed: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)

    try:
        for idx, (w, wav_start) in enumerate(selected):
            async with SESSION_PROGRESS_LOCK:
                SESSION_PROGRESS[session]["current"] = idx
                SESSION_PROGRESS[session]["current_file"] = w.name

            json_path = w.with_suffix(".json")
            result: dict[str, Any] | None = None
            if not effective_force and json_path.exists():
                try:
                    result = json.loads(json_path.read_text(encoding="utf-8"))
                    if result.get("model") != model_name:
                        # Different model previously used — re-transcribe.
                        result = None
                except (OSError, ValueError):
                    result = None

            if result is None:
                wav_started = datetime.now(timezone.utc)
                raw = await asyncio.to_thread(
                    transcriber.transcribe,
                    w,
                    initial_prompt=initial_prompt,
                    hotwords=hotwords,
                )
                filtered = hallucinations_mod.apply(raw, rules=rules)
                wav_finished = datetime.now(timezone.utc)
                result = _result_envelope(
                    filtered,
                    path=w,
                    source=source,
                    started=wav_started,
                    finished=wav_finished,
                )
                json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

            speaker = result.get("speaker_name") or parse_wav_speaker_slug(w.name) or "<anon>"
            for seg in result.get("segments", []):
                abs_start = wav_start + timedelta(seconds=float(seg.get("start", 0.0)))
                abs_end = wav_start + timedelta(seconds=float(seg.get("end", 0.0)))
                entry: dict[str, Any] = {
                    "abs_start": abs_start.isoformat(),
                    "abs_end": abs_end.isoformat(),
                    "abs_hms": abs_start.strftime("%H:%M:%S"),
                    "speaker": speaker,
                    "text": seg.get("text", ""),
                    "source_wav": w.name,
                }
                if seg.get("avg_logprob") is not None:
                    ap = float(seg["avg_logprob"])
                    entry["avg_logprob"] = ap
                    entry["low_confidence"] = ap < -0.5
                all_segments.append(entry)
            for sup in result.get("suppressed_hallucinations", []):
                sup_start = wav_start + timedelta(seconds=float(sup.get("start", 0.0)))
                all_suppressed.append({
                    "abs_start": sup_start.isoformat(),
                    "abs_hms": sup_start.strftime("%H:%M:%S"),
                    "speaker": speaker,
                    "text": sup.get("text", ""),
                    "matched_rule": sup.get("matched_rule", ""),
                    "source_wav": w.name,
                })

        all_segments.sort(key=lambda s: s["abs_start"])
        speakers = sorted({s["speaker"] for s in all_segments if s["speaker"]})

        sp_idx = {sp: i for i, sp in enumerate(speakers)}
        speaking_seconds = [0.0] * len(speakers)
        for s in all_segments:
            if s["speaker"] not in sp_idx:
                continue
            try:
                a = datetime.fromisoformat(s["abs_start"])
                b = datetime.fromisoformat(s["abs_end"])
                speaking_seconds[sp_idx[s["speaker"]]] += max(0.0, (b - a).total_seconds())
            except (ValueError, KeyError):
                continue
        speaking_seconds = [round(x, 2) for x in speaking_seconds]

        low_confidence_count = sum(1 for s in all_segments if s.get("low_confidence"))

        def _plain_line(s: dict) -> str:
            # Trailing "[uncertain]" marker so a downstream summarizer LLM
            # treats low-confidence text as a less-reliable signal.
            line = f"[{s['abs_hms']}] {s['speaker']}: {s['text']}"
            if s.get("low_confidence"):
                line += " [uncertain]"
            return line

        plain_lines = [_plain_line(s) for s in all_segments if s["text"]]
        plain_text = "\n".join(plain_lines)

        all_suppressed.sort(key=lambda s: s["abs_start"])
        # Device / transcriber labels now come directly from the Transcriber
        # instance instead of a tuple-tag peek.
        merged = {
            "session": session,
            "model": model_name,
            "from_iso": from_iso,
            "to_iso": to_iso,
            "source": source,
            "transcribed_at": datetime.now(timezone.utc).isoformat(),
            "transcribe_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "wav_count": len(selected),
            "skipped_bad_count": len(skipped_bad),
            "skipped_silent_count": len(skipped_silent),
            "speakers": speakers,
            "segments": all_segments,
            "plain_text": plain_text,
            "suppressed_count": len(all_suppressed),
            "suppressed": all_suppressed,
            "device": transcriber.device,
            "transcriber": transcriber.name,
            "speaking_seconds": speaking_seconds,
            "low_confidence_count": low_confidence_count,
        }

        out_path = session_dir / "session-transcript.json"
        out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        (session_dir / "session-transcript.txt").write_text(plain_text, encoding="utf-8")

        async with SESSION_PROGRESS_LOCK:
            SESSION_PROGRESS.pop(session, None)

        return JSONResponse(merged)
    except Exception as e:
        async with SESSION_PROGRESS_LOCK:
            if session in SESSION_PROGRESS:
                SESSION_PROGRESS[session]["status"] = "error: " + str(e)
        raise


# ---------------------------------------------------------------------------
# WebSocket: record one WAV per connection
# ---------------------------------------------------------------------------

@app.websocket("/record")
async def record(ws: WebSocket):
    await ws.accept()
    # Honor the operator's pause toggle: accept the WS so the bridge knows
    # we heard the open, then close cleanly. The bridge's framework drops
    # frames silently rather than spinning a reconnect loop.
    if not config.RECORDING_ENABLED:
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
    # Lazy-create the session folder so empty/unused sessions never appear
    # on disk. Safe to call every WS open.
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    fpath = config.SESSION_DIR / fname

    conn_id = utt + "-" + short_id
    async with ACTIVE_LOCK:
        ACTIVE[conn_id] = {
            "conn_id": conn_id,
            "identity": identity,
            "name": name,
            "started_at": started.isoformat(),
            "filename": fname,
            "bytes_received": 0,
        }

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
                async with ACTIVE_LOCK:
                    if conn_id in ACTIVE:
                        ACTIVE[conn_id]["bytes_received"] = bytes_received
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pragma: no cover
        print(f"[tapscribe] WS error for {fname}: {e}", flush=True)
    finally:
        wf.close()
        async with ACTIVE_LOCK:
            ACTIVE.pop(conn_id, None)
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
    """Serve dashboard JS modules. Restricted to .js files under web/js/ to
    keep this from doubling as a generic static-file route."""
    if not name.endswith(".js") or "/" in name or "\\" in name:
        raise HTTPException(404, "not found")
    path = DASHBOARD_JS_DIR / name
    if not path.is_file():
        raise HTTPException(404, "not found")
    if DASHBOARD_JS_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="application/javascript")
