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

  3. **The split's own rules hold.** Every route's endpoint is defined in a
     `routes/` module (so `app.py` registers nothing, whatever mechanism a stray
     route might use), no module in the package imports a router (shared helpers
     live in `routes/{deps,body,errors,guards}.py`, and for a support module a
     router import would be a cycle), and no router is included under a prefix,
     so a path in a module's map is the URL it serves.

One more, one level down: the FastAPI routing contract these sweeps read
(`iter_route_contexts`, and the effective path of a non-`APIRoute`) is pinned
directly, because a change there makes every sweep fail OPEN. Hence also the
`fastapi<0.140` cap in pyproject.toml.

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
from fastapi import APIRouter, FastAPI
from route_inventory import registered_routes, route_kind  # type: ignore[import-not-found]

from tapscribe import routes as routes_pkg
from tapscribe.app import app

#: Support modules of the `routes` package: shared seams, no routes of their own.
_SUPPORT_MODULES = {"body", "deps", "errors", "guards"}

#: Every kind a map line may name. ONE source, so the vocabulary a contributor
#: may write can't drift from what `route_kind` emits: a HEAD route or a
#: multi-method `api_route(methods=["GET", "POST"])` must be WRITABLE as a map
#: line ("GET,POST", comma-joined and sorted exactly as `route_kind` joins it),
#: or the map assertion would be unsatisfiable no matter what gets documented.
_KINDS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "WS", "MOUNT")

#: A route-map line: exactly two spaces of indent, the kind, the path.
#: Continuation notes wrap deeper than that, so prose can never be mistaken for
#: a map entry.
_MAP_LINE = re.compile(rf"^ {{2}}((?:{'|'.join(_KINDS)})(?:,(?:{'|'.join(_KINDS)}))*) +(/\S*)", re.MULTILINE)


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
        ("PUT", "/api/tap-mode", "api_tap_mode_put"),
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


def _route_rows() -> list[tuple[str, str, str]]:
    """Every TapScribe-owned route as (kind, path, endpoint name).

    `route_inventory.registered_routes` owns the traversal (and the reason it
    can't just walk `app.routes` any more); this file compares its output
    against the golden table.
    """
    return [(r.kind, r.path, r.name) for r in registered_routes(app)]


def test_every_route_is_served_from_the_routes_package():
    """`app.py` is assembly: app object, middleware, error registry, includes.

    Asserted on the route TABLE rather than on app.py's syntax, because the
    mechanism is not the point: `@app.get(...)`, `app.add_api_route(...)` and
    `@app.router.post(...)` all register a route that no router module owns, and
    such a route is invisible to the map test (which walks the `routes` package),
    which is the drift the maps exist to prevent. Every endpoint's defining
    module answers the question directly.
    """
    declared_mounts = {(path, name) for _m, path, name in _declared_mounts()}
    strays = []
    for row in registered_routes(app):
        endpoint = getattr(row.route, "endpoint", None)
        if endpoint is None:
            # A mount has no endpoint function, and its app (StaticFiles) is
            # defined in starlette wherever it was registered, so provenance
            # can't come from the object. A route module DECLARING it is what
            # makes it owned: exempting `starlette.*` wholesale would let
            # `app.mount(...)` back into app.py, which is the one route kind
            # app.py used to own and the one with no map line to prompt review.
            if (row.path, row.name) not in declared_mounts:
                strays.append(f"{row.kind} {row.path} declared by no route module")
            continue
        module = getattr(endpoint, "__module__", "")
        if not module.startswith(f"{routes_pkg.__name__}."):
            strays.append(f"{row.kind} {row.path} from {module}")
    assert strays == [], (
        f"routes not owned by a router module (move them into {routes_pkg.__name__}/): {strays}"
    )


