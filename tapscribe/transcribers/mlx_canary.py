"""MLX Canary adapter (`mlx-audio` 0.4.x — Apple Silicon).

NVIDIA Canary-1B-v2 is a FastConformer-encoder / Transformer-decoder
audio LLM that transcribes 25 European languages AND translates X↔English.
The MLX port lives inside the broader `mlx-audio` package (Blaizzy).

mlx-audio 0.4.x API
-------------------
- Class is `Model` (was `Canary` in earlier releases — the rename
  landed in 0.4.0; we pin `mlx-audio>=0.4,<0.5` in pyproject and
  alias on import to keep the call sites readable).
- `Model.generate(audio, *, source_lang, target_lang, max_tokens=200,
  …) -> STTOutput` is the only entry point. `audio` accepts a file
  path, a numpy/mlx waveform array, or a pre-computed mel
  spectrogram — we pass the pre-decoded waveform so we never go
  through the package's ffmpeg-aware audio loader.
- `STTOutput.text` is the only useful field. `segments` is a single
  hardcoded `{"text": ..., "start": 0.0, "end": 0.0}` (a known
  upstream limitation as of 0.4.3); word-level timestamps are gone.
- `max_tokens` is a hard cap on *total* output tokens per call. The
  default of 200 truncates audio longer than ~30 s of speech, so the
  adapter chunks the waveform itself and calls `generate` per window.

Chunking
--------
Windows overlap by `overlap_duration_s` so words straddling a
boundary are transcribed in both copies. Without per-token timing
from upstream there's no precise way to trim the overlapped duplicate;
operators may see the last few words of window N repeated at the start
of window N+1. If mlx-audio reintroduces per-token timestamps the
overlap-dedup logic can move into `_stitch_chunks` then.

Segment timestamps are synthesised from the window offsets so the
dashboard shows where in the WAV each transcribed chunk came from.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from ..audio import RECORDER_SAMPLE_RATE
from ..chunking import chunk_windows
from ..config import env_float, env_int
from ..wav_predecode import load_recorder_wav_as_pcm
from .base import (
    TranscriptionResult,
    TranscriptionSegment,
    _lookup,
    build_transcription_result,
)

# mlx-audio's Canary has no published Hub repo; the loader requires a
# locally converted MLX-safetensors directory. Operators point this env
# var at the converted dir.
ENV_LOCAL_PATH = "TAPSCRIBE_CANARY_MLX_PATH"


# Chunking defaults. Each window must stay under Canary's per-call
# `max_tokens` cap. The upstream default is 200, which is tight for
# 30 s windows with fast speakers (4–5 syllables/sec ≈ 150–200 tokens);
# the adapter raises the cap to 256 to give headroom, and still flags
# `truncation_suspected` on the result when any window's
# `generation_tokens` lands at the cap so operators see the signal.
# Operator-tunable via env, hoisted to module constants so a typo at
# the call site surfaces as a NameError instead of silently using the
# default.
_DEFAULT_CHUNK_DURATION_S = 30.0
_DEFAULT_OVERLAP_DURATION_S = 2.0
_DEFAULT_MAX_TOKENS_PER_CHUNK = 256

ENV_CHUNK_S = "TAPSCRIBE_CANARY_CHUNK_S"
ENV_OVERLAP_S = "TAPSCRIBE_CANARY_OVERLAP_S"
ENV_MAX_TOKENS = "TAPSCRIBE_CANARY_MAX_TOKENS"

# Operator-knob bounds. Out-of-range env values are rejected by
# `env_float` / `env_int` (logged + default used).
_CHUNK_S_BOUNDS = (1.0, 600.0)
_OVERLAP_S_BOUNDS = (0.0, 60.0)
_MAX_TOKENS_BOUNDS = (16, 4096)


_CONVERSION_GUIDE_URL = "https://github.com/Blaizzy/mlx-audio/tree/main/mlx_audio/stt/models/canary"


def _resolve_local_path(model_name: str) -> str:
    env_value = (os.environ.get(ENV_LOCAL_PATH) or "").strip()
    if not env_value:
        raise RuntimeError(
            f"MLX Canary {model_name!r} needs a locally converted weights "
            f"directory; set {ENV_LOCAL_PATH}=/path/to/canary-mlx. "
            f"Conversion guide: {_CONVERSION_GUIDE_URL}"
        )
    env_path = Path(env_value).expanduser()
    if not env_path.is_dir():
        raise RuntimeError(f"{ENV_LOCAL_PATH}={env_value!r} is not an existing directory.")
    return str(env_path)


def _trim_leading_overlap(prev_text: str, current_text: str, *, max_words: int = 8) -> str:
    """Remove from the start of `current_text` any word sequence that
    repeats the last few words of `prev_text`. Helps clean the visible
    seam between Canary windows where the overlap region is
    transcribed twice and the upstream API gives us no per-token
    timing to dedupe precisely.

    Algorithm: take the last `max_words` words of `prev_text` and the
    first `max_words` words of `current_text`, find the longest
    matching suffix-of-prev = prefix-of-current (case-insensitive),
    and trim the matched prefix from `current_text`. Returns the
    untrimmed text when no overlap is found (silence, completely
    different content, or upstream truncation that ate the duplicate).
    """
    if not prev_text or not current_text:
        return current_text
    tail = prev_text.split()[-max_words:]
    head_full = current_text.split()
    head = head_full[:max_words]
    if not tail or not head:
        return current_text
    tail_lower = [w.lower() for w in tail]
    head_lower = [w.lower() for w in head]
    # Longest k where tail[-k:] == head[:k].
    best = 0
    for k in range(min(len(tail), len(head)), 0, -1):
        if tail_lower[-k:] == head_lower[:k]:
            best = k
            break
    if best == 0:
        return current_text
    return " ".join(head_full[best:])


def _stitch_chunks(
    per_chunk: list[tuple[float, float, str]],
) -> tuple[tuple[TranscriptionSegment, ...], str]:
    """Build (segments, joined_text) from per-window outputs.

    Each input is `(window_start_s, window_end_s, text)`. Emits one
    segment per non-empty window with the window's real timestamps;
    empty windows drop out. The joined text trims leading overlap
    against the previous non-empty window's tail so seam dupes don't
    reach the merged transcript (see `_trim_leading_overlap`).
    Segment texts are kept verbatim — the operator can still inspect
    the per-window output, but the rolled-up text doesn't repeat.
    """
    segments: list[TranscriptionSegment] = []
    joined_parts: list[str] = []
    prev_for_trim = ""
    for start, end, text in per_chunk:
        text = text.strip()
        if not text:
            continue
        segments.append(
            TranscriptionSegment(
                start=round(start, 2),
                end=round(end, 2),
                text=text,
                words=None,
            )
        )
        trimmed = _trim_leading_overlap(prev_for_trim, text) if joined_parts else text
        if trimmed:
            joined_parts.append(trimmed)
        prev_for_trim = text
    return tuple(segments), " ".join(joined_parts).strip()


class MlxCanaryTranscriber:
    """Canary loaded via `mlx-audio` 0.4.x, satisfying the Transcriber Protocol.

    `name="canary"` is the cross-backend family label. `backend="canary-mlx"`
    distinguishes from the NeMo CUDA/CPU adapter (`backend="canary-nemo"`).

    Tests may inject `model` directly and skip `load()`; production
    always goes through `load()` which imports `mlx_audio` lazily.
    """

    name: ClassVar[str] = "canary"
    backend: ClassVar[str] = "canary-mlx"
    device: ClassVar[str] = "Apple Silicon GPU"

    def __init__(
        self,
        *,
        model_name: str,
        model: Any,
        chunk_duration_s: float | None = None,
        overlap_duration_s: float | None = None,
        max_tokens_per_chunk: int | None = None,
    ):
        self.model_name = model_name
        self._model = model
        self.chunk_duration_s = (
            chunk_duration_s
            if chunk_duration_s is not None
            else env_float(
                ENV_CHUNK_S,
                _DEFAULT_CHUNK_DURATION_S,
                min_value=_CHUNK_S_BOUNDS[0],
                max_value=_CHUNK_S_BOUNDS[1],
            )
        )
        self.overlap_duration_s = (
            overlap_duration_s
            if overlap_duration_s is not None
            else env_float(
                ENV_OVERLAP_S,
                _DEFAULT_OVERLAP_DURATION_S,
                min_value=_OVERLAP_S_BOUNDS[0],
                max_value=_OVERLAP_S_BOUNDS[1],
            )
        )
        self.max_tokens_per_chunk = (
            max_tokens_per_chunk
            if max_tokens_per_chunk is not None
            else env_int(
                ENV_MAX_TOKENS,
                _DEFAULT_MAX_TOKENS_PER_CHUNK,
                min_value=_MAX_TOKENS_BOUNDS[0],
                max_value=_MAX_TOKENS_BOUNDS[1],
            )
        )

    @classmethod
    def load(cls, model_name: str) -> MlxCanaryTranscriber:
        import importlib.util

        if importlib.util.find_spec("mlx_audio") is None:
            raise RuntimeError(
                "MLX Canary requires the `mlx-audio` package "
                "(Apple Silicon only — Canary support lives inside the "
                "broader mlx-audio TTS/STT umbrella). Install with:\n"
                "    pip install 'mlx-audio>=0.4,<0.5'\n"
                "See https://github.com/Blaizzy/mlx-audio"
            )

        # Fail fast before paying mlx_audio's import cost.
        source = _resolve_local_path(model_name)

        # Lazy import — mlx_audio pulls a lot of optional models on first
        # load; we only want the import cost when the operator actually
        # picks Canary. The class was renamed `Canary` → `Model` in
        # mlx-audio 0.4.0; we alias to keep the rest of the file readable.
        from mlx_audio.stt.models.canary import Model as Canary  # type: ignore

        print(f"[tapscribe] loading mlx-audio Canary from {source}", flush=True)
        model = Canary.from_pretrained(source)
        return cls(model_name=model_name, model=model)

    def _generate_window(self, pcm: Any, *, source_lang: str, target_lang: str) -> tuple[str, int | None]:
        """Call the underlying `generate(audio, ...)`. Returns
        `(text, generation_tokens)`. The token count comes from
        `STTOutput.generation_tokens` when upstream emits it; None
        when missing (so the caller can skip truncation detection
        rather than false-flag every window)."""
        out = self._model.generate(
            pcm,
            source_lang=source_lang,
            target_lang=target_lang,
            max_tokens=self.max_tokens_per_chunk,
        )
        text = (_lookup(out, "text", "") or "").strip()
        raw_tokens = _lookup(out, "generation_tokens", None)
        gen_tokens: int | None
        try:
            gen_tokens = int(raw_tokens) if raw_tokens is not None else None
        except (TypeError, ValueError):
            gen_tokens = None
        return text, gen_tokens

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
        # callers that bypass the registry.
        src = source_lang or "en"
        tgt = target_lang or "en"

        # Pre-decode skips mlx-audio's `audio_io.read` (which uses
        # miniaudio for WAVs and ffmpeg for m4a/aac/ogg/opus/webm).
        # `load_recorder_wav_as_pcm` rejects unusual WAVs explicitly
        # so the operator gets a clear error instead of a silent
        # ffmpeg dependency.
        pcm = load_recorder_wav_as_pcm(path)
        windows = chunk_windows(
            int(pcm.shape[0]),
            chunk_s=self.chunk_duration_s,
            overlap_s=self.overlap_duration_s,
        )

        per_chunk: list[tuple[float, float, str]] = []
        truncation_suspected = False
        for window in windows:
            chunk_pcm = pcm[window.start_sample : window.end_sample]
            text, gen_tokens = self._generate_window(chunk_pcm, source_lang=src, target_lang=tgt)
            window_end_s = window.end_sample / RECORDER_SAMPLE_RATE
            per_chunk.append((window.start_s, window_end_s, text))
            # Canary 0.4.x caps each call at `max_tokens` — a window
            # that landed exactly at the cap likely ran out of room
            # mid-sentence. Flag once for the whole result so the
            # operator knows to bump TAPSCRIBE_CANARY_MAX_TOKENS or
            # shrink TAPSCRIBE_CANARY_CHUNK_S.
            if gen_tokens is not None and gen_tokens >= self.max_tokens_per_chunk:
                truncation_suspected = True

        segments, text = _stitch_chunks(per_chunk)
        dur = round(pcm.shape[0] / RECORDER_SAMPLE_RATE, 2)

        # When _stitch_chunks emitted nothing (every window was empty
        # or whitespace) but we still have rolled-up text, fall back
        # to one segment covering the WAV so the merged view shows
        # the duration with the text.
        if not segments and text:
            segments = (TranscriptionSegment(start=0.0, end=dur, text=text, words=None),)

        quality_settings: dict[str, Any] = {
            "chunk_duration_s": self.chunk_duration_s,
            "overlap_duration_s": self.overlap_duration_s,
            "max_tokens_per_chunk": self.max_tokens_per_chunk,
        }
        if truncation_suspected:
            quality_settings["truncation_suspected"] = True

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
            quality_settings=quality_settings,
        )
