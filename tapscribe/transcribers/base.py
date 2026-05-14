"""Transcriber protocol + result dataclasses.

These types form the boundary that every adapter (faster-whisper, mlx,
Voxtral) talks across. Frozen dataclasses keep pipeline composition
honest — `dataclasses.replace` is the only way a post-processor like
`hallucinations.apply` can extend a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable


@dataclass(frozen=True)
class Word:
    """One token of word-level alignment, when the underlying model emits it."""
    start: float
    end: float
    word: str
    prob: float


@dataclass(frozen=True)
class TranscriptionSegment:
    """One decoded segment from a Transcriber. `matched_rule` is populated
    by `hallucinations.apply` when the segment is moved into the
    suppressed list."""
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    words: tuple[Word, ...] | None = None
    matched_rule: str | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    """The output of `Transcriber.transcribe(path, ...)`.

    Carries the segments and per-call metadata. Raw — before any post-
    processing. Pipeline steps like `hallucinations.apply` return a new
    `TranscriptionResult` via `dataclasses.replace`.
    """
    transcriber: str                              # echoes Transcriber.name
    device: str
    model: str
    language: str
    language_probability: float
    duration: float
    text: str                                     # joined raw segment texts
    segments: tuple[TranscriptionSegment, ...]
    initial_prompt_used: str
    hotwords_used: str
    quality_settings: dict[str, Any]
    suppressed_hallucinations: tuple[TranscriptionSegment, ...] = field(default_factory=tuple)


@runtime_checkable
class Transcriber(Protocol):
    """The protocol every adapter satisfies. Stateful — each instance owns
    one loaded model."""
    name: ClassVar[str]
    device: str
    model_name: str

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> TranscriptionResult: ...
