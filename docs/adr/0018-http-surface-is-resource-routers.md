---
status: accepted
date: 2026-07-28
---

# The HTTP surface is resource routers; `app.py` is assembly only

`tapscribe/app.py` had accreted to 2298 lines: 61 routes and 2 static mounts
across eight-plus resource groups, plus the uvicorn access-log filter, the
lifespan, the ETag machinery and the `/api/state` read-model assembly. The
routes themselves were already thin (parse the boundary, call an orchestrator,
let a registered domain-error handler map the failure), so the depth was fine.
What was wrong was locality: answering "where does X get served" meant scanning
the whole file, and the orientation aid at the top actively misled, since its
"big-picture route map" listed 7 of 61 routes, omitted whole groups (people,
summarize, setup, the pipeline poll) and still described `/tap` as the highlight.
For a codebase that leans this hard on doc-as-navigation, a 12%-coverage map on
the largest module is the real cost (#229).

## Decision

Every route lives in a **route module** under `tapscribe/routes/`, grouped by
**domain concern**. `app.py` owns app construction, middleware, the domain-error
registry, the router includes and the mounts, and nothing else.

Four decisions inside that, each of which had a plausible alternative:

**1. Group by domain concern, not URL prefix.** The alternative (one module per
`/api/<noun>` prefix, which is what the issue's suggested fix listed) splits
strip-silence across two modules, because its four routes live under
`/api/sessions/*` and `/api/wav/*`. Those four share
`_parse_strip_knob_overrides`, the one owner of the knob names, their bounds and
the only-forward-explicit contract, and the reason it has one owner is that a
preview must plan with exactly the knobs a commit would use. Prefix grouping
would promote it to a helper two modules import, which is the drift its own
docstring warns about. So `routes/strip.py` owns all four, and the two modules
whose prefixes it borrows carry a pointer line.

**2. Auth twins stay together.** `routes/tap.py` holds both `new-session` verbs
and all three pipeline verbs, tap-bearer and Basic-auth alike. The asymmetries
between each pair are deliberate and only legible side by side: the tap rotate is
idempotent and never prunes, because deleting session folders must stay a
Basic-auth operator action, and both pipeline triggers ignore the request body
entirely so no caller can choose which model gets loaded. `_trigger_pipeline` is
the shared body the twins cannot drift from. Splitting by auth scheme would put
each half in a different file and make that a cross-module import.

**3. Routers are included without a prefix.** Every route keeps its absolute
path, so a path written in a route module reads exactly as the URL it serves. A
prefix would move the auth boundary without touching a handler, since
`config.AUTH_EXEMPT_ROUTES` matches exact `(method, path)` pairs and the
tap-bearer branch matches `config.TAP_PREFIX` (ADR-0008), both against the final
path.

**4. The destructive preflight stays in the HTTP layer.**
`routes/guards.py::refuse_current_or_busy` needs the live Recorder (jobs +
streams), and `session_maintenance` is deliberately recorder-free (its bulk
reclaim takes a `busy_check` callback). Moving the guard down would mean either
dragging the Recorder into that module or inventing a domain error for
session-identity; neither is worth it for a guard whose whole job is to answer an
HTTP request with a 409.

Alongside the split, the `/api/state` assembly becomes the **State view**
(`tapscribe/state_view.py`): FastAPI-free, pure given snapshots, so the
projection, the override counts and the byte bucketing are unit-testable without
a route or a Recorder, matching what the batch orchestrators already do.

## The map is enforced, not encouraged

A route map that can decay is worth nothing, which is what #229 demonstrated.
`tests/test_route_surface.py` pins five properties:

- each route module's docstring route map equals what that module registers,
  both directions, so a route added without a map line fails CI;
- `routes/__init__.py`'s index names every module in the package;
- `app.py` registers no routes itself (AST check for `@app.<verb>`);
- no route module imports another route module (only `deps`, `body`, `errors`,
  `guards`);
- the whole registered surface matches a golden `(kind, path, endpoint)` table,
  which is what made a 2300-line relocation reviewable.

Include order is left free, and a test pins the premise that makes that safe: no
two registered routes are match-ambiguous (no literal-versus-parameter collision
at equal segment depth and method), so ordering cannot change which handler
answers a request. Add an ambiguous pair and that test fails, at which point
include order has to become a deliberate decision rather than the module list's
side effect.

## Consequences

- Where a new route goes is a question with an answer: the module whose domain
  concern it belongs to, plus a line in that module's map. If it fits no module,
  that is the signal to add one (router, map, entry in `ALL_ROUTERS`, line in the
  package index).
- A shared helper has two legal homes, module-private or a support module, and
  the no-cross-import test makes the third option (reach into a sibling)
  unwritable.
- Monkeypatch targets moved with their routes, and deliberately were NOT
  re-exported from `app.py`. A re-export would let
  `monkeypatch.setattr("tapscribe.app.start_pipeline", ...)` succeed while the
  route kept reading its own module global: a patch that looks applied and is
  not. `get_recorder` IS re-exported, because `app.dependency_overrides` keys on
  the callable's identity, so the re-export is the same object.
- `app.routes` no longer enumerates routes: FastAPI keeps an included router as
  one lazily-resolved entry. Anything that walks the surface (the tap-bearer
  sweep in `tests/test_tap_endpoint.py`, the route-surface tests) goes through
  `fastapi.routing.iter_route_contexts`.
