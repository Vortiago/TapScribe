"""Loaders for the committed diarizer reference vectors.

See PROVENANCE.md for how `reference.npz` was generated and what settings it
pins.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_AUDIO = _HERE.parent / "audio"


def load_reference() -> dict[str, np.ndarray]:
    """`{"fbank__<name>": (100, 80), "emb__<name>": (512,)}` per audio fixture."""
    with np.load(_HERE / "reference.npz") as data:
        return {k: data[k] for k in data.files}


def read_fixture_wav(name: str) -> np.ndarray:
    """One audio fixture as float32 in [-1, 1) — the scaling the embedding
    model's `normalize_samples=1` expects."""
    with wave.open(str(_AUDIO / f"{name}.wav")) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0
