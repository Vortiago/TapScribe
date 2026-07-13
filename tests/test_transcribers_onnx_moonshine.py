"""Tests for OnnxMoonshineEngine — the ONNX-CPU inference adapter behind
MoonshineLiveChannel on Linux/Windows/no-GPU hosts.

No real `useful-moonshine-onnx` weights ever load: the constructed
`MoonshineOnnxModel` + tokenizer are injected stubs, mirroring the
MLX-adapter test pattern. If `moonshine_onnx` happens to be importable in
this environment, the upstream-contract smoke test at the bottom also runs
for real against the installed package.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest


def _tokenizer_stub(decoded: str) -> MagicMock:
    tok = MagicMock()
    tok.decode_batch.return_value = [decoded]
    return tok


def test_generate_runs_model_and_decodes_via_tokenizer():
    """`MoonshineOnnxModel.generate(audio) -> [[token_ids]]`; the engine
    decodes the batch via the shared tokenizer and returns the first
    (only) result — audio is reshaped to the `[1, num_samples]` batch
    shape the upstream `generate()` documents."""
    from tapscribe.transcribers.moonshine_onnx import OnnxMoonshineEngine

    model = MagicMock()
    model.generate.return_value = [[1, 42, 43, 2]]
    tokenizer = _tokenizer_stub("hello there")

    engine = OnnxMoonshineEngine("moonshine-tiny", model=model, tokenizer=tokenizer)
    audio = np.zeros(16000, dtype=np.float32)
    text = engine.generate(audio)

    assert text == "hello there"
    model.generate.assert_called_once()
    called_audio = model.generate.call_args.args[0]
    assert called_audio.shape == (1, 16000)  # batch dim added
    tokenizer.decode_batch.assert_called_once_with([[1, 42, 43, 2]])


def test_load_rejects_unknown_model_id():
    from tapscribe.transcribers.moonshine_onnx import OnnxMoonshineEngine

    with pytest.raises(ValueError, match="moonshine-large"):
        OnnxMoonshineEngine.load("moonshine-large")


def test_load_strips_moonshine_prefix_for_upstream_model_name(monkeypatch):
    """`MoonshineOnnxModel`'s HF subfolder layout is `onnx/merged/tiny/…`
    / `.../base/…` — NOT `.../moonshine-tiny/…`. The engine must pass the
    bare `tiny`/`base` name upstream, not the catalog's `moonshine-`
    prefixed model_id, or the HF download 404s."""
    import types

    captured: dict[str, str] = {}

    class FakeMoonshineOnnxModel:
        def __init__(self, model_name=None):
            captured["model_name"] = model_name

    fake_module = types.SimpleNamespace(
        MoonshineOnnxModel=FakeMoonshineOnnxModel,
        load_tokenizer=lambda: _tokenizer_stub(""),
    )
    monkeypatch.setitem(__import__("sys").modules, "moonshine_onnx", fake_module)

    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec
    monkeypatch.setattr(
        importlib_util,
        "find_spec",
        lambda name, *a, **k: object() if name == "moonshine_onnx" else real_find_spec(name, *a, **k),
    )

    from tapscribe.transcribers.moonshine_onnx import OnnxMoonshineEngine

    OnnxMoonshineEngine.load("moonshine-tiny")
    assert captured["model_name"] == "tiny"


def test_load_fails_fast_when_moonshine_onnx_missing(monkeypatch):
    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "moonshine_onnx":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake_find_spec)

    from tapscribe.transcribers.moonshine_onnx import OnnxMoonshineEngine

    with pytest.raises(RuntimeError, match="useful-moonshine-onnx"):
        OnnxMoonshineEngine.load("moonshine-tiny")


def test_moonshine_onnx_upstream_contract():
    """If `useful-moonshine-onnx` is installed, its array entry point must
    still have the shape the adapter relies on: `MoonshineOnnxModel(...).
    generate(audio)` and `load_tokenizer().decode_batch(tokens)`. An
    upstream rename fails here instead of in production."""
    import inspect

    pytest.importorskip("moonshine_onnx")
    import moonshine_onnx  # type: ignore[import-not-found]

    assert hasattr(moonshine_onnx, "MoonshineOnnxModel")
    assert hasattr(moonshine_onnx, "load_tokenizer")
    sig = inspect.signature(moonshine_onnx.MoonshineOnnxModel.generate)
    assert "audio" in sig.parameters, (
        f"MoonshineOnnxModel.generate signature changed; expected audio, saw {sorted(sig.parameters)}"
    )
