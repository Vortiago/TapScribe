"""Summarizers — adapters that turn a session's merged transcript into a summary.

The post-transcription mirror of the Transcriber seam (`tapscribe.transcribers`),
one altitude up: a `Summarizer` is "something that can summarize one transcript
given a prompt" (one verb, like `Transcriber.transcribe`), and
`load_summarizer(source=…)` is the factory that picks the adapter — the same
shape as `load_transcriber(name, backend=…)`.

Package layout (mirrors `transcribers/`):

- `base` — the `Summarizer` protocol, `SummaryResult`, the domain errors, and
  `DEFAULT_SUMMARY_PROMPT`. The leaf everything else builds on.
- `command` — `CommandSummarizer`: pipes the transcript to an operator CLI tool.
- `catalog` — the selectable-model table (`SUMMARY_MODELS`), the allowlist, the
  per-machine hardware routing, and the operator knobs (source-neutral home;
  the API source's model list will land here too).
- `local` — `LocalSummarizer`: a bundled, offline, hardware-routed model.

This `__init__` re-exports the public interface so callers keep importing from
`tapscribe.summarizers`; the factory below is the single dispatch seam.
"""

from __future__ import annotations

from .base import (
    DEFAULT_SUMMARY_PROMPT,
    Summarizer,
    SummarizerError,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
)
from .catalog import (
    ENV_LOCAL_GGUF_FILE,
    ENV_LOCAL_GGUF_MODEL,
    ENV_LOCAL_MLX_MODEL,
    ENV_MAX_TOKENS,
    LOCAL_GGUF_MODEL,
    LOCAL_MLX_MODEL,
    SummaryModel,
    summary_model_catalog,
)
from .command import CommandSummarizer
from .local import LocalSummarizer

__all__ = [
    "DEFAULT_SUMMARY_PROMPT",
    "ENV_LOCAL_GGUF_FILE",
    "ENV_LOCAL_GGUF_MODEL",
    "ENV_LOCAL_MLX_MODEL",
    "ENV_MAX_TOKENS",
    "LOCAL_GGUF_MODEL",
    "LOCAL_MLX_MODEL",
    "CommandSummarizer",
    "LocalSummarizer",
    "Summarizer",
    "SummarizerError",
    "SummarizerFailed",
    "SummarizerUnavailable",
    "SummaryModel",
    "SummaryResult",
    "load_summarizer",
    "summary_model_catalog",
]


def load_summarizer(
    *,
    source: str,
    command: str = "",
    model: str = "",
    max_tokens: int | None = None,
    timeout_s: float | None = None,
) -> Summarizer:
    """Return a `Summarizer` for the chosen source, dispatching on `source`
    exactly as `load_transcriber` resolves a backend.

    `command` (#82) and `local` (#86, the bundled offline default) are wired;
    `api` (#85) still raises `SummarizerUnavailable` until its slice lands, so
    the view shows it disabled and a stray request fails with a clear 400. The
    `local` branch itself raises `SummarizerUnavailable` when the `[summarize]`
    extra isn't installed — the 'degrade clearly' path — or when `model` isn't in
    the backend's catalog allowlist.

    `model` selects which bundled model the `local` source loads (the dashboard's
    model dropdown sends it); empty falls back to the backend default. `max_tokens`
    caps the OUTPUT length (the dashboard's number input sends it); None falls back
    to the env default. Both are ignored by the `command` source (an external CLI
    owns its own model + length)."""
    src = (source or "").strip().lower()
    if src == "command":
        return CommandSummarizer(command, timeout_s=timeout_s)
    if src == "local":
        return LocalSummarizer(model=model, max_tokens=max_tokens)
    if src == "api":
        raise SummarizerUnavailable("the 'api' summarizer source isn't wired yet")
    raise SummarizerUnavailable(f"unknown summarizer source: {source!r}")
