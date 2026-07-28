"""MLX Moonshine engine — the live-channel inference adapter for Apple
Silicon (see PRD #120 and `tapscribe.moonshine_live.MoonshineLiveChannel`).

Uses `mlx-audio`'s Moonshine port (the same library this repo already uses
for Canary): `from mlx_audio.stt import load` + `model.generate(array) ->
STTOutput`. The array path is ffmpeg-free by construction — `generate()`
only shells out to `mlx_audio.audio_io` when handed a path/str; handing it
a numpy/mx.array directly (as `MoonshineWindow` always does) skips that
entirely, consistent with the rest of the MLX adapters' no-ffmpeg
convention (`tapscribe/wav_predecode.py`).

This is deliberately NOT a `Transcriber` — Moonshine has no batch adapter
(see PRD #120 "Out of Scope"). It exposes the narrow `generate(audio) ->
str` shape `MoonshineWindow`'s injected `generate_fn` needs.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np

# HF repo ids mlx-audio's `load()` resolves — same catalog `model_id`s
# TapScribe already uses everywhere else, just qualified with the
# publishing org (see the mlx-audio Moonshine README).
_MODEL_REPOS: dict[str, str] = {
    "moonshine-tiny": "UsefulSensors/moonshine-tiny",
    "moonshine-base": "UsefulSensors/moonshine-base",
}


class MlxMoonshineEngine:
    """Wraps an already-loaded mlx-audio Moonshine model. Tests inject
    `model` directly (a stub with a `.generate` attribute) so real
    weights never load in the suite."""

    def __init__(self, model_id: str, *, model: Any) -> None:
        self._model_id = model_id
        self._model = model

    def generate(self, audio: np.ndarray) -> str:
        """`MoonshineWindow`'s `generate_fn` seam: a 1-D float32 16 kHz
        array in, decoded text out. `mlx_audio`'s Model.generate returns
        an `STTOutput` dataclass whose `.text` holds the string; fall
        back to `str(result)` if a future version ever returns something
        else, rather than raising on a missing attribute mid-session."""
        result = self._model.generate(audio)
        text = getattr(result, "text", None)
        return text if text is not None else str(result)

    @classmethod
    def load(cls, model_id: str) -> MlxMoonshineEngine:
        """Load Moonshine via mlx-audio. Raises a clear, actionable
        `RuntimeError` if the optional dependency isn't installed —
        same convention as every other MLX adapter."""
        # Registry-first per #206, module mapping as the convention fallback.
        from . import catalog

        repo = catalog.resolve_repo(model_id, "moonshine-mlx", lambda m: _MODEL_REPOS.get(m))
        if repo is None:
            raise ValueError(f"{model_id!r} is not a known Moonshine model. Known: {sorted(_MODEL_REPOS)!r}")
        if importlib.util.find_spec("mlx_audio") is None:
            raise RuntimeError("mlx-audio is not installed. Install `pip install tapscribe[moonshine-mlx]`.")
        from mlx_audio.stt import load as mlx_audio_load

        model = mlx_audio_load(repo)
        return cls(model_id, model=model)
