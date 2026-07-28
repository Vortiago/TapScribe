"""Structural contract for the HTTP surface (#229).

`app.py` used to register every route itself: 61 routes across 8+ resource
groups in one 2298-line module, with a top-of-file "route map" docstring that
listed 7 of them. The split moves each resource group into a router module
under `tapscribe/routes/`; this file is what keeps the result honest.

Three properties, none of which any behavioural route test can see:

  1. **The route table is frozen.** `_GOLDEN` below is the (kind, path,
     endpoint-name) table captured before the split. A relocation that drops a
     route, registers one twice, renames a handler, or fat-fingers a path
     (which would silently re-classify its AUTH scheme: `AUTH_EXEMPT_ROUTES`
     matches exact (method, path) pairs and the tap-bearer branch matches the
     `TAP_PREFIX` prefix) fails here rather than in production.

  2. **Every route is documented where it lives.** Each router module's
     docstring carries a route map, and the map must match that module's
     registered routes exactly, both directions. This is the enforcement the
     issue asked for: a new route with no map line fails CI, so the map can't
     decay to 12% coverage again.

  3. **`app.py` registers nothing itself** and no router imports another
     router (shared helpers live in `routes/{deps,body,errors,guards}.py`).

Route ORDER is deliberately not pinned: no two registered routes are
match-ambiguous (no literal-vs-parameter collision at equal segment depth and
method), so `include_router` order can't change which handler answers a
request. `test_no_two_routes_are_match_ambiguous` pins that premise.
"""

from __future__ import annotations

import ast
import importlib
import itertools
import pkgutil
import re
from pathlib import Path

import pytest
from fastapi.routing import _IncludedRouter, iter_route_contexts
from starlette.routing import Mount, WebSocketRoute

from tapscribe import app as tapscribe_app
from tapscribe import routes as routes_pkg
from tapscribe.app import app

#: Decorators that register a route. `middleware` is deliberately absent: the
#: security-header middleware IS app.py's business.
_ROUTE_DECORATORS = {
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "head",
    "options",
    "websocket",
    "api_route",
    "route",
}

# FastAPI's own docs endpoints, not part of TapScribe's surface.
_FASTAPI_DOCS = {"openapi", "swagger_ui_html", "swagger_ui_redirect", "redoc_html"}

#: Every route TapScribe registers, as (kind, path, endpoint name). `kind` is
#: the sorted method list for an HTTP route, "WS" for a websocket, "MOUNT" for
#: a StaticFiles mount. Captured at 060dab8, before the #229 split.
_GOLDEN = frozenset(
    {
        ("GET", "/", "dashboard"),
        ("GET", "/api/bridges", "api_bridges"),
        ("POST", "/api/client-errors", "client_errors"),
        ("PUT", "/api/config/{key}", "api_config_put"),
        ("GET", "/api/languages", "api_languages"),
        ("DELETE", "/api/live-transcript", "api_live_transcript_clear"),
        ("GET", "/api/live/log", "api_live_log"),
        ("POST", "/api/live/start", "api_live_start"),
        ("POST", "/api/live/stop", "api_live_stop"),
        ("GET", "/api/models", "api_models"),
        ("DELETE", "/api/models/cache", "api_models_cache_clear"),
        ("POST", "/api/new-session", "api_new_session"),
        ("GET", "/api/people", "api_people_get"),
        ("POST", "/api/people/merge", "api_people_merge"),
        ("PUT", "/api/people/{person_id}", "api_people_rename"),
        ("POST", "/api/people/{person_id}/detach", "api_people_detach"),
        ("POST", "/api/recording/toggle", "api_recording_toggle"),
        ("GET", "/api/search", "api_search"),
        ("GET", "/api/session-meta/{session}", "api_session_meta_get"),
        ("PUT", "/api/session-meta/{session}", "api_session_meta_put"),
        ("POST", "/api/sessions/bulk-reclaim-audio", "api_bulk_reclaim_audio"),
        ("POST", "/api/sessions/prune-empty", "api_sessions_prune_empty"),
        ("DELETE", "/api/sessions/{session}", "api_session_delete"),
        ("DELETE", "/api/sessions/{session}/audio", "api_session_audio_delete"),
        ("GET", "/api/sessions/{session}/files", "api_session_files"),
        ("POST", "/api/sessions/{session}/pipeline", "api_dashboard_pipeline_trigger"),
        ("POST", "/api/sessions/{session}/strip-silence", "api_session_strip_silence"),
        ("DELETE", "/api/sessions/{session}/stripped", "api_session_stripped_delete"),
        ("POST", "/api/sessions/{session}/summarize", "api_session_summarize"),
        ("GET", "/api/sessions/{session}/summary", "api_session_summary"),
        ("GET", "/api/sessions/{session}/transcript", "api_session_transcript"),
        ("POST", "/api/sessions/{target}/absorb", "api_session_absorb"),
        ("POST", "/api/setup/install", "api_setup_install"),
        ("GET", "/api/setup/state", "api_setup_state"),
        ("GET", "/api/state", "api_state"),
        ("GET", "/api/summarize/config", "api_summarize_config_get"),
        ("PUT", "/api/summarize/config", "api_summarize_config_put"),
        ("GET", "/api/summarize/models", "api_summarize_models"),
        ("PUT", "/api/tap-settings", "api_tap_settings_put"),
        ("GET", "/api/tap-token", "api_tap_token"),
        ("POST", "/api/tap/new-session", "api_tap_new_session"),
        ("GET", "/api/tap/sessions/{session}/pipeline", "api_tap_pipeline_poll"),
        ("POST", "/api/tap/sessions/{session}/pipeline", "api_tap_pipeline_trigger"),
        ("POST", "/api/transcribe", "api_transcribe"),
        ("POST", "/api/transcribe-session", "api_transcribe_session"),
        ("DELETE", "/api/wav/{session}/{name}", "api_wav_delete"),
        ("GET", "/api/wav/{session}/{name}", "get_wav"),
        ("GET", "/api/wav/{session}/{name}/peaks", "api_wav_peaks"),
        ("PUT", "/api/wav/{session}/{name}/primary", "api_set_primary"),
        ("GET", "/api/wav/{session}/{name}/strip-meta", "api_wav_strip_meta"),
        ("GET", "/api/wav/{session}/{name}/strip-preview", "api_wav_strip_preview"),
        ("GET", "/api/wav/{session}/{name}/transcript", "api_wav_transcript"),
        ("GET", "/dashboard.css", "dashboard_css"),
        ("GET", "/health", "health"),
        ("GET", "/healthz", "healthz"),
        ("GET", "/next.css", "next_css"),
        ("GET", "/sessions", "list_sessions_simple"),
        ("GET", "/setup", "setup_page"),
        ("WS", "/tap", "tap"),
        ("GET", "/tokens.css", "tokens_css"),
        ("GET", "/tones.css", "tones_css"),
        ("MOUNT", "/web/components", "web_components"),
        ("MOUNT", "/web/js", "web_js"),
    }
)


