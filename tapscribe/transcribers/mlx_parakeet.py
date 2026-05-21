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

from collections.abc import Callable
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

    def __init__(
        self,
        *,
        model_name: str,
        model: Any,
        mel_fn: Callable[[Any, Any], Any] | None = None,
    ):
        """`mel_fn` is the (pcm_float32_array, preprocessor_config) → mel
        function — production wires `parakeet_mlx.audio.get_logmel` wrapped
        in `mx.array(...)` (resolved lazily on first use so the module
        stays importable on non-Apple hosts). Tests inject a stub so
        `_transcribe_via_generate` can be exercised without parakeet-mlx
        installed.
        """
        self.model_name = model_name
        self._model = model
        self._mel_fn = mel_fn
        # Latches True after `_resolve_mel_fn` hits an ImportError so a
        # host without parakeet-mlx pays the import cost (and prints the
        # log line) once per instance instead of once per request. In
        # practice `MlxParakeetTranscriber.load()` already raises before
        # any instance is built without parakeet-mlx, but tests and
        # future refactors that construct the adapter directly hit this
        # path.
        self._mel_fn_unavailable = False

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

    def _resolve_mel_fn(self) -> Callable[[Any, Any], Any] | None:
        """Return the `(pcm, preproc) → mel` function for the pre-decode
        path. Cached on `self._mel_fn` so the lazy import only happens
        once per transcriber instance. Returns None (with a logged
        reason) if the parakeet-mlx audio helpers aren't importable —
        the caller falls back to the ffmpeg-backed path.

        `parakeet_mlx.audio.get_logmel` is not part of the README's
        documented public API (`from_pretrained` is). If upstream
        relocates it in a point release this lazy import is where the
        break surfaces — the upper bound on `parakeet-mlx` in
        `pyproject.toml` is the primary defence; the import-time log
        line is the secondary signal so the operator sees ffmpeg is
        being used again before they hit a host without it.
        """
        if self._mel_fn is not None:
            return self._mel_fn
        if self._mel_fn_unavailable:
            # Already tried and failed once on this instance — don't
            # re-import or re-log on every subsequent transcribe.
            return None
        try:
            import mlx.core as mx  # type: ignore[import-not-found]  # noqa: PLC0415
            from parakeet_mlx.audio import get_logmel  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as e:
            print(
                f"[tapscribe] parakeet pre-decode helpers unavailable "
                f"({type(e).__name__}: {e}); using model.transcribe(path) "
                "which needs ffmpeg on PATH (logged once per instance).",
                flush=True,
            )
            self._mel_fn_unavailable = True
            return None
        self._mel_fn = lambda pcm, preproc: get_logmel(mx.array(pcm), preproc)
        return self._mel_fn

    def _transcribe_via_generate(self, path: Path) -> Any | None:
        """Pre-decode + `model.generate(mel)` short-circuit so we never
        shell out to ffmpeg in the common case.

        Returns the AlignedResult on success, or None for every failure
        mode — the caller falls back to `self._model.transcribe(
        str(path))`, which is parakeet-mlx's own ffmpeg-backed path.
        Each failure mode logs its own specific reason so the operator
        can tell "mismatched sample rate" from "unusual WAV" from
        "upstream API changed" in the recorder log.
        """
        mel_fn = self._resolve_mel_fn()
        if mel_fn is None:
            return None

        preproc = self._model.preprocessor_config
        try:
            sample_rate = int(getattr(preproc, "sample_rate", 0))
        except (TypeError, ValueError):
            # Either preprocessor_config doesn't expose sample_rate, or its
            # value isn't int-coercible. The fallback handles this — log so
            # a parakeet-mlx upgrade that renamed the attribute is visible.
            print(
                "[tapscribe] parakeet preprocessor_config.sample_rate not "
                "readable; using model.transcribe(path) which needs ffmpeg "
                "on PATH.",
                flush=True,
            )
            return None
        if sample_rate != RECORDER_SAMPLE_RATE:
            print(
                f"[tapscribe] parakeet preprocessor sample_rate {sample_rate} "
                f"!= recorder {RECORDER_SAMPLE_RATE}; using model.transcribe("
                "path) so ffmpeg can resample.",
                flush=True,
            )
            return None

        try:
            pcm = load_recorder_wav_as_pcm(path)
        except (RuntimeError, OSError) as e:
            print(
                f"[tapscribe] parakeet WAV pre-decode rejected ({e}); using "
                "model.transcribe(path) which needs ffmpeg on PATH.",
                flush=True,
            )
            return None

        try:
            mel = mel_fn(pcm, preproc)
            results = self._model.generate(mel)
        except Exception as e:  # noqa: BLE001 — fallback covers every failure mode here
            # `parakeet_mlx`'s generate / get_logmel internals can raise a
            # variety of mlx / numpy errors; catching broadly so a single
            # bad input doesn't tank the batch transcribe — the fallback
            # path retries via the ffmpeg-backed model.transcribe.
            print(
                f"[tapscribe] parakeet generate(mel) failed ({type(e).__name__}"
                f": {e}); using model.transcribe(path) which needs ffmpeg "
                "on PATH.",
                flush=True,
            )
            return None

        if not results:
            # parakeet-mlx's own `transcribe()` does `results[0]` unconditionally,
            # so a non-empty list is the documented contract. Defensive None here
            # routes through the ffmpeg fallback instead of IndexError-ing into
            # Starlette — protects against a future regression in either branch.
            # Logged (like every other bail-out in this function) so a recurring
            # fallback shows up in the recorder log with its cause.
            print(
                "[tapscribe] parakeet generate(mel) returned empty list; "
                "using model.transcribe(path) which needs ffmpeg on PATH.",
                flush=True,
            )
            return None
        return results[0]

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,  # noqa: ARG002 — Parakeet doesn't translate
    ) -> TranscriptionResult:
        # parakeet-mlx's `transcribe(path)` calls its bundled `load_audio()`,
        # which shells out to `ffmpeg` — so a host without ffmpeg fails
        # at request time with `RuntimeError("FFmpeg is not installed …")`
        # deep in Starlette middleware. The recorder always writes 16 kHz
        # mono int16 (matches parakeet's preprocessor sample rate exactly),
        # so we can pre-decode the WAV ourselves, build the log-mel via
        # `parakeet_mlx.audio.get_logmel`, and call the model's lower-level
        # `generate(mel)` directly. Same pattern as `mlx_whisper`'s
        # pre-decode short-circuit. The fallback to the path-based
        # `transcribe()` is kept for the rare unusual-WAV case (e.g. a
        # session WAV that didn't come from the recorder) — that path
        # still needs ffmpeg.
        aligned = self._transcribe_via_generate(path)
        if aligned is None:
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
