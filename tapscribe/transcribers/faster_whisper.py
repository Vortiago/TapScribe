"""faster-whisper / CTranslate2 adapter.

Also serves NB-Whisper checkpoints (the `nb-whisper-*` family loads via the
same backend on its `ct2/` weights, downloaded by the factory).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..models import default_language_for, download_nb_whisper_ct2_dir
from .base import TranscriptionResult, TranscriptionSegment, Word


class FasterWhisperTranscriber:
    """A faster-whisper / CTranslate2 model wrapped to satisfy the
    `Transcriber` Protocol."""

    name: ClassVar[str] = "faster-whisper"

    def __init__(self, *, model_name: str, model: Any, device: str):
        self.model_name = model_name
        self._model = model
        self.device = device

    @classmethod
    def load(cls, model_name: str) -> FasterWhisperTranscriber:
        from faster_whisper import WhisperModel  # type: ignore

        if model_name.startswith("nb-whisper-"):
            ct2_dir = download_nb_whisper_ct2_dir(model_name)
            print(f"[tapscribe] loading faster-whisper from {ct2_dir}", flush=True)
            model = WhisperModel(str(ct2_dir), device="cpu", compute_type="int8")
        else:
            print(f"[tapscribe] loading faster-whisper model: {model_name}", flush=True)
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        return cls(
            model_name=model_name,
            model=model,
            device="CPU (CTranslate2; NOT MLX)",
        )

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> TranscriptionResult:
        common = dict(
            language=default_language_for(self.model_name),
            beam_size=5,
            patience=2.0,
            vad_filter=True,
            initial_prompt=initial_prompt or None,
            condition_on_previous_text=False,
            word_timestamps=True,
            no_speech_threshold=0.4,
        )
        optional = dict(
            hotwords=hotwords or None,
            repetition_penalty=1.1,
            hallucination_silence_threshold=2.0,
        )

        # Older faster-whisper versions reject some kwargs. Fall back to the
        # required set if the quality-knob call raises TypeError.
        try:
            segments_iter, info = self._model.transcribe(str(path), **common, **optional)
            segments = list(segments_iter)
            applied = {**common, **optional}
        except TypeError:
            segments_iter, info = self._model.transcribe(str(path), **common)
            segments = list(segments_iter)
            applied = dict(common)

        typed_segments: list[TranscriptionSegment] = []
        for s in segments:
            words: tuple[Word, ...] | None = None
            if getattr(s, "words", None):
                words = tuple(
                    Word(
                        start=round(w.start, 2),
                        end=round(w.end, 2),
                        word=w.word,
                        prob=round(w.probability, 3),
                    )
                    for w in s.words
                )
            avg = getattr(s, "avg_logprob", None)
            typed_segments.append(
                TranscriptionSegment(
                    start=round(s.start, 2),
                    end=round(s.end, 2),
                    text=s.text.strip(),
                    avg_logprob=round(float(avg), 3) if avg is not None else None,
                    words=words,
                )
            )

        applied_view = {k: (v if not callable(v) else str(v)) for k, v in applied.items()}
        return TranscriptionResult(
            transcriber=self.name,
            device=self.device,
            model=self.model_name,
            language=info.language,
            language_probability=round(info.language_probability or 0.0, 3),
            duration=round(info.duration or 0.0, 2),
            text=" ".join(s.text for s in typed_segments).strip(),
            segments=tuple(typed_segments),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings=applied_view,
        )
