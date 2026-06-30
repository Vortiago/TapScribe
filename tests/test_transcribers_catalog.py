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
    assert families >= {"whisper", "nb-whisper", "voxtral", "parakeet"}


def test_every_default_entry_has_at_least_one_backend_binding():
    for e in REGISTRY.entries():
        assert e.backends, f"entry {e.model_id} has no backend bindings"
        assert e.supported_backend_kinds(), f"entry {e.model_id} declares zero kinds"


def test_for_context_batch_includes_parakeet():
    batch_ids = {e.model_id for e in REGISTRY.for_context("batch")}
    assert "parakeet-tdt-0.6b-v3" in batch_ids


def test_for_context_live_excludes_parakeet():
    """Parakeet has no true-streaming checkpoint and WhisperLiveKit has no
    Parakeet backend — so it's batch-only until a follow-up PR adds a
    pseudo-streaming live channel."""
    live_ids = {e.model_id for e in REGISTRY.for_context("live")}
    assert "parakeet-tdt-0.6b-v3" not in live_ids


def test_for_context_batch_includes_voxtral():
    batch_ids = {e.model_id for e in REGISTRY.for_context("batch")}
    assert "voxtral-mini" in batch_ids


def test_for_context_live_excludes_voxtral():
    """Voxtral is batch-only: `build_live_cmd` only spawns whisperlivekit-server
    with a faster-whisper / mlx-whisper backend (+ the NB-Whisper model-path
    route) and has no Voxtral backend, so a live selection couldn't launch. The
    live picker must not offer it until a Voxtral live channel is wired."""
    live_ids = {e.model_id for e in REGISTRY.for_context("live")}
    assert "voxtral-mini" not in live_ids


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


def test_nb_whisper_excludes_mlx_binding():
    """NB-Whisper has no public MLX weights, so its only binding is the
    faster-whisper one (which handles cpu + cuda)."""
    nb = REGISTRY.require("nb-whisper-medium")
    assert "mlx" not in nb.supported_backend_kinds()
    assert {"cpu", "cuda"} <= nb.supported_backend_kinds()


def test_parakeet_supports_mlx_cuda_and_cpu():
    pk = REGISTRY.require("parakeet-tdt-0.6b-v3")
    assert pk.supported_backend_kinds() == frozenset({"mlx", "cuda", "cpu"})


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
    # binding's loader is _load_parakeet_hf (the transformers CUDA/CPU
    # adapter).
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


def test_to_mapping_serialises_text_inputs_with_discriminator():
    """TextInput renders to a JSON-friendly dict with a `type` field so the
    UI can dispatch which form widget to render."""
    whisper = REGISTRY.require("small.en").to_mapping()
    by_name = {i["name"]: i for i in whisper["inputs"]}
    assert by_name["initial_prompt"]["type"] == "text"
    assert by_name["initial_prompt"]["kind"] == "textarea"
    assert by_name["hotwords"]["type"] == "text"
    assert by_name["hotwords"]["kind"] == "text"


def test_select_input_serialises_with_discriminator():
    """No registry model declares a SelectInput today (Canary's source/
    target selects were removed with the family), but the type is kept for
    future use — pin its serialisation shape directly."""
    sel = SelectInput(
        name="source_lang",
        label="Source language",
        options=(("en", "English"), ("de", "German")),
        default="en",
    )
    out = sel.to_mapping()
    assert out["type"] == "select"
    assert out["default"] == "en"
    assert {"value": "en", "label": "English"} in out["options"]


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


# ── Moonshine (live-only) — issue #121 ───────────────────────────────────────
# Contract for the new live family: catalog registration + availability +
# /api/models surfacing (no inference wiring yet — see #122/#123). These pin the
# acceptance criteria; the route-level surfacing is asserted in test_routes.py.

_MOONSHINE_IDS = ("moonshine-tiny", "moonshine-base")


def _moonshine_entries():
    return tuple(e for e in REGISTRY.entries() if e.family == "moonshine")


