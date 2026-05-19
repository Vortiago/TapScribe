"""Canary adapter via NVIDIA NeMo Toolkit (CUDA / CPU).

NeMo's `ASRModel.from_pretrained("nvidia/canary-1b-v2")` returns a model
with a `transcribe([paths], source_lang=..., target_lang=...,
timestamps=True)` method that yields word- and segment-level
timestamps and supports X↔English translation in addition to
transcription.

This is the non-MLX counterpart to `tapscribe.transcribers.mlx_canary`.
Same `name="canary"`; `backend="canary-nemo"` distinguishes them on
the dashboard. The translation contract — `source_language` always
set, `target_language` set only when it differs — is shared between
both adapters so the dashboard's badge logic is backend-agnostic.

NeMo is heavyweight (~PyTorch + Lightning + Hydra). The optional-dep
group `canary` pulls it; the lazy import keeps every other code path
free of that cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment, Word

# HF repo NeMo's `from_pretrained` resolves canary against.
_HF_REPO_TABLE: dict[str, str] = {
    "canary-1b-v2": "nvidia/canary-1b-v2",
}


def _resolve_repo(model_name: str) -> str:
    return _HF_REPO_TABLE.get(model_name, f"nvidia/{model_name}")


def _lookup(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _word_from_payload(payload: Any) -> Word:
    text = (_lookup(payload, "word", "") or _lookup(payload, "text", "") or "")
    start = float(_lookup(payload, "start", 0.0) or 0.0)
    end = float(_lookup(payload, "end", 0.0) or 0.0)
    return Word(start=round(start, 2), end=round(end, 2), word=text, prob=1.0)


def _build_segments(
    segment_dicts: list[Any], word_dicts: list[Any]
) -> tuple[TranscriptionSegment, ...]:
    if not segment_dicts:
        return ()
    all_words = [_word_from_payload(w) for w in word_dicts]
    out: list[TranscriptionSegment] = []
    for seg in segment_dicts:
        start = float(_lookup(seg, "start", 0.0) or 0.0)
        end = float(_lookup(seg, "end", 0.0) or 0.0)
        text = (_lookup(seg, "segment", "") or _lookup(seg, "text", "") or "").strip()
        in_range = tuple(
            w for w in all_words if w.start >= start - 1e-3 and w.end <= end + 1e-3
        )
        out.append(
            TranscriptionSegment(
                start=round(start, 2),
                end=round(end, 2),
                text=text,
                words=in_range or None,
            )
        )
    return tuple(out)


class CanaryTranscriber:
    """NVIDIA Canary loaded via NeMo on CUDA / CPU."""

    name: ClassVar[str] = "canary"
    backend: ClassVar[str] = "canary-nemo"

    def __init__(self, *, model_name: str, model: Any, device: str):
        self.model_name = model_name
        self._model = model
        self.device = device

    @classmethod
    def load(cls, model_name: str, *, kind: str = "auto") -> CanaryTranscriber:
        import importlib.util

        # NeMo is a namespace package; probe the collections.asr submodule
        # rather than just `nemo` (the top namespace can exist even when
        # the ASR collection isn't installed).
        if (
            importlib.util.find_spec("nemo") is None
            or importlib.util.find_spec("nemo.collections") is None
            or importlib.util.find_spec("nemo.collections.asr") is None
        ):
            raise RuntimeError(
                "Canary on CUDA/CPU requires NVIDIA NeMo Toolkit's ASR "
                "collection. Install with:\n"
                "    pip install -U 'nemo_toolkit[asr]>=2.5'\n"
                "Note: this is a large dependency (~PyTorch + Lightning + "
                "Hydra). On Apple Silicon, prefer `pip install mlx-audio` "
                "for the MLX adapter instead."
            )

        import nemo.collections.asr as nemo_asr  # type: ignore
        import torch  # type: ignore

        repo = _resolve_repo(model_name)
        print(f"[tapscribe] loading NeMo Canary: {repo}", flush=True)
        model = nemo_asr.models.ASRModel.from_pretrained(repo)

        # Pin the device. NeMo models default to CPU unless moved; CUDA
        # placement happens via `model.to(device)`.
        if kind == "cuda" or (kind == "auto" and torch.cuda.is_available()):
            model = model.to("cuda")
            device_label = "CUDA"
        else:
            device_label = "CPU"
        model.eval()
        return cls(model_name=model_name, model=model, device=device_label)

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,
    ) -> TranscriptionResult:
        src = source_lang or "en"
        tgt = target_lang or "en"
        responses = self._model.transcribe(
            [str(path)], source_lang=src, target_lang=tgt, timestamps=True
        )
        result = responses[0]
        text = (_lookup(result, "text", "") or "").strip()
        timestamps = _lookup(result, "timestamp", {}) or {}
        seg_list = timestamps.get("segment", []) if isinstance(timestamps, dict) else []
        word_list = timestamps.get("word", []) if isinstance(timestamps, dict) else []

        segments = _build_segments(seg_list, word_list)
        dur = round(wav_duration_s(path), 2)
        if not segments and text:
            segments = (
                TranscriptionSegment(start=0.0, end=dur, text=text, words=None),
            )

        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language=src,
            language_probability=0.0,
            duration=dur,
            text=text,
            segments=segments,
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={"timestamps": True},
            source_language=src,
            target_language=tgt if tgt != src else "",
        )
