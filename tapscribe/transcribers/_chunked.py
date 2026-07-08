"""Shared skeleton for chunked-window Transcriber adapters (Parakeet ×2).

Both Parakeet adapters (`parakeet.py` on `transformers` CUDA/CPU,
`mlx_parakeet.py` on `parakeet-mlx` Apple Silicon) transcribe long inputs
the same way: pre-decode the recorder's WAV to float32 PCM
(`load_recorder_wav_as_pcm` — no ffmpeg, no path fallback), split into
overlapping windows, run the model once per window, shift per-window
timestamps by the window's offset so the merged result stays
session-relative, and stitch with overlap-midpoint dedup. That skeleton
lives here once; each adapter implements only `_transcribe_window` (the
model call for one window's PCM).

ADR-0001 is untouched: the `Transcriber` Protocol stays the seam, and each
adapter is still one loaded model per `(backend × model_name)`. This base
is implementation sharing *behind* that seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..audio import RECORDER_SAMPLE_RATE
from ..chunking import Window, chunk_windows
from ..config import env_float
from ..wav_predecode import load_recorder_wav_as_pcm
from .base import TranscriptionResult, TranscriptionSegment, build_transcription_result

# Chunking defaults — 120 s windows with 15 s overlap matches the
# parakeet-mlx authors' own `transcribe()` tuning, fits comfortably under a
# base M1 mini's ~14 GB max-buffer Metal cap, and keeps a multi-hour
# recording from building one giant activation tensor on CPU/CUDA.
# Operator-tunable via env — ONE env pair shared by both adapters,
# deliberately, so the dashboard wiring (when it lands) has one source of
# truth. Out-of-range env values are rejected by `env_float` (logged +
# default used).
_DEFAULT_CHUNK_DURATION_S = 120.0
_DEFAULT_OVERLAP_DURATION_S = 15.0

ENV_CHUNK_S = "TAPSCRIBE_PARAKEET_CHUNK_S"
ENV_OVERLAP_S = "TAPSCRIBE_PARAKEET_OVERLAP_S"

_CHUNK_S_BOUNDS = (1.0, 600.0)
_OVERLAP_S_BOUNDS = (0.0, 60.0)


def stitch_windows(
    per_window: list[tuple[Window, Sequence[TranscriptionSegment]]],
    *,
    overlap_s: float,
) -> tuple[TranscriptionSegment, ...]:
    """Merge per-window segment sequences into one session-spanning tuple.

    For every adjacent pair (N, N+1) the overlap region is
    `[window_{N+1}.start, window_{N+1}.start + overlap_s)`. Segments in
    window N+1 whose `start` falls before the overlap midpoint were already
    transcribed (and likely identical) in window N — they get dropped.
    Above the midpoint, window N+1's segment wins. This is the same
    crude-but-effective dedup parakeet-mlx uses upstream; a segment
    straddling the seam is double-counted. Word-level dedup would need
    confidence scores we don't currently have.
    """
    if not per_window:
        return ()
    out: list[TranscriptionSegment] = list(per_window[0][1])
    for prev_idx in range(len(per_window) - 1):
        nxt_window, nxt_segs = per_window[prev_idx + 1]
        midpoint_s = nxt_window.start_s + overlap_s / 2.0
        out.extend(seg for seg in nxt_segs if seg.start >= midpoint_s)
    return tuple(out)


class ChunkedTranscriber:
    """Template for adapters that transcribe window-by-window.

    Subclasses provide the `Transcriber` identity fields (`name`,
    `backend`, `device`, `model_name` — `build_transcription_result` reads
    them off `self`) and implement `_transcribe_window`. The API contract
    both Parakeet adapters share:

    - `prompt` / `hotwords`: not supported by Parakeet — accepted for
      protocol parity, dropped at the model call, echoed onto the result
      for audit.
    - `source_lang`: recorded on the result. Parakeet doesn't echo a
      detected language, so we trust the operator's pick; missing →
      `language="auto"`.
    """

    def __init__(
        self,
        *,
        model_name: str,
        chunk_duration_s: float | None = None,
        overlap_duration_s: float | None = None,
    ):
        self.model_name = model_name
        self.chunk_duration_s = (
            chunk_duration_s
            if chunk_duration_s is not None
            else env_float(
                ENV_CHUNK_S,
                _DEFAULT_CHUNK_DURATION_S,
                min_value=_CHUNK_S_BOUNDS[0],
                max_value=_CHUNK_S_BOUNDS[1],
            )
        )
        self.overlap_duration_s = (
            overlap_duration_s
            if overlap_duration_s is not None
            else env_float(
                ENV_OVERLAP_S,
                _DEFAULT_OVERLAP_DURATION_S,
                min_value=_OVERLAP_S_BOUNDS[0],
                max_value=_OVERLAP_S_BOUNDS[1],
            )
        )

    def _transcribe_window(self, chunk_pcm: Any, window: Window) -> Sequence[TranscriptionSegment]:
        """Run the model on one window's PCM and return its segments with
        timestamps already shifted by `window.start_s`."""
        raise NotImplementedError

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
    ) -> TranscriptionResult:
        # Pre-decode skips any ffmpeg load. `load_recorder_wav_as_pcm`
        # raises on unusual WAV formats — the operator's signal to convert
        # the file, not a cue to re-introduce ffmpeg.
        pcm = load_recorder_wav_as_pcm(path)
        windows = chunk_windows(
            int(pcm.shape[0]),
            chunk_s=self.chunk_duration_s,
            overlap_s=self.overlap_duration_s,
        )

        per_window: list[tuple[Window, Sequence[TranscriptionSegment]]] = []
        for window in windows:
            chunk_pcm = pcm[window.start_sample : window.end_sample]
            per_window.append((window, self._transcribe_window(chunk_pcm, window)))

        segments = stitch_windows(per_window, overlap_s=self.overlap_duration_s)
        text = " ".join(s.text for s in segments if s.text).strip()
        duration = pcm.shape[0] / RECORDER_SAMPLE_RATE

        return build_transcription_result(
            self,
            text=text,
            segments=segments,
            duration=duration,
            language=source_lang or "auto",
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
            quality_settings={
                "chunk_duration_s": self.chunk_duration_s,
                "overlap_duration_s": self.overlap_duration_s,
            },
        )
