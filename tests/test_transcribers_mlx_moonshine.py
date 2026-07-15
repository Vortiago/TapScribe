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
    here (the macos-arm64 `upstream-contract` CI lane, on the
    dependency-bump PR) rather than in production. Self-skips without
    the package (Linux/Windows), per CLAUDE.md's importorskip
    convention for MLX adapters.

    Pins BOTH halves the adapter depends on: `mlx_audio.stt.load(repo)`
    AND `Model.generate(audio) -> STTOutput.text` (verified against
    upstream v0.4.1, the bottom of the pyproject pin range) — the
    original smoke pinned only `load`, so a `generate` rename or an
    STTOutput field change would have shipped silently (the adapter's
    `str(result)` fallback would turn it into garbage captions, not a
    crash). The deep module path (`stt.models.moonshine.moonshine`) is
    DELIBERATELY part of the pinned contract: an upstream file reorg
    should red the bump-PR lane for a human look, not slide through."""
    import inspect

    pytest.importorskip("mlx_audio")
    from mlx_audio.stt import load  # type: ignore[import-not-found]

    assert callable(load), "mlx_audio.stt.load is the entry point the adapter calls"
    sig = inspect.signature(load)
    assert "model_path" in sig.parameters, (
        f"mlx_audio.stt.load signature changed; expected model_path, saw {sorted(sig.parameters)}"
    )

    from mlx_audio.stt.models.moonshine import moonshine as moonshine_mod  # type: ignore[import-not-found]

    model_cls = getattr(moonshine_mod, "Model", None)
    assert model_cls is not None, "mlx_audio moonshine no longer defines Model"
    gen_params = list(inspect.signature(model_cls.generate).parameters)
    assert gen_params[:2] == ["self", "audio"], (
        f"Model.generate signature changed; the adapter calls generate(audio) positionally, saw {gen_params}"
    )
    out_cls = getattr(moonshine_mod, "STTOutput", None)
    assert out_cls is not None, "moonshine module namespace no longer carries STTOutput"
    assert "text" in getattr(out_cls, "__dataclass_fields__", {}) or hasattr(out_cls, "text"), (
        "STTOutput lost the .text field the adapter unwraps"
    )
