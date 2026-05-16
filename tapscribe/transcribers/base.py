"""Transcriber protocol + result dataclasses.

These types form the boundary that every adapter (faster-whisper, mlx,
Voxtral) talks across. Frozen dataclasses keep pipeline composition
honest — `dataclasses.replace` is the only way a post-processor like
`hallucinations.apply` can extend a result.

`Word` and `TranscriptionSegment` carry their own marshaling: every
adapter and the sidecar cache build them through the same
`from_payload` factory (which works with either dict or attribute-style
decoder output) and serialise them through `to_mapping`. Decoder
adapters never hand-roll the field-by-field wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable


def _lookup(payload: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a dict or as an attribute from an object."""
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


@dataclass(frozen=True)
class Word:
    """One token of word-level alignment, when the underlying model emits it."""

    start: float
    end: float
    word: str
    prob: float

    @classmethod
    def from_payload(cls, payload: Any) -> Word:
        """Build a `Word` from a decoder word (dict or attribute object).
        Accepts `prob` or `probability` for the confidence field."""
        prob = _lookup(payload, "prob", None)
        if prob is None:
            prob = _lookup(payload, "probability", 0.0)
        return cls(
            start=round(float(_lookup(payload, "start", 0.0) or 0.0), 2),
            end=round(float(_lookup(payload, "end", 0.0) or 0.0), 2),
            word=_lookup(payload, "word", "") or "",
            prob=round(float(prob or 0.0), 3),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "word": self.word, "prob": self.prob}


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

    @classmethod
    def from_payload(cls, payload: Any) -> TranscriptionSegment:
        """Build a `TranscriptionSegment` from a decoder segment (dict or
        attribute object). Word list is normalised through `Word.from_payload`."""
        raw_words = _lookup(payload, "words", None)
        words: tuple[Word, ...] | None = None
        if raw_words:
            words = tuple(Word.from_payload(w) for w in raw_words)
        avg = _lookup(payload, "avg_logprob", None)
        text = _lookup(payload, "text", "") or ""
        return cls(
            start=round(float(_lookup(payload, "start", 0.0) or 0.0), 2),
            end=round(float(_lookup(payload, "end", 0.0) or 0.0), 2),
            text=text.strip(),
            avg_logprob=round(float(avg), 3) if avg is not None else None,
            words=words,
            matched_rule=_lookup(payload, "matched_rule", None),
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {"start": self.start, "end": self.end, "text": self.text}
        if self.avg_logprob is not None:
            out["avg_logprob"] = self.avg_logprob
        if self.words is not None:
            out["words"] = [w.to_mapping() for w in self.words]
        if self.matched_rule is not None:
            out["matched_rule"] = self.matched_rule
        return out


@dataclass(frozen=True)
class TranscriptionResult:
    """The output of `Transcriber.transcribe(path, ...)`.

    Carries the segments and per-call metadata. Raw — before any post-
    processing. Pipeline steps like `hallucinations.apply` return a new
    `TranscriptionResult` via `dataclasses.replace`.
    """

    transcriber: str  # echoes Transcriber.name
    device: str
    model: str
    language: str
    language_probability: float
    duration: float
    text: str  # joined raw segment texts
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


def default_language_for(model_name: str) -> str | None:
    """Pick a language hint from the model name.

    `.en` suffix → English-only Whisper checkpoint.
    `nb-*` → Norwegian-tuned (NB-Whisper).
    Everything else returns None so the model runs language detection.
    """
    n = (model_name or "").lower()
    if n.endswith(".en"):
        return "en"
    if n.startswith("nb-"):
        return "no"
    return None
