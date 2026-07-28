"""The HTTP surface, one module per resource group. Start here to answer
"where does X get served".

  tap              the Bridge control plane: WS /tap, session rotation,
                   the pipeline trigger/poll twins, tap settings, pause
  state            GET /api/state, the dashboard's poll (over state_view)
  sessions         the listing, the lazy bodies, destructive housekeeping
  strip            strip silence: preview, commit, read back, discard
  wav              one WAV: download, transcript, peaks, delete, primary
  transcribe       the batch transcribe triggers
  summarize        summarize a session, the model list, the saved default
  live             live channel control and its transcript feed
  people           the People registry view and its mutations
  models           the model catalog and the in-process model cache
  operator_config  the persisted text config and the language catalog
  setup            first-run / manage-models state and the install SSE
  bridges          bridge onboarding: the tap token and the download catalog
  diagnostics      /health, /healthz, the browser error relay
  assets           the page shells, top-level stylesheets, /web/* mounts

Support modules (never a route between them, so a router imports only these):

  deps             get_recorder, the one shared FastAPI dependency
  body             read a JSON body, parse one field of it
  errors           domain error to HTTP status, registered on the app
  guards           the destructive-route preflight (current / busy / live tap)

Grouping follows the DOMAIN CONCERN, not the URL prefix (ADR-0018), so a helper
that two routes must share stays module-private: `strip` owns four routes across
two URL prefixes for exactly that reason, and `tap` keeps each auth twin beside
its sibling. Every module's docstring opens with a complete route map, and
`tests/test_route_surface.py` fails when a route is missing from it or a map line
names a route that isn't registered.

`ALL_ROUTERS` is what `app.py` includes. Order is free: no two registered routes
are match-ambiguous, and a test pins that premise.
"""

from __future__ import annotations

from fastapi import APIRouter

from .assets import mount_static
from .assets import router as assets_router
from .bridges import router as bridges_router
from .diagnostics import router as diagnostics_router
from .live import router as live_router
from .models import router as models_router
from .operator_config import router as operator_config_router
from .people import router as people_router
from .sessions import router as sessions_router
from .setup import router as setup_router
from .state import router as state_router
from .strip import router as strip_router
from .summarize import router as summarize_router
from .tap import router as tap_router
from .transcribe import router as transcribe_router
from .wav import router as wav_router

#: Every router, in include order (which is presentation order only).
ALL_ROUTERS: tuple[APIRouter, ...] = (
    tap_router,
    state_router,
    sessions_router,
    strip_router,
    wav_router,
    transcribe_router,
    summarize_router,
    live_router,
    people_router,
    models_router,
    operator_config_router,
    setup_router,
    bridges_router,
    diagnostics_router,
    assets_router,
)

__all__ = ["ALL_ROUTERS", "mount_static"]
