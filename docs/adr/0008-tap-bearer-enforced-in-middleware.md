---
status: accepted
date: 2026-06-24
---

# Tap-bearer auth enforced in the middleware, not a per-route dependency

The HTTP control routes under `/api/tap/` (`POST /api/tap/new-session`,
`POST` / `GET /api/tap/sessions/{session}/pipeline`) are authenticated by the
**tap token** (`Authorization: Bearer`), not dashboard Basic auth, so a
browser Bridge holding only the low-privilege tap token can rotate sessions
and drive the end-of-meeting pipeline. They are the HTTP twin of the `/tap`
WebSocket's subprotocol gate.

The bearer is enforced in the **Basic-auth middleware**
(`auth.basic_auth_middleware`), keyed on the single `config.TAP_PREFIX =
"/api/tap"`. The middleware dispatches every HTTP request to exactly one of
three schemes — **public** (exact `AUTH_EXEMPT_ROUTES`: just `/health` +
`/healthz`), **tap-bearer** (`TAP_PREFIX`), **Basic** (default) — see
CONTEXT.md "HTTP auth gate · auth schemes". The one branch that routes a
`/api/tap/` request past Basic auth also requires the bearer, so
"exempt from Basic" and "requires the bearer" are a single predicate and
cannot diverge: handlers carry no auth gate of their own, and a bearer-less
`/api/tap/*` route is impossible by construction. (Previously the invariant
was spread across an exact exempt-route entry, an exempt-prefix tuple, and a
401 block pasted into each handler, held together by reviewer vigilance.)

Standing constraints:

- Exact `AUTH_EXEMPT_ROUTES` matches run **before** the prefix branch, so an
  exempt entry under `TAP_PREFIX` would silently un-gate that route — never
  add one.
- The `/tap` WS is separate: middlewares of this kind don't see WS upgrades,
  so it is gated by `auth.pick_tap_subprotocol` from the WS route handler.
  `auth.check_tap_bearer` stays the pure, constant-time bearer predicate.
- A parametrised route-discovery sweep (`tests/test_tap_endpoint.py`)
  enumerates every registered route under `TAP_PREFIX` and asserts each
  rejects a missing/wrong bearer — a future tap route is auto-gated and
  auto-tested the instant it is registered.

Rejected (a review re-suggesting "extract the gate into a FastAPI
dependency" should start here):

- **Per-handler dependency** (`Depends(require_tap_bearer)`): exactly as
  forgettable as the paste — a handler that omits it is still an open door,
  and the Basic exemption stays a separate config entry; not structural.
- **Router-level dependency** (`APIRouter(dependencies=[...])`): makes a
  gate-less route impossible, but leaves **two** constructs (router
  dependency + middleware Basic exemption) that must both reference the
  prefix. The middleware already owns the "tap route, skip Basic" decision;
  folding the bearer into that branch is one construct and the smaller diff.
