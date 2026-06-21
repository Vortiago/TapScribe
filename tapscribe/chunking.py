"""Shared chunking primitives for the Parakeet adapters.

Both `mlx_parakeet` (MLX) and `parakeet` (transformers CUDA/CPU) slice a
pre-decoded PCM array into overlapping windows so each
`model.generate(...)` call stays under the backend's per-call budget
(Metal GPU buffer cap on MLX; activation-tensor memory on torch). The
integer-math for window boundaries is identical between them — this
module is where it lives.

The result is intentionally simple — `Window(start_sample, end_sample,
start_s)` — so adapters can wrap or extend as needed without depending
on each other.
"""

from __future__ import annotations

from dataclasses import dataclass

from .audio import RECORDER_SAMPLE_RATE


@dataclass(frozen=True)
class Window:
    """One pre-decoded PCM window. `start_s` is cached because the
    adapters need it to offset per-window timestamps into session-
    relative coordinates, and computing it once at chunk time is
    cheaper than per-segment in the hot loop."""

    start_sample: int
    end_sample: int
    start_s: float


def chunk_windows(
    total_samples: int,
    *,
    chunk_s: float,
    overlap_s: float,
    sample_rate: int = RECORDER_SAMPLE_RATE,
) -> list[Window]:
    """Slice `[0, total_samples)` into overlapping windows.

    Each window is at most `chunk_s` seconds; consecutive windows share
    `overlap_s` seconds of audio so a word straddling a boundary gets a
    second chance in the next window. Always returns at least one
    window (the whole audio if it fits in a chunk).

    Raises `ValueError` when `overlap_s > chunk_s * 0.9` — overlaps
    approaching the chunk length make `step` approach one sample, so
    a 5-hour WAV would spawn millions of windows. Almost always a
    misconfigured env var; the clamp catches the foot-gun loudly.
    """
    if chunk_s <= 0:
        raise ValueError(f"chunk_s must be > 0, got {chunk_s}")
    if overlap_s < 0:
        raise ValueError(f"overlap_s must be >= 0, got {overlap_s}")
    if overlap_s > chunk_s * 0.9:
        raise ValueError(
            f"overlap_s ({overlap_s}) must be <= chunk_s * 0.9 ({chunk_s * 0.9}) — "
            "near-100% overlaps spawn pathologically many windows"
        )

    chunk = max(1, int(chunk_s * sample_rate))
    overlap = max(0, int(overlap_s * sample_rate))
    step = chunk - overlap  # > 0 because of the chunk/2 cap above
    if total_samples <= chunk:
        return [Window(0, total_samples, 0.0)]

    windows: list[Window] = []
    start = 0
    while True:
        end = min(start + chunk, total_samples)
        windows.append(Window(start, end, start / sample_rate))
        if end == total_samples:
            return windows
        start += step
