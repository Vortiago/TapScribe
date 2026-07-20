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
import json
from pathlib import Path

import pytest
from wav_builders import seed_wav  # type: ignore[import-not-found]  # tests/ on sys.path

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


# --- Harm-layer guards: the structural pins above force the consolidation but are
# blind to what it costs. The cheap PRIMARY path (`read_primary_payload` /
# `read_primary_marker`, on the once-per-second /api/state poll) must resolve the
# primary via the `_primary` pointer WITHOUT parsing every sibling, and must stay
# lenient about the primary's shape. Routing it through the parse-all seam ships green
# past the behavior suite (a corrupt sibling is filtered, the valid primary still
# returns) while regressing both — so pin the parse COUNT and the lenient case directly.


def _seed_new_layout(wav: Path, keys: tuple[str, ...], primary: str) -> None:
    """Write `keys` sidecars into `<wav>.transcripts/` (valid JSON, layout-blind
    content) and point the `_primary` marker at `primary` — no transcriber needed."""
    d = wav_cache.transcripts_dir(wav)
    d.mkdir(parents=True, exist_ok=True)
    for key in keys:
        (d / f"{key}.json").write_text(
            json.dumps({"backend": key, "model": key, "text": key}), encoding="utf-8"
        )
    (d / wav_cache._PRIMARY_POINTER).write_text(primary, encoding="utf-8")


def test_primary_path_does_not_parse_every_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Hot-path parse-count ceiling: resolving the primary path parses AT MOST the
    primary, never every sibling. RED if a reader is rebuilt on the parse-all seam
    (3 siblings -> 3 parses); the pointer-based resolution parses zero."""
    wav = seed_wav(tmp_path / "x.wav")
    _seed_new_layout(wav, keys=("a", "b", "c"), primary="a")

    calls = {"n": 0}
    real_read_entry = wav_cache._read_entry
    monkeypatch.setattr(
        wav_cache,
        "_read_entry",
        lambda p: (calls.__setitem__("n", calls["n"] + 1), real_read_entry(p))[1],
    )

    assert wav_cache._primary_sidecar_path(wav) == wav_cache.transcripts_dir(wav) / "a.json"
    assert calls["n"] <= 1, f"_primary_sidecar_path parsed {calls['n']} sidecars (should read the pointer)"

    calls["n"] = 0
    assert wav_cache.read_primary_payload(wav) is not None
    assert calls["n"] <= 1, f"read_primary_payload parsed {calls['n']} sidecars"


def test_read_primary_payload_streams_valid_json_the_dataclass_parse_rejects(tmp_path: Path):
    """Lenient-input guard: `read_primary_payload` streams the raw primary dict for ANY
    valid JSON — including a shape the strict dataclass parse (`_read_entry`) rejects.
    Rebuilding it on the parse-all seam silently flips such a primary to None."""
    wav = seed_wav(tmp_path / "x.wav")
    incomplete = {"backend": "faster-whisper", "model": "tiny", "text": "partial"}
    wav.with_suffix(".json").write_text(json.dumps(incomplete), encoding="utf-8")

    # This shape is one the strict dataclass parse rejects (missing required fields)...
    assert wav_cache._read_entry(wav.with_suffix(".json")) is None
    # ...yet the raw-streaming primary reader must still surface it verbatim.
    assert wav_cache.read_primary_payload(wav) == incomplete
