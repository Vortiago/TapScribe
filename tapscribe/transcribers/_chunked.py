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

# Mirrors the ratio hard-coded in `chunking.chunk_windows`:
# `chunk_windows` requires `overlap_s <= chunk_s * MAX_OVERLAP_FRACTION`
# (otherwise a window advances by ≤0 and the walk can't terminate). The two
# env knobs are validated INDEPENDENTLY by `env_float`, so a legal-but-
# incompatible PAIR — `TAPSCRIBE_PARAKEET_CHUNK_S=10` against the default
# 15 s overlap — passes both bound checks and only blows up inside
# `chunk_windows`, i.e. per-WAV at request time. See `_clamp_overlap`.
MAX_OVERLAP_FRACTION = 0.9


def clamp_overlap(chunk_s: float, overlap_s: float) -> float:
    """Return an overlap `chunk_windows` will accept for `chunk_s`, printing
    the same one-line "ignoring …; using …" notice `env_float` emits when it
    has to reduce one.

    The joint constraint can't live in `env_float`, which validates each knob
    on its own. Degrading LOUDLY at construction keeps the documented
    "typo-tolerant rather than fatal" contract: without this, an operator
    setting `TAPSCRIBE_PARAKEET_CHUNK_S=10` and leaving the 15 s overlap
    default constructs a perfectly healthy adapter whose EVERY transcribe
    dies with a `ValueError` that isn't a domain error — a bare 500 per
    request, and an aborted job in `transcribe_session`.
    """
    limit = chunk_s * MAX_OVERLAP_FRACTION
    if overlap_s <= limit:
        return overlap_s
    print(
        f"[tapscribe] ignoring overlap {overlap_s} > {MAX_OVERLAP_FRACTION:g} × chunk "
        f"{chunk_s} ({ENV_OVERLAP_S} / {ENV_CHUNK_S}); using {limit}",
        flush=True,
    )
    return limit


def stitch_windows(
    per_window: list[tuple[Window, Sequence[TranscriptionSegment]]],
    *,
    overlap_s: float,
) -> tuple[TranscriptionSegment, ...]:
    """Merge per-window segment sequences into one session-spanning tuple.

    For every adjacent pair (N, N+1) the overlap region is
    `[window_{N+1}.start, window_{N+1}.start + overlap_s)`, and its MIDPOINT is
    the seam: below it the audio belongs to window N, at or above it to window
    N+1. Each window therefore contributes only the segments whose `start`
    falls in `[its leading seam, its trailing seam)` — half-open, so a segment
    landing exactly on a seam is claimed by exactly one window.

    Bounding BOTH sides matters. Contributing window N whole and then filtering
    only window N+1's head emits `[seam, window_N_end)` twice — 7.5 s of
    duplicated speech per boundary at the defaults — and with three or more
    windows every interior window needs the upper bound, not just the first.

    This is the same crude-but-effective dedup parakeet-mlx uses upstream; a
    segment straddling the seam is attributed to one side by its start, so a
    word or two can still be clipped or repeated at the join. Word-level dedup
    would need confidence scores we don't currently have.
    """
    if not per_window:
        return ()
    out: list[TranscriptionSegment] = []
    for idx, (_window, segs) in enumerate(per_window):
        lower = per_window[idx][0].start_s + overlap_s / 2.0 if idx > 0 else float("-inf")
        upper = (
            per_window[idx + 1][0].start_s + overlap_s / 2.0 if idx + 1 < len(per_window) else float("inf")
        )
        out.extend(seg for seg in segs if lower <= seg.start < upper)
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
        overlap = (
            overlap_duration_s
            if overlap_duration_s is not None
            else env_float(
                ENV_OVERLAP_S,
                _DEFAULT_OVERLAP_DURATION_S,
                min_value=_OVERLAP_S_BOUNDS[0],
                max_value=_OVERLAP_S_BOUNDS[1],
            )
        )
        # Joint constraint — see `clamp_overlap`. Resolved HERE, at
        # construction, so a bad pair degrades once and loudly instead of
        # raising inside `chunk_windows` on every WAV.
        self.overlap_duration_s = clamp_overlap(self.chunk_duration_s, overlap)

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
