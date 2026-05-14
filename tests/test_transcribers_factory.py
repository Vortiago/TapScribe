"""Tests for `load_transcriber` — routing + cache behavior.

We override the per-adapter `.load(model_name)` classmethods with stubs
so the test never touches real model weights. Cache behaviour is
verified through the factory's public API.
"""

from __future__ import annotations

import pytest

from tapscribe import transcribers
from tapscribe.transcribers.base import TranscriptionResult


class _StubTranscriber:
    """Minimal Transcriber that records its construction args. Used by
    every adapter stub so we can assert which one was instantiated."""

    def __init__(self, name: str, model_name: str):
        self.name = name
        self.device = "stub"
        self.model_name = model_name

    def transcribe(self, path, *, initial_prompt=None, hotwords=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber=self.name, device="stub", model=self.model_name,
            language="en", language_probability=1.0, duration=0.0,
            text="", segments=(), initial_prompt_used="", hotwords_used="",
            quality_settings={},
        )


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    """Replace every adapter's `.load()` with a stub. Also clears the
    factory cache before each test."""
    from tapscribe.transcribers import faster_whisper as fw
    from tapscribe.transcribers import mlx_whisper as mx
    from tapscribe.transcribers import voxtral as vx

    monkeypatch.setattr(
        fw.FasterWhisperTranscriber,
        "load",
        classmethod(lambda cls, name: _StubTranscriber("faster-whisper", name)),
    )
    monkeypatch.setattr(
        mx.MlxWhisperTranscriber,
        "load",
        classmethod(lambda cls, name: _StubTranscriber("mlx-whisper", name)),
    )
    monkeypatch.setattr(
        vx.VoxtralTranscriber,
        "load",
        classmethod(lambda cls, name: _StubTranscriber("voxtral", name)),
    )
    transcribers.clear_cache()
    yield
    transcribers.clear_cache()


def test_dispatches_to_faster_whisper_for_whisper_models_when_mlx_disabled():
    t = transcribers.load_transcriber("small.en", use_mlx=False)
    assert t.name == "faster-whisper"
    assert t.model_name == "small.en"


def test_dispatches_to_mlx_for_whisper_models_when_mlx_enabled():
    t = transcribers.load_transcriber("small.en", use_mlx=True)
    assert t.name == "mlx-whisper"


def test_dispatches_to_faster_whisper_for_nb_whisper_even_when_mlx_enabled():
    """NB-Whisper has no public MLX weights, so the routing rule forces
    faster-whisper regardless of the use_mlx preference."""
    t = transcribers.load_transcriber("nb-whisper-medium", use_mlx=True)
    assert t.name == "faster-whisper"
    assert t.model_name == "nb-whisper-medium"


def test_dispatches_to_voxtral_for_voxtral_models():
    t = transcribers.load_transcriber("voxtral-mini", use_mlx=False)
    assert t.name == "voxtral"


def test_caches_per_model_name_use_mlx_combo():
    """Same key returns the same instance; different keys return distinct
    instances. Multi-language sessions (e.g. nb-whisper for Norwegian + a
    different model for Danish) rely on this isolation."""
    a1 = transcribers.load_transcriber("small.en", use_mlx=False)
    a2 = transcribers.load_transcriber("small.en", use_mlx=False)
    assert a1 is a2

    b = transcribers.load_transcriber("small.en", use_mlx=True)
    assert b is not a1  # different use_mlx → different cache entry, different backend

    c = transcribers.load_transcriber("medium.en", use_mlx=False)
    assert c is not a1  # different model_name → different cache entry
