"""Transcribers — the stateful adapters that turn one WAV into text.

A `Transcriber` instance is one loaded model (faster-whisper / mlx-whisper /
Voxtral / Parakeet / Canary) holding its own model object, model name,
and device label. The factory `load_transcriber(name, *, backend)`
consults the `TranscriberRegistry` (see `tapscribe.transcribers.catalog`)
to pick the right adapter, then caches by `(model_name, resolved_kind)`.

The protocol-level contract is policy-free: callers resolve prompt /
hotwords / source/target language / hallucination rules and pass them
in. Post-processing (notably the hallucination filter) composes on top
of `transcribe()` via pure functions — see `tapscribe.hallucinations.apply`.
"""

from __future__ import annotations

from .base import (
    BackendKind,
    BackendPreference,
    ModelInput,
    SelectInput,
    TextInput,
    Transcriber,
    TranscriptionResult,
    TranscriptionSegment,
    Word,
)
from .catalog import REGISTRY, TranscriberRegistry

__all__ = [
    "BackendKind",
    "BackendPreference",
    "ModelInput",
    "REGISTRY",
    "SelectInput",
    "TextInput",
    "Transcriber",
    "TranscriberRegistry",
    "TranscriptionResult",
    "TranscriptionSegment",
    "Word",
    "clear_cache",
    "load_transcriber",
]


# Cache keyed by (model_name, resolved_kind). Multi-language sessions hold
# several entries — `nb-whisper-medium` on CPU for Norwegian, `parakeet-…`
# on CUDA for English — without double-loading shared models. The cache
# key uses the resolved kind (`mlx` / `cuda` / `cpu`) rather than the
# operator's preference, because two different preferences that resolve
# to the same kind should share one loaded model.
_cache: dict[tuple[str, BackendKind], Transcriber] = {}


def load_transcriber(
    model_name: str,
    *,
    backend: BackendPreference = "auto",
    registry: TranscriberRegistry | None = None,
) -> Transcriber:
    """Return a cached stateful `Transcriber` for `model_name`.

    The registry decides which adapter handles each model on each
    backend (see `tapscribe.transcribers.catalog.REGISTRY` for the
    canonical table). `backend` is the operator's preference; the
    registry resolves it into one of `mlx` / `cuda` / `cpu` based on
    what's available on this machine and what the model supports.

    `registry` is injected only by tests; production passes None and
    gets the module-level singleton.

    Heavy adapter modules are imported lazily (via the registry's
    loader thunks) so booting TapScribe never pulls in PyTorch / MLX /
    NeMo unless an operator actually picks that backend.
    """
    reg = registry or REGISTRY
    resolved = reg.resolve(model_name, preference=backend)
    key = (model_name, resolved.kind)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    transcriber = resolved.loader(model_name, resolved.kind)
    _cache[key] = transcriber
    return transcriber


def clear_cache() -> None:
    """Drop all cached transcribers. Mostly for tests; also useful when an
    operator flips the backend preference at runtime and wants old
    instances GC'd."""
    _cache.clear()
