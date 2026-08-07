---
status: accepted
date: 2026-07-28
---

# The HTTP surface is resource routers; `app.py` is assembly only

Every route lives in a **route module** under `tapscribe/routes/`, grouped by
**domain concern**; `app.py` owns app construction, middleware, the
domain-error registry, the router includes and the two asset mounts, and
nothing else. Routes stay thin (parse the boundary, call an orchestrator, let
a registered domain-error handler map the failure) — the split exists for
locality: "where does X get served" is answered by each module's docstring
route map plus the package index in `routes/__init__.py`, and an unenforced
map decays to worthlessness (#229: `app.py`'s hand-kept map covered 7 of 61
routes).

Four decisions inside that, each with a plausible alternative:

**1. Group by domain concern, not URL prefix.** Prefix grouping splits
strip-silence across two modules — its four routes live under
`/api/sessions/*` and `/api/wav/*` — and promotes
`_parse_strip_knob_overrides` (the one owner of the knob names, bounds and
only-forward-explicit contract, single-owned because a preview must plan with
exactly the knobs a commit would use) to a helper two modules import, the
drift its own docstring warns about. So `routes/strip.py` owns all four, and
the modules whose prefixes it borrows carry a pointer line.

**2. Auth twins stay together.** `routes/tap.py` holds both `new-session`
verbs and all three pipeline verbs, tap-bearer and Basic-auth alike: the
asymmetries between each pair are deliberate and only legible side by side —
the tap rotate is idempotent and never prunes (deleting session folders stays
a Basic-auth operator action), and both pipeline triggers ignore the request
body so no caller can choose which model gets loaded. `_trigger_pipeline` is
the shared body the twins cannot drift from; splitting by auth scheme would
make it a cross-module import.

**3. Routers are included without a prefix.** A path written in a route
module reads exactly as the URL it serves. A prefix would move the auth
boundary without touching a handler: `config.AUTH_EXEMPT_ROUTES` matches
exact `(method, path)` pairs and the tap-bearer branch matches
`config.TAP_PREFIX` (ADR-0008), both against the final path.

**4. The destructive preflight stays in the HTTP layer.**
`routes/guards.py::refuse_current_or_busy` needs the live Recorder (jobs +
streams), and `session_maintenance` is deliberately recorder-free (its bulk
reclaim takes a `busy_check` callback). Pushing the guard down means dragging
the Recorder into that module or inventing a domain error for session
identity — not worth it for a guard whose whole job is answering an HTTP
request with a 409.

The `/api/state` assembly is the **State view** (`tapscribe/state_view.py`):
Request-free and Recorder-free, pure given snapshots, so the projection, the
override counts and the byte bucketing are unit-testable without a route or a
Recorder. (Not literally fastapi-free: `jsonable_encoder` gives the payload
its datetime-aware encoding, and that is the wire format.) The route hands it
one frozen `StateInputs` value object, which makes "nothing the worker thread
touches is still being mutated" structural rather than careful, and lets
`live_identities` be derived from the rows it must match instead of passed
beside them (#365).

## The map is enforced, not encouraged

`tests/test_route_surface.py` pins:

- each route module's docstring route map equals what that module registers,
  both directions — a route added without a map line fails CI;
- `routes/__init__.py`'s index names every module in the package;
- every registered route's endpoint is defined in a `routes/` module, which
  says "app.py registers nothing" without depending on HOW a stray route was
  registered (`@app.get`, `add_api_route`, `@app.router.post` all fail it);
- no module in the package imports a router, in any of the four import forms,
  support modules included (there, it would be a cycle);
- no router is included under a prefix;
- the FastAPI routing contract the sweeps read, which fails OPEN if it ever
  changes — hence the audited upper cap on `fastapi` in `pyproject.toml`;
- the whole registered surface matches a golden `(kind, path, endpoint)`
  table.

The dashboard's two StaticFiles mounts are the one thing that cannot ride a
router: `Mount` is not a `Route` subclass, and `include_router` carries it
across only from FastAPI 0.139, silently dropping it before that (the
dashboard then serves its shell and 404s every JS module). So
`routes/assets.py` DECLARES them in `STATIC_MOUNTS` and attaches them to the
app, and that declaration is what the route-map test and the ownership test
both read. The ownership test deliberately does not exempt `starlette.*`
endpoints wholesale — a mount's app is defined in starlette wherever it was
registered, and the exemption would let `app.mount(...)` back into `app.py`
unnoticed.

Include order is left free, and a test pins the premise that makes that safe:
no two registered routes are match-ambiguous (no literal-versus-parameter
collision at equal segment depth and method), so ordering cannot change which
handler answers a request. Add an ambiguous pair and that test fails, at
which point include order has to become a deliberate decision.

## Consequences

- A new route goes in the module whose domain concern it belongs to, plus a
  line in that module's map. If it fits no module, add one (router, map,
  entry in `ALL_ROUTERS`, line in the package index).
- A shared helper has two legal homes, module-private or a support module;
  the no-cross-import test makes the third option (reach into a sibling)
  unwritable.
- Monkeypatch targets live with their routes and are deliberately NOT
  re-exported from `app.py`: a re-export would let
  `monkeypatch.setattr("tapscribe.app.start_pipeline", ...)` succeed while
  the route kept reading its own module global — a patch that looks applied
  and is not. `get_recorder` IS re-exported, because
  `app.dependency_overrides` keys on the callable's identity, so the
  re-export is the same object.
- `app.routes` no longer enumerates routes (FastAPI ≥ 0.139 keeps an
  included router as one lazily-resolved entry). Anything that walks the
  surface goes through `tests/route_inventory.py`, which owns the traversal
  (`fastapi.routing.iter_route_contexts` plus the effective path of a
  non-`APIRoute`) and handles both sides of 0.139.
