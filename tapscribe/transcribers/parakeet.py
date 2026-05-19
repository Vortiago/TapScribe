"""Parakeet adapter via NVIDIA NeMo Toolkit (CUDA / CPU).

NVIDIA Parakeet-TDT 0.6B v3 is supported officially through NeMo's
`ASRModel.from_pretrained("nvidia/parakeet-tdt-0.6b-v3")` factory, which
returns a model whose `transcribe([paths], timestamps=True)` method
yields word- and segment-level timestamps natively (Parakeet's headline
differentiator versus Voxtral's prose blob).

Why NeMo and not HF transformers: as of mid-2026 the released
`transformers` packages don't carry the `parakeet_tdt` model type
mapping — `AutoConfig.from_pretrained("nvidia/parakeet-tdt-0.6b-v3")`
fails with `KeyError: 'parakeet_tdt'`. The integration is landing in
transformers main branch; when it ships in a stable release we can
add a `parakeet-hf` adapter in parallel and let the registry route
explicitly. Until then NeMo is the right CUDA/CPU path.

This is the non-MLX counterpart to `tapscribe.transcribers.mlx_parakeet`.
Same `name="parakeet"`; `backend="parakeet-nemo"` distinguishes them
on the dashboard. NeMo is heavyweight (~PyTorch + Lightning + Hydra);
the optional-dep group `parakeet` pulls it. The lazy import keeps
every other code path free of that cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment, Word

# Default NeMo repo. Other Parakeet variants (e.g. parakeet-rnnt-1.1b)
# can be added later by extending this table and registering them in
# catalog.py.
_NEMO_REPO_TABLE: dict[str, str] = {
    "parakeet-tdt-0.6b-v3": "nvidia/parakeet-tdt-0.6b-v3",
}


def _resolve_repo(model_name: str) -> str:
    return _NEMO_REPO_TABLE.get(model_name, f"nvidia/{model_name}")


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
    """Pair NeMo's segment list with its word list.

    Same shape as `mlx_canary._build_segments` / `canary._build_segments`
    — words whose timing falls inside a segment's [start, end] range
    get attached to it. Words outside every segment are dropped from
    the segments' `words` field (still visible in the raw NeMo output
    if a future feature wants to surface them).
    """
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


class ParakeetTranscriber:
    """NVIDIA Parakeet loaded via NeMo on CUDA / CPU.

    Constructor takes a ready-made `model` so tests can inject a mock;
    `load()` builds the real one on CUDA / CPU.
    """

    name: ClassVar[str] = "parakeet"
    backend: ClassVar[str] = "parakeet-nemo"

    def __init__(self, *, model_name: str, model: Any, device: str):
        self.model_name = model_name
        self._model = model
        self.device = device

    @classmethod
    def load(cls, model_name: str, *, kind: str = "auto") -> ParakeetTranscriber:
        import importlib.util

        # NeMo is a namespace package; probe the collections.asr submodule
        # rather than just `nemo` (the top namespace can exist even when
        # the ASR collection isn't installed). The error message stays
        # forward-pointing — once `transformers` ships `parakeet_tdt`
        # support, this adapter can grow a fallback path.
        if (
            importlib.util.find_spec("nemo") is None
            or importlib.util.find_spec("nemo.collections") is None
            or importlib.util.find_spec("nemo.collections.asr") is None
        ):
            raise RuntimeError(
                "Parakeet on CUDA/CPU requires NVIDIA NeMo Toolkit's ASR "
                "collection (the released `transformers` packages don't "
                "yet have the `parakeet_tdt` model type). Install with:\n"
                "    pip install -U 'nemo_toolkit[asr]>=2.5'\n"
                "Note: this is a large dependency. On Apple Silicon, "
                "prefer `pip install parakeet-mlx` for the MLX adapter."
            )

        import nemo.collections.asr as nemo_asr  # type: ignore
        import torch  # type: ignore

        repo = _resolve_repo(model_name)
        print(f"[tapscribe] loading NeMo Parakeet: {repo}", flush=True)
        model = nemo_asr.models.ASRModel.from_pretrained(repo)

        # Pin the device. NeMo models default to CPU unless moved.
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
        target_lang: str | None = None,  # noqa: ARG002 — Parakeet doesn't translate
    ) -> TranscriptionResult:
        # NeMo's transcribe returns a list (one entry per input path),
        # each with `.text` and `.timestamp = {"word": [...], "segment": [...]}`.
        responses = self._model.transcribe([str(path)], timestamps=True)
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
            language=source_lang or "auto",
            language_probability=0.0,
            duration=dur,
            text=text,
            segments=segments,
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={"timestamps": True},
            source_language=source_lang or "",
        )
