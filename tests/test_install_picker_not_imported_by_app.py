"""The picker lives in the package but is NOT part of the app's import surface.

ADR-0015 moved `install_picker` and `cuda_torch` out of `tools/` and into
`tapscribe/`, because `tools/` isn't shipped in the wheel and a Bundle installs
a wheel. That move is purely about *reachability* — both modules are still
bring-up scripts the app only ever spawns as a subprocess.

Two properties keep that true, and both used to be enforced only by the old
directory boundary. Now that the boundary is gone, they're enforced here:

  1. **No app module imports them.** `setup_install.py` builds an argv and
     spawns; if someone "simplifies" that to a direct import, the app gains a
     module that deliberately duplicates catalog knowledge and is designed to
     run *before* the package's dependencies exist.
  2. **They stay stdlib-only.** Both run before the operator has agreed to
     install anything — `start.sh` invokes the picker against a venv holding
     nothing but pip. A third-party import here is a bootstrap failure that
     only shows up on a fresh machine.

Static (AST) rather than runtime `sys.modules` inspection: the test suite
itself imports the picker, so a runtime check would be measuring pytest's
import graph rather than the app's.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "tapscribe"

#: The bring-up scripts themselves, plus the seam they legitimately share.
_BOOTSTRAP = {"install_picker", "cuda_torch", "preflight", "install_target"}

#: What a bootstrap module may import beyond the standard library. Nothing
#: third-party — `install_target` is the one intra-package exception, and it is
#: itself stdlib-only (its own import list is checked by this same test).
_ALLOWED_NON_STDLIB = {"tapscribe", "tapscribe.install_target", "tapscribe.cuda_torch"}


def _module_files() -> list[Path]:
    return sorted(_PKG.rglob("*.py"))


def _imported_roots(path: Path, *, skip_probes: bool = False) -> set[str]:
    """Module names imported by `path`, including lazy imports inside function
    bodies — a deferred `import x` is still a dependency the bootstrap venv has
    to satisfy.

    `skip_probes` excludes imports inside a `try:` block. Those are *presence
    probes*, not dependencies: the canonical case is `cuda_torch.torch_build`,
    whose whole job is to answer "is torch installed, and is it a CUDA build?"
    by importing torch and treating any failure as "no torch". Counting that as
    a dependency would forbid the one thing the module exists to do.
    """
    # encoding pinned: Windows defaults to cp1252, and this repo's sources
    # are full of em-dashes — reading them without utf-8 raises there and
    # nowhere else (caught by the CI Windows matrix, not a Linux run).
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    probed: set[int] = set()
    if skip_probes:
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                    probed.add(id(child))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in probed:
            continue
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — same package, not a dependency
                continue
            if node.module:
                roots.add(node.module)
    return roots


def test_no_app_module_imports_the_bootstrap_scripts():
    offenders: list[str] = []
    for path in _module_files():
        if path.stem in _BOOTSTRAP:
            continue
        for imported in _imported_roots(path):
            head = imported.split(".")
            if head[:2] == ["tapscribe", "install_picker"] or head[:2] == ["tapscribe", "cuda_torch"]:
                offenders.append(f"{path.relative_to(_PKG.parent)} imports {imported}")
    assert not offenders, (
        "The app must spawn these as a subprocess, never import them "
        f"(see setup_install.picker_install_argv): {offenders}"
    )


@pytest.mark.parametrize("name", sorted(_BOOTSTRAP))
def test_bootstrap_modules_are_stdlib_only(name):
    path = _PKG / f"{name}.py"
    if not path.exists():
        pytest.skip(f"{name} not present yet")
    third_party = {
        imported
        for imported in _imported_roots(path, skip_probes=True)
        if imported.split(".")[0] not in sys.stdlib_module_names and imported not in _ALLOWED_NON_STDLIB
    }
    assert not third_party, (
        f"tapscribe/{name}.py runs before TapScribe's dependencies exist "
        f"(a venv with only pip in it) — it cannot import: {sorted(third_party)}"
    )
