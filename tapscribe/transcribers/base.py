"""Transcriber protocol + result dataclasses + model-input types.

These types form the boundary that every adapter (faster-whisper, mlx,
Voxtral, Parakeet) talks across. Frozen dataclasses keep pipeline
composition honest — `dataclasses.replace` is the only way a
post-processor like `hallucinations.apply` can extend a result.

`Word` and `TranscriptionSegment` carry their own marshaling: every
adapter and the sidecar cache build them through the same
`from_payload` factory (which works with either dict or attribute-style
decoder output) and serialise them through `to_mapping`. Decoder
adapters never hand-roll the field-by-field wiring.

`TextInput` describes the per-model UI form fields the
`TranscriberRegistry` declares. The dashboard reads those declarations
from `/api/models` and renders form fields accordingly; the API call
forwards only the values the registry says the model accepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

# Hardware/runtime kinds the registry can route to. "auto" is a *preference*
# the operator selects; the registry resolves it into one of the concrete
# kinds at load_transcriber time based on what's importable on this box.
BackendKind = Literal["mlx", "cuda", "cpu"]
BackendPreference = Literal["auto", "mlx", "cuda", "cpu"]


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

    `language` (the existing field) is the source language as the model
    saw it — kept for back-compat with cached sidecar JSONs.
    `source_language` records what the model was told to expect (the
    per-region language pin, ADR-0010); empty means the model's own
    auto-detect was in charge.
    """

    transcriber: str  # echoes Transcriber.name
    backend: str  # library/framework: "faster-whisper", "mlx-whisper", "parakeet-mlx", "parakeet-hf", etc.
    device: str  # hardware only: "CPU", "Apple Silicon GPU", "CUDA"
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
    # The language the adapter was told to expect (`source_lang`), part of
    # the cache match key. Empty = the model auto-detected; `language`
    # carries what it decided.
    source_language: str = ""


# ---------------------------------------------------------------------------
# Per-model UI input declarations
# ---------------------------------------------------------------------------
#
# The TranscriberRegistry attaches a tuple of `ModelInput`s to every entry.
# The dashboard renders form fields from those tuples (`/api/models`), and
# the transcribe routes forward only the values the registry says the
# adapter accepts. Adding a new field type (e.g. checkbox) means widening
# the union here and updating the renderer — adapters get no new burden.


@dataclass(frozen=True)
class TextInput:
    """A free-text field (single line or multi-line textarea).

    `name` is both the form-field id on the wire and the kwarg name on
    `transcribe()`. Today: `initial_prompt`, `hotwords`.
    """

    name: str
    label: str
    kind: Literal["text", "textarea"] = "text"
    placeholder: str = ""
    description: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "type": "text",
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "placeholder": self.placeholder,
            "description": self.description,
        }


# The one input kind the dashboard knows how to render. Widen back into a
# discriminated union (`TextInput | NewKind`) in the same PR that ships a
# model actually declaring a new kind — the renderer lives in
# web/js/next/components/engine.js.
ModelInput = TextInput


@runtime_checkable
class Transcriber(Protocol):
    """The protocol every adapter satisfies. Stateful — each instance owns
    one loaded model.

    `transcribe()` accepts a generous superset of kwargs so the same call
    site can dispatch any registry-declared adapter. Each adapter consumes
    the ones it understands and silently echoes the rest into the result's
    `initial_prompt_used` / `hotwords_used` / `source_language` for audit
    parity — no adapter ever raises on an unfamiliar kwarg.
    """

    name: ClassVar[str]
    backend: str
    device: str
    model_name: str

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
    ) -> TranscriptionResult: ...


@runtime_checkable
class ConstrainedLanguageDetector(Protocol):
    """An adapter that can restrict language auto-detection to a candidate set
    (ADR-0010). The cache loop (`wav_cache.cached_transcribe`) resolves a
    multi-language candidate set to a concrete per-region pin through this
    BEFORE `transcribe()`, so the chosen language flows through the existing
    `source_lang` channel — the on-disk cache key and the result's
    `source_language` both reflect it, no new field to thread.

    Adapters that don't implement it (every non-Whisper backend today) fall
    back to unconstrained auto-detect for a multi-language set: the slice-1
    limitation noted in ADR-0010. `cached_transcribe` feature-detects via
    `isinstance`, so adding the method to another adapter later opts it in with
    no call-site change."""

    def detect_constrained_language(self, path: Path, candidate_languages: tuple[str, ...]) -> str | None:
        """Return the most likely language WITHIN `candidate_languages` for the
        audio at `path`, or None to leave the model's own auto-detect in charge
        (e.g. when the model can't produce any of the candidates)."""
        ...


def build_transcription_result(
    adapter: Transcriber,
    *,
    text: str,
    segments: tuple[TranscriptionSegment, ...],
    duration: float,
    language: str,
    language_probability: float = 0.0,
    initial_prompt: str | None = None,
    hotwords: str | None = None,
    source_lang: str | None = None,
    quality_settings: dict[str, Any] | None = None,
) -> TranscriptionResult:
    """Build a `TranscriptionResult` with the audit-field boilerplate
    centralised. Reads `transcriber`/`backend`/`device`/`model` off the
    adapter; rounds `language_probability` and `duration` to the
    canonical precision; and coerces None prompt/hotwords/source_lang
    into "" (the wire format never carries None for these).

    Adapters call it with just the engine-specific parts; the
    constructor is the one place that knows the audit shape."""
    return TranscriptionResult(
        transcriber=adapter.name,
        backend=adapter.backend,
        device=adapter.device,
        model=adapter.model_name,
        language=language,
        language_probability=round(float(language_probability or 0.0), 3),
        duration=round(float(duration or 0.0), 2),
        text=text,
        segments=segments,
        initial_prompt_used=initial_prompt or "",
        hotwords_used=hotwords or "",
        quality_settings=quality_settings if quality_settings is not None else {},
        source_language=source_lang or "",
    )


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
