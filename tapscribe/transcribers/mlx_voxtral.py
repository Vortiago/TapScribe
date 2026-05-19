"""MLX Voxtral adapter (community port for Apple Silicon).

Parallel to `tapscribe.transcribers.voxtral` but uses the `mlx_voxtral`
package (https://github.com/mzbac/mlx.voxtral / pip install mlx-voxtral)
instead of `transformers`. The high-level call shape mirrors the HF
adapter — apply_transcrition_request → generate → decode — so most of the
adapter is the same; the differences are:

  - The upstream method is spelled `apply_transcrition_request` (sic —
    note the missing 'c'). We forward that name as-is. A regression test
    locks the spelling so a future upstream rename to match HF will fail
    loudly here rather than at first run.
  - MLX arrays live in unified memory; no `.to(device)` step.
  - `processor.decode(...)` returns a single string instead of HF's
    `batch_decode(...) -> list[str]`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment, default_language_for

# Quantised MLX builds live under mlx-community. Default to bf16 — full
# quality with the MLX speedup; users on tight RAM can swap to 4bit/8bit
# by editing the constant below or routing through a config knob later.
_MLX_VOXTRAL_REPO = "mlx-community/Voxtral-Mini-3B-2507-bf16"


class MlxVoxtralTranscriber:
    """A Voxtral model loaded via mlx_voxtral, satisfying the `Transcriber`
    Protocol. Same `name="voxtral"` as the HF adapter so the dashboard
    treats them as the same model family; `backend` disambiguates."""

    name: ClassVar[str] = "voxtral"
    backend: ClassVar[str] = "mlx-voxtral"
    device: ClassVar[str] = "Apple Silicon GPU"

    def __init__(self, *, model_name: str, processor: Any, model: Any):
        self.model_name = model_name
        self._processor = processor
        self._model = model

    @classmethod
    def load(cls, model_name: str) -> MlxVoxtralTranscriber:
        import importlib.util

        if importlib.util.find_spec("mlx_voxtral") is None:
            raise RuntimeError(
                "MLX Voxtral requires the `mlx-voxtral` package "
                "(Apple Silicon only). Install with:\n"
                "    pip install mlx-voxtral\n"
                "See https://github.com/mzbac/mlx.voxtral"
            )

        # Imported lazily so non-Apple-Silicon hosts can still import this
        # module (for type checks and factory dispatch) without crashing.
        from mlx_voxtral import (  # type: ignore
            VoxtralForConditionalGeneration,
            VoxtralProcessor,
        )

        print(f"[tapscribe] loading mlx-voxtral model: {_MLX_VOXTRAL_REPO}", flush=True)
        processor = VoxtralProcessor.from_pretrained(_MLX_VOXTRAL_REPO)
        model = VoxtralForConditionalGeneration.from_pretrained(_MLX_VOXTRAL_REPO)
        return cls(model_name=model_name, processor=processor, model=model)

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> TranscriptionResult:
        # apply_transcrition_request mirrors HF's apply_transcription_request
        # (typo preserved); prompt + hotwords have no place in this call so
        # we drop them but record them on the result for protocol parity.
        language = default_language_for(self.model_name)
        request_kwargs: dict[str, Any] = {"audio": str(path), "model_id": _MLX_VOXTRAL_REPO}
        if language:
            request_kwargs["language"] = language
        inputs = self._processor.apply_transcrition_request(**request_kwargs)

        gen_kwargs: dict[str, Any] = dict(
            max_new_tokens=2048,
            temperature=0.0,
        )
        outputs = self._model.generate(**_inputs_kwargs(inputs), **gen_kwargs)

        prompt_len = inputs.input_ids.shape[1] if hasattr(inputs, "input_ids") else 0
        # mlx-voxtral's processor.decode takes a single token sequence and
        # returns one string (unlike HF transformers' batch_decode).
        gen_ids = outputs[0][prompt_len:]
        text_value = self._processor.decode(gen_ids, skip_special_tokens=True)
        text = (text_value or "").strip()

        dur = wav_duration_s(path)
        seg = TranscriptionSegment(start=0.0, end=round(dur, 2), text=text)
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            # Voxtral doesn't echo a detected language; record the hint
            # we sent, or "auto" when we let it auto-detect.
            language=language or "auto",
            language_probability=0.0,
            duration=round(dur, 2),
            text=text,
            segments=(seg,),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings=dict(gen_kwargs),
        )


def _inputs_kwargs(inputs: Any) -> dict[str, Any]:
    """Best-effort: convert the processor output into kwargs for `.generate()`.

    Same helper as the HF voxtral adapter — the processor output supports
    `**inputs` in practice; this exists so a MagicMock in tests can still
    be unpacked without raising."""
    try:
        return dict(inputs)
    except (TypeError, ValueError):
        if hasattr(inputs, "input_ids"):
            return {"input_ids": inputs.input_ids}
        return {}
