"""faster-whisper / CTranslate2 adapter.

Also serves NB-Whisper checkpoints (the `nb-whisper-*` family loads via the
same backend on its `ct2/` weights, downloaded by the factory).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ..nb_whisper import download_nb_whisper_ct2_dir
from . import base
from .base import (
    TranscriptionResult,
    TranscriptionSegment,
    build_transcription_result,
    default_language_for,
)


class FasterWhisperTranscriber:
    """A faster-whisper / CTranslate2 model wrapped to satisfy the
    `Transcriber` Protocol."""

    name: ClassVar[str] = "faster-whisper"
    backend: ClassVar[str] = "faster-whisper"

    def __init__(self, *, model_name: str, model: Any, device: str, fixed_language: str | None = None):
        self.model_name = model_name
        self._model = model
        self.device = device
        # Registry-declared fixed language, threaded in by `load()` at
        # construction (catalog.fixed_language_for). None (the default for
        # direct constructions, e.g. tests) falls back to the catalog-free
        # name heuristic at the use sites below.
        self.fixed_language = fixed_language

    @classmethod
    def load(cls, model_name: str, *, kind: str = "cpu") -> FasterWhisperTranscriber:
        """Load the underlying CTranslate2 model.

        `kind` is the resolved BackendKind from the registry — "cpu" or
        "cuda". CUDA uses `float16` compute (the canonical fast path on
        consumer NVIDIA cards); CPU uses `int8` (small, fast enough, and
        what we shipped before the backend split). Other kinds (`mlx`)
        never reach this loader — the registry routes them elsewhere.
        """
        from faster_whisper import WhisperModel  # type: ignore

        # Lazy import (same shape as the adapters' repo resolvers): catalog
        # imports this module only inside its loader thunks, so neither
        # direction is a module-level import edge — no import cycle.
        from .catalog import fixed_language_for

        if kind == "cuda":
            ct_device = "cuda"
            compute_type = "float16"
            device_label = "CUDA"
        else:
            ct_device = "cpu"
            compute_type = "int8"
            device_label = "CPU"

        if model_name.startswith("nb-whisper-"):
            ct2_dir = download_nb_whisper_ct2_dir(model_name)
            print(
                f"[tapscribe] loading faster-whisper ({ct_device}/{compute_type}) from {ct2_dir}",
                flush=True,
            )
            model = WhisperModel(str(ct2_dir), device=ct_device, compute_type=compute_type)
        else:
            print(
                f"[tapscribe] loading faster-whisper ({ct_device}/{compute_type}) model: {model_name}",
                flush=True,
            )
            model = WhisperModel(model_name, device=ct_device, compute_type=compute_type)
        return cls(
            model_name=model_name,
            model=model,
            device=device_label,
            fixed_language=fixed_language_for(model_name),
        )

    def detect_constrained_language(self, path: Path, candidate_languages: tuple[str, ...]) -> str | None:
        """Snap Whisper's language auto-detection to the meeting's candidate set
        (ADR-0010): return the highest-probability language WITHIN
        `candidate_languages`, so a multi-language meeting never drifts to a
        language the operator didn't declare. None when there's nothing to
        constrain or this checkpoint can't emit any candidate.

        Runs a cheap detect-only pass (one mel window) over the pre-decoded
        recorder PCM — the same no-ffmpeg decode path the MLX adapters use —
        and restricts the argmax to the set. A fixed-language checkpoint
        skips the pass entirely and answers from its registry-declared
        language (name heuristic for checkpoints not in the catalog)."""
        cands = tuple(c for c in candidate_languages if c and c != "auto")
        if not cands:
            return None
        # A fixed-language checkpoint can only ever emit its own language —
        # don't waste a detect pass, and don't claim a candidate it can't produce.
        hint = self.fixed_language or default_language_for(self.model_name)
        if hint is not None:
            return hint if hint in cands else None

        import wave

        from ..wav_predecode import load_recorder_wav_as_pcm

        try:
            audio = load_recorder_wav_as_pcm(path)
        except (RuntimeError, OSError, wave.Error, EOFError):
            # The cheap stdlib pre-decode couldn't read the file — a non-recorder
            # format (RuntimeError), an unreadable/corrupt RIFF (wave.Error /
            # EOFError), or an I/O error (OSError). `transcribe()` still handles
            # such a file via faster-whisper's own decoder, so fall back to None
            # (unconstrained auto-detect, the pre-ADR-0010 behaviour) rather than
            # failing the whole transcribe just to constrain the language.
            return None
        _, _, all_language_probs = self._model.detect_language(audio)
        prob = {lang: p for lang, p in all_language_probs}
        return max(cands, key=lambda c: prob.get(c, 0.0))

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
    ) -> TranscriptionResult:
        # An explicit source_lang (the language pin, ADR-0010) overrides the
        # model's own fixed language; that in turn is the registry-declared
        # one threaded in at load() (#206), with `default_language_for`'s
        # name heuristic covering directly-constructed adapters.
        language = base.resolve_language(source_lang, self.fixed_language, self.model_name)
        common = dict(
            language=language,
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
        # required set if the quality-knob CALL raises TypeError.
        #
        # Only the call is inside the try. `WhisperModel.transcribe` returns a
        # LAZY generator, so the actual decode happens in `list(...)` below —
        # and a TypeError raised DURING generation (an upstream bug, a None
        # field on a segment, a numpy dtype mismatch on one CPU/CUDA build) is
        # not a kwarg-compatibility signal. Catching it here would silently
        # re-transcode the whole WAV without hotwords / repetition_penalty /
        # hallucination_silence_threshold: double the wall-clock, a
        # lower-quality transcript, and a `quality_settings` audit field
        # recording the reduced set with no warning anywhere. Let it propagate.
        try:
            segments_iter, info = self._model.transcribe(str(path), **common, **optional)
            applied = {**common, **optional}
        except TypeError:
            segments_iter, info = self._model.transcribe(str(path), **common)
            applied = dict(common)
        segments = list(segments_iter)

        typed_segments = [TranscriptionSegment.from_payload(s) for s in segments]

        applied_view = {k: (v if not callable(v) else str(v)) for k, v in applied.items()}
        return build_transcription_result(
            self,
            text=" ".join(s.text for s in typed_segments).strip(),
            segments=tuple(typed_segments),
            duration=info.duration or 0.0,
            language=info.language,
            language_probability=info.language_probability or 0.0,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
            quality_settings=applied_view,
        )
