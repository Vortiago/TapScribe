"""Voxtral adapter (Mistral Voxtral via HuggingFace transformers).

Voxtral is an audio-LLM rather than a Whisper-style segmenter — it
produces one free-form text response per WAV. Because each WAV the
recorder writes is already one mute-to-mute utterance, mapping that to
a single `TranscriptionSegment` covering the whole duration works
cleanly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment, default_language_for

# Sentence boundary: a terminator (`.`, `!`, `?`) followed by whitespace.
# Lookbehind keeps the terminator with the preceding sentence. The negative
# lookbehind `(?<!\.\.)` skips the final dot of an ellipsis (`...`) — that's
# internal punctuation, not a sentence end. `What?!` still splits because
# the position behind `!` is `?!`, not `..`.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(?<!\.\.)\s+")


def split_voxtral_text_into_segments(
    text: str, *, duration: float
) -> tuple[TranscriptionSegment, ...]:
    """Sentence-split Voxtral's free-form output and interpolate timestamps.

    Voxtral returns one text blob per WAV with no per-token timing. Without
    splitting, the merged transcript renders a 60-second utterance as one
    unreadable paragraph. We split on `.`, `!`, `?` (followed by
    whitespace), then allocate each sentence a share of the WAV's duration
    proportional to its character count.

    Timestamps are approximate (off by seconds within a WAV) — fine for a
    chat-style merged view, not for subtitle alignment. Will be replaced
    with real timestamps once HF transformers issue #41999 lands.

    Invariants:
      - Adjacent: `segments[i].end == segments[i+1].start`.
      - The first segment starts at 0.0; the last ends exactly at
        `duration` (rounding leftover is absorbed by the last segment).
      - Empty/whitespace input returns an empty tuple.
    """
    stripped = text.strip()
    if not stripped:
        return ()

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(stripped) if s.strip()]
    if not sentences:
        return ()

    if len(sentences) == 1 or duration <= 0:
        # One sentence — or no measurable duration to allocate across.
        # Either way, every sentence collapses to [0, duration].
        end = max(0.0, duration)
        return tuple(
            TranscriptionSegment(start=0.0, end=end, text=s) for s in sentences
        )

    total_chars = sum(len(s) for s in sentences)
    segments: list[TranscriptionSegment] = []
    cursor = 0.0
    for i, sent in enumerate(sentences):
        if i == len(sentences) - 1:
            # Last sentence ends exactly at `duration` to absorb rounding.
            end = duration
        else:
            end = cursor + (len(sent) / total_chars) * duration
        segments.append(
            TranscriptionSegment(
                start=round(cursor, 2),
                end=round(end, 2),
                text=sent,
            )
        )
        cursor = end
    return tuple(segments)

# Currently only Voxtral Mini is realistic for local CPU use; other Voxtral
# sizes can be added later by branching on model_name in load().
_VOXTRAL_REPO = "mistralai/Voxtral-Mini-3B-2507"


class VoxtralTranscriber:
    """A Voxtral model wrapped to satisfy the `Transcriber` Protocol."""

    name: ClassVar[str] = "voxtral"
    backend: ClassVar[str] = "hf-transformers"

    def __init__(self, *, model_name: str, processor: Any, model: Any, device: str):
        self.model_name = model_name
        self._processor = processor
        self._model = model
        self._raw_device = device
        # Hardware-only label; the library name lives on `backend`.
        self.device = "CPU" if device == "cpu" else device.upper()

    @classmethod
    def load(cls, model_name: str) -> VoxtralTranscriber:
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
        # Voxtral exposes a purpose-built transcription request that bypasses
        # the chat-template path (which ships broken on some transformers
        # releases — tokenizer.chat_template unset). The trade-off: this path
        # takes language + audio only, so initial_prompt and hotwords have
        # nowhere to go and are dropped. They're still recorded on the result
        # for parity with other transcribers' bookkeeping.
        language = default_language_for(self.model_name)
        request_kwargs: dict[str, Any] = {"audio": str(path), "model_id": _VOXTRAL_REPO}
        if language:
            request_kwargs["language"] = language
        inputs = self._processor.apply_transcription_request(**request_kwargs).to(self._raw_device)

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

        dur = round(wav_duration_s(path), 2)
        segments = split_voxtral_text_into_segments(text, duration=dur)
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            # Voxtral doesn't echo a detected language in its response; record
            # the hint we sent, or "auto" when we let it auto-detect. Distinct
            # from "?" (genuinely unknown) so the UI never shows a populated
            # field as if it were missing.
            language=language or "auto",
            language_probability=0.0,
            duration=dur,
            text=text,
            segments=segments,
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
