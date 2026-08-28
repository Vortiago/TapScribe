---
status: proposed (amends ADR-0008)
date: 2026-08-28
---

# The dashboard's Basic scheme accepts a session cookie, minted by a single-use login link

ADR-0008's three schemes are unchanged — PUBLIC, TAP-BEARER, BASIC, one
predicate. BASIC gains a second **credential form** for the same secret: an
`Authorization: Basic` header, or a dashboard **session cookie**. Two forms,
still one scheme; no route is gated differently depending on which the caller
used.

The cookie comes from a [login link](../../CONTEXT.md#login-link):

1. `POST /api/login-link`, authenticated (Basic). Minting therefore requires the
   password, so only a caller that could already reach the dashboard can mint —
   the [host role](../../CONTEXT.md#host-role) reads `.auth-password` off disk
   (the read the Bundle's tray does today for Copy password).
2. Returns a token with a mint-site TTL: **60 s** for a tray click (the browser
   opens immediately), **one hour** for the URL `start.sh` / `start.ps1` print
   at boot, which an operator reaches only after preflight and a model load.
   One knob, two values, because a single TTL is either too short for the
   banner or too long for the click.
3. `GET /login?k=<token>` spends it, sets the cookie, and 302s to `/` so the
   token leaves the address bar. Exact `(method, path)` in
   `AUTH_EXEMPT_ROUTES` — it authenticates by spending the token. A spent or
   expired token renders a short HTML page ("this link is used up — get a fresh
   one from the tray, or sign in with the password"), NEVER a bare 401: a 401
   from a navigation pops the browser's native Basic dialog, which is the
   prompt this whole ADR exists to avoid.
4. Single-use is enforced with a **grace window**: the first spend binds the
   token to the cookie it issued, and a re-spend inside ~10 s re-issues the
   same session rather than failing. A link scanner, a terminal's URL preview
   or a double-click otherwise spends the token before the operator's real
   navigation lands, and the operator sees the dead-link page for a link they
   just made.
5. Cookie: `HttpOnly`, `SameSite=Strict`, `Secure` only under `--tls`, session
   lifetime (no `Max-Age`), and its NAME carries the port — cookies are scoped
   by host but not by port, so a checkout on 8001 and a second Recorder on 8002
   would otherwise clobber each other's session. Validated against an in-memory
   set (constant-time compare, per `auth.py`'s convention), so a Recorder
   restart logs the browser out — one click to get back in.

`basic_auth_middleware` omits `WWW-Authenticate` on a 401 when the request
carried a session cookie. Otherwise a Recorder restart turns the dashboard's
500 ms `/api/state` poll into a native Basic dialog, which is exactly the
prompt the login link removes.

## Why not default a co-located Recorder to `--no-auth`

`app.py` mounts `CORSMiddleware(allow_origins=["*"], allow_methods=["*"],
allow_headers=["*"])`, load-bearing for the SpatialChat bridge's cross-origin
POST from `spatial.chat`. With auth on, a cross-origin request carries no
credential and 401s. Under `--no-auth`, `basic_auth_middleware` returns before
any scheme runs, and permissive CORS then lets **any website the operator
visits** read and write their Recorder — `fetch("http://localhost:8001/api/state")`
returns transcripts and session listings, POSTs delete sessions and trigger
pipelines. No DNS rebinding, no local malware, no second user account. The
threat `--no-auth`'s "trusted single-user localhost" framing misses is the
browser on that same localhost.

Middleware order makes the with-auth case stronger than it looks. Starlette's
`add_middleware` prepends, so execution is security-headers → Basic → CORS →
GZip → routes: a hostile cross-origin request 401s from a middleware CORS never
decorates, so the response carries no `Access-Control-Allow-Origin` and cannot
be read at all. `--no-auth` removes precisely that outer layer.

How far a public web page gets is browser-dependent — Chromium gates
public→private requests (Private Network Access) and `app.py` sets no
`allow_private_network`, so the fetch is interposed there. That narrows the
attacker, it does not close the hole: a page served from ANOTHER localhost port
crosses no boundary at all, and not every browser gates.

A cookie is safe under the same CORS, by two independent mechanisms — and the
order matters, because someone relaxing one while trusting the other ships the
CSRF this sentence claims to prevent. `SameSite=Strict` is what stops the
browser ATTACHING the cookie to a cross-site request, navigation and `fetch`
alike; server CORS config never governs sending. Separately,
`allow_credentials` is false, so even a credentialed response could not be READ
cross-origin. Being host-scoped, the cookie also covers DNS rebinding.

SameSite scoping ignores ports, so a hostile page on another localhost port can
still fire credentialed simple-POSTs (writes execute; the response stays
unreadable). Cached Basic credentials have the same exposure today, so the
cookie is no worse — but state-changing routes get an `Origin` check, which is
the cheap answer to both.

`SameSite=Strict` over `Lax` is deliberate: it costs a cookie on
web-page-initiated navigation to the dashboard (a link in a wiki or a chat app
arrives logged-out), which is rare for a loopback dashboard whose front door is
a tray click, and it buys immunity to cross-site navigation-triggered writes.

`--no-auth` is untouched as a dev flag on `python -m tapscribe`; no Bundle
passes it. Loopback-only binding needed no change — `--host` already defaults to
`localhost` and a Bundle passes no override.

## Considered options

**An HMAC-signed cookie over a persisted secret.** Survives a Recorder restart,
but adds a third secret at rest next to `.auth-password` and `.tap-token`. The
restart-logs-you-out cost is one tray click.

**`GET /?k=…` instead of a dedicated route.** Rejected: `AUTH_EXEMPT_ROUTES` is
exact `(method, path)` by design, and a query-param carve-out on `/` muddies the
one predicate ADR-0008 exists to protect.

**Credentials in the launched URL (`http://admin:pw@localhost:8001/`).**
Rejected: deprecated, inconsistent across browsers, and it puts the password in
history.
