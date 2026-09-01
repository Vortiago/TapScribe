"""The login link and the dashboard session it mints.

  POST /api/login-link  mint a single-use login link (Basic auth, like any /api)
  GET  /login           spend one, set the session cookie, and land on /

A resource group of its own under ADR-0018: it is not the Bridge's tap-bearer
plane, not bridge onboarding, and not asset serving — it is the dashboard's way
IN. The credential itself, and the rule that a cookie is a second form of
ADR-0008's BASIC scheme rather than a fourth scheme, live in `login_links` and
`auth` respectively; this module is only the two doors.

`GET /login` is in `config.AUTH_EXEMPT_ROUTES` because it authenticates by
spending its token. It answers a spent or expired one with a small HTML page and
NEVER with a bare 401: a 401 on a navigation is exactly what pops the browser's
native Basic dialog, which is the prompt this whole feature exists to remove.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config
from ..login_links import LoginLinks

router = APIRouter()


def cookie_name() -> str:
    """The session cookie's name, which carries the port.

    Cookies are scoped by host but NOT by port, so a checkout on 8001 and a
    second Recorder on 8002 would otherwise overwrite each other's session and
    log the operator out of whichever they touched second. Read at call time
    rather than computed at import, because `config.PORT` is stamped by
    `__main__.main()` after this module is imported.
    """
    return f"tapscribe_session_{config.PORT}"


#: Deliberately a string here rather than a file under `web/`: it carries no
#: script and no asset, so it needs neither the static mounts nor the dashboard
#: shell, and it has to render correctly for somebody who is NOT signed in. The
#: app's CSP (`default-src 'self'`, inline styles allowed) covers it as-is.
_SPENT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link used up — TapScribe</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0; display: grid;
         place-items: center; min-height: 100vh; color: #222; background: #fafafa; }
  main { max-width: 30rem; padding: 2rem; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  p { margin: .5rem 0; color: #555; }
</style></head>
<body><main>
  <h1>This link is used up</h1>
  <p>A login link works once, and only for a minute. Open the dashboard from the
     TapScribe tray again to get a fresh one.</p>
  <p>You can also sign in with the password from <code>.auth-password</code>
     &mdash; the tray&rsquo;s <em>Copy password</em> puts it on your clipboard.</p>
</main></body></html>
"""


def _store(request: Request) -> LoginLinks:
    """The per-app store the lifespan built. Absent only in a test app that
    skipped the lifespan, which gets an empty one rather than a 500 — every
    token is then unknown, which is the correct answer for a Recorder that has
    issued none."""
    store = getattr(request.app.state, "login_links", None)
    if store is None:
        store = LoginLinks()
        request.app.state.login_links = store
    return store


@router.post("/api/login-link")
async def api_login_link(request: Request):
    """Mint a single-use login link.

    Basic-gated like every other `/api/*` route, which is the whole access
    control: minting requires the password, so only a caller that could already
    reach the dashboard can make one. The tray is that caller — it reads
    `.auth-password` off disk for "Copy password" already.

    Answers the PATH rather than an absolute URL: the tray knows which host and
    port it supervises, and a Recorder behind a proxy would guess wrong.
    """
    token = _store(request).mint()
    return {"path": f"/login?k={token}"}


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, k: str = ""):
    """Spend a login link: set the session cookie and land on the dashboard.

    The 303 is what gets the token out of the address bar (and so out of history
    and out of the next `Referer`), which is why this is not just a page that
    sets a cookie.
    """
    cookie = _store(request).spend(k)
    if cookie is None:
        # 200, not 401: see the module docstring. The page IS the error.
        return HTMLResponse(_SPENT_PAGE)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        cookie_name(),
        cookie,
        httponly=True,
        # Strict, not Lax: it costs the cookie on a web-page-initiated
        # navigation to the dashboard (a link in a wiki arrives logged out),
        # which is rare for a loopback dashboard whose front door is a tray
        # click, and it buys immunity to cross-site navigation-triggered writes.
        samesite="strict",
        # Only under --tls. Setting it unconditionally would make the cookie
        # unusable over plain http, which is how a Bundle serves by default.
        secure=config.TLS_ENABLED,
        # No max-age: a session cookie. The store is in memory, so a Recorder
        # restart invalidates it anyway, and the browser holding it any longer
        # than the process did would just mean a 401 later.
        path="/",
    )
    return response
