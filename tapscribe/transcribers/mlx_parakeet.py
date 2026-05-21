"""MLX Parakeet adapter (`parakeet-mlx` — Apple Silicon).

Wraps `parakeet_mlx.from_pretrained` to satisfy the `Transcriber`
protocol. Parakeet's headline differentiator is **real word-level
timestamps** straight from the decoder — `AlignedToken.start` /
`.end` flow into `Word` tuples without the sentence-split + linear-
interpolation fallback the Voxtral adapters need.

Languages: 25 European (NVIDIA parakeet-tdt-0.6b-v3), no Norwegian.
Models: defaults to `mlx-community/parakeet-tdt-0.6b-v3`; future
variants can register additional entries in the catalog with their
own HF repo strings.

API contract:
  - `prompt` / `hotwords`: not supported by parakeet-mlx — accepted on
    the call for protocol parity, dropped at the model call, echoed
    onto the result for audit.
  - `source_lang`: recorded on the result. Parakeet does not echo a
    detected language, so we trust the operator's pick. Missing →
    `language="auto"`.
  - `target_lang`: ignored. Parakeet does not translate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import (
    TranscriptionResult,
    TranscriptionSegment,
    Word,
    build_transcription_result,
)

# Default repo on Hugging Face — `from_pretrained` resolves the catalog
# model_id `parakeet-tdt-0.6b-v3` to this repo. Additional variants can
# be added by extending `_MODEL_REPO_TABLE`.
_MODEL_REPO_TABLE: dict[str, str] = {
    "parakeet-tdt-0.6b-v3": "mlx-community/parakeet-tdt-0.6b-v3",
}


def _resolve_repo(model_name: str) -> str:
    """Map a catalog model_id to its Hugging Face repo string."""
    return _MODEL_REPO_TABLE.get(model_name, f"mlx-community/{model_name}")


def _attr(payload: Any, name: str, default: Any = None) -> Any:
    """Tolerant accessor: works for dicts and attribute-style objects.

    parakeet-mlx returns dataclass-ish objects; tests fake them via
    `types.SimpleNamespace`. Both expose the same names but via
    different lookup mechanisms — this collapses that into one path.
    """
    if isinstance(payload, dict):
        return payload.get(name, default)
    return getattr(payload, name, default)


def _tokens_to_words(tokens: Any) -> tuple[Word, ...] | None:
    """Convert a sentence's tokens (AlignedToken list) into Word tuples.

    Parakeet does not emit per-token probabilities, so `prob` is pinned
    to 1.0 — distinct from "missing" so downstream consumers can tell
    "Parakeet didn't report" from "low confidence". Returns None when
    there are no tokens so the sidecar JSON simply omits the field.
    """
    if not tokens:
        return None
    out: list[Word] = []
    for tok in tokens:
        text = _attr(tok, "text", "") or ""
        start = float(_attr(tok, "start", 0.0) or 0.0)
        end = float(_attr(tok, "end", 0.0) or 0.0)
        out.append(Word(start=round(start, 2), end=round(end, 2), word=text, prob=1.0))
    return tuple(out)


def _sentence_to_segment(sentence: Any) -> TranscriptionSegment:
    text = (_attr(sentence, "text", "") or "").strip()
    start = float(_attr(sentence, "start", 0.0) or 0.0)
    end = float(_attr(sentence, "end", 0.0) or 0.0)
    words = _tokens_to_words(_attr(sentence, "tokens", None))
    return TranscriptionSegment(
        start=round(start, 2),
        end=round(end, 2),
        text=text,
        words=words,
    )


class MlxParakeetTranscriber:
    """Parakeet model loaded via `parakeet_mlx`, satisfying the
    `Transcriber` Protocol.

    `name="parakeet"` is the cross-backend family label that lands in
    result JSON; `backend="parakeet-mlx"` disambiguates from the
    `transformers`-based CUDA/CPU adapter (`backend="parakeet-hf"`).
    """

    name: ClassVar[str] = "parakeet"
    backend: ClassVar[str] = "parakeet-mlx"
    device: ClassVar[str] = "Apple Silicon GPU"

    def __init__(self, *, model_name: str, model: Any):
        self.model_name = model_name
        self._model = model

    @classmethod
    def load(cls, model_name: str) -> MlxParakeetTranscriber:
        import importlib.util

        if importlib.util.find_spec("parakeet_mlx") is None:
            raise RuntimeError(
                "MLX Parakeet requires the `parakeet-mlx` package "
                "(Apple Silicon only). Install with:\n"
                "    pip install parakeet-mlx\n"
                "See https://github.com/senstella/parakeet-mlx"
            )

        # Lazy import so non-Apple-Silicon hosts can still import this
        # module (for type checks and factory dispatch) without crashing.
        from parakeet_mlx import from_pretrained  # type: ignore

        repo = _resolve_repo(model_name)
        print(f"[tapscribe] loading parakeet-mlx model: {repo}", flush=True)
        model = from_pretrained(repo)
        return cls(model_name=model_name, model=model)

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,  # noqa: ARG002 — Parakeet doesn't translate
    ) -> TranscriptionResult:
        # parakeet-mlx's transcribe takes only the audio path (plus optional
        # chunk_duration / overlap_duration tuning kwargs we don't expose).
        # No language hint slot — operator's source_lang choice is recorded
        # on the result, not forwarded.
        aligned = self._model.transcribe(str(path))
        sentences = _attr(aligned, "sentences", None) or []
        segments = tuple(_sentence_to_segment(s) for s in sentences)
        text = (_attr(aligned, "text", "") or "").strip()

        # Parakeet doesn't echo a detected language; record the hint
        # the operator pinned, or "auto" when they didn't.
        return build_transcription_result(
            self,
            text=text,
            segments=segments,
            duration=wav_duration_s(path),
            language=source_lang or "auto",
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
        )
