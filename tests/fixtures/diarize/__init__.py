"""Loaders for the committed diarizer reference vectors.

See PROVENANCE.md for how `reference.npz` was generated and what settings it
pins.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from tapscribe.wav_predecode import load_recorder_wav_as_pcm

_HERE = Path(__file__).resolve().parent
_AUDIO = _HERE.parent / "audio"


def load_reference() -> dict[str, np.ndarray]:
    """`{"fbank__<name>": (100, 80), "emb__<name>": (512,)}` per audio fixture."""
    with np.load(_HERE / "reference.npz") as data:
        return {k: data[k] for k in data.files}


def read_fixture_wav(name: str) -> np.ndarray:
    """One audio fixture as float32 in [-1, 1) — the scaling the embedding
    model's `normalize_samples=1` expects.

    Through the repo's one decode path, so a fixture re-encoded at the wrong
    rate raises instead of silently producing garbage fbank.
    """
    return load_recorder_wav_as_pcm(_AUDIO / f"{name}.wav")
