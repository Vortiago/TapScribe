"""Setup state for the browser-based first-run / manage-models surface.

This is the read-only backbone of the "D" setup pattern (see
`prototypes/setup/`): a catalog-driven description of which model families
exist, whether each is installed, what it can do (live vs. batch), and which
backends this host can actually run. The setup UI renders from THIS rather than
a hand-maintained list, so it can't drift from what the app supports — the same
reason `/api/models` reads the catalog.

Scope: this module only describes *what exists and what's installed*. The
install *execution* (resolving families to pip extras, running pip, streaming
progress) is deliberately separate — and lives with the dependency-free
`tools/install_picker.py`, which must not import this package (it runs before
the package is installed).
"""

from __future__ import annotations

from .transcribers.catalog import (
    REGISTRY,
    TranscriberRegistry,
    available_backends,
)

# Families surfaced in the setup matrix, in display order, each with a human
# label and a ROUGH download size hint. Curated (an allowlist, like
# SUMMARY_MODELS) rather than derived, so unimplemented/experimental families
# (e.g. moonshine) don't appear until deliberately added. Sizes live here, not
# in the catalog, because download size is a setup concern, not a per-model
# runtime fact — and they're approximate (they vary by backend and host), shown
# to set expectations, not as a contract. These display strings are
# intentionally independent of tools/install_picker.py's install-family list
# (which groups by pip extra — Whisper+NB-Whisper share one install — and can't
# import this package); they are not kept in lockstep.
FAMILY_META: tuple[tuple[str, str, str], ...] = (
    ("whisper", "Whisper", "~80–150 MB"),
    ("nb-whisper", "NB-Whisper", "~150 MB"),
    ("voxtral", "Voxtral (Mistral)", "~2 GB"),
    ("parakeet", "Parakeet (NVIDIA)", "~1.5–2.5 GB"),
)
_BACKEND_DISPLAY_ORDER: tuple[str, ...] = ("mlx", "cuda", "cpu")


def is_first_run(registry: TranscriberRegistry = REGISTRY) -> bool:
    """True when no transcription backend is installed yet — i.e. setup hasn't
    happened. Once any model's adapter is importable on a host-capable backend,
    it's no longer a first run. (`ModelEntry.is_installed()` already combines
    "adapter importable" with "backend kind available on this host".)"""
    return not any(e.is_installed() for e in registry.entries())


def _family_state(registry: TranscriberRegistry, family: str, avail: frozenset[str]) -> dict | None:
    # Only `available` entries count — a family of "coming soon" placeholders
    # (available=False) must not advertise runnable backends or models. None
    # means "don't surface this family".
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
    label = next((lbl for key, lbl, _ in FAMILY_META if key == family), family)
    size_hint = next((sz for key, _, sz in FAMILY_META if key == family), "")
    return {
        "family": family,
        "label": label,
        "size_hint": size_hint,
        "live": "live" in contexts,
        "batch": "batch" in contexts,
        "installed": installed,
        # Only backends this host can actually run, in display order.
        "backends": [k for k in _BACKEND_DISPLAY_ORDER if k in kinds and k in avail],
        "models": models,
    }


def build_setup_state(registry: TranscriberRegistry = REGISTRY) -> dict:
    """Assemble the setup state the first-run / manage surface renders from.

    Returns ``{first_run, available_backends, families}`` where each family is
    ``{family, label, size_hint, live, batch, installed, backends, models}``.
    Only the curated ``FAMILY_META`` families that exist in the registry are
    included, in that order.
    """
    avail = frozenset(str(k) for k in available_backends())
    families = [
        state
        for family, _label, _size in FAMILY_META
        if (state := _family_state(registry, family, avail)) is not None
    ]
    return {
        "first_run": is_first_run(registry),
        "available_backends": sorted(avail),
        "families": families,
    }
