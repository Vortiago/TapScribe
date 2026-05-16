"""mlx-whisper adapter (Apple Silicon GPU).

Quality knobs that overlap with the faster-whisper path are kept
identical (no_speech_threshold, hallucination_silence_threshold,
word_timestamps, condition_on_previous_text). mlx-whisper has no
`hotwords` kwarg, so the adapter folds hotwords into `initial_prompt`
with a short framing line.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from ..audio import load_recorder_wav_as_pcm, wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment, default_language_for

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
    device: str = "Apple Silicon GPU (MLX)"

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

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
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
            language=default_language_for(self.model_name),
            initial_prompt=prompt_arg,
            condition_on_previous_text=False,
            word_timestamps=True,
            no_speech_threshold=0.4,
            hallucination_silence_threshold=2.0,
            temperature=0.0,
        )

        fn = self._transcribe_fn or _import_mlx_transcribe()

        # mlx-whisper's path-based loader runs `ffmpeg` as a subprocess,
        # which fails on machines without ffmpeg on PATH. The recorder
        # always writes the exact format mlx-whisper wants, so pre-decode
        # ourselves to skip that dependency. Fall back to the string path
        # if the WAV has an unexpected format.
        try:
            audio = load_recorder_wav_as_pcm(path)
            result = fn(audio, **kwargs)
        except RuntimeError as e:
            print(
                f"[tapscribe] mlx pre-decode failed ({e}); falling back to path (needs ffmpeg on PATH).",
                flush=True,
            )
            result = fn(str(path), **kwargs)

        segments = [TranscriptionSegment.from_payload(s) for s in (result.get("segments") or [])]

        applied_view = {k: (v if not callable(v) else str(v)) for k, v in kwargs.items()}
        return TranscriptionResult(
            transcriber=self.name,
            device=self.device,
            model=self.model_name,
            language=result.get("language", "?"),
            language_probability=0.0,
            duration=round(wav_duration_s(path), 2),
            text=" ".join(s.text for s in segments).strip(),
            segments=tuple(segments),
            initial_prompt_used=effective_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings=applied_view,
        )


def _import_mlx_transcribe() -> Callable[..., dict[str, Any]]:
    """Lazy import so tests don't need mlx_whisper installed."""
    import mlx_whisper  # type: ignore

    return mlx_whisper.transcribe
