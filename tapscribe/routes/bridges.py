"""Bridge onboarding: the two cards under Settings.

  GET  /api/tap-token  reveal the /tap bearer token (on click, never per poll)
  GET  /api/bridges    the downloadable Bridge catalog with release URLs

Both are dashboard reads under Basic auth, not the tap-bearer surface. The token
path is deliberately `/api/tap-token` and not `/api/tap/token`: the auth
middleware routes everything under `config.TAP_PREFIX + "/"` into the TAP-BEARER
scheme, and a bridge holding only the tap token must not be able to read the
token back out. The Bridge-facing surface itself lives in `routes/tap.py`.
"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
)

from .. import bridges_catalog, config
from ..recorder import Recorder
from .deps import get_recorder

router = APIRouter()


@router.get("/api/tap-token")
async def api_tap_token(recorder: Recorder = Depends(get_recorder)):
    """Reveal the /tap bearer token for bridge onboarding (#190) — the
    dashboard's "Connect a bridge" card fetches this only when the operator
    clicks reveal, never on every poll. Gated by dashboard BASIC auth like
    any other /api/* route: the path starts with "/api/tap" but NOT
    "/api/tap/" (config.TAP_PREFIX + "/"), so the auth middleware's
    startswith check does not route it into the TAP-BEARER scheme — see
    `auth.basic_auth_middleware`. Never logged."""
    return {"token": recorder.tap.value}


@router.get("/api/bridges")
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
