"""One way to enumerate the app's registered routes, shared by the tests that
sweep the HTTP surface.

Since #229 the routes live in router modules under `tapscribe/routes/`. From
FastAPI 0.139 `app.routes` no longer enumerates them: an included router stays a
single entry, resolved lazily per request, and
`fastapi.routing.iter_route_contexts` is the flattening walk. BEFORE 0.139
`include_router` copied each route onto the app, so `app.routes` is already flat
and that symbol does not exist. Both are supported here, because a hard import of
it would turn every in-range older install into a COLLECTION error, taking the
tap-bearer sweep down with it rather than reporting a version problem. (Verified:
with the fallback, this file's tests pass under fastapi 0.115.14. The suite as a
whole still needs a newer starlette for a different, pre-existing reason, namely
pyproject's `ignore::starlette.exceptions.StarletteDeprecationWarning` filter, so
the point of the fallback is that route enumeration is not the thing that breaks
first.)

Reading the EFFECTIVE path takes care, and getting it wrong is how a sweep goes
quietly blind. FastAPI populates the route context's `path` only for an
`APIRoute`; a websocket route or a mount gets its prefixed clone in
`starlette_route` and leaves `context.path` empty. Reading `context.path` alone
therefore drops every websocket and mount (empty string, so a `startswith`
filter never matches), and falling back to the route's OWN path reports the
unprefixed one. `_effective_path` prefers the prefixed clone, so the rows are
exact for every route kind at any include depth, prefix or no prefix, and it
RAISES rather than guessing if a future release leaves it nothing to read: these
sweeps are the auth boundary's structural net, so failing closed is the only
acceptable direction.

Callers pass the app in, so importing this module never imports `tapscribe.app`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.routing import Mount, WebSocketRoute

try:  # FastAPI >= 0.139
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - exercised only on older in-range installs
    iter_route_contexts = None

#: FastAPI's own docs endpoints, not part of TapScribe's surface.
FASTAPI_DOCS = frozenset({"openapi", "swagger_ui_html", "swagger_ui_redirect", "redoc_html"})


@dataclass(frozen=True)
class RegisteredRoute:
    """One registered route. `kind` is the comma-joined method list for an HTTP
    route, "WS" for a websocket, "MOUNT" for a mount (StaticFiles, sub-app).
    `route` is the route object as its module declared it; `path` is what the
    app actually serves."""

    kind: str
    path: str
    name: str
    route: Any

    @property
    def methods(self) -> list[str]:
        return self.kind.split(",")


def route_kind(route) -> str:
    if isinstance(route, Mount):
        return "MOUNT"
    if isinstance(route, WebSocketRoute):
        return "WS"
    return ",".join(sorted(route.methods))


def _effective_path(ctx) -> str:
    """The path the app serves for this route context. See the module docstring
    for why the prefixed clone comes first, and why this raises instead of
    falling back to the route's own (unprefixed) path."""
    prefixed = getattr(ctx, "starlette_route", None)
    if prefixed is not None:
        return prefixed.path
    if ctx.path:
        return ctx.path
    raise AssertionError(
        f"cannot read the effective path of {ctx.original_route!r}: FastAPI's route-context "
        "shape changed, and guessing here would make every route sweep silently report "
        "unprefixed paths (see this module's docstring)"
    )


def registered_routes(app, *, include_docs: bool = False) -> list[RegisteredRoute]:
    """Every route the app serves, in registration order. A LIST, not a set, so
    a double registration shows up as a duplicate row."""
    rows = []
    for route, path in _routes_with_paths(app):
        name = route.name
        if not include_docs and name in FASTAPI_DOCS:
            continue
        rows.append(RegisteredRoute(route_kind(route), path, name, route))
    return rows


def _routes_with_paths(app):
    """(route, effective path) for every route, on either side of FastAPI 0.139."""
    if iter_route_contexts is None:  # pre-0.139: app.routes is already flat
        return [(route, route.path) for route in app.routes]
    return [(ctx.original_route, _effective_path(ctx)) for ctx in iter_route_contexts(app.routes)]
