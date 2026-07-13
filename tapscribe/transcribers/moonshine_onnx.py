"""ONNX-CPU Moonshine engine — the live-channel inference adapter for
Linux/Windows/no-GPU hosts (see PRD #120 and
`tapscribe.moonshine_live.MoonshineLiveChannel`).

Uses `useful-moonshine-onnx` (top-level module `moonshine_onnx`): a
`MoonshineOnnxModel(model_name=...)` runs the encoder/decoder ONNX graphs
directly (`generate(audio) -> [[token_ids]]`, batch shape `[1,
num_samples]`), decoded via the bundled BPE tokenizer's `decode_batch`.
Confirmed against the installed source (there is no path-based fallback
here — this module never touches the package's file-path `transcribe()`
convenience wrapper): `MoonshineOnnxModel.generate` documents `audio` as
"a numpy array of shape [1, num_audio_samples]", so `MoonshineWindow`'s
1-D array only needs a batch dimension added, no ffmpeg/decode step.

Model-name quirk: the HF repo's ONNX weights are laid out under
`onnx/merged/tiny/…` and `onnx/merged/base/…` — `MoonshineOnnxModel` wants
the BARE `"tiny"`/`"base"` name, not TapScribe's catalog `model_id`
(`"moonshine-tiny"`/`"moonshine-base"`). `_UPSTREAM_MODEL_NAMES` maps
between them.

This is deliberately NOT a `Transcriber` — Moonshine has no batch adapter
(see PRD #120 "Out of Scope"). It exposes the narrow `generate(audio) ->
str` shape `MoonshineWindow`'s injected `generate_fn` needs.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np

_UPSTREAM_MODEL_NAMES: dict[str, str] = {
    "moonshine-tiny": "tiny",
    "moonshine-base": "base",
}


class OnnxMoonshineEngine:
    """Wraps an already-constructed `MoonshineOnnxModel` + tokenizer.
    Tests inject both directly so real ONNX weights never load."""

    def __init__(self, model_id: str, *, model: Any, tokenizer: Any) -> None:
        self._model_id = model_id
        self._model = model
        self._tokenizer = tokenizer

    def generate(self, audio: np.ndarray) -> str:
        """`MoonshineWindow`'s `generate_fn` seam: a 1-D float32 16 kHz
        array in, decoded text out. `MoonshineOnnxModel.generate` wants a
        `[1, num_samples]` batch and returns a batch of token-id lists;
        `decode_batch` turns that into a batch of strings, of which we
        return the sole (batch size 1) entry."""
        batched = audio[None, :].astype(np.float32, copy=False)
        tokens = self._model.generate(batched)
        texts = self._tokenizer.decode_batch(tokens)
        return texts[0] if texts else ""

    @classmethod
    def load(cls, model_id: str) -> OnnxMoonshineEngine:
        """Load Moonshine via `useful-moonshine-onnx`. Raises a clear,
        actionable `RuntimeError` if the optional dependency isn't
        installed — same convention as every other adapter."""
        upstream_name = _UPSTREAM_MODEL_NAMES.get(model_id)
        if upstream_name is None:
            raise ValueError(
                f"{model_id!r} is not a known Moonshine model. Known: {sorted(_UPSTREAM_MODEL_NAMES)!r}"
            )
        if importlib.util.find_spec("moonshine_onnx") is None:
            raise RuntimeError(
                "useful-moonshine-onnx is not installed. "
                "Install `pip install tapscribe[moonshine-cpu]`."
            )
        import moonshine_onnx

        model = moonshine_onnx.MoonshineOnnxModel(model_name=upstream_name)
        tokenizer = moonshine_onnx.load_tokenizer()
        return cls(model_id, model=model, tokenizer=tokenizer)
