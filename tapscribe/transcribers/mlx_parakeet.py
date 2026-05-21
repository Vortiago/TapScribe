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

Chunking & ffmpeg-free path
---------------------------
`parakeet-mlx`'s own `model.transcribe(path)` shells out to ffmpeg
to load the audio AND chunks long inputs internally to fit the
encoder's per-call activation budget. We do both ourselves:

1. Pre-decode the recorder's WAV (16 kHz mono 16-bit) into a numpy
   float32 array via `load_recorder_wav_as_pcm`.
2. Split into overlapping windows (`chunk_duration_s` /
   `overlap_duration_s`) and call `model.generate(mel)` per window
   via `parakeet_mlx.audio.get_logmel`. Per-window timestamps are
   shifted by the window's offset so the merged result stays
   session-relative.
3. Stitch the per-window `AlignedResult.sentences` with overlap
   dedup: drop sentences in window N+1 whose start lies before the
   overlap midpoint (those were already transcribed by window N).

There is no ffmpeg fallback. Non-recorder WAVs (mismatched sample
rate / channels / sample width) raise a clear error at pre-decode
time — the operator gets an actionable message instead of a
silent ffmpeg dependency.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ..audio import RECORDER_SAMPLE_RATE, wav_duration_s
from ..wav_predecode import load_recorder_wav_as_pcm
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

# Chunking defaults — 120 s windows with 15 s overlap matches the
# parakeet-mlx authors' own `transcribe()` tuning and fits comfortably
# under a base M1 mini's ~14 GB max-buffer Metal cap. Overridable per
# instance and via env so operators on bigger hardware (M-Max, M-Ultra)
# can grow the window for fewer stitching seams without an adapter
# rebuild. Follow-up PR will plumb per-request dashboard knobs through
# to the constructor.
_DEFAULT_CHUNK_DURATION_S = 120.0
_DEFAULT_OVERLAP_DURATION_S = 15.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[tapscribe] ignoring unparseable {name}={raw!r}; using default {default}", flush=True)
        return default


def _resolve_repo(model_name: str) -> str:
    """Map a catalog model_id to its Hugging Face repo string."""
    return _MODEL_REPO_TABLE.get(model_name, f"mlx-community/{model_name}")


def _attr(payload: Any, name: str, default: Any = None) -> Any:
    """Tolerant accessor: works for dicts and attribute-style objects."""
    if isinstance(payload, dict):
        return payload.get(name, default)
    return getattr(payload, name, default)


def _tokens_to_words(tokens: Any, *, offset_s: float) -> tuple[Word, ...] | None:
    """Convert a sentence's tokens (AlignedToken list) into Word tuples.

    `offset_s` is added to each token's start/end so timestamps stay
    session-relative when the token came from a chunked window.
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
        start = float(_attr(tok, "start", 0.0) or 0.0) + offset_s
        end = float(_attr(tok, "end", 0.0) or 0.0) + offset_s
        out.append(Word(start=round(start, 2), end=round(end, 2), word=text, prob=1.0))
    return tuple(out)


def _sentence_to_segment(sentence: Any, *, offset_s: float) -> TranscriptionSegment:
    """Translate one `AlignedSentence` to a `TranscriptionSegment`,
    shifting start/end by `offset_s` (the window's position in the
    source WAV). For non-chunked callers, `offset_s=0` leaves
    timestamps unchanged."""
    text = (_attr(sentence, "text", "") or "").strip()
    start = float(_attr(sentence, "start", 0.0) or 0.0) + offset_s
    end = float(_attr(sentence, "end", 0.0) or 0.0) + offset_s
    words = _tokens_to_words(_attr(sentence, "tokens", None), offset_s=offset_s)
    return TranscriptionSegment(
        start=round(start, 2),
        end=round(end, 2),
        text=text,
        words=words,
    )


@dataclass(frozen=True)
class _ChunkWindow:
    """One window's worth of work for the chunked transcribe loop."""

    start_sample: int
    end_sample: int
    start_s: float  # cached: start_sample / RECORDER_SAMPLE_RATE


