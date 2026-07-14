"""MLX Voxtral adapter (community port for Apple Silicon).

Parallel to `tapscribe.transcribers.voxtral` but uses the `mlx_voxtral`
package (https://github.com/mzbac/mlx.voxtral / pip install mlx-voxtral)
instead of `transformers`. The shared transcribe flow lives in
`_voxtral_common.py`'s `VoxtralTranscriberBase`; this module implements
only the mlx-voxtral-specific hooks, which differ from the HF adapter in:

  - The upstream method is spelled `apply_transcrition_request` (sic —
    note the missing 'c'). We forward that name as-is. A regression test
    locks the spelling so a future upstream rename to match HF will fail
    loudly here rather than at first run.
  - MLX arrays live in unified memory; no `.to(device)` step.
  - `processor.decode(...)` returns a single string instead of HF's
    `batch_decode(...) -> list[str]`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ._voxtral_common import VoxtralTranscriberBase, _inputs_kwargs

# Quantised MLX builds live under mlx-community. Default to bf16 — full
# quality with the MLX speedup; users on tight RAM can swap to 4bit/8bit
# by editing the constant below or routing through a config knob later.
_MLX_VOXTRAL_REPO = "mlx-community/Voxtral-Mini-3B-2507-bf16"


class MlxVoxtralTranscriber(VoxtralTranscriberBase):
    """A Voxtral model loaded via mlx_voxtral, satisfying the `Transcriber`
    Protocol. Same `name="voxtral"` as the HF adapter so the dashboard
    treats them as the same model family; `backend` disambiguates."""

    name: ClassVar[str] = "voxtral"
    backend: ClassVar[str] = "mlx-voxtral"
    device: ClassVar[str] = "Apple Silicon GPU"

    def __init__(self, *, model_name: str, processor: Any, model: Any, fixed_language: str | None = None):
        self.model_name = model_name
        self._processor = processor
        self._model = model
        # Registry-declared fixed language (see VoxtralTranscriberBase).
        self.fixed_language = fixed_language

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

        # Lazy catalog import (same shape as the adapters' repo resolvers):
        # catalog imports this module only inside its loader thunk — no cycle.
        from .catalog import fixed_language_for

        print(f"[tapscribe] loading mlx-voxtral model: {_MLX_VOXTRAL_REPO}", flush=True)
        processor = VoxtralProcessor.from_pretrained(_MLX_VOXTRAL_REPO)
        model = VoxtralForConditionalGeneration.from_pretrained(_MLX_VOXTRAL_REPO)
        return cls(
            model_name=model_name,
            processor=processor,
            model=model,
            fixed_language=fixed_language_for(model_name),
        )

    def _repo_id(self) -> str:
        return _MLX_VOXTRAL_REPO

    def _apply_request(self, request_kwargs: dict[str, Any]) -> Any:
        # apply_transcrition_request mirrors HF's apply_transcription_request
        # (typo preserved); prompt + hotwords have no place in this call so
        # the base class drops them but records them on the result for
        # protocol parity.
        return self._processor.apply_transcrition_request(**request_kwargs)

    def _gen_kwargs(self) -> dict[str, Any]:
        return dict(
            max_new_tokens=2048,
            temperature=0.0,
        )

    def _generate(self, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:
        return self._model.generate(**_inputs_kwargs(inputs), **gen_kwargs)

    def _decode(self, outputs: Any, prompt_len: int) -> str:
        # mlx-voxtral's processor.decode takes a single token sequence and
        # returns one string (unlike HF transformers' batch_decode).
        gen_ids = outputs[0][prompt_len:]
        text_value = self._processor.decode(gen_ids, skip_special_tokens=True)
        return (text_value or "").strip()
