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
from ._nemo_payload import build_segments_from_nemo_payload
from .base import TranscriptionResult, TranscriptionSegment, build_transcription_result

# Default MLX repo. Other Canary variants can be added by extending this
# table and registering them in catalog.py.
_MLX_REPO_TABLE: dict[str, str] = {
    "canary-1b-v2": "mlx-community/canary-1b-v2",
}


def _resolve_repo(model_name: str) -> str:
    return _MLX_REPO_TABLE.get(model_name, f"mlx-community/{model_name}")


def _lookup(payload: Any, key: str, default: Any = None) -> Any:
    """Read `key` off a NeMo-shape top-level result (dict or
    attribute-style). Local helper kept because the per-segment /
    per-word pairing now lives in `_nemo_payload`; the top-level
    `result.text` / `result.timestamp` access still needs a tolerant
    reader."""
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


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

        segments = build_segments_from_nemo_payload(seg_list, word_list)
        dur = round(wav_duration_s(path), 2)

        # When the model emits no segment list (rare — short audio?), fall
        # back to one segment covering the WAV so the merged view shows
        # something.
        if not segments and text:
            segments = (TranscriptionSegment(start=0.0, end=dur, text=text, words=None),)

        # `language=src` is the back-compat behaviour: Canary doesn't
        # detect a language, so we echo the requested source. The
        # constructor blanks target_language when it equals source
        # (no translation badge for a no-op).
        return build_transcription_result(
            self,
            text=text,
            segments=segments,
            duration=dur,
            language=src,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=src,
            target_lang=tgt,
            quality_settings={"timestamps": True},
        )
