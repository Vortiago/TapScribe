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
  - `target_lang`: ignored. Parakeet doesn't translate.

ffmpeg-free path & chunking
---------------------------
`transformers`' `ParakeetProcessor` accepts a raw float32 numpy array,
so we feed the recorder's pre-decoded PCM (`load_recorder_wav_as_pcm`,
16 kHz mono) directly — no ffmpeg, no on-disk path. Long sessions are
split into overlapping windows (`chunk_duration_s` / `overlap_duration_s`)
and generated one window at a time so a multi-hour meeting doesn't build
one enormous activation tensor; per-window token timestamps are shifted
by the window offset and the windows are stitched with the same
overlap-midpoint dedup the MLX adapter uses.

Timestamps come from the lower-level `model.generate(...,
return_dict_in_generate=True)` + `processor.decode(sequences,
durations=...)` path, NOT the high-level ASR pipeline: the pipeline's
`return_timestamps="word"` assumes a CTC tokenizer (char offsets) and
raises on a TDT transducer. `_parakeet_tdt.build_segments_from_tdt_tokens`
folds the returned token stream into word+segment alignment.

There is no path-based fallback. Non-recorder WAVs raise a clear error
at pre-decode time — the operator's signal to convert the file, not a
cue to silently re-introduce ffmpeg.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..audio import RECORDER_SAMPLE_RATE
from ..chunking import Window, chunk_windows
from ..config import env_float
from ..wav_predecode import load_recorder_wav_as_pcm
from ._parakeet_tdt import build_segments_from_tdt_tokens
from .base import TranscriptionResult, TranscriptionSegment, build_transcription_result

# Catalog model_id → Hugging Face repo. Other Parakeet variants can be
# added by extending this table and registering them in catalog.py.
_MODEL_REPO_TABLE: dict[str, str] = {
    "parakeet-tdt-0.6b-v3": "nvidia/parakeet-tdt-0.6b-v3",
}

# Chunking defaults — shared operator knob with the MLX adapter
# (same env names): 120 s windows, 15 s overlap. Generous enough that
# typical sessions run in one window, small enough that a multi-hour
# recording won't build one giant activation tensor on CPU/CUDA.
_DEFAULT_CHUNK_DURATION_S = 120.0
_DEFAULT_OVERLAP_DURATION_S = 15.0

ENV_CHUNK_S = "TAPSCRIBE_PARAKEET_CHUNK_S"
ENV_OVERLAP_S = "TAPSCRIBE_PARAKEET_OVERLAP_S"

_CHUNK_S_BOUNDS = (1.0, 600.0)
_OVERLAP_S_BOUNDS = (0.0, 60.0)

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


def _resolve_repo(model_name: str) -> str:
    return _MODEL_REPO_TABLE.get(model_name, f"nvidia/{model_name}")


def _stitch_segments(
    per_window: list[tuple[Window, tuple[TranscriptionSegment, ...]]],
    *,
    overlap_s: float,
) -> tuple[TranscriptionSegment, ...]:
    """Merge per-window segment tuples into one session-spanning tuple,
    dropping window N+1 segments that start before the overlap midpoint
    (already transcribed by window N). Same crude-but-effective dedup the
    MLX adapter uses; a segment straddling the seam is double-counted."""
    if not per_window:
        return ()
    out: list[TranscriptionSegment] = list(per_window[0][1])
    for prev_idx in range(len(per_window) - 1):
        nxt_window, nxt_segs = per_window[prev_idx + 1]
        midpoint_s = nxt_window.start_s + overlap_s / 2.0
        out.extend(seg for seg in nxt_segs if seg.start >= midpoint_s)
    return tuple(out)


class ParakeetTranscriber:
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
        self.model_name = model_name
        self._model = model
        self._processor = processor
        self.device = device
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

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,  # noqa: ARG002 — Parakeet doesn't translate
    ) -> TranscriptionResult:
        import numpy as np

        # Pre-decode skips any ffmpeg load. `load_recorder_wav_as_pcm`
        # raises on unusual WAV formats — the operator's signal to
        # convert the file, not a cue to re-introduce ffmpeg.
        pcm = load_recorder_wav_as_pcm(path)
        sr = RECORDER_SAMPLE_RATE
        windows = chunk_windows(
            int(pcm.shape[0]),
            chunk_s=self.chunk_duration_s,
            overlap_s=self.overlap_duration_s,
        )

        per_window: list[tuple[Window, tuple[TranscriptionSegment, ...]]] = []
        for window in windows:
            chunk_pcm = np.asarray(pcm[window.start_sample : window.end_sample], dtype=np.float32)
            inputs = self._processor([chunk_pcm], sampling_rate=sr)
            inputs = inputs.to(self._model.device, dtype=self._model.dtype)
            output = self._model.generate(
                **inputs,
                return_dict_in_generate=True,
                max_new_tokens=_max_new_tokens_for(int(chunk_pcm.shape[0]), sr),
            )
            _, token_lists = self._processor.decode(
                output.sequences, durations=output.durations, skip_special_tokens=True
            )
            tokens = token_lists[0] if token_lists else []
            segs = build_segments_from_tdt_tokens(tokens, offset_s=window.start_s)
            per_window.append((window, segs))

        segments = _stitch_segments(per_window, overlap_s=self.overlap_duration_s)
        text = " ".join(s.text for s in segments if s.text).strip()
        duration = pcm.shape[0] / RECORDER_SAMPLE_RATE

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
