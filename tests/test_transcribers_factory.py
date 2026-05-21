"""Tests for `load_transcriber` — registry-driven routing + cache behaviour.

We override the loader thunks in the catalog with stubs so the test
never touches real model weights. Cache behaviour is verified through
the factory's public API.
"""

from __future__ import annotations

import pytest

from tapscribe import transcribers
from tapscribe.transcribers import catalog
from tapscribe.transcribers.base import TranscriptionResult


class _StubTranscriber:
    """Minimal Transcriber that records its construction args. Used by
    every adapter stub so tests can assert which loader was called."""

    def __init__(self, name: str, model_name: str, kind: str):
        self.name = name
        self.backend = f"stub-{name}-{kind}"
        self.device = kind.upper()
        self.model_name = model_name

    def transcribe(self, path, **_kwargs):  # noqa: ARG002 — protocol parity
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=0.0,
            text="",
            segments=(),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


@pytest.fixture(autouse=True)
def _stub_loaders_and_backends(monkeypatch):
    """Replace each loader thunk in catalog with a stub, and force the
    available-backends set to {mlx, cuda, cpu} so 'auto' resolution and
    explicit preferences both have something to pick. Cache is cleared
    before and after each test."""
    monkeypatch.setattr(
        catalog,
        "_load_faster_whisper",
        lambda mid, kind: _StubTranscriber("faster-whisper", mid, kind),
    )
    monkeypatch.setattr(
        catalog,
        "_load_mlx_whisper",
        lambda mid, kind: _StubTranscriber("mlx-whisper", mid, kind),
    )
    monkeypatch.setattr(
        catalog,
        "_load_voxtral_hf",
        lambda mid, kind: _StubTranscriber("voxtral-hf", mid, kind),
    )
    monkeypatch.setattr(
        catalog,
        "_load_voxtral_mlx",
        lambda mid, kind: _StubTranscriber("voxtral-mlx", mid, kind),
    )
    monkeypatch.setattr(
        catalog,
        "_load_parakeet_mlx",
        lambda mid, kind: _StubTranscriber("parakeet-mlx", mid, kind),
    )
    monkeypatch.setattr(
        catalog,
        "_load_parakeet_hf",
        lambda mid, kind: _StubTranscriber("parakeet-nemo", mid, kind),
    )
    monkeypatch.setattr(
        catalog,
        "_load_canary_nemo",
        lambda mid, kind: _StubTranscriber("canary-nemo", mid, kind),
    )

    # The default REGISTRY captures the *original* loader functions
    # by reference at module-import time, so monkeypatching the module
    # attribute doesn't reach inside the existing entries. Rebuild a
    # registry that picks up the patched loaders.
    monkeypatch.setattr(transcribers, "REGISTRY", _rebuild_registry(monkeypatch))

    catalog.set_available_backends_for_testing(frozenset({"mlx", "cuda", "cpu"}))
    transcribers.clear_cache()
    yield
    transcribers.clear_cache()
    catalog.set_available_backends_for_testing(None)


