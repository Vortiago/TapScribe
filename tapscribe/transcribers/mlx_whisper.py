"""mlx-whisper adapter (Apple Silicon GPU).

Quality knobs that overlap with the faster-whisper path are kept
identical (no_speech_threshold, hallucination_silence_threshold,
word_timestamps, condition_on_previous_text). mlx-whisper has no
`hotwords` kwarg, so the adapter folds hotwords into `initial_prompt`
with a short framing line.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from ..wav_predecode import load_recorder_wav_as_pcm
from .base import (
    TranscriptionResult,
    TranscriptionSegment,
    build_transcription_result,
    default_language_for,
)

# Used when folding hotwords into the initial prompt.
_HOTWORDS_FRAMING = "Proper nouns, names, and jargon that may appear: "

# Mirrors whisperlivekit/model_mapping.py exactly — upstream is the source
# of truth since they've pressure-tested it. Note: `large-v3-turbo` is
# published WITHOUT the `-mlx` suffix; the naive `whisper-<name>-mlx`
# pattern would 404. Anything not in this table falls back to that pattern.
MLX_REPO_TABLE: dict[str, str] = {
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v1": "mlx-community/whisper-large-v1-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large": "mlx-community/whisper-large-mlx",
    # NB-Whisper (Norwegian-finetuned by Nasjonalbiblioteket) intentionally
    # excluded here: probed HF and there are NO public MLX conversions
    # (NbAiLabBeta/*-mlx, mlx-community/nb-whisper-*-mlx all 404). NB-Whisper
    # runs via faster-whisper on the CT2 weights bundled inside
    # NbAiLab/nb-whisper-<size>/ct2/, even on Apple Silicon.
}


def mlx_whisper_repo(name: str) -> str:
    """Map an OpenAI-style Whisper model name to its mlx-community HF repo.
    Looks up the WhisperLiveKit-compatible table first; falls back to
    `mlx-community/whisper-<name>-mlx` for anything else."""
    return MLX_REPO_TABLE.get(name, f"mlx-community/whisper-{name}-mlx")


class MlxWhisperTranscriber:
    """A mlx-whisper-backed adapter for Apple Silicon GPU.

    Holds the HuggingFace repo string rather than a loaded model object —
    mlx-whisper takes the repo on every call and caches internally. The
    `transcribe_fn` parameter is here primarily for testability; in
    production it defaults to `mlx_whisper.transcribe`.
    """

    name: ClassVar[str] = "mlx-whisper"
    backend: ClassVar[str] = "mlx-whisper"
    device: str = "Apple Silicon GPU"

    def __init__(
        self,
        *,
        model_name: str,
        hf_repo: str,
        transcribe_fn: Callable[..., dict[str, Any]] | None = None,
    ):
        self.model_name = model_name
        self._hf_repo = hf_repo
        self._transcribe_fn = transcribe_fn

    @classmethod
    def load(cls, model_name: str) -> MlxWhisperTranscriber:
        # Just resolves the HF repo and remembers it. mlx-whisper does its
        # own lazy fetch/cache on first call.
        repo = mlx_whisper_repo(model_name)
        print(f"[tapscribe] using mlx-whisper for batch model: {repo}", flush=True)
        return cls(model_name=model_name, hf_repo=repo)

    def unload(self) -> None:
        """Free the weights mlx-whisper cached for this repo.

        Unlike every other adapter this class holds no model object —
        mlx_whisper caches loaded models internally (an `@lru_cache` on its
        model loader), keyed by repo — so dropping the cache entry frees
        nothing on its own. Clear mlx_whisper's loader cache so those weights
        can be collected; the Metal buffer pool is reclaimed separately by the
        factory's `_free_framework_memory`.

        Best-effort and version-tolerant: the loader's module path has moved
        across mlx_whisper releases, and the package is only imported once a
        real transcribe has run (so `sys.modules` may not have it at all).
        A miss just means the weights persist until process exit — never a
        hard error on the teardown path.
        """
        if sys.modules.get("mlx_whisper") is None:
            return
        for mod_name in ("mlx_whisper.load_models", "mlx_whisper.transcribe"):
            module = sys.modules.get(mod_name)
            loader = getattr(module, "load_model", None) if module is not None else None
            cache_clear = getattr(loader, "cache_clear", None)
            if callable(cache_clear):
                try:
                    cache_clear()
                except Exception:
                    # An upstream rename / signature change must not break
                    # teardown; the cached weights then linger until the
                    # process exits. The factory drops the adapter regardless.
                    pass

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,  # noqa: ARG002 — accepted for protocol parity; Whisper doesn't translate
    ) -> TranscriptionResult:
        # Fold hotwords into the prompt with a clearly-marked framing line.
        # Keeps the joined value distinguishable from real prose context.
        effective_prompt = initial_prompt or ""
        if hotwords:
            if effective_prompt:
                effective_prompt = effective_prompt + "\n" + _HOTWORDS_FRAMING + hotwords
            else:
                effective_prompt = _HOTWORDS_FRAMING + hotwords
        prompt_arg = effective_prompt or None

        kwargs: dict[str, Any] = dict(
            path_or_hf_repo=self._hf_repo,
            language=source_lang or default_language_for(self.model_name),
            initial_prompt=prompt_arg,
            condition_on_previous_text=False,
            word_timestamps=True,
            no_speech_threshold=0.4,
            hallucination_silence_threshold=2.0,
            temperature=0.0,
        )

        fn = self._transcribe_fn or _import_mlx_transcribe()

        # mlx-whisper's path-based loader runs `ffmpeg` as a subprocess.
        # The recorder always writes the exact format mlx-whisper wants
        # (16 kHz mono 16-bit), so pre-decode ourselves and skip that
        # dependency entirely. `load_recorder_wav_as_pcm` raises on
        # unusual WAVs (different sample rate / channels / sample width)
        # — that's the operator's signal to convert the file, not a
        # cue to silently fall back to ffmpeg.
        audio = load_recorder_wav_as_pcm(path)
        result = fn(audio, **kwargs)

        segments = [TranscriptionSegment.from_payload(s) for s in (result.get("segments") or [])]

        applied_view = {k: (v if not callable(v) else str(v)) for k, v in kwargs.items()}
        return build_transcription_result(
            self,
            text=" ".join(s.text for s in segments).strip(),
            segments=tuple(segments),
            duration=wav_duration_s(path),
            language=result.get("language", "?"),
            initial_prompt=effective_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
            quality_settings=applied_view,
        )


def _import_mlx_transcribe() -> Callable[..., dict[str, Any]]:
    """Lazy import so tests don't need mlx_whisper installed."""
    import mlx_whisper  # type: ignore

    return mlx_whisper.transcribe
