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
encoder's per-call activation budget. The adapter does both itself:

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

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from ..audio import RECORDER_SAMPLE_RATE
from ..chunking import Window, chunk_windows
from ..config import env_float
from ..wav_predecode import load_recorder_wav_as_pcm
from .base import (
    TranscriptionResult,
    TranscriptionSegment,
    Word,
    _lookup,
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
# under a base M1 mini's ~14 GB max-buffer Metal cap. Operator-tunable
# via env, hoisted to module constants so typos surface as NameError.
_DEFAULT_CHUNK_DURATION_S = 120.0
_DEFAULT_OVERLAP_DURATION_S = 15.0

ENV_CHUNK_S = "TAPSCRIBE_PARAKEET_CHUNK_S"
ENV_OVERLAP_S = "TAPSCRIBE_PARAKEET_OVERLAP_S"

# Operator-knob bounds. Out-of-range env values are rejected by
# `env_float` (logged + default used).
_CHUNK_S_BOUNDS = (1.0, 600.0)
_OVERLAP_S_BOUNDS = (0.0, 60.0)


def _resolve_repo(model_name: str) -> str:
    """Map a catalog model_id to its Hugging Face repo string."""
    return _MODEL_REPO_TABLE.get(model_name, f"mlx-community/{model_name}")


def _tokens_to_words(tokens: Any, *, offset_s: float) -> tuple[Word, ...] | None:
    """Convert a sentence's tokens (AlignedToken list) into Word tuples,
    shifting each token's start/end by `offset_s` so timestamps stay
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
        text = _lookup(tok, "text", "") or ""
        start = float(_lookup(tok, "start", 0.0) or 0.0) + offset_s
        end = float(_lookup(tok, "end", 0.0) or 0.0) + offset_s
        out.append(Word(start=round(start, 2), end=round(end, 2), word=text, prob=1.0))
    return tuple(out)


def _sentence_to_segment(sentence: Any, *, offset_s: float) -> TranscriptionSegment:
    text = (_lookup(sentence, "text", "") or "").strip()
    start = float(_lookup(sentence, "start", 0.0) or 0.0) + offset_s
    end = float(_lookup(sentence, "end", 0.0) or 0.0) + offset_s
    words = _tokens_to_words(_lookup(sentence, "tokens", None), offset_s=offset_s)
    return TranscriptionSegment(
        start=round(start, 2),
        end=round(end, 2),
        text=text,
        words=words,
    )


def _stitch_sentences(
    per_window: list[tuple[Window, list[TranscriptionSegment]]],
    *,
    overlap_s: float,
) -> tuple[TranscriptionSegment, ...]:
    """Merge per-window sentence lists into one session-spanning tuple.

    For every adjacent pair (N, N+1) the overlap region is
    `[window_{N+1}.start, window_{N+1}.start + overlap_s)`. Sentences
    in window N+1 whose `start` falls before the overlap midpoint were
    already transcribed (and likely identical) in window N — they get
    dropped. Above the midpoint, window N+1's sentence wins. This is
    the same crude-but-effective dedup parakeet-mlx uses upstream; if a
    sentence straddles the seam we double-count it. Word-level dedup
    would need confidence scores we don't currently have.
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
        function — production wires `parakeet_mlx.audio.get_logmel`
        wrapped in `mx.array(...)` (resolved lazily on first use so the
        module stays importable on non-Apple hosts). Tests inject a
        stub so the chunked transcribe path can be exercised without
        parakeet-mlx installed.
        """
        self.model_name = model_name
        self._model = model
        self._mel_fn = mel_fn
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
        instance = cls(model_name=model_name, model=model)
        # Validate up-front: the preprocessor's sample rate is an
        # immutable property of the loaded model; better to fail loudly
        # at load time than on the first transcribe.
        instance._assert_preproc_sample_rate()
        return instance

    def _resolve_mel_fn(self) -> Callable[[Any, Any], Any]:
        """Return the `(pcm, preproc) → mel` function. Cached on
        `self._mel_fn` so the lazy import only happens once.

        Raises `RuntimeError` if the parakeet-mlx audio helpers aren't
        importable — `transcribe()` lets that propagate as a clear,
        actionable error rather than silently falling back to an
        ffmpeg-shelling path. The smoke test in
        `tests/test_transcribers_mlx_parakeet.py` is the secondary
        signal that catches an upstream rename.
        """
        if self._mel_fn is not None:
            return self._mel_fn
        try:
            import mlx.core as mx  # type: ignore[import-not-found]  # noqa: PLC0415
            from parakeet_mlx.audio import get_logmel  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                f"parakeet-mlx pre-decode helpers unavailable ({type(e).__name__}: {e}). "
                "Reinstall parakeet-mlx — the adapter no longer falls back to the "
                "ffmpeg-backed path. See https://github.com/senstella/parakeet-mlx"
            ) from e
        self._mel_fn = lambda pcm, preproc: get_logmel(mx.array(pcm), preproc)
        return self._mel_fn

    def _assert_preproc_sample_rate(self) -> None:
        """Validate the loaded model's preprocessor expects 16 kHz (the
        recorder format). Called once at `load()`. A mismatch means the
        upstream model file was built for a different sample rate;
        raise instead of silently re-sampling (would need ffmpeg back)
        or feeding wrong-rate PCM (would silently corrupt output).
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
        preproc = self._model.preprocessor_config

        # Pre-decode skips parakeet-mlx's `load_audio()` which shells
        # out to ffmpeg. `load_recorder_wav_as_pcm` raises on unusual
        # WAV formats — that's the operator's signal to convert the
        # file rather than have the adapter silently depend on ffmpeg.
        pcm = load_recorder_wav_as_pcm(path)
        windows = chunk_windows(
            int(pcm.shape[0]),
            chunk_s=self.chunk_duration_s,
            overlap_s=self.overlap_duration_s,
        )

        per_window: list[tuple[Window, list[TranscriptionSegment]]] = []
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
            sentences = _lookup(aligned, "sentences", None) or []
            segs = [_sentence_to_segment(s, offset_s=window.start_s) for s in sentences]
            per_window.append((window, segs))

        segments = _stitch_sentences(per_window, overlap_s=self.overlap_duration_s)
        text = " ".join(s.text for s in segments if s.text).strip()
        duration = pcm.shape[0] / RECORDER_SAMPLE_RATE

        # Parakeet doesn't echo a detected language; record the hint
        # the operator pinned, or "auto" when they didn't.
        return build_transcription_result(
            self,
            text=text,
            segments=segments,
            duration=duration,
            language=source_lang or "auto",
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
            quality_settings={
                "chunk_duration_s": self.chunk_duration_s,
                "overlap_duration_s": self.overlap_duration_s,
            },
        )
