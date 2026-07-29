"""Dashboard assets: the page shells, the top-level stylesheets, the mounts.

  GET    /               the Stages dashboard HTML shell (next.html)
  GET    /setup          first-run / manage-models surface (setup.html)
  GET    /dashboard.css  shared design tokens + primitives
  GET    /next.css       Stages layer on top of dashboard.css
  GET    /tokens.css     vendored toolkit token sheet (canon names)
  GET    /tones.css      vendored toolkit tone sheet
  MOUNT  /web/js         dashboard JS modules (StaticFiles)
  MOUNT  /web/components HTML component templates (StaticFiles)

The Stages dashboard ("/next" during its incubation; promoted to "/" once the
classic dashboard was retired). The shell is next.html; it layers next.css on
top of dashboard.css, and loads everything else through the /web/... mounts.

The two mounts are declared in `STATIC_MOUNTS` and attached to the APP by
`mount_static(app)`, not to the router. A router CAN hold a mount, but
`include_router` only carries a `Mount` across from FastAPI 0.139: `Mount` is not
a `Route` subclass, and the older isinstance ladder has no branch for it, so a
router-held mount is DROPPED with no error. Verified on 0.115.14, which
`fastapi>=0.110` permits, where the dashboard then serves its HTML shell and 404s
every JS module and component template. Registering on the app works on every
version in range, so the mounts keep their own attach step and
`STATIC_MOUNTS` is what the route-map test reads.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..setup_state import is_first_run

router = APIRouter()

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


@router.get("/", response_class=HTMLResponse)
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


@router.get("/setup", response_class=HTMLResponse)
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


@router.get("/dashboard.css")
async def dashboard_css():
    return _css_response(DASHBOARD_CSS_PATH, "dashboard.css")


@router.get("/next.css")
async def next_css():
    return _css_response(NEXT_CSS_PATH, "next.css")


@router.get("/tokens.css")
async def tokens_css():
    return _css_response(TOKENS_CSS_PATH, "tokens.css")


@router.get("/tones.css")
async def tones_css():
    return _css_response(TONES_CSS_PATH, "tones.css")


#: (mount path, directory, mount name) for each StaticFiles mount this module
#: owns. Read by `mount_static` and by the route-map test, so a new mount is
#: documented in the docstring above like any route.
STATIC_MOUNTS: tuple[tuple[str, Path, str], ...] = (
    ("/web/js", DASHBOARD_JS_DIR, "web_js"),
    ("/web/components", DASHBOARD_COMPONENTS_DIR, "web_components"),
)


def mount_static(app: FastAPI) -> None:
    """Mount the dashboard JS modules and HTML component templates ON THE APP
    (see the module docstring for why not on the router). StaticFiles handles
    path-traversal protection and content-type detection.

    A missing directory is skipped: a source checkout always has both, but a
    trimmed install should not fail to boot over a missing asset dir. The tests
    that pin the mount rows say so in their failure message, since a missing dir
    and a lost mount otherwise look identical."""
    for path, directory, name in STATIC_MOUNTS:
        if directory.is_dir():
            app.mount(path, StaticFiles(directory=str(directory)), name=name)
