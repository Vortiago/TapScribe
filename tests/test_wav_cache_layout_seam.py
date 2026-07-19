"""RED contract for #366 — consolidate the per-WAV transcript-cache legacy-layout
fallback behind ONE seam.

`tapscribe/wav_cache.py` serves two on-disk sidecar layouts: the current
directory-per-WAV (`<wav>.transcripts/`) and the legacy single `<wav>.json`. The
"new dir? else legacy" fallback is copy-pasted across four readers —
`read_all_cached`, `cache_listing`, `_read_entry_for`, `_primary_sidecar_path` —
each independently reaching for the legacy sidecar via `legacy_sidecar(wav_path)`.
The refactor routes every reader through the single enumeration seam (`sidecar_paths`
already has that shape) so the legacy layout lives in exactly one place.

This file pins the consolidation STRUCTURALLY: no reader may reach the legacy
sidecar itself — `legacy_sidecar` access must move into the seam. Behavior
preservation across BOTH layouts is guarded by the existing `test_wav_cache.py`
suite (kept in the gate and protected), so this file only pins that the duplication
is actually gone, not that reads still work.

Why key on `legacy_sidecar` and NOT the `.is_dir()` layout check: the `.is_dir()`
dispatch is trivially hoisted into a one-line helper (`_layout_is_new`) while every
duplicated branch survives verbatim — a false green. A reader that still owns its
own legacy fallback cannot avoid calling `legacy_sidecar`, so that reference is the
faithful proxy for "this reader still does its own fallback." A structural proxy,
by nature — whether the resulting seam is genuinely clean (vs merely present) is a
`/code-review` judgment, not something an AST can settle.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tapscribe.wav_cache as wav_cache

# The readers that today each hand-roll the new-or-legacy fallback (RED: all four
# reference `legacy_sidecar`), PLUS their stable public callers — clean today and
# required to stay clean, so the fallback can't be "consolidated" merely by inlining
# a private helper's legacy branch up into its public caller.
_MUST_NOT_TOUCH_LEGACY = {
    "read_all_cached",
    "cache_listing",
    "_read_entry_for",
    "_primary_sidecar_path",
    "read_cached",
    "read_primary_payload",
    "read_primary_marker",
}
_LEGACY_ACCESSOR = "legacy_sidecar"


def _module_tree() -> ast.Module:
    return ast.parse(Path(wav_cache.__file__).read_text(encoding="utf-8"))


def _funcs_referencing(name: str, tree: ast.Module) -> set[str]:
    """Module-level function names whose body references `name` (a bare Name)."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node)):
                out.add(node.name)
    return out


def test_legacy_fallback_consolidated_out_of_readers():
    """No cache reader reaches the legacy `<wav>.json` sidecar on its own — the
    layout fallback is owned by the single seam. RED today (all four readers call
    `legacy_sidecar`); green once each crosses the seam layout-blind."""
    offenders = _MUST_NOT_TOUCH_LEGACY & _funcs_referencing(_LEGACY_ACCESSOR, _module_tree())
    assert not offenders, (
        f"these readers still reach the legacy `{_LEGACY_ACCESSOR}` sidecar directly "
        f"instead of crossing the single layout seam: {sorted(offenders)}"
    )


def test_public_readers_still_exist():
    """Guard against 'consolidating' by deleting a reader: the stable public read
    surface must remain defined."""
    defined = {
        n.name for n in ast.walk(_module_tree()) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"read_all_cached", "cache_listing"} <= defined
