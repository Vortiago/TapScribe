"""Tests for `tapscribe.transcribers.catalog` — the TranscriberRegistry.

These tests stay away from instantiating any adapter (loader thunks
import heavy modules); they exercise the *routing + filtering* logic
that lives in the registry itself, plus the JSON shape `/api/models`
will serve.
"""

from __future__ import annotations

import pytest

from tapscribe.transcribers.base import SelectInput, TextInput
from tapscribe.transcribers.catalog import (
    REGISTRY,
    BackendBinding,
    ModelEntry,
    ResolvedBinding,
    TranscriberRegistry,
    resolve_backend_preference,
    set_available_backends_for_testing,
    set_installed_modules_for_testing,
)


@pytest.fixture(autouse=True)
def _restore_backends():
    """Most tests force a specific available-backends set; restore the
    auto-probe afterwards so other test files (e.g. routes) see real
    detection."""
    yield
    set_available_backends_for_testing(None)


def test_default_registry_contains_expected_families():
    families = {e.family for e in REGISTRY.entries()}
    assert families >= {"whisper", "nb-whisper", "voxtral", "parakeet", "canary"}


def test_every_default_entry_has_at_least_one_backend_binding():
    for e in REGISTRY.entries():
        assert e.backends, f"entry {e.model_id} has no backend bindings"
        assert e.supported_backend_kinds(), f"entry {e.model_id} declares zero kinds"


def test_for_context_batch_includes_parakeet_and_canary():
    batch_ids = {e.model_id for e in REGISTRY.for_context("batch")}
    assert "parakeet-tdt-0.6b-v3" in batch_ids
    assert "canary-1b-v2" in batch_ids


def test_for_context_live_excludes_parakeet_and_canary():
    """Parakeet/Canary have no true-streaming checkpoint and WhisperLiveKit
    has no Parakeet/Canary backend — so they're batch-only until a
    follow-up PR adds pseudo-streaming live channels."""
    live_ids = {e.model_id for e in REGISTRY.for_context("live")}
    assert "parakeet-tdt-0.6b-v3" not in live_ids
    assert "canary-1b-v2" not in live_ids


def test_for_context_live_includes_whisper_and_nb_whisper():
    live_ids = {e.model_id for e in REGISTRY.for_context("live")}
    assert "tiny.en" in live_ids
    assert "nb-whisper-medium" in live_ids
    assert "large-v3" in live_ids


def test_whisper_entries_declare_prompt_and_hotwords_inputs():
    whisper = REGISTRY.require("small.en")
    input_names = {i.name for i in whisper.inputs}
    assert input_names == {"initial_prompt", "hotwords"}


def test_voxtral_declares_no_text_inputs():
    """Voxtral's apply_transcription_request takes neither prompt nor
    hotwords — the UI should render zero extra fields."""
    voxtral = REGISTRY.require("voxtral-mini")
    assert voxtral.inputs == ()


def test_parakeet_declares_no_text_inputs():
    """Parakeet's model.transcribe(path) takes neither prompt nor hotwords;
    languages come from the catalog, not a user input."""
    pk = REGISTRY.require("parakeet-tdt-0.6b-v3")
    assert pk.inputs == ()


def test_canary_declares_source_and_target_language_selects():
    canary = REGISTRY.require("canary-1b-v2")
    names = {i.name for i in canary.inputs}
    assert names == {"source_lang", "target_lang"}
    for inp in canary.inputs:
        assert isinstance(inp, SelectInput)
        assert ("en", "English") in inp.options
        assert inp.default == "en"


def test_nb_whisper_excludes_mlx_binding():
    """NB-Whisper has no public MLX weights, so its only binding is the
    faster-whisper one (which handles cpu + cuda)."""
    nb = REGISTRY.require("nb-whisper-medium")
    assert "mlx" not in nb.supported_backend_kinds()
    assert {"cpu", "cuda"} <= nb.supported_backend_kinds()


def test_parakeet_supports_mlx_cuda_and_cpu():
    pk = REGISTRY.require("parakeet-tdt-0.6b-v3")
    assert pk.supported_backend_kinds() == frozenset({"mlx", "cuda", "cpu"})


def test_canary_supports_mlx_cuda_and_cpu():
    c = REGISTRY.require("canary-1b-v2")
    assert c.supported_backend_kinds() == frozenset({"mlx", "cuda", "cpu"})


# ── resolve / preference handling ────────────────────────────────────────


def test_resolve_auto_picks_mlx_when_available():
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    assert resolve_backend_preference("auto") == "mlx"


def test_resolve_auto_prefers_cuda_over_cpu_when_no_mlx():
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    assert resolve_backend_preference("auto") == "cuda"


def test_resolve_auto_falls_back_to_cpu():
    set_available_backends_for_testing(frozenset({"cpu"}))
    assert resolve_backend_preference("auto") == "cpu"


def test_resolve_explicit_kind_passes_when_available():
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    assert resolve_backend_preference("cuda") == "cuda"
    assert resolve_backend_preference("cpu") == "cpu"


