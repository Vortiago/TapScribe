"""The FastAPI app: construction, middleware, error registry, router includes.

No routes live here. The HTTP surface is `tapscribe/routes/`, one module per
resource group, and that package's docstring is the index: start there to answer
"where does X get served". ADR-0018 records why the grouping follows the domain
concern rather than the URL prefix, and `tests/test_route_surface.py` fails if a
route is registered here, if a router's route map drifts from what it serves, or
if the registered surface changes without the golden table changing with it.

What this module owns, in the order it happens:

  1. the app object, with the lifespan from `tapscribe/lifespan.py`
  2. middleware: gzip, CORS, Basic auth, security headers (the CSP below)
  3. the domain error to HTTP status handlers (`routes/errors.py`)
  4. every router, then the dashboard's two StaticFiles mounts

Routes receive the running `Recorder` via `Depends(get_recorder)`, which reads
from `request.app.state.recorder`. `__main__.py` constructs the Recorder and
attaches it before uvicorn starts; tests build their own and override
`app.state.recorder` for isolation.
"""

from __future__ import annotations

from fastapi import (
    FastAPI,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import auth
from .lifespan import lifespan
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

app = FastAPI(title="TapScribe recorder", lifespan=lifespan)
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

# The HTTP surface. Include order is presentation order only: no two registered
# routes are match-ambiguous, and no router carries a prefix, both pinned by
# tests/test_route_surface.py.
for _router in ALL_ROUTERS:
    app.include_router(_router)
# The mounts attach here rather than riding a router: `include_router` only
# carries a Mount across from FastAPI 0.139 (see routes/assets.py).
mount_static(app)
