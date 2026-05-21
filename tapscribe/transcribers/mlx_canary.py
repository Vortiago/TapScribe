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
  default of 200 truncates any audio longer than ~30 s of speech, so
  we chunk the waveform ourselves and call `generate` per window —
  same pattern as `mlx_parakeet`.

Chunking
--------
Each window is at most `chunk_duration_s` seconds with
`overlap_duration_s` seconds of overlap with its neighbour so words
straddling a boundary are transcribed in both copies. The stitcher
(`_stitch_chunks`) currently emits one segment per non-empty
window verbatim — without per-token timing from upstream there's no
precise way to trim the overlapped duplicate. Operators may see the
last few words of window N repeated at the start of window N+1;
this is a known limitation and the price of not depending on
ffmpeg. If mlx-audio reintroduces per-token timestamps in a later
release we can do word-level dedup in `_stitch_chunks` then.

Segment timestamps are synthesised from the window offsets so the
dashboard shows where in the WAV each transcribed chunk came from,
even though the upstream API reports `start=0.0/end=0.0` for every
segment.

If the WAV is shorter than one chunk we still go through the
chunking loop (one window, no stitching) so the same code path
handles every input.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from ..audio import RECORDER_SAMPLE_RATE, wav_duration_s
from ..wav_predecode import load_recorder_wav_as_pcm
from .base import (
    TranscriptionResult,
    TranscriptionSegment,
    build_transcription_result,
)

# Default MLX repo. Other Canary variants can be added by extending this
# table and registering them in catalog.py.
_MLX_REPO_TABLE: dict[str, str] = {
    "canary-1b-v2": "mlx-community/canary-1b-v2",
}


# Chunking defaults — tuned so each window stays under Canary's
# `max_tokens=200` cap (≈30 s of speech) with a small overlap that
# covers word straddle without producing a confusing pile of dupes.
# Overridable per-instance and via env so the operator can tune on a
# big-memory M-Max without an adapter rebuild. The eventual dashboard
# knobs (follow-up PR) will plumb the per-request values through to
# the constructor.
_DEFAULT_CHUNK_DURATION_S = 30.0
_DEFAULT_OVERLAP_DURATION_S = 2.0
_DEFAULT_MAX_TOKENS_PER_CHUNK = 200


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        # An unparseable env override is a user mistake but not fatal —
        # the default lets the transcribe still run; we just log it once.
        print(f"[tapscribe] ignoring unparseable {name}={raw!r}; using default {default}", flush=True)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[tapscribe] ignoring unparseable {name}={raw!r}; using default {default}", flush=True)
        return default


def _resolve_repo(model_name: str) -> str:
    return _MLX_REPO_TABLE.get(model_name, f"mlx-community/{model_name}")


