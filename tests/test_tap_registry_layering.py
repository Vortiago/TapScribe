"""RED contract for #405 item 1: the in-flight tap registry moves to a neutral leaf.

`tap_fan_out.py` does `from .session_maintenance import mark_session_in_flight,
release_session_mark` for one refcount dict. The dependency direction is hot path
to operator maintenance, which is backwards, and it is not free: measured against
base, importing `tapscribe.tap_fan_out` in a fresh interpreter pulls 27 tapscribe
modules, and 14 of those are reachable ONLY through that one edge, including the
whole `transcribers` package, `wav_cache`, `strip_meta`, `sessions` and
`hallucinations`. Drop the edge and the `/tap` import graph is 13 modules.

WHAT IS PINNED, AND WHAT IS NOT. No module path is named. Every test below
resolves "the module the mark functions come from" out of `tap_fan_out`'s own
source, so any neutral home passes: a new leaf, or an existing leaf already in
the `/tap` graph. The registry's shape, signatures and refcount semantics are
already pinned by #257's `tests/test_prune_vs_tap_race.py` and are not restated.

THE FAILURE MODE THIS EXISTS TO CATCH is not the import line. It is two
divergent registries: the mark written through `tap_fan_out`'s path landing in a
different dict than the one `prune_empty_sessions` reads. That build passes a
naive layering check, passes a naive mark/release unit test, and silently
reopens the #257 race in production. `test_both_sides_read_one_registry` and
`test_the_leak_detector_still_watches_the_live_registry` are the load-bearing
rungs here; the import-graph tests are the cheap half.

TWO CHECK STYLES, DELIBERATELY. The runtime `sys.modules` check measures the real
weight but cannot see a deferred import inside a function body, which would keep
the backwards dependency while passing. The AST check sees deferred imports but
not weight. Both, or the dodge is open.

Not in scope (per #405): moving the registry onto `Recorder`, and real mutual
exclusion for the threaded-rmtree routes, which is tracked as #408.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tapscribe import session_maintenance

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "tapscribe"

#: Modules that are in the `/tap` import graph ONLY because of the
#: `tap_fan_out -> session_maintenance` edge. Measured against base by importing
#: `tap_fan_out` and then importing its other direct deps alone and taking the
#: difference. A forbidden SET rather than a module count: a count would break on
#: any unrelated future import and would say nothing about the direction.
_FORBIDDEN_IN_TAP_GRAPH = {
    "tapscribe.session_maintenance",
    "tapscribe.wav_cache",
    "tapscribe.wav_predecode",
    "tapscribe.strip_meta",
    "tapscribe.sessions",
    "tapscribe.people",
    "tapscribe.chunking",
    "tapscribe.hallucinations",
    "tapscribe.name_resolution",
    "tapscribe.runtime_probe",
    "tapscribe.transcribers",
    "tapscribe.transcribers.base",
    "tapscribe.transcribers.catalog",
    "tapscribe.transcribers._chunked",
}

#: The mark/release/read trio. Whichever module `tap_fan_out` gets these from is
#: "the registry module" as far as this file is concerned.
_MARK_NAMES = {"mark_session_in_flight", "release_session_mark", "session_has_open_tap"}


def _tapscribe_import_graph(module: str) -> set[str]:
    """The tapscribe modules a fresh interpreter loads to import `module`.

    A subprocess, not this process: pytest has already imported most of the
    package, so an in-process `sys.modules` read would measure the test suite's
    graph instead of the app's.
    """
    code = (
        "import json, sys\n"
        f"import {module}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('tapscribe'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=_REPO, check=True)
    return set(json.loads(out.stdout))


def _imports_of(path: Path) -> set[str]:
    """Absolute tapscribe module names imported by `path`, function-body imports
    included: a deferred `from .x import y` is still a dependency of this module,
    and deferring one is exactly how a build dodges the runtime graph check.

    Relative imports are resolved against the package, so `from .foo import bar`
    inside `tapscribe/` reads as `tapscribe.foo`. `from . import foo` yields
    `tapscribe.foo` too, so both import styles are visible here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("tapscribe"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.startswith("tapscribe"):
                    found.add(node.module)
                continue
            # Relative. `path` is tapscribe/<...>/<mod>.py; level 1 is its own
            # package, level 2 the parent, and so on.
            pkg = ["tapscribe", *path.relative_to(_PKG).parts[:-1]]
            base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
            if node.module:
                found.add(".".join([*base, node.module]))
            else:
                found.update(".".join([*base, a.name]) for a in node.names)
    return found


def _registry_module() -> str:
    """The module `tap_fan_out` imports the mark functions from, read from its
    source so both `from .x import mark_session_in_flight` and `from . import x`
    plus `x.mark_session_in_flight(...)` resolve. Deliberately NOT
    `tap_fan_out.mark_session_in_flight.__module__`: under the second style
    `tap_fan_out` has no such attribute and this file would fail a correct build.
    """
    path = _PKG / "tap_fan_out.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    source = path.read_text(encoding="utf-8")
    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            if _MARK_NAMES & {a.asname or a.name for a in node.names}:
                candidates.add(f"tapscribe.{node.module}")
            continue
        # `from . import helper` / `import tapscribe.helper`, used attribute-style.
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.level and not node.module:
            names = [a.asname or a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.asname or a.name.split(".")[-1] for a in node.names]
        for local in names:
            if any(f"{local}.{fn}" in source for fn in _MARK_NAMES):
                candidates.add(f"tapscribe.{local}")
    assert candidates, (
        "could not find where tap_fan_out imports the in-flight mark functions from; "
        f"expected an import bringing in one of {sorted(_MARK_NAMES)}"
    )
    assert len(candidates) == 1, (
        f"tap_fan_out reaches the mark functions through more than one module: {sorted(candidates)}. "
        "There must be exactly one registry home."
    )
    return candidates.pop()


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is a process-global dict. Clear it both ways so a mark left
    by one of these tests cannot perturb a later one, or any other test file's
    prune or destructive-route assertions."""
    session_maintenance._tap_open_sessions.clear()
    yield
    session_maintenance._tap_open_sessions.clear()


def test_the_tap_hot_path_carries_no_operator_maintenance_weight():
    """The measured harm: 14 modules ride into the `/tap` import graph on one
    refcount import. Base pulls all of them."""
    graph = _tapscribe_import_graph("tapscribe.tap_fan_out")
    leaked = sorted(_FORBIDDEN_IN_TAP_GRAPH & graph)
    assert not leaked, (
        "importing tapscribe.tap_fan_out pulls these in, and every one of them is reachable "
        f"only through the session_maintenance edge: {leaked}"
    )


def test_the_tap_hot_path_does_not_name_the_maintenance_module_even_lazily():
    """Anti-dodge for the test above: moving the import into `_open`'s body drops
    it out of the runtime graph while leaving the dependency direction backwards.
    A deferred import is still an import."""
    assert "tapscribe.session_maintenance" not in _imports_of(_PKG / "tap_fan_out.py"), (
        "tap_fan_out must not import session_maintenance at all, including inside a function body"
    )


def test_the_registry_module_is_a_leaf():
    """Direction pinned BOTH ways. A purity test on `tap_fan_out` alone is
    satisfied by a registry module that imports back into the maintenance or
    recorder layer, which just relocates the cycle instead of breaking it."""
    registry = _registry_module()
    assert registry != "tapscribe.session_maintenance", (
        "the registry must move OUT of session_maintenance; tap_fan_out still imports it from there"
    )
    path = _PKG / Path(*registry.split(".")[1:]).with_suffix(".py")
    assert path.is_file(), f"{registry} does not resolve to a module file at {path}"
    back_edges = sorted(
        _imports_of(path)
        & (_FORBIDDEN_IN_TAP_GRAPH | {"tapscribe.tap_fan_out", "tapscribe.recorder", "tapscribe.tap_relay"})
    )
    assert not back_edges, (
        f"{registry} owns the in-flight registry, so it has to be a leaf. It imports: {back_edges}"
    )


def test_both_sides_read_one_registry():
    """A mark taken through the hot path's own import must be the mark
    `session_maintenance` reads. Two registries that each pass their own unit
    test reopen #257's race in production while every gate stays green."""
    registry = sys.modules.get(_registry_module()) or __import__(
        _registry_module(), fromlist=["mark_session_in_flight"]
    )
    registry.mark_session_in_flight("shared-session")
    try:
        assert session_maintenance.session_has_open_tap("shared-session"), (
            "session_maintenance cannot see a mark taken through the registry module "
            "tap_fan_out imports: there are two registries, not one"
        )
    finally:
        registry.release_session_mark("shared-session")
    assert not session_maintenance.session_has_open_tap("shared-session")


def test_the_leak_detector_still_watches_the_live_registry():
    """#257's `tests/test_prune_vs_tap_race.py` guards against a leaked mark with
    an autouse fixture that reads and clears `session_maintenance._tap_open_sessions`
    directly. That file is the protected contract, so the name must keep resolving
    to the LIVE dict, not to a stale copy left behind at its old home. Rebinding
    it to a second dict leaves the leak detector green and blind.

    The cheap way to satisfy this is to keep a single binding to the same object
    (`from .<registry> import _tap_open_sessions` binds the dict itself, so
    `.clear()` on it is visible everywhere). Pinned through the fixture's two
    actual operations rather than as a bare identity check, because those two
    operations are what has to keep working.
    """
    registry = sys.modules.get(_registry_module()) or __import__(
        _registry_module(), fromlist=["mark_session_in_flight"]
    )
    registry.mark_session_in_flight("watched")

    assert dict(session_maintenance._tap_open_sessions) == {"watched": 1}, (
        "the leak detector reads session_maintenance._tap_open_sessions; it no longer sees "
        f"live marks (got {dict(session_maintenance._tap_open_sessions)!r})"
    )
    session_maintenance._tap_open_sessions.clear()
    assert not session_maintenance.session_has_open_tap("watched"), (
        "clearing session_maintenance._tap_open_sessions did not clear the live registry, "
        "so the leak detector's cleanup no longer resets state between tests"
    )
