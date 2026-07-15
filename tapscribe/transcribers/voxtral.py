"""Voxtral adapter (Mistral Voxtral via HuggingFace transformers).

The shared transcribe flow (language resolution → request → generate →
decode → sentence-split → result) lives in `_voxtral_common.py`'s
`VoxtralTranscriberBase`, mirroring how `_chunked.ChunkedTranscriber`
shares the Parakeet pair's flow. This module implements only the
HF-transformers-specific hooks and model loading.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ._voxtral_common import VoxtralTranscriberBase, _inputs_kwargs, split_voxtral_text_into_segments

__all__ = ["VoxtralTranscriber", "split_voxtral_text_into_segments"]

# Currently only Voxtral Mini is realistic for local CPU use; other Voxtral
# sizes can be added later by branching on model_name in load().
_VOXTRAL_REPO = "mistralai/Voxtral-Mini-3B-2507"


class VoxtralTranscriber(VoxtralTranscriberBase):
    """A Voxtral model wrapped to satisfy the `Transcriber` Protocol."""

    name: ClassVar[str] = "voxtral"
    backend: ClassVar[str] = "hf-transformers"

    def __init__(
        self, *, model_name: str, processor: Any, model: Any, device: str, fixed_language: str | None = None
    ):
        self.model_name = model_name
        self._processor = processor
        self._model = model
        self._raw_device = device
        # Hardware-only label; the library name lives on `backend`.
        self.device = "CPU" if device == "cpu" else device.upper()
        # Registry-declared fixed language (see VoxtralTranscriberBase).
        self.fixed_language = fixed_language

    @classmethod
    def load(cls, model_name: str, *, kind: str = "auto") -> VoxtralTranscriber:
        """Load the HF Voxtral model. `kind` is the resolved BackendKind
        from the registry ("cuda" / "cpu"); "auto" falls back to the
        legacy probe (cuda if available else cpu). MLX never reaches
        here — the registry routes it to `MlxVoxtralTranscriber`."""
        import importlib.util

        # transformers' Voxtral processor imports TranscriptionRequest from
        # mistral_common conditionally (`if is_mistral_common_available()`),
        # so without the package every apply_transcription_request() call
        # blows up with `NameError: TranscriptionRequest` deep in the
        # transformers stack — not a clean ImportError. Catch that here so
        # the operator sees an actionable "pip install" hint. Run this
        # before the torch / transformers imports so the test can exercise
        # the branch without those heavyweight deps installed.
        if importlib.util.find_spec("mistral_common") is None:
            raise RuntimeError(
                "Voxtral requires the `mistral-common` package "
                "(transformers' Voxtral processor uses it for "
                "TranscriptionRequest). Install with:\n"
                "    pip install 'mistral-common>=1.5'\n"
                "or re-run start.sh / start.ps1 which now installs it."
            )

        import torch  # type: ignore
        from transformers import (  # type: ignore
            AutoProcessor,
            VoxtralForConditionalGeneration,
        )

        print(f"[tapscribe] loading Voxtral model from HuggingFace: {_VOXTRAL_REPO}", flush=True)
        if kind == "cuda":
            device, dtype = "cuda", torch.bfloat16
        elif kind == "cpu":
            device, dtype = "cpu", torch.float32
        else:
            # "auto" — legacy probe, matches pre-registry behaviour.
            if torch.cuda.is_available():
                device, dtype = "cuda", torch.bfloat16
            else:
                device, dtype = "cpu", torch.float32

        processor = AutoProcessor.from_pretrained(_VOXTRAL_REPO)
        model = VoxtralForConditionalGeneration.from_pretrained(_VOXTRAL_REPO, torch_dtype=dtype).to(device)
        model.eval()
        # Lazy catalog import (same shape as the adapters' repo resolvers):
        # catalog imports this module only inside its loader thunk — no cycle.
        from .catalog import fixed_language_for

        return cls(
            model_name=model_name,
            processor=processor,
            model=model,
            device=device,
            fixed_language=fixed_language_for(model_name),
        )

    def _repo_id(self) -> str:
        return _VOXTRAL_REPO

    def _apply_request(self, request_kwargs: dict[str, Any]) -> Any:
        return self._processor.apply_transcription_request(**request_kwargs).to(self._raw_device)

    def _gen_kwargs(self) -> dict[str, Any]:
        return dict(
            max_new_tokens=2048,
            do_sample=False,
            repetition_penalty=1.1,
            num_beams=1,
        )

    def _generate(self, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:
        # torch.no_grad() is the standard hot-path hint; we import it lazily
        # so tests with mocked torch don't have to provide a stub.
        try:
            import torch  # type: ignore

            with torch.no_grad():
                return self._model.generate(**_inputs_kwargs(inputs), **gen_kwargs)
        except ImportError:
            return self._model.generate(**_inputs_kwargs(inputs), **gen_kwargs)

    def _decode(self, outputs: Any, prompt_len: int) -> str:
        gen_ids = outputs[:, prompt_len:]
        return self._processor.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()
