"""No module is imported with both `import X` and `from X import ...`.

CodeQL's `security-and-quality` suite runs `py/import-and-import-from` on every
PR, so a file that reaches for a module both ways fails review after the push
rather than before it. Ruff has no equivalent rule — `PLR0402` objects to the
`import a.b as b` SPELLING, which is a different thing: it fires on the five
files that deliberately bind a module object for `monkeypatch.setattr` (the
`strip_meta` / `wav_cache` / `sessions` convention) and stays silent on a file
that mixes the two forms without an alias. This scan pins the rule CodeQL
actually enforces, at the same instant as the rest of `pytest tests`.

Prefer ONE form per module. `import tapscribe.x as x` is right when a test
patches module attributes; `from tapscribe.x import verb` is right otherwise.
Needing both in one file means the module-object binding can serve the whole
file — reference through it, or use `monkeypatch.setattr("tapscribe.x.verb", …)`
and keep only the `from` import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_TREES = ("tapscribe", "tests", "tools", "benchmarks")


def _source_files() -> list[Path]:
    return sorted(p for tree in _TREES for p in (_ROOT / tree).rglob("*.py"))


def _package_of(path: Path) -> list[str]:
    """The dotted package `path` lives in, as parts — the base a relative
    `from . import x` resolves against."""
    return list(path.relative_to(_ROOT).parts[:-1])


def _dual_imports(path: Path) -> set[str]:
    """Modules `path` imports BOTH ways. Walks the whole tree, so a
    function-local `import types` beside a module-level `from types import …`
    counts — that is the shape CodeQL flags and the easiest one to miss."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plain: set[str] = set()
    froms: set[str] = set()
    package = _package_of(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
            parts = [*base, node.module] if node.level and node.module else [node.module or ""]
            module = ".".join(p for p in (parts if node.level else [node.module or ""]) if p)
            if module:
                froms.add(module)
    return plain & froms


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_module_is_imported_both_ways(path: Path) -> None:
    dual = _dual_imports(path)
    assert not dual, (
        f"{path.relative_to(_ROOT)} imports {sorted(dual)} with both `import` and `from … import`. "
        "Pick one form — see this module's docstring."
    )
