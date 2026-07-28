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
import logging
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import auth, config
from .live_control import (
    DesiredLiveState,
    LiveReconcileError,
    apply_live,
    plan_live,
)
from .recorder import Recorder
from .routes import ALL_ROUTERS, mount_static
from .routes.deps import get_recorder
from .routes.errors import register_domain_errors

# `get_recorder` is re-exported deliberately (it now lives in `routes/deps.py`):
# `app.dependency_overrides` keys on the callable's IDENTITY, so
# `from tapscribe.app import app, get_recorder` stays the way a caller swaps the
# Recorder for a test double. Re-exporting a name that gets MONKEYPATCHED would
# be the opposite of a favour: the patch would land on this module while the
# route kept reading its own global, so nothing else is re-exported here.
__all__ = ["app", "get_recorder"]

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


register_domain_errors(app)


# ---------------------------------------------------------------------------
# Health + simple listings
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Live channel control
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Recording toggle
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Editable config — dashboard "save" buttons for prompt / live-prompt /
# hotwords. Atomic via tempfile + rename inside the writer helpers.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session housekeeping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# People Registry (ADR-0009) — the canonical cross-session Person model. The
# registry view also rides on /api/state (`people`); these routes are the
# explicit fetch + the rename / merge / detach mutations. people.json is
# mutated ONLY here and in the /api/state sync — both on the event loop, so
# they can't race. A person_id / identity from the body is validated against
# the loaded registry (KeyError→404) before anything is written; nothing here
# builds a filesystem path from request input (people.json is a fixed path).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WAV download + transcription
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket: one Bridge utterance per connection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Routers: the HTTP surface lives in tapscribe/routes/ (ADR-0018)
# ---------------------------------------------------------------------------

for _router in ALL_ROUTERS:
    app.include_router(_router)
mount_static(app)