def test_resolve_explicit_kind_raises_when_unavailable():
    """Operator-asked-for-mlx on a Linux/CUDA box should fail loudly, not
    silently fall back to CPU."""
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    with pytest.raises(RuntimeError, match="mlx"):
        resolve_backend_preference("mlx")


# ── registry.resolve dispatches to the right binding ─────────────────────


def test_resolve_returns_correct_loader_for_parakeet_on_cuda():
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    rb = REGISTRY.resolve("parakeet-tdt-0.6b-v3", preference="auto")
    assert isinstance(rb, ResolvedBinding)
    assert rb.kind == "cuda"
    # The MLX binding's loader is the one for parakeet_mlx; the cuda
    # binding's loader is _load_parakeet_hf (which routes to NeMo
    # internally — the underscore-hf suffix is historical and kept for
    # registry stability).
    assert rb.loader.__name__ == "_load_parakeet_hf"


def test_resolve_returns_correct_loader_for_parakeet_on_mlx():
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    rb = REGISTRY.resolve("parakeet-tdt-0.6b-v3", preference="auto")
    assert rb.kind == "mlx"
    assert rb.loader.__name__ == "_load_parakeet_mlx"


def test_resolve_nb_whisper_with_mlx_preference_raises():
    """User picks MLX but NB-Whisper has no MLX binding → clear error."""
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    with pytest.raises(RuntimeError, match="doesn't support backend"):
        REGISTRY.resolve("nb-whisper-medium", preference="mlx")


def test_resolve_nb_whisper_with_auto_skips_mlx_and_picks_cpu():
    """Back-compat with ADR-0001 §4: 'auto' on an Apple Silicon mac
    where MLX is available but the model has no MLX binding should
    silently fall through to the next kind (CPU here), not raise."""
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    rb = REGISTRY.resolve("nb-whisper-medium", preference="auto")
    assert rb.kind == "cpu"
    assert rb.loader.__name__ == "_load_faster_whisper"


def test_resolve_auto_with_no_compatible_backend_raises():
    """A model whose only binding is MLX, on a machine without MLX,
    should raise — `auto` doesn't manufacture support out of nothing."""
    set_available_backends_for_testing(frozenset({"cpu"}))
    # Build a fresh registry with an MLX-only entry to avoid mutating REGISTRY.
    entry = ModelEntry(
        model_id="mlx-only-model",
        family="whisper",
        display_name="mlx only",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(BackendBinding(kinds=frozenset({"mlx"}), loader=lambda *_: None),),  # type: ignore[arg-type]
    )
    reg = TranscriberRegistry((entry,))
    with pytest.raises(RuntimeError, match="no backend that runs on this"):
        reg.resolve("mlx-only-model", preference="auto")


def test_resolve_unknown_model_raises_key_error():
    with pytest.raises(KeyError, match="not in registry"):
        REGISTRY.resolve("definitely-not-a-real-model", preference="auto")


# ── JSON serialisation shape (for /api/models) ───────────────────────────


def test_to_mapping_serialises_inputs_with_discriminator():
    """SelectInput and TextInput both render to a JSON-friendly dict with
    a `type` field so the UI can dispatch which form widget to render."""
    canary = REGISTRY.require("canary-1b-v2").to_mapping()
    by_name = {i["name"]: i for i in canary["inputs"]}
    assert by_name["source_lang"]["type"] == "select"
    assert by_name["target_lang"]["type"] == "select"
    # Options serialise as list-of-dicts.
    assert {"value": "en", "label": "English"} in by_name["source_lang"]["options"]

    whisper = REGISTRY.require("small.en").to_mapping()
    by_name = {i["name"]: i for i in whisper["inputs"]}
    assert by_name["initial_prompt"]["type"] == "text"
    assert by_name["initial_prompt"]["kind"] == "textarea"
    assert by_name["hotwords"]["type"] == "text"
    assert by_name["hotwords"]["kind"] == "text"


def test_to_mapping_emits_sorted_backends_and_contexts():
    """Deterministic JSON keys so dashboard snapshots don't flap."""
    pk = REGISTRY.require("parakeet-tdt-0.6b-v3").to_mapping()
    assert pk["backends"] == sorted(pk["backends"])
    assert pk["contexts"] == sorted(pk["contexts"])


# ── Construction-time invariants ─────────────────────────────────────────


def test_duplicate_model_id_in_constructor_raises():
    a = ModelEntry(
        model_id="dup",
        family="whisper",
        display_name="x",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(BackendBinding(kinds=frozenset({"cpu"}), loader=lambda *_: None),),  # type: ignore[arg-type]
    )
    b = ModelEntry(
        model_id="dup",
        family="whisper",
        display_name="y",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(BackendBinding(kinds=frozenset({"cpu"}), loader=lambda *_: None),),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="duplicate"):
        TranscriberRegistry((a, b))


