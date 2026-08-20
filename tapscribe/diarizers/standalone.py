"""Segmentation → windowed embeddings → one clustering over the whole session.

The default `Diarizer` (ADR-0021). Clustering spans every clip of the identity
at once: a level-gated tap emits one WAV per utterance, and per-WAV runs label
the same human differently in each.

Windows are 1.5 s at a 0.75 s hop, measured on the fixtures rather than assumed:
below 1.5 s a speaker stops resembling herself (window-to-window cosine drops to
0.04), above ~2.5 s a turn change hides inside one window. Every threshold in
0.6–0.85 splits the two-speaker fixtures and holds the one-speaker ones
together; 0.7 is the middle of that plateau, not a derived optimum.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from datetime import timedelta

import numpy as np

from .. import config
from ..vad import speech_timestamps
from .base import AudioClip, DiarizationResult, Diarizer, voice_label
from .cluster import cluster_voices
from .fbank import FRAME_SHIFT, SUPPORTED_RATE, fbank

#: Seconds per fbank frame — the resolution every span below is quantised to.
FRAME_SHIFT_S = FRAME_SHIFT / SUPPORTED_RATE

WINDOW_FRAMES = 150  # 1.5 s
HOP_FRAMES = 75  # 0.75 s

#: A region shorter than this contributes one window rather than none. Below it
#: the embedding is mostly noise, and a junk vector clusters into its own Voice.
MIN_WINDOW_FRAMES = WINDOW_FRAMES // 2

#: Recall-favouring, against `speech_timestamps`' 0.5 default. The asymmetry is
#: the opposite of strip-silence's: a false negative here leaves audio
#: unattributed, while a false positive costs one window that clusters away.
VAD_THRESHOLD = 0.3

DEFAULT_THRESHOLD = 0.7
DEFAULT_MAX_SPEAKERS = 8

#: Dashboard-tunable (Settings -> Advanced), resolved env > config file >
#: default at use-time like every other operator knob.
ENV_THRESHOLD = "TAPSCRIBE_DIARIZE_THRESHOLD"
ENV_MAX_SPEAKERS = "TAPSCRIBE_DIARIZE_MAX_SPEAKERS"


def resolve_threshold() -> float:
    return config.resolve_knob(
        ENV_THRESHOLD,
        config.DIARIZE_THRESHOLD_FILE,
        config._parse_diarize_threshold,
        DEFAULT_THRESHOLD,
    )


def resolve_max_speakers() -> int:
    return config.resolve_knob(
        ENV_MAX_SPEAKERS,
        config.DIARIZE_MAX_SPEAKERS_FILE,
        config._parse_diarize_max_speakers,
        DEFAULT_MAX_SPEAKERS,
    )


def speech_windows(
    samples: np.ndarray, *, vad, vad_threshold: float = VAD_THRESHOLD
) -> list[tuple[int, int]]:
    """`[(first_frame, last_frame)]` covering the clip's speech, in fbank frames.

    Windows overlap by a hop, and the last one of a region is anchored to its
    end rather than overhanging it.
    """
    windows: list[tuple[int, int]] = []
    for region in speech_timestamps(samples, vad, threshold=vad_threshold):
        lo = -(-region["start"] // FRAME_SHIFT)  # ceil: never claim a frame before speech
        hi = region["end"] // FRAME_SHIFT
        if hi - lo < MIN_WINDOW_FRAMES:
            continue
        if hi - lo <= WINDOW_FRAMES:
            windows.append((lo, hi))
            continue
        starts = list(range(lo, hi - WINDOW_FRAMES + 1, HOP_FRAMES))
        windows.extend((s, s + WINDOW_FRAMES) for s in starts)
        if starts[-1] + WINDOW_FRAMES < hi - HOP_FRAMES // 2:
            windows.append((hi - WINDOW_FRAMES, hi))
    return windows


def _spans_for_region(region: Sequence[tuple[int, int]], labels: Sequence[int]) -> list[tuple[int, int, int]]:
    """`[(label, first_frame, last_frame)]` tiling one region's frames.

    Each frame goes to the window whose centre is nearest, so a turn change
    lands midway between the two windows that disagree and the spans neither
    overlap nor leave a hole. Merging same-label windows end-to-end instead
    would overlap them by a hop — ambiguity exactly where the turn changes.
    """
    lo, hi = region[0][0], region[-1][1]
    centres = np.array([(s + e) / 2 for s, e in region])
    frames = np.arange(lo, hi)
    owner = np.abs(frames[:, None] - centres[None, :]).argmin(axis=1)
    per_frame = np.asarray(labels)[owner]

    spans: list[tuple[int, int, int]] = []
    edges = np.flatnonzero(np.diff(per_frame)) + 1
    for start, end in zip([0, *edges.tolist()], [*edges.tolist(), len(per_frame)], strict=True):
        spans.append((int(per_frame[start]), lo + start, lo + end))
    return spans


class StandaloneDiarizer:
    """Holds the embedder and one VAD instance — silero's recurrent state lives
    on the model object, so this is single-run, never shared."""

    engine = "standalone"

    def __init__(
        self,
        embedder,
        *,
        vad,
        threshold: float | None = None,
        max_speakers: int | None = None,
    ) -> None:
        self._embedder = embedder
        self._vad = vad
        self._threshold = resolve_threshold() if threshold is None else threshold
        self._max_speakers = resolve_max_speakers() if max_speakers is None else max_speakers

    def diarize(self, clips: Iterable[AudioClip]) -> DiarizationResult:
        started = time.perf_counter()
        vectors: list[np.ndarray] = []
        regions: list[tuple[AudioClip, list[tuple[int, int]], int]] = []
        at = 0

        # Embed clip by clip: the fbank of an hour of audio is 230 MB, the
        # vectors it reduces to are 10 MB.
        for clip in clips:
            windows = speech_windows(clip.samples, vad=self._vad, vad_threshold=VAD_THRESHOLD)
            if not windows:
                continue
            feats = fbank(clip.samples)
            vectors.append(self._embedder.embed([feats[lo:hi] for lo, hi in windows]))
            regions.extend(_group_regions(clip, windows, offset=at))
            at += len(windows)

        if not vectors:
            return DiarizationResult(engine=self.engine, took_ms=_ms_since(started))

        labels = cluster_voices(
            np.concatenate(vectors), threshold=self._threshold, max_speakers=self._max_speakers
        )
        voices: dict[str, list] = {}
        for clip, windows, offset in regions:
            for label, lo, hi in _spans_for_region(windows, labels[offset : offset + len(windows)]):
                voices.setdefault(voice_label(label), []).append(
                    (
                        clip.start + timedelta(seconds=lo * FRAME_SHIFT_S),
                        clip.start + timedelta(seconds=hi * FRAME_SHIFT_S),
                    )
                )
        return DiarizationResult(
            voices={label: voices[label] for label in sorted(voices)},
            engine=self.engine,
            took_ms=_ms_since(started),
        )


def _group_regions(
    clip: AudioClip, windows: list[tuple[int, int]], *, offset: int
) -> list[tuple[AudioClip, list[tuple[int, int]], int]]:
    """Split a clip's windows back into the speech regions they came from, with
    each group's offset into the run's vector list. A span may not cross a
    silence: the pause belongs to neither speaker (#441)."""
    groups: list[tuple[AudioClip, list[tuple[int, int]], int]] = []
    current: list[tuple[int, int]] = []
    at = offset
    for window in windows:
        if current and window[0] > current[-1][1]:
            groups.append((clip, current, at))
            at += len(current)
            current = []
        current.append(window)
    if current:
        groups.append((clip, current, at))
    return groups


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


_: type[Diarizer] = StandaloneDiarizer
