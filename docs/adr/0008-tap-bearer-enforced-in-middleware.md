---
status: accepted
date: 2026-06-24
---

# Tap-bearer auth enforced in the middleware, not a per-route dependency

## Context

Three HTTP control routes under `/api/tap/` (`POST /api/tap/new-session`,
`POST` / `GET /api/tap/sessions/{session}/pipeline`) are authenticated by the
**tap token** (`Authorization: Bearer`), not dashboard Basic auth, so a
browser Bridge holding only the low-privilege tap token can rotate sessions
and drive the end-of-meeting pipeline (see CONTEXT.md: Bridge, Bracketed
meeting). They are the HTTP twin of the `/tap` WebSocket's subprotocol gate.

Before this ADR the invariant *"a route is exempt from Basic auth **iff** it
requires the tap bearer"* was spread across three places kept in sync by
hand:

- `config.AUTH_EXEMPT_ROUTES` — the exact entry `("POST", "/api/tap/new-session")`.
- `config.AUTH_EXEMPT_PREFIXES = ("/api/tap/",)` — for the path-parameter
  pipeline routes that exact `(method, path)` matching can never cover.
- A byte-identical `if config.AUTH_ENABLED and not auth.check_tap_bearer(...):
  return 401` block hand-copied into each of the three handlers.

config.py's own comment named the risk: *"every handler under `/api/tap/`
MUST validate the tap bearer itself … a bearer-less `/api/tap/*` route would
be an open door."* The safety net was reviewer vigilance plus remembering to
add a 401 test per route — the same discretionary discipline the duplication
itself represents. An architecture review (2026-06-24) flagged it; its first
instinct — a FastAPI dependency — turned out to be the weaker fix (below).

## Decision

Enforce the tap bearer in the **Basic-auth middleware**
(`auth.basic_auth_middleware`), keyed on a single `config.TAP_PREFIX =
"/api/tap"`. The middleware dispatches every HTTP request to exactly one of
three schemes — **public** (exact `AUTH_EXEMPT_ROUTES`), **tap-bearer**
(`TAP_PREFIX`), **Basic** (default). The one branch that routes a `/api/tap/`
request past Basic auth also requires the bearer, so the two halves of the
invariant are a single predicate and cannot diverge.

Consequences in code:

- The three per-handler bearer blocks are deleted; handlers carry no auth
  gate of their own.
- `AUTH_EXEMPT_PREFIXES` is retired (it held only `/api/tap/`, now
  `TAP_PREFIX`). `AUTH_EXEMPT_ROUTES` shrinks to the genuinely public
  `/health` + `/healthz`: the redundant **and now dangerous**
  `("POST", "/api/tap/new-session")` entry is removed (exact matches run
  before the prefix branch, so leaving it would route new-session to the
  no-auth path and silently un-gate it), and the stale
  `("POST", "/api/live-transcript")` exemption — that route was removed long
  ago, see `test_old_live_transcript_post_route_is_gone` — is dropped.
- The middleware fetches the recorder once, shared by the tap-bearer and
  Basic branches behind the existing `recorder is None → 503` guard; the tap
  branch drops its own `AUTH_ENABLED` conjunct because the middleware already
  returns early when auth is disabled.
- `auth.check_tap_bearer` (the pure, constant-time predicate) and
  `auth.pick_tap_subprotocol` (the `/tap` WS gate) are unchanged. The WS path
  stays separate — middlewares of this kind don't see WS upgrades.
- A parametrised route-discovery test enumerates every registered route under
  `TAP_PREFIX` and asserts each rejects a missing/wrong bearer, so a future
  tap route is covered the instant it is registered — the structural twin of
  the structural enforcement.

## Considered alternatives

**Per-handler FastAPI dependency** (`_=Depends(require_tap_bearer)` on each
handler). Dedups the 401 response shape but is exactly as forgettable as the
paste — a new `/api/tap/*` handler that omits the dependency is still an open
door, and the Basic-auth exemption remains a separate config entry. It trades
paste-discipline for decorator-discipline; the invariant is not structural.

**Router-level dependency** (`APIRouter(prefix=TAP_PREFIX,
dependencies=[Depends(require_tap_bearer)])`). Makes a gate-less route
impossible by construction and keeps the gate visible on the router, but
leaves **two** constructs — the router dependency and the middleware
Basic-auth exemption — that must both reference the prefix and both stay
present. The middleware already owns the "this is a tap route, skip Basic"
decision, so folding the bearer check into that same branch is one construct
instead of two, and the smaller diff.

## Consequences

- The invariant is structural: exempt-from-Basic and requires-bearer are one
  predicate keyed on one constant; a new `/api/tap/*` route is auto-gated and
  auto-tested.
- `auth.basic_auth_middleware` widens from "Basic only" to **the HTTP auth
  gate**, owning all three schemes' dispatch. CONTEXT.md gains an "HTTP auth
  gate · auth schemes" entry naming them.
- A future architecture review that re-suggests "extract the tap gate into a
  FastAPI dependency" should consult this ADR first — both dependency forms
  were considered and rejected for being non-structural (the per-handler one)
  or redundant with the middleware's existing prefix decision (the router
  one).
