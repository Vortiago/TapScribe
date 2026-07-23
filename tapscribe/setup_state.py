"""Setup state for the browser-based first-run / manage-models surface.

This is the read-only backbone of the browser setup surface (GET /setup): per
model family — is it installed, what can it do (live vs. batch), which
host-capable backends, which models — derived from the transcriber catalog (the
authoritative registry, also behind `/api/models`) so it can't drift from what
the app supports.

Families are independent
------------------------
Each catalog family is its own row. Families differ in their available models,
languages, and how transcription / WhisperLiveKit is invoked — that's the whole
difference between e.g. Whisper and NB-Whisper. Two families MAY share a backend
package, and that's fine: a family reports installed iff one of ITS OWN backend
bindings is importable on a host-capable kind, so the shared case falls out
correctly without any special-casing —

  * Whisper: faster-whisper (CPU/CUDA) **and** mlx-whisper (MLX).
  * NB-Whisper: faster-whisper (CPU/CUDA) only — no public MLX weights.

So installing Whisper's faster-whisper backend also lights up NB-Whisper; but on
an Apple-Silicon box that installed Whisper via MLX *only*, NB-Whisper stays
not-installed (it needs faster-whisper). Model weights download lazily on first
use. The install-EXECUTION slice (separate) maps a family to its backend extra
and dedups shared extras; it lives with the dependency-free
`tapscribe/install_picker.py`, which must not import this package (it runs before
the package is installed).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .runtime_probe import available_backend_strs
from .setup_install import read_live_choice
from .transcribers.catalog import REGISTRY, TranscriberRegistry

# Curated per-family display metadata, in matrix order: (catalog family, label,
# rough size hint). An allowlist (like SUMMARY_MODELS): only families that make
# sense as a FIRST-RUN install surface here. Moonshine (PRD #120, shipped in
# #334) is implemented but live-only — it can't be an operator's only
# transcriber, so the first-run flow deliberately doesn't offer it; it installs
# via the terminal picker (`tapscribe/install_picker.py`) or
# `pip install "tapscribe[moonshine]"` once a batch family is in place.
# Surfacing live-only extras in /setup as an add-on section is a follow-up.
# Labels + sizes are setup-facing display data, intentionally independent of
# tapscribe/install_picker.py's FamilyDef list (which can't import this package).
# Sizes are approximate (vary by backend/host) — expectation-setting, not a
# contract.
FAMILY_META: tuple[tuple[str, str, str], ...] = (
    ("whisper", "Whisper", "~80–150 MB"),
    ("nb-whisper", "NB-Whisper", "~150 MB"),
    ("voxtral", "Voxtral (Mistral)", "~2 GB"),
    ("parakeet", "Parakeet (NVIDIA)", "~1.5–2.5 GB"),
)
# Display order for backend chips (preferred first). Mirrors the runtime
# probe's AUTO_RESOLUTION_ORDER by intent, but kept a separate local constant
# — display order and auto-resolution priority are distinct concerns that
# needn't move together (and the probe's is private).
_BACKEND_DISPLAY_ORDER: tuple[str, ...] = ("mlx", "cuda", "cpu")


def is_first_run(registry: TranscriberRegistry = REGISTRY) -> bool:
    """True when no transcription backend is installed yet — i.e. setup hasn't
    happened. Once any model's adapter is importable on a host-capable backend,
    it's no longer a first run. (`ModelEntry.is_installed()` already combines
    "adapter importable" with "backend kind available on this host".)"""
    return not any(e.is_installed() for e in registry.entries())


def _family_state(
    registry: TranscriberRegistry, family: str, label: str, size_hint: str, avail: frozenset[str]
) -> dict | None:
    # Only `available` entries count — a family of "coming soon" placeholders
    # must not advertise runnable backends or models. None ⇒ don't surface it.
    entries = [e for e in registry.entries() if e.family == family and e.available]
    if not entries:
        return None
    contexts: set[str] = set()
    kinds: set[str] = set()
    installed = False
    models: list[str] = []
    for e in entries:
        contexts |= set(e.contexts)
        kinds |= set(e.supported_backend_kinds())
        installed = installed or e.is_installed()
        models.append(e.model_id)
    return {
        "family": family,
        "label": label,
        "size_hint": size_hint,
        "live": "live" in contexts,
        "batch": "batch" in contexts,
        "installed": installed,
        # Only THIS family's backends that this host can run, in display order.
        "backends": [k for k in _BACKEND_DISPLAY_ORDER if k in kinds and k in avail],
        "models": models,
    }


def read_stale_selection(path: Path) -> list[dict]:
    """Families the last picker run SKIPPED because their saved backend left the
    catalog, read from the sidecar the picker writes (ADR-0015).

    Degrades to ``[]`` on anything unreadable. /setup is how an operator
    recovers from exactly this situation, so a corrupt sidecar must never be
    what stops the page from rendering.
    """
    try:
        # utf-8 explicitly: family labels carry non-ASCII, and Windows would
        # otherwise decode this cp1252 and blow up the setup page.
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = blob.get("stale_backends") if isinstance(blob, dict) else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and "family" in entry]


def build_setup_state(
    registry: TranscriberRegistry = REGISTRY,
    *,
    warnings_file: Path | None = None,
    picker_state_file: Path | None = None,
) -> dict:
    """Assemble the setup state the first-run / manage surface renders from.

    Returns ``{first_run, available_backends, families, stale_selection,
    live_channel}`` where each family is ``{family, label, size_hint, live,
    batch, installed, backends, models}``. Only curated ``FAMILY_META``
    families with at least one available catalog entry are included, in
    display order.

    ``stale_selection`` is the picker's skipped-family report — see
    ``read_stale_selection``. ``live_channel`` is the operator's PERSISTED
    live-caption choice (#374) — see ``setup_install.read_live_choice`` —
    distinct from a family's ``live`` flag above, which is the CAPABILITY (this
    family has a live channel at all), not the current setting.
    """
    avail = available_backend_strs()
    families = [
        state
        for family, label, size_hint in FAMILY_META
        if (state := _family_state(registry, family, label, size_hint, avail)) is not None
    ]
    return {
        "first_run": is_first_run(registry),
        "available_backends": sorted(avail),
        "families": families,
        "stale_selection": read_stale_selection(warnings_file or config.INSTALL_WARNINGS_FILE),
        "live_channel": read_live_choice(picker_state_file),
    }
