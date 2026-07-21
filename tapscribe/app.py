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
                                  Optional &session=<id> pins the tap to a
                                  detached session (unknown id → upgrade refused).
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
import time
import wave
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import partial
from itertools import islice
from pathlib import Path
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
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import auth, bridges_catalog, config
from . import hallucinations as hallucinations_mod
from .audio import compute_peaks
from .batch_pipeline import PipelineRequest, start_pipeline
from .batch_strip import StrippedDirUnclearable, StripSessionRequest, strip_session
from .batch_summarize import (
    NoMergedTranscript,
    SummarizeSessionRequest,
    effective_summarizer_config,
    summarize_session,
)
from .batch_transcribe import (
    BatchOneRequest,
    BatchSessionRequest,
    WavTooQuiet,
    WavUnreadable,
    resolve_batch_model,
    transcribe_one,
    transcribe_session,
)
from .live_control import (
    DesiredLiveState,
    GateKindUnsupported,
    LiveModelUnknown,
    LiveReconcileError,
    LiveStartFailed,
    apply_live,
    plan_live,
)
from .name_resolution import attach_people, attach_people_mutation, attach_people_view
from .people import PeopleRegistry
from .recorder import Recorder, SessionBusy
from .runtime_probe import available_backend_strs, refresh_backend_probes
from .session_maintenance import (
    AbsorbCollision,
    InvalidAbsorbRequest,
    SessionDeleteError,
    absorb_session,
    delete_session_audio,
    delete_session_wav,
    prune_empty_sessions,
    reclaim_audio_older_than,
    session_is_empty,
)
from .session_merge import InvalidRange, NoUsableWavs
from .session_paths import (
    SessionNotFound,
    StrippedMissing,
    UnknownSource,
    WavNotFound,
    resolve_session_dir,
    resolve_wav,
    stripped_dir,
)
from .sessions import (
    MetaValidationError,
    gather_sessions,
    read_session_files,
    read_session_meta,
    read_session_summary,
    read_session_transcript,
    read_wav_strip_meta,
    read_wav_transcript,
    search_transcripts,
    write_session_meta,
)
from .setup_install import InstallSelectionError, run_install, sse, validate_selection
from .setup_state import build_setup_state, is_first_run
from .strip_silence import plan_strip_regions, read_wav_int16
from .summarizers import SummarizerFailed, SummarizerUnavailable, summary_model_catalog
from .summarizers.catalog import MAX_TOKENS_BOUNDS
from .tap_fan_out import TapFanOut
from .text import (
    CONFIG_KEYS,
    MAX_CONFIG_TEXT_LEN,
    read_config,
    read_languages,
    read_summarizer_config,
    summarizer_default_public,
    write_config,
    write_languages,
    write_summarizer_config,
)
from .transcribers import current_idle_ttl_s, evict_idle_now, run_on_model_thread
from .transcribers.catalog import (
    REGISTRY,
    SPECIALIST_MODELS,
    candidate_language_codes,
    language_display_name,
)
from .wav_cache import set_primary_transcript


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
# branch deep. The plain text-file keys ride text.CONFIG_KEYS/write_config;
# languages.txt keeps its own richer writer (catalog-validated code set).
_CONFIG_WRITERS: dict[str, Callable[[str], None]] = {
    **{key: partial(write_config, key) for key in CONFIG_KEYS},
    "languages": write_languages,
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


def _require_opt_str(raw, field: str) -> str | None:
    """The type boundary for optional string body fields — the ONE owner of
    the non-string 400 (a non-string JSON value 400s like every other
    malformed field in the _parse_* family; the `(body.get(x) or
    "").strip()` idiom 500s with an AttributeError before any validation
    runs). Returns the string VERBATIM — strip/blank policy belongs to the
    thin wrappers below (or the call site, for fields where whitespace is
    meaningful, e.g. the summarize route's prompt/api_key)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(400, f"{field} must be a string, got {type(raw).__name__}")
    return raw


def _parse_opt_str(raw, field: str) -> str | None:
    """Optional string body field: absent/blank → None, non-string → 400,
    otherwise the stripped value."""
    value = _require_opt_str(raw, field)
    return None if value is None else (value.strip() or None)


def _parse_opt_str_keep_empty(raw, field: str) -> str | None:
    """`_parse_opt_str` for fields where the EMPTY string is meaningful
    (an explicit clear — e.g. the summarize route's command/base_url
    overrides): absent → None, non-string → 400, otherwise the stripped
    value — "" included."""
    value = _require_opt_str(raw, field)
    return None if value is None else value.strip()


def _parse_opt_bool(raw, field: str) -> bool | None:
    """Optional boolean body field: absent → None passthrough; anything
    that isn't a JSON true/false 400s — same strictness as the rest of
    the _parse_* family. A truthy non-bool like "false" must never
    silently coerce (bool("false") is True)."""
    if raw is None:
        return None
    if not isinstance(raw, bool):
        raise HTTPException(400, f"{field} must be a boolean, got {type(raw).__name__}")
    return raw


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
        # Reconcile the boot channel toward the operator's persisted default
        # live model (config/live-model.txt) — the SAME transition
        # /api/live/start runs. The Recorder always constructs a
        # WhisperLiveKitChannel at boot, so a persisted Moonshine default
        # needs a family swap even though config.model is unchanged (#259);
        # `plan_live` resolves that swap unconditionally. Auto-start stays
        # best-effort: a reconcile failure (e.g. a weights fetch) is logged
        # and skipped, never crashing startup.
        rec = recorder
        desired = DesiredLiveState(model=rec.live.config.model)
        try:
            plan = plan_live(rec.live, desired, use_mlx=rec.use_mlx)
            await asyncio.to_thread(apply_live, rec.live, plan, set_live=lambda ch: setattr(rec, "live", ch))
        except LiveReconcileError as exc:
            print(f"[tapscribe] live auto-start skipped: {exc}", flush=True)
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

# Security headers on EVERY response (pages, JSON, errors) — the toolkit
# serve.mjs set, adapted to this app (vanilla-web reference/security.md):
#
# - script-src stays fully locked at 'self' (via default-src) AND
#   `require-trusted-types-for 'script'` turns every DOM XSS sink
#   (innerHTML & co) into a policy-guarded write. The dashboard has exactly
#   one sanctioned sink — loadTemplates in the vendored templates lib —
#   wrapped in the `vanilla-templates` policy. `'allow-duplicates'` is
#   load-bearing: the dashboard page loads TWO stamped copies of that lib
#   (the app seam's lib/templates.js and the vc components' vc/lib copy),
#   each lazily creating the same-named policy.
# - style-src carries 'unsafe-inline' for now: the component templates ship
#   ~47 inline style="…" attributes (template-authored markup, not
#   attacker-reachable — untrusted text only ever flows through
#   textContent). Tightening this means moving those into classes; tracked
#   as follow-up cleanup, deliberately not folded into this change.
# - connect-src 'self' covers the dashboard's same-origin fetches AND its
#   same-host WebSockets (/tap, /record, captions); media-src 'self' covers
#   WAV playback.
_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "trusted-types vanilla-templates 'allow-duplicates'; "
    "require-trusted-types-for 'script'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


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
    # subclasses keeps `type(exc)` lookups in `_domain_error_handler` exact.
    LiveModelUnknown: 400,
    GateKindUnsupported: 400,
    LiveStartFailed: 500,
}


async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate a known domain error to its status; anything unmapped falls to
    500, so a new orchestrator error can't silently slip through as a 200."""
    return JSONResponse(status_code=_DOMAIN_ERROR_STATUS.get(type(exc), 500), content={"detail": str(exc)})


for _exc_type in _DOMAIN_ERROR_STATUS:
    app.add_exception_handler(_exc_type, _domain_error_handler)


async def _refuse_current_or_busy(
    recorder: Recorder,
    *sessions: str,
    action: str,
    current: str,
    hint: str = "",
) -> None:
    """The three-guard pre-flight the destructive session/WAV routes need:
    refuse the CURRENT session — before the caller's `resolve_session_dir`,
    since the live session's directory may not be materialised on disk yet
    (rotate_session creates it lazily) — then refuse if any of `sessions`
    has a transcribe/strip job in flight — then refuse if `current` has a
    live tap writing to it.

    `current` names which of `sessions` must not be the live session —
    required, not derived, so a multi-session caller (absorb) can't
    silently skip it: absorb's target MAY be the live session, only its
    source may not. The active-tap guard reuses that SAME `current` scope
    (not all of `sessions`): absorb only moves source's files into target
    and never rewrites target's own files, so a tap on a live TARGET is
    never unsafe — only a tap on the session actually being emptied/deleted
    (`current`) is. `action` fills the current-session message's verb
    phrase ("delete", "absorb", …); `hint` appends extra guidance (absorb's
    rotate-then-absorb tip). The busy-job and active-tap branches both raise
    `SessionBusy` — the same domain error `JobTracker.run` raises, mapped to
    409 by `_DOMAIN_ERROR_STATUS` — so "session busy" has one canonical
    exception app-wide; only the current-session branch raises
    `HTTPException` directly (no domain error exists for session-identity)."""
    if current == recorder.session_start:
        msg = f"cannot {action} the current session — rotate to a new one first"
        raise HTTPException(409, f"{msg}, {hint}" if hint else msg)
    if any(recorder.jobs.get(s) is not None for s in sessions):
        noun = "this session" if len(sessions) == 1 else "one of these sessions"
        raise SessionBusy(f"a transcribe or strip job is in flight on {noun}")
    if any(s.session == current for s in await recorder.streams.snapshot()):
        raise SessionBusy("a live tap is writing to this session")


# ---------------------------------------------------------------------------
# Health + simple listings
# ---------------------------------------------------------------------------


# Client-error relay (wireErrorBar, tapscribe/web/js/lib/chrome.js): the
# dashboard beacons unhandled browser errors here so an operator (or an LLM
# session maintaining the app) can read them from the server log instead of
# needing the browser console. Storage-free by design — log-and-drop, capped
# and flood-guarded, mirroring the toolkit's serve.mjs endpoint.
_CLIENT_ERR_WINDOW_S = 60.0
_CLIENT_ERR_MAX_PER_WINDOW = 30
_client_err_times: deque[float] = deque()


@app.post("/api/client-errors", status_code=204)
async def client_errors(request: Request) -> Response:
    now = time.monotonic()
    while _client_err_times and now - _client_err_times[0] > _CLIENT_ERR_WINDOW_S:
        _client_err_times.popleft()
    if len(_client_err_times) >= _CLIENT_ERR_MAX_PER_WINDOW:
        # Silently drop past the cap: the relay is best-effort telemetry and a
        # crash-looping page must not turn into a log flood.
        return Response(status_code=204)
    _client_err_times.append(now)

    # sendBeacon posts text/plain, so parse the (size-capped) body by hand
    # instead of a JSON body model. Every field is untrusted browser input:
    # cap lengths and strip newlines so a crafted message can't forge extra
    # log lines (log injection).
    raw = (await request.body())[:8192]
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    def _field(name: str, cap: int) -> str:
        value = payload.get(name, "")
        text = value if isinstance(value, str) else str(value)
        # Explicit log-injection hardening: neutralize CR/LF and drop other
        # control chars, then collapse remaining whitespace and cap length.
        text = text.replace("\r", " ").replace("\n", " ")
        text = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in text)
        return " ".join(text.split())[:cap]

    logging.getLogger("tapscribe.client").warning(
        "client error [%s] at %s: %s (ua: %s)",
        _field("src", 40),
        _field("url", 200),
        _field("msg", 2000),
        _field("ua", 200),
    )
    return Response(status_code=204)


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
    active_streams = await recorder.streams.snapshot()
    return gather_sessions(
        current_session=recorder.session_start,
        jobs={k: asdict(v) for k, v in recorder.jobs.snapshot().items()},
        # Same open-WAV masking as /api/state so files_sig stays consistent
        # across the two endpoints during a recording.
        open_wavs={s.filename for s in active_streams if s.record and s.filename},
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


@app.get("/api/tap-token")
async def api_tap_token(recorder: Recorder = Depends(get_recorder)):
    """Reveal the /tap bearer token for bridge onboarding (#190) — the
    dashboard's "Connect a bridge" card fetches this only when the operator
    clicks reveal, never on every poll. Gated by dashboard BASIC auth like
    any other /api/* route: the path starts with "/api/tap" but NOT
    "/api/tap/" (config.TAP_PREFIX + "/"), so the auth middleware's
    startswith check does not route it into the TAP-BEARER scheme — see
    `auth.basic_auth_middleware`. Never logged."""
    return {"token": recorder.tap.value}


@app.get("/api/bridges")
async def api_bridges():
    """List the downloadable Bridges for the Settings "Get a bridge" card.

    Each entry carries the static catalog metadata plus a `download_url`
    composed from `config.GITHUB_REPO` — a permanent
    `releases/latest/download/<asset>` link the browser follows straight to
    GitHub (no FileResponse, no server-side proxy). The `latest/download`
    URLs 404 until the first `vX.Y.Z` tag is cut; the card renders the links
    unconditionally plus an "available after the first tagged release" hint
    rather than probing the GitHub API (which would add a network dependency
    and break airgapped servers — see ADR-0012). Basic-auth gated like any
    other read `/api/*` route."""
    return [
        {
            **a._asdict(),
            "download_url": (
                f"https://github.com/{config.GITHUB_REPO}/releases/latest/download/{a.filename}"
            ),
        }
        for a in bridges_catalog.BRIDGE_ARTIFACTS
    ]


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
    raw_body = await req.body()
    if raw_body:
        try:
            body = json.loads(raw_body)
        except ValueError:
            return JSONResponse({"detail": "malformed JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "JSON body must be an object"}, status_code=400)
    else:
        body = {}
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


@app.post("/api/tap/sessions/{session}/pipeline", status_code=202)
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


@app.post("/api/sessions/{session}/pipeline", status_code=202)
async def api_dashboard_pipeline_trigger(session: str, recorder: Recorder = Depends(get_recorder)):
    """Trigger the end-of-meeting pipeline from the dashboard (Basic auth).

    A thin shim over `start_pipeline` — fire-and-forget, 202, body ignored.
    Resolves the batch model, backend, and summarizer from operator-side
    configuration; a dashboard operator can no more pick a model than the
    tap caller can. The request body is IGNORED entirely, never parsed."""
    return await _trigger_pipeline(recorder, session)


@app.get("/api/tap/sessions/{session}/pipeline")
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


# ---------------------------------------------------------------------------
# /api/state — the dashboard's once-per-second polling endpoint
# ---------------------------------------------------------------------------

# Round each open tap's raw bytes_received (bumped per 20 ms audio frame) to the
# nearest bucket before it lands in /api/state, so a quiet-but-open tap's
# sub-bucket byte drift stops busting the response ETag every poll (#217). One
# constant so the round-to-nearest stays centred: half a bucket is _TAP_BYTES_BUCKET // 2.
_TAP_BYTES_BUCKET = 64 * 1024


def _build_state_blob(
    current_session: str,
    active: list[dict[str, Any]],
    sessions_list: list[dict[str, Any]],
    registry: PeopleRegistry,
    occs: list[dict[str, Any]],
    live_identities: set[str],
    live_feed: list[dict[str, Any]],
    live_info: dict[str, Any],
    live_log: list[str],
    live_supports_native_vad: bool,
    recording_enabled: bool,
    backend: str,
    available_backends: list[str],
) -> tuple[bytes, str]:
    """Config reads, pure people joins, payload assembly, and ETag serialization.

    `sessions_list` is pre-gathered; the registry is pre-synced (mutation ran on
    the event loop). All recorder-owned inputs are snapshotted. Returns
    (body_bytes, etag_string)."""
    prompt = read_config("prompt")
    live_prompt = read_config("live-prompt")
    live_model_default = read_config("live-model")
    batch_model_default = read_config("batch-model")
    batch_model_effective = resolve_batch_model(warn=False)
    languages_default = list(read_languages())
    hotwords = read_config("hotwords")
    summarizer_default = summarizer_default_public(read_summarizer_config())
    halluc_rules = hallucinations_mod.parse_rules()
    hallucinations_content = read_config("hallucinations")
    inputs_support = _compute_inputs_support()

    people = attach_people_view(sessions_list, registry, occs, live_identities)

    override_counts: dict[str, int] = {"prompt": 0, "hotwords": 0, "summarizer": 0}
    for s in sessions_list:
        m = s.get("session_meta") or {}
        if m.get("prompt"):
            override_counts["prompt"] += 1
        if m.get("hotwords"):
            override_counts["hotwords"] += 1
        if m.get("summary_source") or m.get("summary_prompt"):
            override_counts["summarizer"] += 1

    payload = {
        "current_session": current_session,
        "active": active,
        "sessions": sessions_list,
        "people": people,
        "default_override_counts": override_counts,
        "live_feed": live_feed,
        "live_info": live_info,
        "live_log": live_log,
        "live_supports_native_vad": live_supports_native_vad,
        "backend": backend,
        "available_backends": available_backends,
        "recording_enabled": recording_enabled,
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
        "live_model_default": live_model_default,
        "batch_model_default": batch_model_default,
        "batch_model_effective": batch_model_effective,
        "languages": {
            "path": str(config.LANGUAGES_FILE),
            "default": languages_default,
        },
        "summarizer_default": summarizer_default,
        # No "rules" list here: the config card renders the raw content
        # textarea and reads only content + count — shipping every raw rule
        # string per poll tick was dead payload weight (#303 follow-up).
        "hallucinations": {
            "path": str(config.HALLUCINATIONS_FILE),
            "content": hallucinations_content,
            "count": len(halluc_rules),
        },
        "idle_ttl_s": current_idle_ttl_s(),
    }
    body = json.dumps(jsonable_encoder(payload), separators=(",", ":")).encode("utf-8")
    etag = 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    return body, etag


@app.get("/api/state")
async def api_state(req: Request, recorder: Recorder = Depends(get_recorder)):
    active_streams = await recorder.streams.snapshot()
    live_identities = {s.identity for s in active_streams}
    jobs_snapshot = {k: asdict(v) for k, v in recorder.jobs.snapshot().items()}
    open_wavs = {s.filename for s in active_streams if s.record and s.filename}

    # Active rows with tap_settings overlay (on loop, unchanged)
    active = []
    for s in active_streams:
        row = asdict(s)
        pref = recorder.tap_settings.get(s.identity)
        row["record"] = pref.record
        row["live"] = pref.live
        row["level"] = round(row["level"], 2)
        row["bytes_received"] = (
            (row["bytes_received"] + _TAP_BYTES_BUCKET // 2) // _TAP_BYTES_BUCKET * _TAP_BYTES_BUCKET
        )
        active.append(row)

    # Thread hop 1: gather_sessions (disk walk, off the loop)
    sessions_list = await asyncio.to_thread(
        lambda: gather_sessions(
            current_session=recorder.session_start,
            jobs=jobs_snapshot,
            open_wavs=open_wavs,
        )
    )

    # Mutation: load → sync → save (on event loop, serialised with /api/people)
    registry, occs = attach_people_mutation(sessions_list, live_identities=live_identities)

    # Thread hop 2: config reads + people joins + payload build + serialize + ETag
    body, etag = await asyncio.to_thread(
        _build_state_blob,
        recorder.session_start,
        active,
        sessions_list,
        registry,
        occs,
        live_identities,
        recorder.transcripts.snapshot(),
        dict(recorder.live.info),
        # Last 30 lines without copying the whole 200-entry deque on every
        # poll tick — islice walks straight to the tail.
        list(islice(recorder.live.log, max(0, len(recorder.live.log) - 30), None)),
        bool(getattr(recorder.live, "supports_native_vad", False)),
        recorder.recording_enabled,
        recorder.backend,
        sorted(available_backend_strs()),
    )

    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if req.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


# ---------------------------------------------------------------------------
# Live channel control
# ---------------------------------------------------------------------------


@app.post("/api/live/start")
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
    body = await _json_body(req)
    # Boundary parsing FIRST — non-strings and out-of-range numbers 400 at
    # the HTTP edge (CodeQL treats Request.json() as untrusted; the
    # dashboard's min/max attrs are client-side hints only) while building
    # the DesiredLiveState, before any domain logic runs. Nothing downstream
    # can mutate on a rejected request.
    desired = DesiredLiveState(
        model=_parse_opt_str(body.get("model"), "model"),
        language=_parse_opt_str(body.get("language"), "language"),
        gate_kind=_parse_opt_str(body.get("gate_kind"), "gate_kind"),
        conf=_parse_opt_bool(body.get("confidence_validation"), "confidence_validation"),
        gate_speech_threshold=_parse_bounded_float(
            body.get("gate_speech_threshold"), "gate_speech_threshold", lo=0.0, hi=1.0
        ),
        gate_hangover_ms=_parse_bounded_int(
            body.get("gate_hangover_ms"), "gate_hangover_ms", lo=0, hi=10_000
        ),
        gate_pre_roll_ms=_parse_bounded_int(body.get("gate_pre_roll_ms"), "gate_pre_roll_ms", lo=0, hi=5_000),
        gate_min_speech_ms=_parse_bounded_int(
            body.get("gate_min_speech_ms"), "gate_min_speech_ms", lo=0, hi=5_000
        ),
    )
    # Pure: validates (raising a LiveReconcileError the domain-error handler
    # maps) and decides the transition without touching the running channel.
    plan = plan_live(recorder.live, desired, use_mlx=recorder.use_mlx)
    return await asyncio.to_thread(
        apply_live, recorder.live, plan, set_live=lambda ch: setattr(recorder, "live", ch)
    )


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
    # selected at install time (the picker in tapscribe/install_picker.py only
    # pulls in extras the operator ticks). Without this filter, the
    # dashboard would advertise Parakeet even on machines that skipped the
    # transformers install — and the operator would only find out by
    # clicking and hitting the lazy-import error.
    entries = REGISTRY.for_context(context, only_installed=True)  # type: ignore[arg-type]
    return {
        "context": context,
        "available_backends": sorted(available_backend_strs()),
        "models": [e.to_mapping() for e in entries],
    }


@app.get("/api/languages")
async def api_languages():
    """The candidate-language catalog (ADR-0010) for the dashboard picker: the
    full allowlist of selectable languages with display names, plus the
    operator's current global default. Static apart from the default, so the
    dashboard fetches it once (like /api/models) rather than per poll.

    `specialists` maps a language code → the purpose-built model the cover adds
    for it (ADR-0010's specialist table, v1: `{"no": "nb-whisper-large"}`),
    registry-filtered like `cover_models`. The Transcript page reads it (with the
    generalist from `/api/state`'s `batch_model_effective`) to show which models a
    transcribe will actually run for the declared languages — so a Norwegian
    meeting's nb-whisper (faster-whisper) pass is visible up front
    rather than a surprise sidecar (ADR-0011).

    Response shape:
      {
        "languages":   [ {"code": "da", "name": "Danish"}, ... ],
        "default":     ["da", "no", "en"],
        "specialists": {"no": "nb-whisper-large"}
      }
    """
    return {
        "languages": [{"code": c, "name": language_display_name(c)} for c in candidate_language_codes()],
        "default": list(read_languages()),
        # Registry-filtered so the readout drops exactly what `cover_models` drops
        # (an env-overridden specialist absent from the catalog never runs), keeping
        # the client-side "models that will run" union provably equal to the cover.
        "specialists": {lang: m for lang, m in SPECIALIST_MODELS.items() if REGISTRY.get(m) is not None},
    }


@app.get("/api/setup/state")
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


@app.post("/api/setup/install")
async def api_setup_install(request: Request):
    """Install the selected model families and stream progress as Server-Sent
    Events. Body: ``{"families": {"<family>": "<mlx|cuda|cpu>", ...}}``.

    Delegates the actual pip work to the dependency-free install picker
    (`tapscribe/install_picker.py --non-interactive`) against a selection written
    from the validated request, streaming one SSE `data:` event per output line
    then a terminal `done`/`error`. On success the backend probes are refreshed
    so `/api/models` + `/api/setup/state` reflect the new install without a
    restart. Concurrent installs are refused (409)."""
    body = await _json_body(request)
    try:
        selection = validate_selection(body.get("families", {}))
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
                # Set by `python -m tapscribe --install-spec` (the Bundle's
                # Launcher passes its wheel); absent in a checkout.
                install_spec=getattr(request.app.state, "install_spec", None),
                on_success=refresh_backend_probes,
            ):
                yield sse(ev)
        finally:
            request.app.state.setup_install_active = False

    # no-cache + no proxy buffering so events arrive as they're produced
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


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
    # _parse_opt_bool, not bool(): see api_recording_toggle — "false" from a
    # client that stringifies its flags must 400, never silently mean True.
    setting = recorder.tap_settings.set(
        identity,
        record=_parse_opt_bool(body.get("record"), "record"),
        live=_parse_opt_bool(body.get("live"), "live"),
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
    set explicitly; without a body, just toggles. New /tap WSes are
    accepted then immediately closed when disabled — already-open WAVs
    continue to record their current utterance, which finalises cleanly
    on the bridge's normal trackMuted close."""
    body = await _json_body(req)
    if "enabled" in body:
        # _parse_opt_bool, not bool(): a client that stringifies its flags would
        # otherwise turn {"enabled": "false"} into ENABLED (bool("false") is
        # True) and keep recording every participant after the operator asked
        # to pause — a wrong-direction privacy bug, not just a bad request.
        enabled = recorder.toggle_recording(enabled=_parse_opt_bool(body["enabled"], "enabled"))
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


def _parse_strip_knob_overrides(min_silence_ms: Any, pad_ms: Any, speech_floor_db: Any) -> dict[str, Any]:
    """Range-bound the strip-silence knobs and return only the explicitly
    provided ones, ready to splat into StripSessionRequest (which owns the
    DEFAULTS). One owner for the names + bounds + only-forward-explicit
    contract, shared by the strip route (body knobs) and the strip-preview
    route (query knobs) so the two can never drift — the preview must plan
    with exactly the knobs a commit would use. The `is not None` checks keep
    an explicit 0 (e.g. pad_ms=0 to disable region padding for A/B) from
    silently falling back to the default; out-of-range values → 400 instead
    of a 500 from int()/float()."""
    overrides: dict[str, Any] = {}
    bounded_min_silence = _parse_bounded_int(min_silence_ms, "min_silence_ms", lo=100, hi=600_000)
    if bounded_min_silence is not None:
        overrides["min_silence_ms"] = bounded_min_silence
    bounded_pad = _parse_bounded_int(pad_ms, "pad_ms", lo=0, hi=5_000)
    if bounded_pad is not None:
        overrides["pad_ms"] = bounded_pad
    bounded_floor = _parse_bounded_float(speech_floor_db, "speech_floor_db", lo=-120.0, hi=0.0)
    if bounded_floor is not None:
        overrides["speech_floor_db"] = bounded_floor
    return overrides


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
    overrides = _parse_strip_knob_overrides(
        body.get("min_silence_ms"), body.get("pad_ms"), body.get("speech_floor_db")
    )
    return await strip_session(recorder, StripSessionRequest(session=session, **overrides))


@app.post("/api/sessions/{session}/summarize")
async def api_session_summarize(
    session: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
):
    """Summarize a session's merged transcript. Thin HTTP shim over
    `batch_summarize.summarize_session` — parse the body; the registered
    domain-error handlers map failures to status codes. The Local (bundled,
    offline — #86) and Command (#82) sources are wired, while the API source
    (#85) still maps to a clear 400.

    Body fields the caller omits resolve through the saved config (#84):
    session-meta override → global default → built-ins, via
    `effective_summarizer_config`. An explicit body field wins over both
    saved layers, so a Generate with hand-edited values behaves exactly as
    before. The effective model/source were allowlist-validated at write
    time AND are re-validated inside `load_summarizer` (double guard)."""
    body = await _json_body(req)
    overrides: dict[str, Any] = await asyncio.to_thread(effective_summarizer_config, session)
    # Boundary validation through the _parse_* family: a non-string value
    # 400s instead of being silently ignored. source/model/api_key treat
    # blank as "not supplied" (fall through to the saved config);
    # command/prompt/base_url keep the empty string — an explicit clear of
    # the saved value is meaningful for them. prompt and api_key values are
    # passed VERBATIM (see their call sites below); the rest are stripped.
    source = _parse_opt_str(body.get("source"), "source")
    if source is not None:
        overrides["source"] = source
    command = _parse_opt_str_keep_empty(body.get("command"), "command")
    if command is not None:
        overrides["command"] = command
    model = _parse_opt_str(body.get("model"), "model")
    if model is not None:
        overrides["model"] = model
    # max_tokens: parse + bounds-check exactly like the other numeric body knobs
    # (gate / strip-silence) — a clear 400 for out-of-range, None when omitted.
    # The adapter also clamps as a final safety net for non-route callers.
    max_tokens = _parse_bounded_int(
        body.get("max_tokens"), "max_tokens", lo=MAX_TOKENS_BOUNDS[0], hi=MAX_TOKENS_BOUNDS[1]
    )
    if max_tokens is not None:
        overrides["max_tokens"] = max_tokens
    # prompt is deliberately VERBATIM (no strip): leading/trailing whitespace
    # in an operator-authored prompt template is meaningful, and "" is an
    # explicit clear. Only the type boundary applies.
    prompt = _require_opt_str(body.get("prompt"), "prompt")
    if prompt is not None:
        overrides["prompt"] = prompt
    base_url = _parse_opt_str_keep_empty(body.get("base_url"), "base_url")
    if base_url is not None:
        overrides["base_url"] = base_url
    # api_key: blank means "not supplied" (fall through to saved config) but
    # the ACCEPTED value is passed verbatim — keys are opaque tokens and a
    # strip could corrupt one that legitimately contains edge whitespace.
    api_key = _require_opt_str(body.get("api_key"), "api_key")
    if api_key is not None and api_key.strip():
        overrides["api_key"] = api_key
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


@app.get("/api/summarize/config")
async def api_summarize_config_get():
    """The structured global summarizer default (#84) as the REDACTED public
    projection. The api_key is write-only and never returned; `key_set`
    reflects whether one is stored. See `summarizer_default_public`."""
    return summarizer_default_public(read_summarizer_config())


@app.put("/api/summarize/config")
async def api_summarize_config_put(req: Request):
    """Persist the global summarizer default. Full-object semantics (a
    missing key clears that field). Dedicated endpoint rather than a
    `_CONFIG_WRITERS` entry — that map is `{content: str}`-shaped, this is
    one structured object. ALL validation (source/model allowlists, text
    caps, max_tokens int + bounds) lives in `write_summarizer_config`; its
    ValueError is the 400."""
    body = await _json_body(req)
    try:
        stored = write_summarizer_config(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(500, f"failed to write config: {e}") from None
    return {"ok": True, "config": stored}


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
    session-transcript + session-meta. Refuses the CURRENT session, any
    session with a transcribe/strip job in flight, or a session with a live
    tap writing to it."""
    await _refuse_current_or_busy(recorder, session, current=session, action="delete audio from")
    resolve_session_dir(session)
    # Hold the session's job slot for the walk's duration: the pre-flight
    # above is check-then-act (a transcribe/strip could claim the freed slot
    # between the check and the thread hop and race the unlink walk), so the
    # delete claims the SAME slot the batch jobs use — a job arriving
    # mid-delete gets the standard SessionBusy 409, and vice versa. run()
    # releases on every exit path, so unlike whole-session delete the
    # surviving session's slot is always freed again.
    async with recorder.jobs.run(session, kind="delete", total=1):
        # Offload the filesystem walk (many WAVs + .transcripts/ dirs) so the
        # ~1 Hz /api/state poll stays responsive — same as strip-silence.
        summary = await asyncio.to_thread(delete_session_audio, session)
    print(
        f"[tapscribe] deleted audio from session {session}: "
        f"{summary['wavs_deleted']} wavs, {summary['bytes_freed']} bytes freed",
        flush=True,
    )
    return {"ok": True, **summary}


@app.post("/api/sessions/bulk-reclaim-audio")
async def api_bulk_reclaim_audio(req: Request, recorder: Recorder = Depends(get_recorder)):
    """Bulk reclaim audio from old sessions. Walks the recordings archive
    and, for every session older than ``older_than_days`` with a merged
    transcript, reclaims its audio (originals + ``stripped/`` go; the
    merged transcript + meta stay).

    Body (JSON), like the sibling ``absorb`` route — NOT query params — so a
    dashboard POSTing ``{older_than_days, execute}`` can't silently arrive as
    a no-op ``execute=False`` (a bare-annotated scalar would resolve as a
    Query param and drop a JSON body):

        ``{"older_than_days": <int > 0>, "execute": <bool>}``

    ``execute`` absent/false (preview): lists eligible sessions and their
    reclaimable byte counts without deleting anything. ``execute: true``
    performs the reclaim across all eligible sessions.

    Returns ``{ok, sessions, total_bytes, failed}``. Refuses
    ``older_than_days <= 0`` (400). The current session, any session with a
    transcribe/strip job in flight, and any session with a live tap are all
    excluded, so live/busy audio is never touched.
    """
    body = await _json_body(req)
    older_than_days = body.get("older_than_days")
    if not isinstance(older_than_days, int) or isinstance(older_than_days, bool) or older_than_days <= 0:
        raise HTTPException(400, "older_than_days must be a positive integer")
    # Strict: only a literal JSON `true` triggers a real delete — `bool("false")`
    # is True, so never coerce an arbitrary body value on a destructive flag.
    execute = body.get("execute") is True

    # Exclude what the recorder-free reclaim fn can't see: a session with a
    # transcribe/strip job in flight (recorder.jobs) or a live tap writing to
    # it (recorder.streams). Deleting their WAVs mid-job would corrupt output;
    # the current session is excluded inside the fn. Mirrors the single-item
    # DELETE route's _refuse_current_or_busy, but as an EXCLUSION (skip the
    # busy ones) rather than refusing the whole bulk op.
    busy = set(recorder.jobs.snapshot())
    busy |= {s.session for s in await recorder.streams.snapshot()}

    result = await asyncio.to_thread(
        reclaim_audio_older_than,
        recorder.session_start,
        older_than_days,
        execute=execute,
        exclude_sessions=frozenset(busy),
        busy_check=recorder.jobs.get,
    )
    return {"ok": True, **result}


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
    Refuses if either side has an in-flight transcribe / strip job, or if
    the SOURCE has a live tap writing to it. The target may freely be the
    live session or have a live tap open — absorb only ever moves source's
    files in, it never rewrites target's own files.
    """
    body = await _json_body(req)
    source = body.get("source") or ""
    if not isinstance(source, str) or not source:
        raise HTTPException(400, "source session id required")
    if source == target:
        raise HTTPException(400, "cannot absorb a session into itself")
    await _refuse_current_or_busy(
        recorder,
        target,
        source,
        current=source,
        action="absorb",
        hint="then absorb the now-previous folder into the target",
    )
    resolve_session_dir(target)
    resolve_session_dir(source)

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
    """Recursively delete a recordings folder. Refuses the CURRENT session,
    any session with a transcribe/strip job in flight, or a session with a
    live tap writing to it — `rmtree`-ing the folder out from under a running
    job thread or an open tap WS would crash it mid-write (the same guard
    the sibling /audio and /absorb endpoints enforce)."""
    await _refuse_current_or_busy(recorder, session, current=session, action="delete")
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


# ---------------------------------------------------------------------------
# People Registry (ADR-0009) — the canonical cross-session Person model. The
# registry view also rides on /api/state (`people`); these routes are the
# explicit fetch + the rename / merge / detach mutations. people.json is
# mutated ONLY here and in the /api/state sync — both on the event loop, so
# they can't race. A person_id / identity from the body is validated against
# the loaded registry (KeyError→404) before anything is written; nothing here
# builds a filesystem path from request input (people.json is a fixed path).
# ---------------------------------------------------------------------------


async def _people_view(recorder: Recorder) -> list[dict[str, Any]]:
    active_streams = await recorder.streams.snapshot()
    live_identities = {s.identity for s in active_streams}
    sessions = await asyncio.to_thread(gather_sessions, current_session=recorder.session_start, jobs={})
    return attach_people(sessions, live_identities=live_identities)


@app.get("/api/people")
async def api_people_get(recorder: Recorder = Depends(get_recorder)):
    """The cross-session People view: one row per Person with name, member
    identities, sessions, recorded/live source. Same shape /api/state ships."""
    return {"people": await _people_view(recorder)}


@app.put("/api/people/{person_id}")
async def api_people_rename(person_id: str, req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await _json_body(req)
    name = body.get("name", "")
    if not isinstance(name, str):
        raise HTTPException(400, "name must be a string")
    registry = PeopleRegistry.load()
    try:
        registry.rename(person_id, name.strip())
    except KeyError:
        raise HTTPException(404, "person not found") from None
    registry.save()
    return {"ok": True, "people": await _people_view(recorder)}


@app.post("/api/people/merge")
async def api_people_merge(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await _json_body(req)
    survivor = body.get("survivor")
    absorbed = body.get("absorbed")
    if not isinstance(survivor, str) or not isinstance(absorbed, str) or not survivor or not absorbed:
        raise HTTPException(400, "survivor and absorbed person ids are required")
    registry = PeopleRegistry.load()
    try:
        registry.merge(survivor, absorbed)
    except KeyError:
        raise HTTPException(404, "person not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    registry.save()
    return {"ok": True, "people": await _people_view(recorder)}


@app.post("/api/people/{person_id}/detach")
async def api_people_detach(person_id: str, req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await _json_body(req)
    identity = body.get("identity")
    if not isinstance(identity, str) or not identity:
        raise HTTPException(400, "identity is required")
    registry = PeopleRegistry.load()
    try:
        new_person = registry.detach(person_id, identity)
    except KeyError:
        raise HTTPException(404, "person not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    registry.save()
    return {"ok": True, "detached": new_person["id"], "people": await _people_view(recorder)}


@app.get("/api/sessions/{session}/transcript")
async def api_session_transcript(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """The FULL merged session-transcript.json (or null when none).

    Lazy companion to `/api/state`, whose `session_transcript` is now a slim
    marker. The dashboard fetches this once per (session, transcribed_at) when
    a session is opened and caches it client-side, so the heavy segments[] /
    plain_text / suppressed[] body crosses the wire on open, not every poll.
    The disk read is offloaded with to_thread like the rest of the poll path."""
    return await asyncio.to_thread(read_session_transcript, session)


@app.get("/api/sessions/{session}/files")
async def api_session_files(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """The FULL per-session WAV listing (originals + their stripped region
    clips), the `files[]` array `/api/state` no longer embeds.

    Lazy companion to `/api/state`, which now carries only `wav_count`,
    `total_bytes`, `total_duration_s` and a `files_sig`. The dashboard fetches
    this once per (session, files_sig) when a session is opened and caches it
    client-side, so a huge session's per-WAV array crosses the wire on open +
    on change — not on every poll. `resolve_session_dir` (inside
    `read_session_files`) validates the id against path traversal; the disk walk
    is offloaded with to_thread like the rest of the poll path."""
    return await asyncio.to_thread(read_session_files, session)


@app.get("/api/sessions/{session}/summary")
async def api_session_summary(session: str, recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """The FULL persisted session summary (or null when none).

    Lazy companion to `/api/state`, whose `session_summary` is a slim marker
    (summarized_at + source + model). The dashboard fetches this once per
    (session, summarized_at) when the Summary stage is opened and caches it
    client-side. The disk read is offloaded with to_thread like the rest of
    the poll path."""
    return await asyncio.to_thread(read_session_summary, session)


@app.get("/api/search")
async def api_search(q: str = "", recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """Cross-session transcript-content search.

    Scans every session's merged transcript for a query term (case-
    insensitive) and returns one hit per matching session:
    ``{session, label, snippet, count}``.

    Basic-auth (not tap-bearer, not exempt). The scan runs off the event
    loop via ``asyncio.to_thread`` so it doesn't block /api/state polling.
    """
    return await asyncio.to_thread(search_transcripts, q)


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
    return await asyncio.to_thread(read_wav_transcript, session, name, source)


@app.get("/api/wav/{session}/{name}/strip-meta")
async def api_wav_strip_meta(session: str, name: str):
    """The committed strip-silence cut for one ORIGINAL wav (or null when the
    session was never stripped or this wav produced no regions). Lazy
    companion to /api/state, same contract as the transcript sidecar route:
    resolve_wav path-safety inside the reader, disk read off the event loop."""
    return await asyncio.to_thread(read_wav_strip_meta, session, name)


@app.get("/api/wav/{session}/{name}/strip-preview")
async def api_wav_strip_preview(
    session: str,
    name: str,
    min_silence_ms: int | None = None,
    pad_ms: int | None = None,
    speech_floor_db: float | None = None,
    source: str = "original",
):
    """What ✂ strip WOULD cut for one WAV at the given knobs — the live
    waveform overlay's data source (#89). Runs the REAL detector through the
    same plan the splitter writes from, with NO disk writes. Shares the
    strip route's knob bounds (out-of-range → 400) and get_wav's
    path-sanitiser; omitted knobs fall back to StripSessionRequest's
    defaults, mirroring the strip route's only-forward-explicit contract."""
    overrides = _parse_strip_knob_overrides(min_silence_ms, pad_ms, speech_floor_db)
    knobs = StripSessionRequest(session=session, **overrides)
    path = resolve_wav(session, name, source)

    def _plan() -> dict[str, Any]:
        samples = read_wav_int16(path)
        plan = plan_strip_regions(
            samples,
            min_silence_ms=knobs.min_silence_ms,
            pad_ms=knobs.pad_ms,
            speech_floor_db=knobs.speech_floor_db,
        )
        return {
            "spans": plan.spans,
            "in_seconds": plan.in_seconds,
            "speech_seconds": plan.speech_seconds,
            "segments": len(plan.regions),
            "segments_filtered_below_floor": plan.segments_filtered_below_floor,
            "silent": plan.silent,
            "rms_dbfs": round(plan.rms_dbfs, 1),
            "reason": plan.reason,
            "detector": plan.detector,
            "knobs": {
                "min_silence_ms": knobs.min_silence_ms,
                "pad_ms": knobs.pad_ms,
                "speech_floor_db": knobs.speech_floor_db,
            },
        }

    try:
        return await asyncio.to_thread(_plan)
    except (ValueError, wave.Error, EOFError, OSError) as e:
        # read_wav_int16 rejects non-recorder formats with ValueError and
        # surfaces corrupt/truncated/vanished files as wave.Error/EOFError/
        # OSError — every "this WAV can't be planned" case → 422
        # unprocessable, the same outcome the peaks route reaches via
        # compute_peaks's RuntimeError wrapping.
        raise HTTPException(422, str(e)) from e


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
    RECORDINGS_DIR — including rejecting an unknown `source`), clamps `bins`
    to a sane band, and offloads the O(samples) read off the event loop. The
    payload is `bins` floats regardless of recording length."""
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
    `delete_session_wav`. Refuses the CURRENT session, any session with
    a transcribe/strip job in flight, or a session with a live tap writing
    to it. An unknown `source` is rejected (400) by `resolve_source_dir` —
    the path seam owns that check; `source` itself is never a path
    component (only compared against the two literals)."""
    await _refuse_current_or_busy(recorder, session, current=session, action="delete WAVs from")
    resolve_session_dir(session)
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
    body = await _json_body(req)
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
    payload = await transcribe_one(recorder, request)
    print(
        f"[tapscribe] transcribed {request.name} ({request.source}) with {request.model}",
        flush=True,
    )
    return JSONResponse(payload)


@app.post("/api/transcribe-session")
async def api_transcribe_session(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await _json_body(req)
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
    return JSONResponse(await transcribe_session(recorder, request))


# ---------------------------------------------------------------------------
# WebSocket: one Bridge utterance per connection
# ---------------------------------------------------------------------------


@app.websocket("/tap")
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
SETUP_HTML_PATH = config.WEB_DIR / "setup.html"
# Vendored toolkit token sheets (canon names; dashboard.css overrides the
# values) — shared by the dashboard AND /setup, hence top-level like the
# other page stylesheets.
TOKENS_CSS_PATH = config.WEB_DIR / "tokens.css"
TONES_CSS_PATH = config.WEB_DIR / "tones.css"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    # First run (no transcription backend installed) → send the operator to the
    # browser setup surface instead of an empty dashboard. A no-op once any
    # backend is installed; is_first_run() reads the cached catalog probes.
    if is_first_run():
        return RedirectResponse("/setup", status_code=307)
    try:
        return HTMLResponse(NEXT_HTML_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(
            "<!doctype html><html><body>"
            "<h1>Dashboard HTML missing</h1>"
            "<p>Expected at <code>" + str(NEXT_HTML_PATH) + "</code>.</p>"
            "</body></html>"
        )


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    """First-run / manage-models setup surface. Reachable any time (it doubles
    as "manage models"); the bootstrap directs a fresh install here. The page's
    JS drives GET /api/setup/state + POST /api/setup/install. A separate route
    (not gating `/`) so the dashboard is never affected by install state."""
    try:
        return HTMLResponse(SETUP_HTML_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, f"setup.html missing at {SETUP_HTML_PATH}") from None


def _css_response(path: Path, name: str) -> FileResponse:
    """The one is_file → 404 → FileResponse(text/css) body the four top-level
    stylesheet routes share. One decorated handler per route stays (explicit
    routing table); only the body is deduplicated."""
    if not path.is_file():
        raise HTTPException(404, f"{name} not found")
    return FileResponse(path, media_type="text/css")


@app.get("/dashboard.css")
async def dashboard_css():
    return _css_response(DASHBOARD_CSS_PATH, "dashboard.css")


@app.get("/next.css")
async def next_css():
    return _css_response(NEXT_CSS_PATH, "next.css")


@app.get("/tokens.css")
async def tokens_css():
    return _css_response(TOKENS_CSS_PATH, "tokens.css")


@app.get("/tones.css")
async def tones_css():
    return _css_response(TONES_CSS_PATH, "tones.css")


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
