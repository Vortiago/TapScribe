"""MLX Canary adapter (`mlx-audio` — Apple Silicon).

NVIDIA Canary-1B-v2 is a FastConformer-encoder / Transformer-decoder
audio LLM that transcribes 25 European languages AND translates X↔English.
The MLX port lives inside the broader `mlx-audio` package (Blaizzy).

API: `model.transcribe([wav_path], source_lang='en', target_lang='en',
timestamps=True)` returns a list of result objects with `.text` and
`.timestamp = {"word": [...], "segment": [...]}`.

Each segment is `{"start": float, "end": float, "segment": str}`.
Each word is `{"start": float, "end": float, "word": str}`.

Translation: when `source_lang != target_lang`, the result's `text`
is the translated output, and the adapter records both languages on
the `TranscriptionResult` so the dashboard can show a translation
badge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment, Word

# Default MLX repo. Other Canary variants can be added by extending this
# table and registering them in catalog.py.
_MLX_REPO_TABLE: dict[str, str] = {
    "canary-1b-v2": "mlx-community/canary-1b-v2",
}


def _resolve_repo(model_name: str) -> str:
    return _MLX_REPO_TABLE.get(model_name, f"mlx-community/{model_name}")


def _lookup(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _word_from_payload(payload: Any) -> Word:
    text = (_lookup(payload, "word", "") or _lookup(payload, "text", "") or "")
    start = float(_lookup(payload, "start", 0.0) or 0.0)
    end = float(_lookup(payload, "end", 0.0) or 0.0)
    return Word(start=round(start, 2), end=round(end, 2), word=text, prob=1.0)


def _build_segments(
    segment_dicts: list[Any], word_dicts: list[Any]
) -> tuple[TranscriptionSegment, ...]:
    """Pair Canary's segment list with its word list.

    Each word's timestamp is checked against each segment's [start, end]
    range; words that fall inside a segment get attached to it. Words
    outside every segment are dropped from the segments' `words` field
    (they're still visible in the cached JSON's word-level timestamps
    if we wanted to surface them — but the segment view is what powers
    the merged transcript).
    """
    if not segment_dicts:
        return ()
    all_words = [_word_from_payload(w) for w in word_dicts]
    segments: list[TranscriptionSegment] = []
    for seg in segment_dicts:
        start = float(_lookup(seg, "start", 0.0) or 0.0)
        end = float(_lookup(seg, "end", 0.0) or 0.0)
        text = (_lookup(seg, "segment", "") or _lookup(seg, "text", "") or "").strip()
        in_range = tuple(
            w for w in all_words if w.start >= start - 1e-3 and w.end <= end + 1e-3
        )
        segments.append(
            TranscriptionSegment(
                start=round(start, 2),
                end=round(end, 2),
                text=text,
                words=in_range or None,
            )
        )
    return tuple(segments)


class MlxCanaryTranscriber:
    """Canary loaded via `mlx-audio`, satisfying the Transcriber Protocol.

    `name="canary"` is the cross-backend family label. `backend="canary-mlx"`
    distinguishes from the NeMo CUDA/CPU adapter (`backend="canary-nemo"`).
    """

    name: ClassVar[str] = "canary"
    backend: ClassVar[str] = "canary-mlx"
    device: ClassVar[str] = "Apple Silicon GPU"

    def __init__(self, *, model_name: str, model: Any):
        self.model_name = model_name
        self._model = model

    @classmethod
    def load(cls, model_name: str) -> MlxCanaryTranscriber:
        import importlib.util

        if importlib.util.find_spec("mlx_audio") is None:
            raise RuntimeError(
                "MLX Canary requires the `mlx-audio` package "
                "(Apple Silicon only — Canary support lives inside the "
                "broader mlx-audio TTS/STT umbrella). Install with:\n"
                "    pip install mlx-audio\n"
                "See https://github.com/Blaizzy/mlx-audio"
            )

        # Lazy import — mlx_audio pulls a lot of optional models on first
        # load; we only want the import cost when the operator actually
        # picks Canary.
        from mlx_audio.stt.models.canary import Canary  # type: ignore

        repo = _resolve_repo(model_name)
        print(f"[tapscribe] loading mlx-audio Canary: {repo}", flush=True)
        model = Canary.from_pretrained(repo)
        return cls(model_name=model_name, model=model)

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,
    ) -> TranscriptionResult:
        # Canary's API REQUIRES source_lang + target_lang. We default both
        # to "en" when missing rather than refuse — the catalog's
        # SelectInputs default to "en", so this only triggers for API
        # callers that bypass the registry. Same UX as Whisper's
        # implicit auto-detect.
        src = source_lang or "en"
        tgt = target_lang or "en"
        responses = self._model.transcribe([str(path)], source_lang=src, target_lang=tgt, timestamps=True)
        # `responses` is a list; one entry per input path.
        result = responses[0]
        text = (_lookup(result, "text", "") or "").strip()
        timestamps = _lookup(result, "timestamp", {}) or {}
        seg_list = timestamps.get("segment", []) if isinstance(timestamps, dict) else []
        word_list = timestamps.get("word", []) if isinstance(timestamps, dict) else []

        segments = _build_segments(seg_list, word_list)
        dur = round(wav_duration_s(path), 2)

        # When the model emits no segment list (rare — short audio?), fall
        # back to one segment covering the WAV so the merged view shows
        # something.
        if not segments and text:
            segments = (
                TranscriptionSegment(start=0.0, end=dur, text=text, words=None),
            )

        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language=src,  # back-compat: language echoes source
            language_probability=0.0,
            duration=dur,
            text=text,
            segments=segments,
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={"timestamps": True},
            source_language=src,
            # Only mark target_language when it differs from source —
            # equal langs is plain transcription, no translation badge.
            target_language=tgt if tgt != src else "",
        )
