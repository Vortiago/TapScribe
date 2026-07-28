"""The HTTP surface, one module per resource group. Start here to answer
"where does X get served".

  assets           the page shells, top-level stylesheets, /web/* mounts
  state            GET /api/state, the dashboard's poll (over state_view)

Support modules (never a route between them, so a router imports only these):

  deps             get_recorder, the one shared FastAPI dependency
  body             read a JSON body, parse one field of it
  errors           domain error → HTTP status, registered on the app
  guards           the destructive-route preflight (current / busy / live tap)

Grouping follows the DOMAIN CONCERN, not the URL prefix (ADR-0018), so a helper
that two routes must share stays module-private. Every module's docstring opens
with a complete route map, and `tests/test_route_surface.py` fails when a route
is missing from it or a map line names a route that isn't registered.

`ALL_ROUTERS` is what `app.py` includes. Order is free: no two registered routes
are match-ambiguous, and a test pins that premise.
"""

from __future__ import annotations

from fastapi import APIRouter

from .assets import mount_static
from .assets import router as assets_router
from .state import router as state_router

#: Every router, in include order. Grows one entry per resource group.
ALL_ROUTERS: tuple[APIRouter, ...] = (
    state_router,
    assets_router,
)

__all__ = ["ALL_ROUTERS", "mount_static"]
