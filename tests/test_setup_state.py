"""Setup-state builder — the catalog-driven view the browser first-run/manage
surface (the "D" pattern) renders from. Pure assembly over the transcriber
registry, so it can't drift from what the app actually supports.

These tests drive the two probes (installed modules + available backends)
through the catalog's test hooks so they're deterministic on any host.
"""

from __future__ import annotations

import pytest
from conftest import all_probe_modules  # type: ignore[import-not-found]

from tapscribe.setup_state import FAMILY_META, build_setup_state, is_first_run
from tapscribe.transcribers.catalog import (
    BackendBinding,
    ModelEntry,
    TranscriberRegistry,
    set_available_backends_for_testing,
    set_installed_modules_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_probes():
    """Restore real probing after each test so overrides don't leak."""
    yield
    set_installed_modules_for_testing(None)
    set_available_backends_for_testing(None)


def _family(state: dict, key: str) -> dict:
    return next(f for f in state["families"] if f["family"] == key)


def test_first_run_true_when_nothing_installed():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset())
    assert is_first_run() is True
    state = build_setup_state()
    assert state["first_run"] is True
    assert all(f["installed"] is False for f in state["families"])


def test_not_first_run_once_a_backend_is_installed():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(all_probe_modules())
    assert is_first_run() is False
    state = build_setup_state()
    assert state["first_run"] is False
    assert _family(state, "whisper")["installed"] is True


def test_capability_flags_match_catalog_contexts():
    set_available_backends_for_testing(frozenset({"cpu", "cuda", "mlx"}))
    set_installed_modules_for_testing(all_probe_modules())
    state = build_setup_state()
    whisper = _family(state, "whisper")
    assert whisper["live"] is True and whisper["batch"] is True
    # Both Parakeet and Voxtral are batch-only (no live channel wired).
    assert _family(state, "parakeet")["live"] is False
    assert _family(state, "parakeet")["batch"] is True
    assert _family(state, "voxtral")["live"] is False
    assert _family(state, "voxtral")["batch"] is True


def test_whisper_and_nb_whisper_are_independent_families():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(all_probe_modules())
    keys = [f["family"] for f in build_setup_state()["families"]]
    assert "whisper" in keys
    assert "nb-whisper" in keys  # its own row — not folded into whisper


def test_nb_whisper_has_no_mlx_backend_even_on_an_mlx_host():
    # NB-Whisper has no public MLX weights — faster-whisper (CPU/CUDA) only,
    # unlike Whisper which also has an MLX backend.
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    set_installed_modules_for_testing(all_probe_modules())
    state = build_setup_state()
    assert "mlx" in _family(state, "whisper")["backends"]
    assert _family(state, "nb-whisper")["backends"] == ["cpu"]


def test_nb_whisper_install_state_tracks_its_own_backend():
    # Proof they're independent: on Apple Silicon, installing Whisper via MLX
    # ONLY (mlx_whisper present, faster-whisper absent) leaves NB-Whisper — which
    # needs faster-whisper — not installed.
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    set_installed_modules_for_testing(frozenset({"mlx_whisper"}))
    state = build_setup_state()
    assert _family(state, "whisper")["installed"] is True
    assert _family(state, "nb-whisper")["installed"] is False
    # Installing the shared faster-whisper backend lights up both.
    set_installed_modules_for_testing(frozenset({"faster_whisper"}))
    state = build_setup_state()
    assert _family(state, "whisper")["installed"] is True
    assert _family(state, "nb-whisper")["installed"] is True


def test_backends_are_filtered_to_host_capable_kinds():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(all_probe_modules())
    state = build_setup_state()
    assert state["available_backends"] == ["cpu"]
    for fam in state["families"]:
        assert fam["backends"] == ["cpu"]  # mlx/cuda filtered out on a cpu-only host


def test_only_curated_families_surface_no_moonshine():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(all_probe_modules())
    keys = {f["family"] for f in build_setup_state()["families"]}
    curated = {k for k, _, _ in FAMILY_META}
    assert keys <= curated
    assert keys == {"whisper", "nb-whisper", "voxtral", "parakeet"}
    assert "moonshine" not in keys  # live-only, inference not implemented yet


def test_each_family_carries_a_size_hint_and_models():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(all_probe_modules())
    for fam in build_setup_state()["families"]:
        assert fam["size_hint"]  # non-empty rough estimate
        assert fam["models"]  # at least one model id in the family


def test_family_with_only_unavailable_entries_is_not_surfaced():
    """A 'coming soon' family (all entries available=False) must not appear — it
    has no loadable models, so advertising backends for it would be misleading."""
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset())
    reg = TranscriberRegistry(
        (
            ModelEntry(
                model_id="whisper-soon",
                family="whisper",  # a curated FAMILY_META family
                display_name="Whisper (coming soon)",
                description="placeholder",
                languages=("auto",),
                contexts=frozenset({"batch"}),
                backends=(BackendBinding(frozenset({"cpu"}), lambda *_: None),),
                available=False,
            ),
        )
    )
    state = build_setup_state(reg)
    assert state["families"] == []  # whisper skipped: no available entries