def _stitch_sentences(
    per_window: list[tuple[_ChunkWindow, list[TranscriptionSegment]]],
    *,
    overlap_s: float,
) -> tuple[TranscriptionSegment, ...]:
    """Merge per-window sentence lists into one session-spanning tuple.

    Strategy: for every adjacent pair (N, N+1) the overlap region is
    `[window_{N+1}.start, window_{N+1}.start + overlap_s)`. Sentences
    in window N+1 whose `start` falls before the overlap midpoint were
    already transcribed (and likely identical) in window N — we drop
    them. Above the midpoint, window N+1's sentence wins. This is the
    same crude-but-effective dedup parakeet-mlx uses upstream; if a
    sentence straddles the seam we double-count it. The window is
    sized so straddle is rare in practice; word-level dedup would
    need confidence scores we don't currently have.
    """
    if not per_window:
        return ()
    out: list[TranscriptionSegment] = list(per_window[0][1])
    for prev_idx in range(len(per_window) - 1):
        nxt_window, nxt_sentences = per_window[prev_idx + 1]
        midpoint_s = nxt_window.start_s + overlap_s / 2.0
        for seg in nxt_sentences:
            if seg.start < midpoint_s:
                continue
            out.append(seg)
    return tuple(out)


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

    def __init__(
        self,
        *,
        model_name: str,
        model: Any,
        mel_fn: Callable[[Any, Any], Any] | None = None,
        chunk_duration_s: float | None = None,
        overlap_duration_s: float | None = None,
    ):
        """`mel_fn` is the (pcm_float32_array, preprocessor_config) → mel
        function — production wires `parakeet_mlx.audio.get_logmel` wrapped
        in `mx.array(...)` (resolved lazily on first use so the module
        stays importable on non-Apple hosts). Tests inject a stub so the
        chunked transcribe path can be exercised without parakeet-mlx
        installed.

        `chunk_duration_s` / `overlap_duration_s` default to module
        constants overridable via `TAPSCRIBE_PARAKEET_CHUNK_S` /
        `TAPSCRIBE_PARAKEET_OVERLAP_S` so operators can tune without a
        rebuild.
        """
        self.model_name = model_name
        self._model = model
        self._mel_fn = mel_fn
        self._mel_fn_unavailable = False
        self.chunk_duration_s = (
            chunk_duration_s
            if chunk_duration_s is not None
            else _env_float("TAPSCRIBE_PARAKEET_CHUNK_S", _DEFAULT_CHUNK_DURATION_S)
        )
        self.overlap_duration_s = (
            overlap_duration_s
            if overlap_duration_s is not None
            else _env_float("TAPSCRIBE_PARAKEET_OVERLAP_S", _DEFAULT_OVERLAP_DURATION_S)
        )

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

    def _resolve_mel_fn(self) -> Callable[[Any, Any], Any]:
        """Return the `(pcm, preproc) → mel` function. Cached on
        `self._mel_fn` so the lazy import only happens once.

        Raises `RuntimeError` if the parakeet-mlx audio helpers aren't
        importable — `transcribe()` lets that propagate as a clear,
        actionable error rather than silently falling back to an
        ffmpeg-shelling path. `parakeet_mlx.audio.get_logmel` is not
        part of the README's documented public API; the upper bound on
        `parakeet-mlx` in `pyproject.toml` is the primary defence and
        the smoke test in `tests/test_transcribers_mlx_parakeet.py` is
        the secondary signal.
        """
        if self._mel_fn is not None:
            return self._mel_fn
        try:
            import mlx.core as mx  # type: ignore[import-not-found]  # noqa: PLC0415
            from parakeet_mlx.audio import get_logmel  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as e:
            self._mel_fn_unavailable = True
            raise RuntimeError(
                f"parakeet-mlx pre-decode helpers unavailable ({type(e).__name__}: {e}). "
                "Reinstall parakeet-mlx — the adapter no longer falls back to the "
                "ffmpeg-backed path. See https://github.com/senstella/parakeet-mlx"
            ) from e
        self._mel_fn = lambda pcm, preproc: get_logmel(mx.array(pcm), preproc)
        return self._mel_fn

    def _chunk_windows(self, total_samples: int) -> list[_ChunkWindow]:
        """Return one window per chunk covering the whole PCM, with the
        configured overlap. Always returns at least one window."""
        sr = RECORDER_SAMPLE_RATE
        chunk = max(1, int(self.chunk_duration_s * sr))
        overlap = max(0, min(chunk - 1, int(self.overlap_duration_s * sr)))
        step = chunk - overlap
        if total_samples <= chunk:
            return [_ChunkWindow(0, total_samples, 0.0)]
        windows: list[_ChunkWindow] = []
        start = 0
        while start < total_samples:
            end = min(start + chunk, total_samples)
            windows.append(_ChunkWindow(start, end, start / sr))
            if end == total_samples:
                break
            start += step
        return windows

    def _assert_preproc_sample_rate(self) -> None:
        """Validate the loaded model's preprocessor expects 16 kHz —
        the recorder format. A mismatch means the upstream model file
        was built for a different sample rate; raise instead of
        silently re-sampling (we'd need ffmpeg back) or feeding wrong-
        rate PCM (would silently corrupt output).
        """
        preproc = self._model.preprocessor_config
        try:
            sample_rate = int(getattr(preproc, "sample_rate", 0))
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                "parakeet-mlx preprocessor_config.sample_rate is not readable; "
                "the upstream API may have changed. Pin parakeet-mlx to a known-good "
                "version (see pyproject.toml) and retry."
            ) from e
        if sample_rate != RECORDER_SAMPLE_RATE:
            raise RuntimeError(
                f"parakeet-mlx preprocessor expects sample_rate={sample_rate} but "
                f"the recorder writes {RECORDER_SAMPLE_RATE}. This model variant "
                "is incompatible with the ffmpeg-free pre-decode path; pick a "
                "model whose preprocessor matches the recorder rate."
            )

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,  # noqa: ARG002 — Parakeet doesn't translate
    ) -> TranscriptionResult:
        mel_fn = self._resolve_mel_fn()
        self._assert_preproc_sample_rate()
        preproc = self._model.preprocessor_config

        # Pre-decode skips parakeet-mlx's `load_audio()` which shells
        # out to ffmpeg. `load_recorder_wav_as_pcm` raises on unusual
        # WAV formats — that's the operator's signal to convert the
        # file rather than have the adapter silently depend on ffmpeg.
        pcm = load_recorder_wav_as_pcm(path)
        windows = self._chunk_windows(int(pcm.shape[0]))

        per_window: list[tuple[_ChunkWindow, list[TranscriptionSegment]]] = []
        for window in windows:
            chunk_pcm = pcm[window.start_sample : window.end_sample]
            mel = mel_fn(chunk_pcm, preproc)
            results = self._model.generate(mel)
            if not results:
                # parakeet-mlx's documented contract is non-empty; treat
                # the empty list as "this window had no speech" rather
                # than crashing the whole transcribe.
                per_window.append((window, []))
                continue
            aligned = results[0]
            sentences = _attr(aligned, "sentences", None) or []
            segs = [_sentence_to_segment(s, offset_s=window.start_s) for s in sentences]
            per_window.append((window, segs))

        segments = _stitch_sentences(per_window, overlap_s=self.overlap_duration_s)
        text = " ".join(s.text for s in segments if s.text).strip()

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
            quality_settings={
                "chunk_duration_s": self.chunk_duration_s,
                "overlap_duration_s": self.overlap_duration_s,
                "windows": len(windows),
            },
        )
