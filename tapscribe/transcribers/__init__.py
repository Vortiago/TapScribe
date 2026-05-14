"""Transcribers — the stateful adapters that turn one WAV into text.

A `Transcriber` instance is one loaded model (faster-whisper / mlx-whisper /
Voxtral) holding its own model object, model name, and device label. The
factory `load_transcriber(name, *, use_mlx)` lazily imports the right
adapter module and caches by `(model_name, use_mlx)`.

The protocol-level contract is policy-free: callers resolve prompt /
hotwords / hallucination rules and pass them in. Post-processing (notably
the hallucination filter) composes on top of `transcribe()` via pure
functions — see `tapscribe.hallucinations.apply`.
"""

from __future__ import annotations

import asyncio

from ..models import is_voxtral
from .base import (
    Transcriber,
    TranscriptionResult,
    TranscriptionSegment,
    Word,
)

__all__ = [
    "Transcriber",
    "TranscriptionResult",
    "TranscriptionSegment",
    "Word",
    "load_transcriber",
    "clear_cache",
]


# Cache keyed by (model_name, use_mlx). Multi-language sessions hold several
# entries — `nb-whisper-medium` for Norwegian, a Voxtral or large-v3 for
# other speakers — without double-loading shared models.
_cache: dict[tuple[str, bool], Transcriber] = {}
_cache_lock = asyncio.Lock()


def load_transcriber(model_name: str, *, use_mlx: bool) -> Transcriber:
    """Return a cached stateful `Transcriber` for `model_name`.

    Routing rules:
      voxtral-*     → VoxtralTranscriber
      nb-whisper-*  → FasterWhisperTranscriber (NB-Whisper has no public
                      MLX weights; use_mlx is ignored for this prefix)
      anything else → MlxWhisperTranscriber if use_mlx else
                      FasterWhisperTranscriber

    Heavy adapter modules are imported lazily so booting TapScribe never
    pulls in PyTorch unless Voxtral is actually requested.
    """
    key = (model_name, use_mlx)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    transcriber = _build_transcriber(model_name, use_mlx=use_mlx)
    _cache[key] = transcriber
    return transcriber


def _build_transcriber(model_name: str, *, use_mlx: bool) -> Transcriber:
    if is_voxtral(model_name):
        from .voxtral import VoxtralTranscriber
        return VoxtralTranscriber.load(model_name)

    if model_name.startswith("nb-whisper-"):
        # No public MLX weights for NB-Whisper — fall through to faster-whisper
        # regardless of the operator's MLX preference.
        from .faster_whisper import FasterWhisperTranscriber
        return FasterWhisperTranscriber.load(model_name)

    if use_mlx:
        from .mlx_whisper import MlxWhisperTranscriber
        return MlxWhisperTranscriber.load(model_name)

    from .faster_whisper import FasterWhisperTranscriber
    return FasterWhisperTranscriber.load(model_name)


def clear_cache() -> None:
    """Drop all cached transcribers. Mostly for tests; also useful when an
    operator flips MLX at runtime and wants old MLX-loaded models GC'd."""
    _cache.clear()
