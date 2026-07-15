"""Shared skeleton for the two Voxtral adapters (HF transformers, MLX).

Voxtral is an audio-LLM rather than a Whisper-style segmenter — it
produces one free-form text response per WAV. Because each WAV the
recorder writes is already one mute-to-mute utterance, mapping that to
a single `TranscriptionSegment` (or a handful of sentence-split ones)
covering the whole duration works cleanly.

`VoxtralTranscriberBase.transcribe()` owns the flow both adapters share:
resolve a language hint → build the transcription-request kwargs → apply
the request → generate → slice off the prompt tokens → decode → sentence-
split → `build_transcription_result`. Each adapter (`voxtral.py`'s
`VoxtralTranscriber`, `mlx_voxtral.py`'s `MlxVoxtralTranscriber`)
implements only the backend-specific hooks: `_repo_id`, `_apply_request`,
`_gen_kwargs`, `_generate`, `_decode`. This mirrors the Parakeet pair's
`_chunked.ChunkedTranscriber` — "implementation sharing *behind* the
`Transcriber` Protocol seam" (see `_chunked.py`'s module docstring);
ADR-0001 is unaffected the same way.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from ..audio import wav_duration_s
from .base import TranscriptionResult, TranscriptionSegment, build_transcription_result, default_language_for

# Sentence boundary: a terminator (`.`, `!`, `?`) followed by whitespace.
# Lookbehind keeps the terminator with the preceding sentence. The negative
# lookbehind `(?<!\.\.)` skips the final dot of an ellipsis (`...`) — that's
# internal punctuation, not a sentence end. `What?!` still splits because
# the position behind `!` is `?!`, not `..`.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(?<!\.\.)\s+")


def split_voxtral_text_into_segments(text: str, *, duration: float) -> tuple[TranscriptionSegment, ...]:
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
        return tuple(TranscriptionSegment(start=0.0, end=end, text=s) for s in sentences)

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


def inputs_kwargs(inputs: Any) -> dict[str, Any]:
    """Best-effort: convert the processor output into kwargs for `.generate()`.

    The transformers/mlx-voxtral processor returns a `BatchEncoding`/
    `ModelInput`-shaped object that supports `**inputs` directly in
    practice; this helper exists so a MagicMock in tests can still be
    unpacked without raising. Shared by both adapters."""
    try:
        return dict(inputs)
    except (TypeError, ValueError):
        # MagicMock fallback: just pass the object as a single positional via
        # input_ids when possible, otherwise empty kwargs (mock model accepts).
        if hasattr(inputs, "input_ids"):
            return {"input_ids": inputs.input_ids}
        return {}


class VoxtralTranscriberBase:
    """Template for the two Voxtral adapters.

    Subclasses provide the `Transcriber` identity fields (`name`,
    `backend`, `device`, `model_name` — `build_transcription_result`
    reads them off `self`) and implement the hooks below:

    - `_repo_id()` — the HF Hub repo id passed as `model_id` in the
      request kwargs (also what each adapter's `load()` fetches).
    - `_apply_request(request_kwargs)` — call the processor's
      transcription-request method (the two adapters call a differently
      -spelled method — see `mlx_voxtral.py`'s module docstring — and
      only the HF path needs a `.to(device)` move) and return the
      processor's inputs object.
    - `_gen_kwargs()` — the `model.generate()` keyword arguments.
    - `_generate(inputs, gen_kwargs)` — call `model.generate(...)`
      (the HF path wraps this in `torch.no_grad()`).
    - `_decode(outputs, prompt_len)` — slice off the prompt tokens and
      decode to a stripped string (HF's `batch_decode` returns a list;
      mlx-voxtral's `decode` returns a single string).

    API contract both adapters share:

    - `initial_prompt` / `hotwords`: the transcription-request call has
      no hook for either — dropped at the model call, echoed onto the
      result for protocol parity.
    - `source_lang`: used to build the `language` request kwarg; when
      unset, the adapter's registry-declared `fixed_language` (threaded
      in at `load()`) applies, then `default_language_for()`'s name
      heuristic. Voxtral doesn't echo a detected language, so the hint we
      sent (or "auto" when there was none) is what gets recorded.
    """

    name: ClassVar[str] = "voxtral"
    backend: ClassVar[str]
    device: str
    model_name: str
    # Registry-declared fixed language (catalog.fixed_language_for), set by
    # each adapter's `load()` at construction. The class-level None default
    # keeps directly-constructed instances (tests' fakes) on the name-
    # heuristic fallback in `transcribe`.
    fixed_language: str | None = None

    def _repo_id(self) -> str:
        raise NotImplementedError

    def _apply_request(self, request_kwargs: dict[str, Any]) -> Any:
        raise NotImplementedError

    def _gen_kwargs(self) -> dict[str, Any]:
        raise NotImplementedError

    def _generate(self, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:
        raise NotImplementedError

    def _decode(self, outputs: Any, prompt_len: int) -> str:
        raise NotImplementedError

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
    ) -> TranscriptionResult:
        # Voxtral exposes a purpose-built transcription request that bypasses
        # the chat-template path (which ships broken on some transformers
        # releases — tokenizer.chat_template unset). The trade-off: this path
        # takes language + audio only, so initial_prompt and hotwords have
        # nowhere to go and are dropped. They're still recorded on the result
        # for parity with other transcribers' bookkeeping.
        language = source_lang or self.fixed_language or default_language_for(self.model_name)
        request_kwargs: dict[str, Any] = {"audio": str(path), "model_id": self._repo_id()}
        if language:
            request_kwargs["language"] = language
        inputs = self._apply_request(request_kwargs)

        gen_kwargs = self._gen_kwargs()
        outputs = self._generate(inputs, gen_kwargs)

        prompt_len = inputs.input_ids.shape[1] if hasattr(inputs, "input_ids") else 0
        text = self._decode(outputs, prompt_len)

        dur = round(wav_duration_s(path), 2)
        segments = split_voxtral_text_into_segments(text, duration=dur)
        # Voxtral doesn't echo a detected language in its response; record
        # the hint we sent, or "auto" when we let it auto-detect. Distinct
        # from "?" (genuinely unknown) so the UI never shows a populated
        # field as if it were missing.
        return build_transcription_result(
            self,
            text=text,
            segments=segments,
            duration=dur,
            language=language or "auto",
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
            quality_settings=dict(gen_kwargs),
        )
