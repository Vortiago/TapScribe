"""Tests for MlxMoonshineEngine — the MLX inference adapter behind
MoonshineLiveChannel on Apple Silicon.

No real mlx-audio weights ever load here (Apple Silicon only, large
download): the loaded-model object is injected, mirroring the pattern in
test_transcribers_mlx_parakeet.py.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import numpy as np
import pytest


def test_generate_returns_stt_output_text():
    """mlx-audio's `Model.generate(array) -> STTOutput` exposes the
    decoded text on `.text` — the engine unwraps that for the rolling
    window's `generate_fn` seam."""
    from tapscribe.transcribers.moonshine_mlx import MlxMoonshineEngine

    model = MagicMock()
    model.generate.return_value = types.SimpleNamespace(text="hello there")
    engine = MlxMoonshineEngine("moonshine-tiny", model=model)

    audio = np.zeros(16000, dtype=np.float32)
    text = engine.generate(audio)

    assert text == "hello there"
    model.generate.assert_called_once()
    called_audio = model.generate.call_args.args[0]
    assert called_audio is audio


def test_generate_strips_whitespace_free_form_result():
    """If `generate` ever returns a bare string (not an STTOutput-shaped
    object), the engine falls back to str() rather than crashing on a
    missing `.text` attribute."""
    from tapscribe.transcribers.moonshine_mlx import MlxMoonshineEngine

    model = MagicMock()
    model.generate.return_value = "plain string result"
    engine = MlxMoonshineEngine("moonshine-tiny", model=model)

    assert engine.generate(np.zeros(1600, dtype=np.float32)) == "plain string result"


def test_load_rejects_unknown_model_id():
    from tapscribe.transcribers.moonshine_mlx import MlxMoonshineEngine

    with pytest.raises(ValueError, match="moonshine-large"):
        MlxMoonshineEngine.load("moonshine-large")


def test_load_fails_fast_when_mlx_audio_missing(monkeypatch):
    """Same actionable-error convention as the other MLX adapters when
    the optional dependency isn't installed."""
    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "mlx_audio":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake_find_spec)

    from tapscribe.transcribers.moonshine_mlx import MlxMoonshineEngine

    with pytest.raises(RuntimeError, match="mlx-audio"):
        MlxMoonshineEngine.load("moonshine-tiny")


def test_mlx_audio_moonshine_upstream_contract():
    """If mlx-audio is installed, its Moonshine load/generate entry
    points must exist with the expected shape — an upstream rename fails
    here (CI, on the dependency-bump PR) rather than in production. Self-
    skips off macOS-arm64 / without the package, per CLAUDE.md's
    importorskip convention for MLX adapters."""
    import inspect

    pytest.importorskip("mlx_audio")
    from mlx_audio.stt import load  # type: ignore[import-not-found]

    assert callable(load), "mlx_audio.stt.load is the entry point the adapter calls"
    sig = inspect.signature(load)
    assert "model_path" in sig.parameters, (
        f"mlx_audio.stt.load signature changed; expected model_path, saw {sorted(sig.parameters)}"
    )
