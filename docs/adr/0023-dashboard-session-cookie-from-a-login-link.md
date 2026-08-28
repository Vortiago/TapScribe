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
   the [host role](../../CONTEXT.md#host-role) reads `.auth-password` off disk,
   as the tray already does for Copy password.
2. Returns a single-use token, valid ≤60 s.
3. `GET /login?k=<token>` spends it, sets the cookie, and 302s to `/` so the
   token leaves the address bar. Exact `(method, path)` in
   `AUTH_EXEMPT_ROUTES` — it authenticates by spending the token.
4. Cookie: `HttpOnly`, `SameSite=Strict`, `Secure` only under `--tls`, session
   lifetime (no `Max-Age`). Validated against an in-memory set, so a Recorder
   restart logs the browser out — one click to get back in.

`start.sh` / `start.ps1` print a login URL alongside the generated password, so
a checkout install is as click-through as a Bundle on the same mechanism.

## Why not default a co-located Recorder to `--no-auth`

`app.py` mounts `CORSMiddleware(allow_origins=["*"], allow_methods=["*"],
allow_headers=["*"])`, load-bearing for the SpatialChat bridge's cross-origin
POST from `spatial.chat`. With auth on, a cross-origin request carries no
credential and 401s. Under `--no-auth`, `basic_auth_middleware` returns before
any scheme runs, and permissive CORS then lets **any website the operator
visits** read and write their Recorder — `fetch("http://localhost:8001/api/state")`
returns transcripts and session listings, POSTs delete sessions and trigger
pipelines. No DNS rebinding, no local malware, no second user account: a browser
tab. The threat `--no-auth`'s "trusted single-user localhost" framing misses is
the browser on that same localhost.

A cookie is safe under the same CORS: `allow_credentials` is false, so a
cross-origin `fetch` neither sends it nor exposes the response, and
`SameSite=Strict` covers cross-site navigation.

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
