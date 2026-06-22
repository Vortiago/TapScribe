"""Install resolver for the browser setup surface (the "D" pattern).

The app does NOT resolve pip extras or run pip itself — it delegates to the
dependency-free `tools/install_picker.py`, which already encapsulates the messy
parts (shared extras like `whisper-live`, the auto-appended `cuda-libs`,
per-backend extras, the skip-if-unchanged stamp). The app's job is the
translation seam: turn the catalog-family selection the UI speaks into the
picker's selection (`.tapscribe-install.json` v2), then run the picker
`--non-interactive`, which loads that selection and installs.

Catalog families → picker (install) families
---------------------------------------------
The UI shows catalog families (whisper, nb-whisper, voxtral, parakeet). The
picker installs by BACKEND extra, so several catalog families fold onto one
picker family: Whisper and NB-Whisper both map to the picker's `whisper` family
(NB-Whisper rides its CPU/CUDA faster-whisper backend — it has no MLX). When the
operator picks Whisper-via-MLX *and* NB-Whisper, the picker family resolves to
backend "both" (MLX + faster-whisper), and `resolve_extras` dedups the shared
`whisper-live`. This dedup is exactly what folding here buys.

The picker family/backend key strings below are duplicated as constants (the app
must not import the dependency-free picker at runtime). `tests/test_setup_install.py`
validates every produced selection against the picker's real `resolve_extras`,
so a drift in the picker's model fails the suite rather than shipping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PICKER_SCRIPT = _REPO_ROOT / "tools" / "install_picker.py"
_STATE_FILE = _REPO_ROOT / ".tapscribe-install.json"

# Mirror of tools/install_picker.py's keys (see module docstring on why these
# are duplicated rather than imported; the test pins them to the real picker).
_STATE_VERSION = 2
_BK_CPU, _BK_MLX, _BK_BOTH = "cpu", "mlx", "both"
_PICKER_FAMILIES: tuple[str, ...] = ("whisper", "voxtral", "parakeet")
# catalog family -> picker (install) family
_CATALOG_TO_PICKER: dict[str, str] = {
    "whisper": "whisper",
    "nb-whisper": "whisper",  # rides the whisper family's faster-whisper backend
    "voxtral": "voxtral",
    "parakeet": "parakeet",
}


def to_picker_state(selection: dict[str, str]) -> dict:
    """Translate a catalog-family selection into the picker's v2 state dict.

    `selection` maps a catalog family to the chosen backend kind
    (``"mlx" | "cuda" | "cpu"`` — the host-valid kind the UI resolved). "cuda"
    and "cpu" both ride the picker's CPU/CUDA (faster-whisper / transformers)
    backend; "mlx" rides the MLX backend. Unknown families are ignored.
    """
    # Collect the picker backends each picker family needs, then merge.
    per_family: dict[str, set[str]] = {}
    for catalog_family, kind in selection.items():
        picker_family = _CATALOG_TO_PICKER.get(catalog_family)
        if picker_family is None:
            continue  # unknown family — ignore rather than fabricate an install
        picker_backend = _BK_MLX if kind == _BK_MLX else _BK_CPU
        per_family.setdefault(picker_family, set()).add(picker_backend)

    choices: dict[str, dict] = {}
    for family in _PICKER_FAMILIES:
        backends = per_family.get(family)
        if not backends:
            choices[family] = {"enabled": False, "backend": _BK_CPU}
        elif _BK_MLX in backends and _BK_CPU in backends:
            choices[family] = {"enabled": True, "backend": _BK_BOTH}
        elif _BK_MLX in backends:
            choices[family] = {"enabled": True, "backend": _BK_MLX}
        else:
            choices[family] = {"enabled": True, "backend": _BK_CPU}
    return {"version": _STATE_VERSION, "choices": choices}


def write_picker_state(state: dict, *, path: Path = _STATE_FILE) -> None:
    """Persist the picker state where `install_picker --non-interactive` reads
    it. Matches the picker's own `Selection.save` formatting."""
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def picker_install_argv(*, python: str = sys.executable, no_mlx: bool = False) -> list[str]:
    """argv to run the install picker non-interactively against the written
    selection. The picker resolves extras, runs pip, and writes the stamp."""
    argv = [python, str(_PICKER_SCRIPT), "--non-interactive"]
    if no_mlx:
        argv.append("--no-mlx")
    return argv