def _lookup(payload: Any, key: str, default: Any = None) -> Any:
    """Read `key` off an STTOutput-shape object (dict or attribute-style)."""
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _stitch_chunks(per_chunk: list[tuple[float, float, str]]) -> tuple[TranscriptionSegment, ...]:
    """Build the merged segment list from per-window outputs.

    Each input is `(window_start_s, window_end_s, text)`. We emit one
    segment per non-empty window, timestamps reflecting the window's
    real position in the source WAV. The overlap-text deduplication is
    intentionally crude (the upstream API gives us no token timing to
    trim precisely): we keep every window's text verbatim and rely on
    the operator-facing "this is windowed Canary output" UX. A future
    pass could do word-level dedup once mlx-audio exposes per-token
    timing again — see the docstring at the top of this module.
    """
    out: list[TranscriptionSegment] = []
    for start, end, text in per_chunk:
        text = text.strip()
        if not text:
            continue
        out.append(
            TranscriptionSegment(
                start=round(start, 2),
                end=round(end, 2),
                text=text,
                words=None,
            )
        )
    return tuple(out)


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
            else _env_float("TAPSCRIBE_CANARY_CHUNK_S", _DEFAULT_CHUNK_DURATION_S)
        )
        self.overlap_duration_s = (
            overlap_duration_s
            if overlap_duration_s is not None
            else _env_float("TAPSCRIBE_CANARY_OVERLAP_S", _DEFAULT_OVERLAP_DURATION_S)
        )
        self.max_tokens_per_chunk = (
            max_tokens_per_chunk
            if max_tokens_per_chunk is not None
            else _env_int("TAPSCRIBE_CANARY_MAX_TOKENS", _DEFAULT_MAX_TOKENS_PER_CHUNK)
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

        # Lazy import — mlx_audio pulls a lot of optional models on first
        # load; we only want the import cost when the operator actually
        # picks Canary. The class was renamed `Canary` → `Model` in
        # mlx-audio 0.4.0; we alias to keep the rest of the file readable.
        from mlx_audio.stt.models.canary import Model as Canary  # type: ignore

        repo = _resolve_repo(model_name)
        print(f"[tapscribe] loading mlx-audio Canary: {repo}", flush=True)
        model = Canary.from_pretrained(repo)
        return cls(model_name=model_name, model=model)

    def _chunk_windows(self, total_samples: int) -> list[tuple[int, int]]:
        """Return `[(start_sample, end_sample), …]` covering the whole
        PCM with overlaps. Always returns at least one window, even for
        sub-chunk inputs — keeps the call-site loop uniform.
        """
        sr = RECORDER_SAMPLE_RATE
        chunk = max(1, int(self.chunk_duration_s * sr))
        overlap = max(0, min(chunk - 1, int(self.overlap_duration_s * sr)))
        step = chunk - overlap
        if total_samples <= chunk:
            return [(0, total_samples)]
        windows: list[tuple[int, int]] = []
        start = 0
        while start < total_samples:
            end = min(start + chunk, total_samples)
            windows.append((start, end))
            if end == total_samples:
                break
            start += step
        return windows

    def _generate_text(self, pcm: Any, *, source_lang: str, target_lang: str) -> str:
        """Call the underlying `generate(audio, ...)` and pull `.text` out.
        Isolated so tests can spy on the per-window calls.
        """
        out = self._model.generate(
            pcm,
            source_lang=source_lang,
            target_lang=target_lang,
            max_tokens=self.max_tokens_per_chunk,
        )
        return (_lookup(out, "text", "") or "").strip()

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

        # Pre-decode skips mlx-audio's `audio_io.read` (which uses
        # miniaudio for WAVs and ffmpeg for m4a/aac/ogg/opus/webm).
        # The recorder always writes 16 kHz mono 16-bit, which matches
        # Canary's preprocessor exactly — no resampling needed.
        # `load_recorder_wav_as_pcm` rejects unusual WAVs explicitly so
        # the operator gets a clear error instead of a silent ffmpeg
        # dependency.
        pcm = load_recorder_wav_as_pcm(path)
        windows = self._chunk_windows(int(pcm.shape[0]))

        sr = RECORDER_SAMPLE_RATE
        per_chunk: list[tuple[float, float, str]] = []
        full_text_parts: list[str] = []
        for start_sample, end_sample in windows:
            window = pcm[start_sample:end_sample]
            text = self._generate_text(window, source_lang=src, target_lang=tgt)
            per_chunk.append((start_sample / sr, end_sample / sr, text))
            if text:
                full_text_parts.append(text)

        segments = _stitch_chunks(per_chunk)
        text = " ".join(full_text_parts).strip()
        dur = round(wav_duration_s(path), 2)

        # When the model emits no text for any window (silent WAV, model
        # refusal, etc.), fall back to one empty segment covering the WAV
        # so the merged view shows the duration with no text — same
        # convention as the old adapter.
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
            quality_settings={
                "chunk_duration_s": self.chunk_duration_s,
                "overlap_duration_s": self.overlap_duration_s,
                "max_tokens_per_chunk": self.max_tokens_per_chunk,
                "windows": len(windows),
            },
        )