def test_unavailable_entry_resolves_with_actionable_error():
    """available=False entries should refuse resolution with a message
    pointing to 'pick another model', not crash deep in an importer."""
    entry = ModelEntry(
        model_id="future-model",
        family="whisper",
        display_name="future",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(BackendBinding(kinds=frozenset({"cpu"}), loader=lambda *_: None),),  # type: ignore[arg-type]
        available=False,
    )
    reg = TranscriberRegistry((entry,))
    with pytest.raises(RuntimeError, match="not available yet"):
        reg.resolve("future-model", preference="auto")


# ── Install-probe filter (drives /api/models `only_installed=True`) ────


def test_binding_with_empty_probe_module_is_installed():
    """`probe_module=""` opts a binding out of probing — used by tests
    that construct hypothetical bindings whose loader is a no-op lambda.
    The is_installed() default for those is True."""
    b = BackendBinding(kinds=frozenset({"cpu"}), loader=lambda *_: None)  # type: ignore[arg-type]
    assert b.probe_module == ""
    assert b.is_installed() is True


def test_binding_is_installed_reflects_probe_module_override():
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    b_present = BackendBinding(
        kinds=frozenset({"cpu"}),
        loader=lambda *_: None,  # type: ignore[arg-type]
        probe_module="fake_present",
    )
    b_absent = BackendBinding(
        kinds=frozenset({"cpu"}),
        loader=lambda *_: None,  # type: ignore[arg-type]
        probe_module="fake_absent",
    )
    set_installed_modules_for_testing(frozenset({"fake_present"}))
    try:
        assert b_present.is_installed() is True
        assert b_absent.is_installed() is False
    finally:
        set_installed_modules_for_testing(None)


def test_entry_is_installed_requires_kind_overlap_with_available_backends():
    """A binding whose adapter is importable but whose kinds aren't
    available on this machine should NOT make the entry count as
    installed — the dashboard would advertise a model the operator
    can't actually run."""
    set_available_backends_for_testing(frozenset({"cpu"}))  # no MLX
    set_installed_modules_for_testing(frozenset({"fake_mlx_adapter"}))
    entry = ModelEntry(
        model_id="mlx-only",
        family="whisper",
        display_name="x",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(
            BackendBinding(
                kinds=frozenset({"mlx"}),
                loader=lambda *_: None,  # type: ignore[arg-type]
                probe_module="fake_mlx_adapter",
            ),
        ),
    )
    try:
        assert entry.is_installed() is False
    finally:
        set_installed_modules_for_testing(None)


def test_entry_is_installed_true_when_at_least_one_binding_matches():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset({"fake_cpu_adapter"}))
    entry = ModelEntry(
        model_id="two-bindings",
        family="whisper",
        display_name="x",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(
            BackendBinding(
                kinds=frozenset({"mlx"}),
                loader=lambda *_: None,  # type: ignore[arg-type]
                probe_module="fake_mlx_only",  # not in the installed set
            ),
            BackendBinding(
                kinds=frozenset({"cpu", "cuda"}),
                loader=lambda *_: None,  # type: ignore[arg-type]
                probe_module="fake_cpu_adapter",  # installed AND cpu is available
            ),
        ),
    )
    try:
        assert entry.is_installed() is True
    finally:
        set_installed_modules_for_testing(None)


def test_unavailable_entries_are_never_reported_installed():
    """`available=False` placeholders ("coming soon") are filtered out
    of /api/models even if their adapter happens to be importable."""
    entry = ModelEntry(
        model_id="future",
        family="whisper",
        display_name="x",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(
            BackendBinding(
                kinds=frozenset({"cpu"}),
                loader=lambda *_: None,  # type: ignore[arg-type]
                probe_module="",  # always-installed sentinel
            ),
        ),
        available=False,
    )
    assert entry.is_installed() is False


def test_for_context_only_installed_filters_entries():
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset({"installed_adapter"}))
    installed_entry = ModelEntry(
        model_id="have-it",
        family="whisper",
        display_name="x",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(
            BackendBinding(
                kinds=frozenset({"cpu"}),
                loader=lambda *_: None,  # type: ignore[arg-type]
                probe_module="installed_adapter",
            ),
        ),
    )
    missing_entry = ModelEntry(
        model_id="missing",
        family="whisper",
        display_name="y",
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=(
            BackendBinding(
                kinds=frozenset({"cpu"}),
                loader=lambda *_: None,  # type: ignore[arg-type]
                probe_module="not_installed",
            ),
        ),
    )
    reg = TranscriberRegistry((installed_entry, missing_entry))
    try:
        unfiltered = {e.model_id for e in reg.for_context("batch")}
        assert unfiltered == {"have-it", "missing"}
        filtered = {e.model_id for e in reg.for_context("batch", only_installed=True)}
        assert filtered == {"have-it"}
    finally:
        set_installed_modules_for_testing(None)


def test_textinput_label_and_description_round_trip():
    inp = TextInput(name="x", label="X", placeholder="p", description="d")
    assert inp.to_mapping() == {
        "type": "text",
        "name": "x",
        "label": "X",
        "kind": "text",
        "placeholder": "p",
        "description": "d",
    }
