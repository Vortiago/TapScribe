"""Voxtral adapter (Mistral Voxtral via HuggingFace transformers).

Voxtral is an audio-LLM rather than a Whisper-style segmenter — it
produces one free-form text response per WAV. Because each WAV the
recorder writes is already one mute-to-mute utterance, mapping that to
a single `TranscriptionSegment` covering the whole duration works
cleanly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment

# Currently only Voxtral Mini is realistic for local CPU use; other Voxtral
# sizes can be added later by branching on model_name in load().
_VOXTRAL_REPO = "mistralai/Voxtral-Mini-3B-2507"

_INSTRUCTION_BASE = (
    "Transcribe the audio verbatim into text. "
    "Do not add commentary, summarisation, translation, or interpretation. "
    "Do not describe the audio. Output only the spoken words. "
    "Use punctuation and casing that matches the speech."
)


class VoxtralTranscriber:
    """A Voxtral model wrapped to satisfy the `Transcriber` Protocol."""

    name: ClassVar[str] = "voxtral"

    def __init__(self, *, model_name: str, processor: Any, model: Any, device: str):
        self.model_name = model_name
        self._processor = processor
        self._model = model
        self._raw_device = device
        # Surface the same human-readable device label the dashboard expects.
        if device == "cpu":
            self.device = "CPU (HF transformers; NOT MLX)"
        else:
            self.device = f"{device.upper()} (HF transformers; NOT MLX)"

    @classmethod
    def load(cls, model_name: str) -> VoxtralTranscriber:
        import torch  # type: ignore
        from transformers import (  # type: ignore
            AutoProcessor,
            VoxtralForConditionalGeneration,
        )

        print(f"[tapscribe] loading Voxtral model from HuggingFace: {_VOXTRAL_REPO}", flush=True)
        device = "cpu"
        dtype = torch.float32
        if torch.cuda.is_available():
            device = "cuda"
            dtype = torch.bfloat16

        processor = AutoProcessor.from_pretrained(_VOXTRAL_REPO)
        model = VoxtralForConditionalGeneration.from_pretrained(_VOXTRAL_REPO, torch_dtype=dtype).to(device)
        model.eval()
        return cls(model_name=model_name, processor=processor, model=model, device=device)

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> TranscriptionResult:
        # Build the instruction. Voxtral is helpfulness-tuned and will drift
        # into summarising if not pinned to verbatim output.
        instruction = _INSTRUCTION_BASE
        if initial_prompt:
            instruction += " Context for this conversation: " + initial_prompt
        if hotwords:
            instruction += " Proper nouns and jargon that may appear (use these spellings): " + hotwords

        conversation = [
            {
                "role": "user",
                "content": [
                    {"type": "audio", "path": str(path)},
                    {"type": "text", "text": instruction},
                ],
            }
        ]
        inputs = self._processor.apply_chat_template(conversation, return_tensors="pt").to(self._raw_device)

        gen_kwargs: dict[str, Any] = dict(
            max_new_tokens=2048,
            do_sample=False,
            repetition_penalty=1.1,
            num_beams=1,
        )

        # torch.no_grad() is the standard hot-path hint; we import it lazily
        # so tests with mocked torch don't have to provide a stub.
        try:
            import torch  # type: ignore

            with torch.no_grad():
                outputs = self._model.generate(**_inputs_kwargs(inputs), **gen_kwargs)
        except ImportError:
            outputs = self._model.generate(**_inputs_kwargs(inputs), **gen_kwargs)

        prompt_len = inputs.input_ids.shape[1] if hasattr(inputs, "input_ids") else 0
        gen_ids = outputs[:, prompt_len:]
        text = self._processor.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()

        dur = wav_duration_s(path)
        seg = TranscriptionSegment(start=0.0, end=round(dur, 2), text=text)
        return TranscriptionResult(
            transcriber=self.name,
            device=self.device,
            model=self.model_name,
            language="auto",
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

    The transformers processor returns a `BatchEncoding`/`ModelInput` object
    that supports `**inputs` directly in practice; this helper exists so a
    MagicMock in tests can still be unpacked without raising."""
    try:
        return dict(inputs)
    except (TypeError, ValueError):
        # MagicMock fallback: just pass the object as a single positional via
        # input_ids when possible, otherwise empty kwargs (mock model accepts).
        if hasattr(inputs, "input_ids"):
            return {"input_ids": inputs.input_ids}
        return {}
