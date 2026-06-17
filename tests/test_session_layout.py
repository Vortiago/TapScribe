"""Pins the on-disk session-layout naming contract to ONE owner.

The per-session bookkeeping filenames (`session-transcript.json` + `.txt`,
`session-summary.json`, `session-meta.json`, `strip-meta.json`) and the
`stripped/` subdir name used to be hand-typed across `sessions.py`,
`session_maintenance.py`, `batch_transcribe.py`, `batch_strip.py`, and
`session_merge.py`. They now live as the `FILENAME_*` / `DIRNAME_STRIPPED`
constants in `session_paths.py`, and every reader/writer/maintenance op
composes those onto an already-resolved session (or stripped) dir.

This test keeps that consolidation from eroding: it AST-scans the whole
`tapscribe` package for the filename literals appearing as *code* (not
docstrings, not comments) anywhere except the one owner module. A future
contributor who reintroduces a raw `dir / "session-transcript.json"` trips
it. `DIRNAME_STRIPPED` is deliberately NOT scanned the same way — the
spelling `"stripped"` also names the `source` API selector value
(`resolve_source_dir`), a separate wire concept free to diverge from the
directory name — so only the unambiguous filename literals are pinned.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tapscribe
from tapscribe import session_paths

# The literals whose single owner is session_paths.py. Each is unambiguous —
# it never legitimately appears as a non-path string elsewhere.
OWNED_FILENAMES = {
    session_paths.FILENAME_TRANSCRIPT_JSON,
    session_paths.FILENAME_TRANSCRIPT_TXT,
    session_paths.FILENAME_SUMMARY_JSON,
    session_paths.FILENAME_META_JSON,
    session_paths.FILENAME_STRIP_META_JSON,
}

_PKG_DIR = Path(tapscribe.__file__).parent
_OWNER = "session_paths.py"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids() of the Constant nodes that are module/class/function docstrings —
    those legitimately spell the filenames in prose and must not count."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def _code_string_literals(py_file: Path) -> list[str]:
    """Every string-constant VALUE used as code in `py_file`, excluding
    docstrings. (Comments are not AST nodes, so they're excluded for free.)"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_session_filenames_have_exactly_one_owner():
    offenders: dict[str, list[str]] = {}
    for py_file in sorted(_PKG_DIR.rglob("*.py")):
        if py_file.name == _OWNER:
            continue
        hits = sorted({s for s in _code_string_literals(py_file) if s in OWNED_FILENAMES})
        if hits:
            offenders[str(py_file.relative_to(_PKG_DIR))] = hits
    assert not offenders, (
        "Session-layout filenames must be referenced via the session_paths "
        f"FILENAME_* constants, not hand-typed. Raw literals found: {offenders}"
    )


def test_owner_module_defines_each_constant_once():
    # The owner module is allowed to (and must) spell each literal exactly
    # once — in its constant definition.
    literals = _code_string_literals(_PKG_DIR / _OWNER)
    for name in OWNED_FILENAMES:
        assert literals.count(name) == 1, f"{name!r} should be defined once in {_OWNER}"
