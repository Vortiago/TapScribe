"""One way to enumerate the app's registered routes, shared by the tests that
sweep the HTTP surface.

Since #229 the routes live in router modules under `tapscribe/routes/`, and
`app.routes` no longer enumerates them: FastAPI keeps an included router as a
single `_IncludedRouter` entry and resolves its routes lazily per request.
`fastapi.routing.iter_route_contexts` is the flattening walk.

Reading the EFFECTIVE path takes care, and getting it wrong is how a sweep goes
quietly blind. FastAPI populates the route context's `path` only for an
`APIRoute`; a websocket route or a mount gets its prefixed clone in
`starlette_route` and leaves `context.path` empty. Reading `context.path` alone
therefore drops every websocket and mount (empty string, so a `startswith`
filter never matches), and falling back to the route's OWN path reports the
unprefixed one. `_effective_path` prefers the prefixed clone, so the rows are
exact for every route kind at any include depth, prefix or no prefix.

Callers pass the app in, so importing this module never imports `tapscribe.app`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.routing import iter_route_contexts
from starlette.routing import Mount, WebSocketRoute

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
    for why the prefixed clone comes first."""
    prefixed = getattr(ctx, "starlette_route", None)
    if prefixed is not None:
        return prefixed.path
    return ctx.path or ctx.original_route.path


def registered_routes(app, *, include_docs: bool = False) -> list[RegisteredRoute]:
    """Every route the app serves, in registration order. A LIST, not a set, so
    a double registration shows up as a duplicate row."""
    rows = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.original_route
        name = ctx.name or route.name
        if not include_docs and name in FASTAPI_DOCS:
            continue
        rows.append(RegisteredRoute(route_kind(route), _effective_path(ctx), name, route))
    return rows
