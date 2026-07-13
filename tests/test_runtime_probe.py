"""Tests for `tapscribe.runtime_probe` — the hardware/runtime probe subsystem
extracted from `tapscribe.transcribers.catalog` (issue #258).

These exercise the module's public surface directly (not through the
transcriber registry) so it's pinned as a standalone unit: `available_backends`
always includes "cpu", the test-override hooks flip cleanly, and
`resolve_backend_preference` walks the auto-resolution order / raises on an
explicit unavailable pick. `tapscribe.transcribers.catalog`'s own
`resolve_backend_preference` / `REGISTRY.resolve` tests (test_transcribers_catalog.py)
cover the registry-level integration on top of this.
"""

from __future__ import annotations

import pytest

from tapscribe import runtime_probe


@pytest.fixture(autouse=True)
def _restore_probes():
    """Every test forces the caches via the public test hooks; restore real
    probing afterwards so no override leaks into another test file."""
    yield
    runtime_probe.set_available_backends_for_testing(None)
    runtime_probe.set_installed_modules_for_testing(None)


def test_available_backends_always_includes_cpu():
    """CPU is always present, real probe or not — the whole resolution
    order relies on this as its unconditional fallback."""
    assert "cpu" in runtime_probe.available_backends()


def test_available_backends_caches_across_calls():
    """Second call returns the identical frozenset object — no re-probe."""
    first = runtime_probe.available_backends()
    second = runtime_probe.available_backends()
    assert first is second


def test_set_available_backends_for_testing_overrides_then_resets():
    runtime_probe.set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    assert runtime_probe.available_backends() == frozenset({"cuda", "cpu"})

    runtime_probe.set_available_backends_for_testing(None)
    assert "cpu" in runtime_probe.available_backends()


def test_available_backend_strs_mirrors_available_backends_as_plain_strs():
    runtime_probe.set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    assert runtime_probe.available_backend_strs() == frozenset({"mlx", "cpu"})
    # BackendKind is itself a str Literal, but the return type must be
    # exactly `str`, not the Literal — serialisers rely on this.
    assert all(type(s) is str for s in runtime_probe.available_backend_strs())


def test_set_installed_modules_for_testing_forces_a_fixed_set():
    runtime_probe.set_installed_modules_for_testing(frozenset({"faster_whisper"}))
    assert runtime_probe._is_module_available("faster_whisper") is True
    assert runtime_probe._is_module_available("definitely_not_a_real_module_xyz") is False


def test_set_installed_modules_for_testing_none_restores_real_probing():
    runtime_probe.set_installed_modules_for_testing(frozenset())
    assert runtime_probe._is_module_available("os") is False  # forced empty set

    runtime_probe.set_installed_modules_for_testing(None)
    assert runtime_probe._is_module_available("os") is True  # real find_spec


def test_refresh_backend_probes_clears_the_override_free_caches():
    """`refresh_backend_probes` re-enables *detection*: it clears the
    memoised find_spec answers and the available-backends cache so a
    freshly pip-installed package is picked up without a restart. It must
    NOT touch a live test override (checked first, no cache involved)."""
    runtime_probe.set_available_backends_for_testing(frozenset({"made-up-kind"}))
    assert runtime_probe.available_backends() == frozenset({"made-up-kind"})

    runtime_probe.refresh_backend_probes()
    assert "made-up-kind" not in runtime_probe.available_backends()
    assert "cpu" in runtime_probe.available_backends()


def test_resolve_backend_preference_auto_walks_resolution_order():
    runtime_probe.set_available_backends_for_testing(frozenset({"mlx", "cuda", "cpu"}))
    assert runtime_probe.resolve_backend_preference("auto") == "mlx"

    runtime_probe.set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    assert runtime_probe.resolve_backend_preference("auto") == "cuda"

    runtime_probe.set_available_backends_for_testing(frozenset({"cpu"}))
    assert runtime_probe.resolve_backend_preference("auto") == "cpu"


def test_resolve_backend_preference_explicit_available_kind_passes_through():
    runtime_probe.set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    assert runtime_probe.resolve_backend_preference("cuda") == "cuda"
    assert runtime_probe.resolve_backend_preference("cpu") == "cpu"


def test_resolve_backend_preference_explicit_unavailable_kind_raises():
    runtime_probe.set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    with pytest.raises(RuntimeError, match="mlx"):
        runtime_probe.resolve_backend_preference("mlx")