def test_moonshine_family_registered_with_both_models():
    assert {e.model_id for e in _moonshine_entries()} == set(_MOONSHINE_IDS)


def test_moonshine_entry_metadata_is_english_live_no_inputs():
    entries = _moonshine_entries()
    assert entries  # registered at all
    for e in entries:
        assert e.languages == ("en",)
        assert e.contexts == frozenset({"live"})
        assert e.inputs == ()
        assert "english" in e.description.lower()


def test_moonshine_listed_for_live_context():
    live_ids = {e.model_id for e in REGISTRY.for_context("live")}
    assert set(_MOONSHINE_IDS) <= live_ids


def test_moonshine_excluded_from_batch_context():
    batch_ids = {e.model_id for e in REGISTRY.for_context("batch")}
    assert batch_ids.isdisjoint(_MOONSHINE_IDS)


def test_moonshine_backends_cover_mlx_and_cpu_cuda_with_probes():
    for e in _moonshine_entries():
        kindsets = {b.kinds for b in e.backends}
        assert frozenset({"mlx"}) in kindsets
        assert frozenset({"cuda", "cpu"}) in kindsets
        assert all(b.probe_module for b in e.backends)  # each declares a probe module


def test_moonshine_resolve_auto_prefers_mlx_when_available():
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    rb = REGISTRY.resolve("moonshine-tiny", preference="auto")
    assert isinstance(rb, ResolvedBinding)
    assert rb.kind == "mlx"


def test_moonshine_resolve_auto_falls_back_to_cpu():
    set_available_backends_for_testing(frozenset({"cpu"}))
    rb = REGISTRY.resolve("moonshine-base", preference="auto")
    assert rb.kind == "cpu"


def test_moonshine_explicit_unavailable_backend_raises_runtimeerror():
    set_available_backends_for_testing(frozenset({"cpu"}))
    with pytest.raises(RuntimeError, match="mlx"):
        REGISTRY.resolve("moonshine-tiny", preference="mlx")


def test_moonshine_install_probe_gates_is_installed():
    set_available_backends_for_testing(frozenset({"cpu"}))
    probes = frozenset(b.probe_module for e in _moonshine_entries() for b in e.backends if b.probe_module)
    assert probes
    set_installed_modules_for_testing(frozenset())  # nothing importable
    try:
        assert all(not e.is_installed() for e in _moonshine_entries())
        set_installed_modules_for_testing(probes)  # all importable
        assert all(e.is_installed() for e in _moonshine_entries())
    finally:
        set_installed_modules_for_testing(None)


def test_unknown_model_id_rejected_before_loader():
    with pytest.raises((KeyError, RuntimeError)):
        REGISTRY.resolve("not-a-real-moonshine", preference="auto")


# ---------------------------------------------------------------------------
# Candidate languages (ADR-0010) — the catalog's language vocabulary is the
# allowlist a per-meeting candidate set validates against.
# ---------------------------------------------------------------------------


def test_candidate_language_codes_cover_the_default_set_without_auto():
    from tapscribe.transcribers.catalog import candidate_language_codes

    codes = candidate_language_codes()
    # The motivating languages are all selectable…
    assert {"da", "no", "en"}.issubset(set(codes))
    # …but the auto-detect sentinel is NOT a language the operator can pick.
    assert "auto" not in codes
    # Default set leads the option list (so the picker shows them first).
    assert codes[:3] == ("da", "no", "en")
    # No duplicates even though several models declare overlapping languages.
    assert len(codes) == len(set(codes))


def test_norwegian_and_danish_have_distinct_display_names():
    from tapscribe.transcribers.catalog import language_display_name

    assert language_display_name("no") == "Norwegian"
    assert language_display_name("da") == "Danish"
    # Unknown code falls back to itself rather than raising.
    assert language_display_name("zz") == "zz"


def test_is_candidate_language_matches_membership():
    from tapscribe.transcribers.catalog import is_candidate_language

    assert is_candidate_language("da")
    assert not is_candidate_language("auto")
    assert not is_candidate_language("xx")