def _sibling_imports(path: Path) -> set[str]:
    """Names inside `tapscribe.routes` that this file imports, whatever form the
    import takes. All four reach a sibling and all four have to be caught, or the
    rule is enforced only for the spelling someone happened to think of:

        from .strip import x        ImportFrom(module="strip", level=1)
        from . import strip        ImportFrom(module=None, level=1), name in names
        from tapscribe.routes.strip import x   ImportFrom(level=0)
        import tapscribe.routes.strip          Import
    """
    pkg = routes_pkg.__name__
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                found.add(node.module.split(".")[0] if node.module else "")
                if not node.module:
                    found |= {alias.name for alias in node.names}
            elif node.level == 0 and node.module and node.module.startswith(f"{pkg}."):
                found.add(node.module[len(pkg) + 1 :].split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{pkg}."):
                    found.add(alias.name[len(pkg) + 1 :].split(".")[0])
    return found - {""}


def test_no_module_in_the_package_imports_a_router():
    """A module in `routes/` imports the shared seams, never a router.

    That is what makes each module readable on its own, and it is the property
    that keeps the grouping decision honest: if two resource groups need the
    same helper, the answer is either to group them together (what
    `routes/strip.py` did) or to put the helper in a support module, never to
    reach across. The rule covers the SUPPORT modules as well, where importing a
    router would be an import cycle rather than merely untidy.
    """
    package_dir = Path(routes_pkg.__path__[0])
    violations = []
    for path in sorted(package_dir.glob("*.py")):
        if path.stem == "__init__":  # the index: importing every router is its job
            continue
        for target in sorted(_sibling_imports(path)):
            if target not in _SUPPORT_MODULES:
                violations.append(f"{path.stem} imports {target}")
    assert violations == [], f"imports of a router from inside the package: {violations}"


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
    served = {(row.kind, row.path) for row in registered_routes(app)}
    declared = {row for module in _router_modules() for row in _registered(module)}
    assert declared, "no router declares a route: the split regressed"
    moved = sorted(declared - served)
    assert moved == [], f"a router is included under a prefix, so these paths are not what it says: {moved}"


def _mount_dirs() -> list[Path]:
    """Every directory a declared mount serves from."""
    return [
        directory
        for module in _router_modules()
        for _path, directory, _name in getattr(module, "STATIC_MOUNTS", ())
    ]


def _router_modules():
    """Every router module in `tapscribe.routes` (support modules excluded)."""
    mods = []
    for info in pkgutil.iter_modules(routes_pkg.__path__):
        if info.name in _SUPPORT_MODULES:
            continue
        mods.append(importlib.import_module(f"{routes_pkg.__name__}.{info.name}"))
    return mods


def _documented(module) -> set[tuple[str, str]]:
    return {m.groups() for m in _MAP_LINE.finditer(module.__doc__ or "")}


def _declared_mounts() -> list[tuple[object, str, str]]:
    """(module, path, name) for every StaticFiles mount a route module declares.

    A mount cannot ride the router (`include_router` only carries a `Mount`
    across from FastAPI 0.139, and the floor is lower), so a module owns one by
    declaring it in `STATIC_MOUNTS` and attaching it to the app. That declaration
    is what both the route-map test and the owner test read."""
    return [
        (module, path, name)
        for module in _router_modules()
        for path, _dir, name in getattr(module, "STATIC_MOUNTS", ())
    ]


def _registered(module) -> set[tuple[str, str]]:
    """What the module serves: its router's routes plus the mounts it declares."""
    rows = {(route_kind(r), r.path) for r in module.router.routes}
    rows |= {("MOUNT", path) for _m, path, _name in _declared_mounts() if _m is module}
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


def test_iter_route_contexts_reports_effective_paths():
    """The FastAPI contract `route_inventory` is built on, pinned directly.

    Two facts, neither promised by FastAPI: `iter_route_contexts` flattens an
    included router, and the effective (prefix-applied) path of a NON-`APIRoute`
    lives on `starlette_route` while the context's own `path` is empty. If either
    changes, every sweep over the surface fails OPEN, quietly finding fewer
    routes than the app serves, so it is worth a test of its own next to the
    `fastapi<0.140` cap in pyproject.toml.
    """
    inner = APIRouter()

    @inner.websocket("/ws")
    async def _ws(sock):  # pragma: no cover - never connected to
        await sock.accept()

    outer = APIRouter()
    outer.include_router(inner, prefix="/inner")
    probe = FastAPI()
    probe.include_router(outer, prefix="/outer")

    rows = {(r.kind, r.path) for r in registered_routes(probe)}
    assert ("WS", "/outer/inner/ws") in rows, (
        f"effective-path reading is broken for a nested websocket route: {sorted(rows)}"
    )


def test_route_table_is_unchanged():
    """The registered surface equals the pre-split table, exactly.

    Both directions matter: a missing row means a resource group was dropped
    on the way into its router module; an extra row means a route was added
    without updating this table (deliberate additions update `_GOLDEN` in the
    same commit, which is the review prompt this test exists to force).
    """
    missing_dirs = [str(d) for _m, _p, _n in _declared_mounts() for d in _mount_dirs() if not d.is_dir()]
    assert missing_dirs == [], (
        "asset directories are missing, so the mount rows below cannot register: "
        f"{sorted(set(missing_dirs))}. This is an environment problem, not a route regression."
    )
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

    rows = [(k, p) for k, p, _ in _route_rows()]
    http = [(k, p) for k, p in rows if k not in ("WS", "MOUNT")]
    ambiguous = [
        (k1, p1, p2)
        for (k1, p1), (k2, p2) in itertools.permutations(http, 2)
        if p1 != p2 and set(k1.split(",")) & set(k2.split(",")) and generalises(p1, p2)
    ]
    # A Mount matches by PREFIX at any depth, which `generalises` (equal segment
    # count) cannot model, so mounts get their own comparison instead of being
    # filtered out: a route under a mount's prefix is answered by whichever was
    # registered first, i.e. exactly the order dependence this test denies.
    shadowed = [
        (f"MOUNT {mount}", f"{kind} {path}")
        for _k, mount in rows
        if _k == "MOUNT"
        for kind, path in rows
        if path != mount and path.startswith(mount.rstrip("/") + "/")
    ]
    assert ambiguous == [], f"ambiguous route pairs (order becomes load-bearing): {ambiguous}"
    assert shadowed == [], f"route under a mount's prefix (order decides which answers): {shadowed}"
