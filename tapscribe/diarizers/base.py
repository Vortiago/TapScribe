"""The `Diarizer` seam — one verb, session-scoped (ADR-0021).

*Given one identity's audio for a session, produce Voices with absolute-time
spans.* Session-scoped rather than per-WAV because a level-gated tap emits
hundreds of WAVs and independently diarized ones produce labels that do not
join; and because a one-pass transcribe+diarize engine (#383) has to satisfy the
same contract.

Clips carry decoded samples rather than paths so the engine is I/O-free and the
caller can hand them over lazily — an hour of 16 kHz float32 is 230 MB held at
once.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import numpy as np


class DiarizerError(Exception):
    """Base class for every diarizer domain error."""


class DiarizerUnavailable(DiarizerError):
    """The engine cannot run at all — the embedding model was never fetched, or
    onnxruntime is missing from an incomplete venv. The operator must fix the
    install; routes map this to 400."""


class DiarizerFailed(DiarizerError):
    """The engine ran and failed. The install was fine — the model or the audio
    was not. Routes map this to 502."""


@dataclass(frozen=True)
class AudioClip:
    """One recorder WAV's samples, with the absolute instant sample 0 starts at.

    `start` is tz-aware UTC, as `text.parse_wav_start` returns and as
    `merge_session` computes `abs_start`/`abs_end` — spans come back in that same
    time base, which is what makes the merge-time join an interval comparison.
    A naive `start` would NOT raise: `text.parse_iso` reads the stored span back
    as UTC, so a local-time clip lands hours off and attributes nothing.

    Clips arrive in chronological order, so Voice A is whoever spoke first.
    """

    samples: np.ndarray
    start: datetime


@dataclass(frozen=True)
class DiarizationResult:
    """`{label: [(start, end), …]}` — the shape `voices.record_voices` stores."""

    voices: dict[str, list[tuple[datetime, datetime]]] = field(default_factory=dict)
    engine: str = ""
    took_ms: int = 0


def voice_label(index: int) -> str:
    """0 → `A`, 25 → `Z`, 26 → `AA`. Letters only, so a label can never carry
    the `#` that separates it from the identity, nor anything path-shaped."""
    label = ""
    while True:
        index, rem = divmod(index, 26)
        label = chr(ord("A") + rem) + label
        if index == 0:
            return label
        index -= 1


@runtime_checkable
class Diarizer(Protocol):
    """One verb, mirroring `Transcriber` / `Summarizer`."""

    engine: str

    def diarize(self, clips: Iterable[AudioClip]) -> DiarizationResult: ...
