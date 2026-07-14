"""Parakeet adapter via Hugging Face `transformers` (CUDA / CPU).

NVIDIA Parakeet-TDT 0.6B v3 is supported natively by `transformers`
(v5+) through `ParakeetForTDT` / `AutoModelForTDT` + `AutoProcessor`.
This replaces the previous NeMo-based adapter: `transformers` is far
lighter (no PyTorch-Lightning / Hydra) and — crucially — carries no
`kaldialign` transitive pin, so it installs cleanly on every platform
(NeMo 2.6+ pinned `kaldialign<=0.9.1`, which has no macOS wheel; that
whole `parakeet-cpu` macOS cap dance is gone).

This is the non-MLX counterpart to `tapscribe.transcribers.mlx_parakeet`.
Same `name="parakeet"`; `backend="parakeet-hf"` distinguishes them on
the dashboard.

API contract (matches the MLX adapter so the registry can dispatch
either transparently):
  - `prompt` / `hotwords`: not supported by Parakeet — accepted on the
    call for protocol parity, dropped at the model call, echoed onto the
    result for audit.
  - `source_lang`: recorded on the result. Parakeet doesn't echo a
    detected language, so we trust the operator's pick. Missing →
    `language="auto"`.

ffmpeg-free path & chunking
---------------------------
`transformers`' `ParakeetProcessor` accepts a raw float32 numpy array,
so the shared chunked skeleton (`_chunked.ChunkedTranscriber`: pre-decode →
overlapping windows → per-window model call → overlap-midpoint stitch)
feeds the recorder's pre-decoded PCM directly — no ffmpeg, no on-disk
path. This adapter implements only the per-window model call.

Timestamps come from the lower-level `model.generate(...,
return_dict_in_generate=True)` + `processor.decode(sequences,
durations=...)` path, NOT the high-level ASR pipeline: the pipeline's
`return_timestamps="word"` assumes a CTC tokenizer (char offsets) and
raises on a TDT transducer. `_parakeet_tdt.build_segments_from_tdt_tokens`
folds the returned token stream into word+segment alignment.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..audio import RECORDER_SAMPLE_RATE
from ..chunking import Window
from ._chunked import ChunkedTranscriber
from ._parakeet_tdt import build_segments_from_tdt_tokens
from .base import TranscriptionSegment

# Catalog model_id → Hugging Face repo. Other Parakeet variants can be
# added by extending the registry entry's `repos` field.


def _resolve_repo(model_name: str) -> str:
    from .catalog import repo_for

    return repo_for(model_name, "parakeet-hf") or f"nvidia/{model_name}"


# Upper bound on generated tokens per window, derived from the window's
# audio length. Without it, `generate` falls back to the model-agnostic
# default `max_length` (~1510), which both warns and risks truncating a
# dense/long window's transcript. Parakeet's Fast-Conformer subsamples to
# ~12.5 encoder frames/s and the TDT decoder emits at most one token per
# frame (duration jumps skip frames, so usually fewer) — 30 tokens/s is a
# safe ceiling well above the real rate, so the decoder still stops
# naturally at end-of-audio and is never cut short. `+ 16` covers BOS/EOS
# and very short clips.
_MAX_TOKENS_PER_S = 30
_MAX_TOKENS_PAD = 16


def _max_new_tokens_for(n_samples: int, sample_rate: int) -> int:
    """Generous per-window token ceiling sized to the audio length."""
    return int(n_samples / sample_rate * _MAX_TOKENS_PER_S) + _MAX_TOKENS_PAD


class ParakeetTranscriber(ChunkedTranscriber):
    """NVIDIA Parakeet loaded via `transformers` on CUDA / CPU.

    Constructor takes a ready-made `model` + `processor` so tests can
    inject mocks; `load()` builds the real ones on CUDA / CPU.
    """

    name: ClassVar[str] = "parakeet"
    backend: ClassVar[str] = "parakeet-hf"

    def __init__(
        self,
        *,
        model_name: str,
        model: Any,
        processor: Any,
        device: str,
        chunk_duration_s: float | None = None,
        overlap_duration_s: float | None = None,
    ):
        super().__init__(
            model_name=model_name,
            chunk_duration_s=chunk_duration_s,
            overlap_duration_s=overlap_duration_s,
        )
        self._model = model
        self._processor = processor
        self.device = device

    @classmethod
    def load(cls, model_name: str, *, kind: str = "auto") -> ParakeetTranscriber:
        import importlib.util

        if importlib.util.find_spec("transformers") is None:
            raise RuntimeError(
                "Parakeet on CUDA/CPU requires the `transformers` package "
                "(>=5.12) and `librosa`. Install with:\n"
                "    pip install -U 'transformers>=5.12' librosa\n"
                "On Apple Silicon, prefer `pip install parakeet-mlx` for the "
                "MLX adapter."
            )

        import torch  # type: ignore
        from transformers import AutoModelForTDT, AutoProcessor  # type: ignore

        repo = _resolve_repo(model_name)
        print(f"[tapscribe] loading transformers Parakeet: {repo}", flush=True)
        processor = AutoProcessor.from_pretrained(repo)
        model = AutoModelForTDT.from_pretrained(repo)

        if kind == "cuda" or (kind == "auto" and torch.cuda.is_available()):
            model = model.to("cuda")
            device_label = "CUDA"
        else:
            device_label = "CPU"
        model.eval()

        instance = cls(model_name=model_name, model=model, processor=processor, device=device_label)
        instance._assert_feature_extractor_sample_rate()
        return instance

    def _assert_feature_extractor_sample_rate(self) -> None:
        """The processor's feature extractor expects a fixed sample rate
        (16 kHz for parakeet-tdt-0.6b-v3, matching the recorder). Fail
        loudly at load if a model variant expects something else, rather
        than silently feeding wrong-rate PCM (would corrupt output) or
        re-sampling (would need ffmpeg back)."""
        fe = getattr(self._processor, "feature_extractor", None)
        sample_rate = getattr(fe, "sampling_rate", None)
        if sample_rate is not None and int(sample_rate) != RECORDER_SAMPLE_RATE:
            raise RuntimeError(
                f"transformers Parakeet feature extractor expects "
                f"sampling_rate={sample_rate} but the recorder writes "
                f"{RECORDER_SAMPLE_RATE}. This model variant is incompatible "
                "with the ffmpeg-free pre-decode path; pick a model whose "
                "feature extractor matches the recorder rate."
            )

    def _transcribe_window(self, chunk_pcm: Any, window: Window) -> tuple[TranscriptionSegment, ...]:
        import numpy as np

        sr = RECORDER_SAMPLE_RATE
        chunk = np.asarray(chunk_pcm, dtype=np.float32)
        inputs = self._processor([chunk], sampling_rate=sr)
        inputs = inputs.to(self._model.device, dtype=self._model.dtype)
        output = self._model.generate(
            **inputs,
            return_dict_in_generate=True,
            max_new_tokens=_max_new_tokens_for(int(chunk.shape[0]), sr),
        )
        _, token_lists = self._processor.decode(
            output.sequences, durations=output.durations, skip_special_tokens=True
        )
        tokens = token_lists[0] if token_lists else []
        return build_segments_from_tdt_tokens(tokens, offset_s=window.start_s)
