"""Speaker diarization — the Diarizer seam and its ONNX engine (ADR-0021).

`load_diarizer` is the one door. There is deliberately no `catalog.py`: the
`summarizers` one doubles as a security allowlist because a request body names
its model, and nothing here does — one engine, one fetched export. A second
engine is when that table earns its place.

The imports sit inside the function because `tapscribe.preflight` imports the
`model` submodule to probe it, and preflight runs against a venv that may hold
nothing but pip — a module-level numpy import here would crash the bring-up
repair that exists to fix exactly that venv.
"""

from __future__ import annotations


def load_diarizer(**overrides):
    """The standalone engine, ready to run. Raises `DiarizerUnavailable` when
    the model or onnxruntime is missing, so a caller can fail before claiming a
    job slot (`batch_summarize` loads in the same order)."""
    from .base import DiarizerUnavailable
    from .embed import CampPlusEmbedder
    from .standalone import StandaloneDiarizer

    embedder = CampPlusEmbedder.load()
    # The CALL is inside the guard, not just the import: `load_model` opens the
    # vendored silero graph, so a wheel whose package data lost
    # `vad/silero_vad.onnx` fails here, not at the import — and this runs BEFORE
    # the job claim precisely so an install problem lands as an actionable 400.
    try:
        from ..vad import load_model

        vad = load_model()
    except Exception as exc:  # pragma: no cover - core dep, broken venv only
        raise DiarizerUnavailable("the VAD backend is unavailable — reinstall TapScribe.") from exc
    return StandaloneDiarizer(embedder, vad=vad, **overrides)
