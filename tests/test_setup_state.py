"""Setup-state builder — the catalog-driven view the browser first-run/manage
surface renders from. Pure assembly over the transcriber registry, so it can't
drift from what the app actually supports.

These tests drive the two probes (installed modules + available backends)
through the runtime probe's test hooks so they're deterministic on any host.
"""

from __future__ import annotations

import pytest
from conftest import all_probe_modules  # type: ignore[import-not-found]

from tapscribe.runtime_probe import (
    set_available_backends_for_testing,
    set_installed_modules_for_testing,
)
from tapscribe.setup_state import FAMILY_META, build_setup_state, is_first_run
from tapscribe.transcribers.catalog import BackendBinding, ModelEntry, TranscriberRegistry

# ── stale-selection surfacing (ADR-0015) ────────────────────────────────────
#
# The picker skips a family whose saved backend left the catalog and warns to
# stderr. In a Bundle that goes to a log file nobody opens, so the operator's
# models silently stop installing after an upgrade. /setup has to say it.


def test_stale_selection_is_absent_when_no_sidecar_exists(tmp_path):
    assert build_setup_state(warnings_file=tmp_path / "nope.json")["stale_selection"] == []


def test_stale_selection_surfaces_the_skipped_families(tmp_path):
    import json

    sidecar = tmp_path / ".tapscribe-install-warnings.json"
    sidecar.write_text(
        json.dumps(
            {"stale_backends": [{"family": "parakeet", "label": "Parakeet (NVIDIA)", "backend": "mlx"}]}
        )
    )
    stale = build_setup_state(warnings_file=sidecar)["stale_selection"]
    assert [entry["family"] for entry in stale] == ["parakeet"]
    assert stale[0]["backend"] == "mlx"


def test_malformed_sidecar_degrades_to_no_warning(tmp_path):
    """A corrupt sidecar must not 500 the setup page — the page is how the
    operator recovers, so it has to render even when this file is garbage."""
    sidecar = tmp_path / ".tapscribe-install-warnings.json"
    sidecar.write_text("not json at all")
    assert build_setup_state(warnings_file=sidecar)["stale_selection"] == []


def test_warnings_filename_matches_the_picker():
    """The app must not import the dependency-free picker, so the filename is
    mirrored in config. This pins the mirror to the real constant — the same
    guard `test_setup_install.py` applies to the picker's family/backend keys."""
    from tapscribe import config, install_picker

    assert config.INSTALL_WARNINGS_FILE.name == install_picker.WARNINGS_FILENAME


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
    # Moonshine is implemented (PRD #120/#334) but live-only — it can't be a
    # first-run operator's ONLY transcriber, so /setup deliberately doesn't
    # offer it (see FAMILY_META's rationale); the terminal picker and the
    # pip extras are its install paths.
    assert "moonshine" not in keys


def test_each_family_carries_a_size_hint_and_models():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(all_probe_modules())
    for fam in build_setup_state()["families"]:
        assert fam["size_hint"]  # non-empty rough estimate
        assert fam["models"]  # at least one model id in the family


# ── live_channel: the persisted live-caption choice (#374) ─────────────────
#
# Distinct from a family's `live` capability flag (does this family HAVE a
# live channel at all) — this is the OPERATOR'S current setting for it, read
# from .tapscribe-install.json so /setup's toggle can pre-check to the real
# state instead of always defaulting to checked.


def test_live_channel_defaults_true_when_no_state_file(tmp_path):
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset())
    state = build_setup_state(picker_state_file=tmp_path / "nope.json")
    assert state["live_channel"] is True


def test_live_channel_defaults_true_when_key_absent(tmp_path):
    """A state file from before #374 (or a family with no `live` key at
    all) must not be read as 'off' — the opt-out is new, the default isn't."""
    import json

    picker_state = tmp_path / ".tapscribe-install.json"
    picker_state.write_text(
        json.dumps({"version": 2, "choices": {"whisper": {"enabled": True, "backend": "cpu"}}})
    )
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset())
    state = build_setup_state(picker_state_file=picker_state)
    assert state["live_channel"] is True


def test_live_channel_reflects_a_persisted_false(tmp_path):
    import json

    picker_state = tmp_path / ".tapscribe-install.json"
    picker_state.write_text(
        json.dumps({"version": 2, "choices": {"whisper": {"enabled": True, "backend": "cpu", "live": False}}})
    )
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset())
    state = build_setup_state(picker_state_file=picker_state)
    assert state["live_channel"] is False


def test_live_channel_degrades_to_true_on_a_corrupt_state_file(tmp_path):
    picker_state = tmp_path / ".tapscribe-install.json"
    picker_state.write_text("not json at all")
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset())
    state = build_setup_state(picker_state_file=picker_state)
    assert state["live_channel"] is True


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
