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

Chunking & ffmpeg-free path
---------------------------
`parakeet-mlx`'s own `model.transcribe(path)` shells out to ffmpeg to
load the audio AND chunks long inputs internally to fit the encoder's
per-call activation budget. The shared chunked skeleton
(`_chunked.ChunkedTranscriber`: pre-decode → overlapping windows →
per-window model call → overlap-midpoint stitch) does both instead; this
adapter implements only the per-window `model.generate(mel)` call via
`parakeet_mlx.audio.get_logmel`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from ..audio import RECORDER_SAMPLE_RATE
from ..chunking import Window
from ._chunked import ChunkedTranscriber
from .base import (
    TranscriptionSegment,
    Word,
    _lookup,
)

# Default repo on Hugging Face — `from_pretrained` resolves the catalog
# model_id `parakeet-tdt-0.6b-v3` to this repo. Additional variants can
# be added by extending the registry entry's `repos` field.


def _resolve_repo(model_name: str) -> str:
    """Map a catalog model_id to its Hugging Face repo string."""
    from . import catalog

    return catalog.resolve_repo(model_name, "parakeet-mlx", lambda n: f"mlx-community/{n}")


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


class MlxParakeetTranscriber(ChunkedTranscriber):
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
        super().__init__(
            model_name=model_name,
            chunk_duration_s=chunk_duration_s,
            overlap_duration_s=overlap_duration_s,
        )
        self._model = model
        self._mel_fn = mel_fn

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

    def _transcribe_window(self, chunk_pcm: Any, window: Window) -> list[TranscriptionSegment]:
        mel_fn = self._resolve_mel_fn()
        preproc = self._model.preprocessor_config
        mel = mel_fn(chunk_pcm, preproc)
        results = self._model.generate(mel)
        if not results:
            # parakeet-mlx's documented contract is non-empty; treat
            # the empty list as "this window had no speech" rather
            # than crashing the whole transcribe.
            return []
        aligned = results[0]
        sentences = _lookup(aligned, "sentences", None) or []
        return [_sentence_to_segment(s, offset_s=window.start_s) for s in sentences]
