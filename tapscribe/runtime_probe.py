"""Runtime probe — "what can this machine run" as its own leaf module.

Backend detection (importable `mlx`, `torch.cuda.is_available()`) and
adapter-module availability (`find_spec` on a family's probe module) form a
self-contained subsystem: their own module-level caches, their own
post-install re-probe (`refresh_backend_probes`), and their own test
overrides. It has consumers well outside the transcriber model registry —
`tapscribe.summarizers.catalog` routes the local summarizer's backend with
it, `tapscribe.live` predicts the live channel's device label with it, and
`tapscribe.setup_state` surfaces it for the install-picker UI — none of
which need the full `TranscriberRegistry` to answer "does this machine have
CUDA" or "is `parakeet_mlx` installed". `tapscribe.transcribers.catalog`
imports this module for the same probes and layers the model registry's
resolution semantics (`resolve()`, `BackendBinding.is_installed()`) on top.

Extracted from `tapscribe.transcribers.catalog` (issue #258) — a pure move,
no behaviour change. See `CONTEXT.md`'s `BackendKind / BackendPreference /
available_backends` entry for the surrounding vocabulary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: importing `tapscribe.transcribers.base` for real would run
    # `tapscribe/transcribers/__init__.py`, which imports `.catalog`, which
    # imports this module — a circular import. `from __future__ import
    # annotations` (PEP 563) stringifies every annotation below, so the
    # names are never touched at runtime; only static type checkers need
    # this import.
    from .transcribers.base import BackendKind, BackendPreference

# ---------------------------------------------------------------------------
# Backend resolution — turn "auto" into one of mlx / cuda / cpu, based on
# what's importable on this machine. Cached at module import so the dashboard
# poll doesn't probe torch.cuda every second.
# ---------------------------------------------------------------------------


def _detect_available_backends() -> frozenset[BackendKind]:
    """Probe the runtime for MLX / CUDA / CPU. CPU is always present.

    Each probe is a quick `find_spec` + (for CUDA) a torch.cuda check.
    Probes never throw — a missing import returns False, never propagates.
    """
    import importlib.util
    import platform

    available: set[BackendKind] = {"cpu"}

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        if importlib.util.find_spec("mlx") is not None:
            available.add("mlx")

    # CUDA: torch must be importable AND report cuda.is_available(). We
    # use find_spec first to avoid importing torch (slow) when it's
    # missing entirely, then the actual import only when we know it's
    # present.
    if importlib.util.find_spec("torch") is not None:
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                available.add("cuda")
        except Exception:  # noqa: BLE001 — torch import can fail in many ways on bad installs
            pass

    return frozenset(available)


_AVAILABLE_BACKENDS_CACHE: frozenset[BackendKind] | None = None


def available_backends() -> frozenset[BackendKind]:
    """Return the cached set of BackendKinds this machine can serve.

    First call probes; subsequent calls are O(1). Tests that want to
    force a specific set can call `set_available_backends_for_testing`."""
    global _AVAILABLE_BACKENDS_CACHE
    if _AVAILABLE_BACKENDS_CACHE is None:
        _AVAILABLE_BACKENDS_CACHE = _detect_available_backends()
    return _AVAILABLE_BACKENDS_CACHE


def available_backend_strs() -> frozenset[str]:
    """`available_backends()` as a plain `str` frozenset for JSON serialisers /
    membership checks (BackendKind is a `str` Literal, so this is a no-op cast).
    One home for the idiom shared by `/api/models`, `/api/state`, and setup."""
    return frozenset(str(k) for k in available_backends())


def set_available_backends_for_testing(kinds: frozenset[BackendKind] | None) -> None:
    """Override the detected-backends cache. `None` re-enables auto-probe."""
    global _AVAILABLE_BACKENDS_CACHE
    _AVAILABLE_BACKENDS_CACHE = kinds


# ---------------------------------------------------------------------------
# Adapter-module availability — does this install have the right Python
# packages to run a given binding? Drives the UI filter so the dashboard
# doesn't advertise Voxtral / Parakeet on installs where the
# operator told the install picker to skip them.
# ---------------------------------------------------------------------------


_INSTALLED_MODULES_OVERRIDE: frozenset[str] | None = None
# Memoised find_spec() results, keyed by module name. Installed packages
# don't appear or vanish within a running process, so a probe's answer is
# stable for the process lifetime — yet `/api/models` and the once-per-second
# /api/state poll (via `_compute_inputs_support`) probe every registry entry
# on every call. Cache the real-probe answer; the test override is checked
# first and never cached, and `set_installed_modules_for_testing` clears this
# so a test that toggles between override and real probing starts clean.
_FIND_SPEC_CACHE: dict[str, bool] = {}


def is_module_available(name: str) -> bool:
    """True iff `name` is importable. The test override hook
    (`set_installed_modules_for_testing`) replaces the probe with a
    fixed set so tests can pretend e.g. `parakeet_mlx` is uninstalled
    without touching the real environment."""
    if _INSTALLED_MODULES_OVERRIDE is not None:
        return name in _INSTALLED_MODULES_OVERRIDE
    cached = _FIND_SPEC_CACHE.get(name)
    if cached is not None:
        return cached
    import importlib.util

    available = importlib.util.find_spec(name) is not None
    _FIND_SPEC_CACHE[name] = available
    return available


def set_installed_modules_for_testing(names: frozenset[str] | None) -> None:
    """Override what `is_module_available` reports. `None` re-enables
    real probing. Use `frozenset()` to simulate "nothing installed"."""
    global _INSTALLED_MODULES_OVERRIDE
    _INSTALLED_MODULES_OVERRIDE = names
    # Drop memoised real-probe answers so a test toggling between override
    # and real probing never sees a stale find_spec result.
    _FIND_SPEC_CACHE.clear()


def refresh_backend_probes() -> None:
    """Re-probe installed adapters + available backends after an in-app install,
    so `/api/models` and `/api/setup/state` reflect a freshly pip-installed
    package WITHOUT a process restart.

    Invalidates Python's import-system caches (so a just-installed module becomes
    importable in this running process), drops the memoised `find_spec` answers,
    and clears the available-backends cache so the next call re-detects (e.g.
    CUDA now that torch is present). Leaves any test override
    (`_INSTALLED_MODULES_OVERRIDE`) untouched — it's checked before the cache.

    Re-enables *detection* of newly-present packages only: a module that already
    failed to import earlier in this process still needs a restart."""
    import importlib

    global _AVAILABLE_BACKENDS_CACHE
    importlib.invalidate_caches()
    _FIND_SPEC_CACHE.clear()
    _AVAILABLE_BACKENDS_CACHE = None


# `auto` resolves to the first kind in this list that's available. MLX first
# (cheapest, lowest-latency on Apple Silicon), then CUDA, then CPU.
AUTO_RESOLUTION_ORDER: tuple[BackendKind, ...] = ("mlx", "cuda", "cpu")


def resolve_backend_preference(preference: BackendPreference) -> BackendKind:
    """Turn a `BackendPreference` (the operator's choice, possibly `auto`)
    into the concrete `BackendKind` that should be used.

    Raises `RuntimeError` when the operator picked a specific kind that's
    not available on this machine — clearer failure than silently
    falling back. `auto` falls through the resolution order until
    something works; CPU is always present so the loop always terminates.
    """
    avail = available_backends()
    if preference == "auto":
        for kind in AUTO_RESOLUTION_ORDER:
            if kind in avail:
                return kind
        # avail always contains "cpu", so this is unreachable, but the
        # explicit fallback keeps the function total.
        return "cpu"
    if preference not in avail:
        raise RuntimeError(
            f"backend={preference!r} requested but not available on this machine. "
            f"Available: {sorted(avail)}. Install the matching extra "
            f"(pip install tapscribe[mlx|parakeet]) or pick a different backend."
        )
    return preference