def _kind(route) -> str:
    if isinstance(route, Mount):
        return "MOUNT"
    if isinstance(route, WebSocketRoute):
        return "WS"
    return ",".join(sorted(route.methods))


def _route_rows() -> list[tuple[str, str, str]]:
    """Every TapScribe-owned route as (kind, path, endpoint name). A LIST, not
    a set, so a double registration shows up as a duplicate row.

    Goes through `iter_route_contexts` rather than `app.routes` directly:
    FastAPI keeps an included router as ONE `_IncludedRouter` entry and resolves
    its routes lazily per request, so walking `app.routes` after the #229 split
    would see routers, not routes.

    `ctx.path or route.path`: FastAPI leaves the context path empty for an
    included WEBSOCKET route. Falling back to the route's own path is exact
    because every router is included WITHOUT a prefix, which
    `test_routers_are_included_without_a_prefix` pins.
    """
    rows = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.original_route
        name = ctx.name or route.name
        if name in _FASTAPI_DOCS:
            continue
        rows.append((_kind(route), ctx.path or route.path, name))
    return rows


def test_app_module_registers_no_routes_itself():
    """`app.py` is assembly: app object, middleware, error registry, includes.

    Enforced structurally rather than by convention, because a single
    `@app.get(...)` in app.py is how the 2298-line module grew in the first
    place. The route it registers would be invisible to the map test (which
    walks the `routes` package), which is exactly the drift the maps exist to
    prevent.
    """
    source = (Path(tapscribe_app.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            call = dec.func if isinstance(dec, ast.Call) else dec
            if not isinstance(call, ast.Attribute) or not isinstance(call.value, ast.Name):
                continue
            if call.value.id == "app" and call.attr in _ROUTE_DECORATORS:
                offenders.append(f"@app.{call.attr} on {node.name}")
    assert offenders == [], (
        f"app.py registers routes directly (move them into tapscribe/routes/): {offenders}"
    )


def test_no_router_imports_another_router():
    """A router imports the shared seams, never a sibling router.

    That is what makes each module readable on its own, and it is the property
    that keeps the grouping decision honest: if two resource groups need the
    same helper, the answer is either to group them together (what
    `routes/strip.py` did) or to put the helper in a support module, never to
    reach across.
    """
    package_dir = Path(routes_pkg.__path__[0])
    violations = []
    for path in sorted(package_dir.glob("*.py")):
        module_name = path.stem
        if module_name in _SUPPORT_MODULES or module_name == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                target = node.module.split(".")[0]
                if target not in _SUPPORT_MODULES:
                    violations.append(f"{module_name} imports {target}")
    assert violations == [], f"router-to-router imports: {violations}"


def test_routes_package_index_names_every_module():
    """`routes/__init__.py`'s docstring is the index a reader lands on, so every
    module in the package has to appear in it. An unlisted module is a resource
    group nobody finds without grepping, which is the state #229 was filed
    about."""
    doc = routes_pkg.__doc__ or ""
    missing = [
        info.name
        for info in pkgutil.iter_modules(routes_pkg.__path__)
        if not re.search(rf"^  {re.escape(info.name)} ", doc, re.MULTILINE)
    ]
    assert missing == [], f"modules missing from the routes/ index docstring: {missing}"


def test_routers_are_included_without_a_prefix():
    """Every route keeps its absolute path, so a path in a router module reads
    exactly as the URL it serves.

    A prefix would move the auth boundary without touching any handler:
    `AUTH_EXEMPT_ROUTES` matches exact (method, path) pairs and the tap-bearer
    branch matches `TAP_PREFIX`, both against the FINAL path. It would also make
    a module's route map a half-truth.
    """
    prefixes = [
        (r.include_context.prefix, r.original_router) for r in app.routes if isinstance(r, _IncludedRouter)
    ]
    assert prefixes, "no router is included: the split regressed"
    assert [p for p, _ in prefixes if p] == [], f"router included with a prefix: {prefixes}"


#: Support modules of the `routes` package: shared seams, no routes of their own.
_SUPPORT_MODULES = {"body", "deps", "errors", "guards"}

#: A route-map line: exactly two spaces of indent, a method token, the path.
#: Continuation notes wrap deeper than that, so prose can never be mistaken for
#: a map entry.
_MAP_LINE = re.compile(r"^ {2}(GET|POST|PUT|DELETE|PATCH|WS|MOUNT) +(/\S*)")


def _router_modules():
    """Every router module in `tapscribe.routes` (support modules excluded)."""
    mods = []
    for info in pkgutil.iter_modules(routes_pkg.__path__):
        if info.name in _SUPPORT_MODULES:
            continue
        mods.append(importlib.import_module(f"{routes_pkg.__name__}.{info.name}"))
    return mods


def _documented(module) -> set[tuple[str, str]]:
    doc = module.__doc__ or ""
    return {(m.group(1), m.group(2)) for m in (_MAP_LINE.match(line) for line in doc.split("\n")) if m}


def _registered(module) -> set[tuple[str, str]]:
    """What the module actually serves: its router's routes, plus any
    StaticFiles mounts it declares (an APIRouter cannot hold a mount, so
    `STATIC_MOUNTS` is how a module owns one declaratively)."""
    rows = {(_kind(r), r.path) for r in module.router.routes}
    rows |= {("MOUNT", path) for path, _dir, _name in getattr(module, "STATIC_MOUNTS", ())}
    return rows


@pytest.mark.parametrize("module", _router_modules(), ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_route_is_documented_in_its_router(module):
    """A router module's docstring route map matches what it registers, exactly.

    This is the fix for the issue's sharpest complaint: app.py's map listed 7 of
    61 routes and had drifted into describing routes that had moved on. A map is
    only load-bearing navigation if it cannot decay, so a route with no map line
    (and a map line naming no route) fails here.

    Format: two spaces, the method, the path, then a note. Wrap continuation
    lines deeper than two spaces.
    """
    documented, registered = _documented(module), _registered(module)
    assert registered - documented == set(), (
        f"undocumented routes in {module.__name__}: {sorted(registered - documented)}"
    )
    assert documented - registered == set(), (
        f"documented but not registered in {module.__name__}: {sorted(documented - registered)}"
    )


def test_route_table_is_unchanged():
    """The registered surface equals the pre-split table, exactly.

    Both directions matter: a missing row means a resource group was dropped
    on the way into its router module; an extra row means a route was added
    without updating this table (deliberate additions update `_GOLDEN` in the
    same commit, which is the review prompt this test exists to force).
    """
    rows = _route_rows()
    actual = frozenset(rows)
    assert len(rows) == len(actual), f"a route is registered twice: {sorted(rows)}"
    assert actual - _GOLDEN == frozenset(), f"routes not in the golden table: {sorted(actual - _GOLDEN)}"
    assert _GOLDEN - actual == frozenset(), f"routes missing from the app: {sorted(_GOLDEN - actual)}"


def test_no_two_routes_are_match_ambiguous():
    """No concrete request path can match two registered routes.

    This is the premise that lets the split reorder registrations freely: for
    every pair sharing a method, one must not be a parameterised generalisation
    of the other at equal segment depth. Add an ambiguous pair (say a literal
    `/api/sessions/latest` GET beside `/api/sessions/{session}` GET) and route
    ORDER becomes load-bearing, at which point `include_router` order has to be
    pinned deliberately rather than left to the module list.
    """

    def segments(path: str) -> list[str]:
        return path.strip("/").split("/")

    def generalises(pattern: str, concrete: str) -> bool:
        a, b = segments(pattern), segments(concrete)
        if len(a) != len(b):
            return False
        return all(x.startswith("{") or x == y for x, y in zip(a, b, strict=True))

    http = [(k, p) for k, p, _ in _route_rows() if k not in ("WS", "MOUNT")]
    ambiguous = [
        (k1, p1, p2)
        for (k1, p1), (k2, p2) in itertools.permutations(http, 2)
        if p1 != p2 and set(k1.split(",")) & set(k2.split(",")) and generalises(p1, p2)
    ]
    assert ambiguous == [], f"ambiguous route pairs (order becomes load-bearing): {ambiguous}"