def _rebuild_registry(monkeypatch):
    """Construct a registry whose entries reference the *current*
    (already-monkeypatched) loader thunks. The loader-thunk patches above
    must be applied first; this helper just wires them in.

    Builds straight from the same family bindings the production catalog
    uses, picking up the live function references."""
    from tapscribe.transcribers.catalog import (
        _CANARY_LANG_CODES,
        _PARAKEET_LANG_CODES,
        CANARY_INPUTS,
        NO_INPUTS,
        WHISPER_INPUTS,
        BackendBinding,
        ModelEntry,
        TranscriberRegistry,
    )

    whisper_backends = (
        BackendBinding(kinds=frozenset({"mlx"}), loader=catalog._load_mlx_whisper),
        BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=catalog._load_faster_whisper),
    )
    nb_backends = (BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=catalog._load_faster_whisper),)
    voxtral_backends = (
        BackendBinding(kinds=frozenset({"mlx"}), loader=catalog._load_voxtral_mlx),
        BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=catalog._load_voxtral_hf),
    )
    parakeet_backends = (
        BackendBinding(kinds=frozenset({"mlx"}), loader=catalog._load_parakeet_mlx),
        BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=catalog._load_parakeet_hf),
    )
    canary_backends = (
        BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=catalog._load_canary_nemo),
    )
    both = frozenset({"batch", "live"})
    batch_only = frozenset({"batch"})
    entries = (
        ModelEntry(
            model_id="small.en",
            family="whisper",
            display_name="small.en",
            description="",
            languages=("en",),
            contexts=both,
            backends=whisper_backends,
            inputs=WHISPER_INPUTS,
        ),
        ModelEntry(
            model_id="medium.en",
            family="whisper",
            display_name="medium.en",
            description="",
            languages=("en",),
            contexts=both,
            backends=whisper_backends,
            inputs=WHISPER_INPUTS,
        ),
        ModelEntry(
            model_id="nb-whisper-medium",
            family="nb-whisper",
            display_name="nb-whisper-medium",
            description="",
            languages=("no",),
            contexts=both,
            backends=nb_backends,
            inputs=WHISPER_INPUTS,
        ),
        ModelEntry(
            model_id="voxtral-mini",
            family="voxtral",
            display_name="voxtral-mini",
            description="",
            languages=("en",),
            contexts=both,
            backends=voxtral_backends,
            inputs=NO_INPUTS,
        ),
        ModelEntry(
            model_id="parakeet-tdt-0.6b-v3",
            family="parakeet",
            display_name="parakeet-tdt-0.6b-v3",
            description="",
            languages=_PARAKEET_LANG_CODES,
            contexts=batch_only,
            backends=parakeet_backends,
            inputs=NO_INPUTS,
        ),
        ModelEntry(
            model_id="canary-1b-v2",
            family="canary",
            display_name="canary-1b-v2",
            description="",
            languages=_CANARY_LANG_CODES,
            contexts=batch_only,
            backends=canary_backends,
            inputs=CANARY_INPUTS,
        ),
    )
    fresh = TranscriberRegistry(entries)
    monkeypatch.setattr(catalog, "REGISTRY", fresh)
    return fresh


def _load(model_id: str, **kwargs):
    """Helper: load through the patched registry."""
    return transcribers.load_transcriber(model_id, registry=catalog.REGISTRY, **kwargs)


def test_routes_whisper_to_faster_whisper_on_cuda():
    t = _load("small.en", backend="cuda")
    assert t.name == "faster-whisper"
    assert t.device == "CUDA"


def test_routes_whisper_to_mlx_when_preference_mlx():
    t = _load("small.en", backend="mlx")
    assert t.name == "mlx-whisper"
    assert t.device == "MLX"


def test_routes_nb_whisper_to_faster_whisper_via_auto_when_mlx_only():
    """Back-compat with ADR-0001 §4: NB-Whisper has no MLX binding, so
    `auto` on a machine that only has MLX should fall through to CPU,
    not raise."""
    catalog.set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    t = _load("nb-whisper-medium", backend="auto")
    assert t.name == "faster-whisper"
    assert t.device == "CPU"


def test_routes_nb_whisper_to_faster_whisper_via_explicit_cuda():
    t = _load("nb-whisper-medium", backend="cuda")
    assert t.name == "faster-whisper"
    assert t.device == "CUDA"


def test_routes_nb_whisper_explicit_mlx_raises():
    """Explicit mlx for nb-whisper is the operator asking for something
    impossible — raise, don't silently swap."""
    with pytest.raises(RuntimeError, match="doesn't support backend"):
        _load("nb-whisper-medium", backend="mlx")


def test_routes_voxtral_to_hf_on_cuda():
    t = _load("voxtral-mini", backend="cuda")
    assert t.name == "voxtral-hf"


def test_routes_voxtral_to_mlx_when_mlx():
    t = _load("voxtral-mini", backend="mlx")
    assert t.name == "voxtral-mlx"


def test_routes_parakeet_to_mlx_when_mlx_preferred():
    t = _load("parakeet-tdt-0.6b-v3", backend="mlx")
    assert t.name == "parakeet-mlx"


def test_routes_parakeet_to_nemo_when_cuda_preferred():
    t = _load("parakeet-tdt-0.6b-v3", backend="cuda")
    assert t.name == "parakeet-nemo"


def test_routes_canary_to_nemo_when_cuda():
    t = _load("canary-1b-v2", backend="cuda")
    assert t.name == "canary-nemo"


def test_caches_per_model_name_resolved_kind_combo():
    """Same key returns the same instance; different keys return distinct
    instances. The cache key uses the *resolved* kind, not the preference,
    so two callers asking for the same effective backend share one model."""
    a1 = _load("small.en", backend="cpu")
    a2 = _load("small.en", backend="cpu")
    assert a1 is a2

    b = _load("small.en", backend="mlx")
    assert b is not a1

    c = _load("medium.en", backend="cpu")
    assert c is not a1
