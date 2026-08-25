"""Segmentation → windowed embeddings → one clustering over the whole session.

The default `Diarizer` (ADR-0021). Clustering spans every clip of the identity
at once: a level-gated tap emits one WAV per utterance, and per-WAV runs label
the same human differently in each.

Windows are 1.5 s at a 0.75 s hop and the clustering cut is 0.7, measured on the
fixtures rather than assumed (`tests/fixtures/diarize/PROVENANCE.md`): a window
longer than a turn hides the change inside it, so 2 s windows return a 2.2 s-turn
conversation as ONE Voice, and 0.7 is inside every passing threshold range.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from datetime import timedelta

import numpy as np

from .. import config
from ..vad import speech_timestamps
from .base import AudioClip, DiarizationResult, voice_label
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


def speech_windows(samples: np.ndarray, *, vad) -> list[list[tuple[int, int]]]:
    """One `[(first_frame, last_frame)]` list per speech region, in fbank frames.

    Grouped by region rather than flat because a span may not cross a silence —
    the pause belongs to neither speaker (#441). Windows overlap by a hop, and a
    region's last window is anchored to its end rather than overhanging it.
    """
    regions: list[list[tuple[int, int]]] = []
    for region in speech_timestamps(samples, vad, threshold=VAD_THRESHOLD):
        lo = -(-region["start"] // FRAME_SHIFT)  # ceil: never claim a frame before speech
        hi = region["end"] // FRAME_SHIFT
        if hi - lo < MIN_WINDOW_FRAMES:
            continue
        if hi - lo <= WINDOW_FRAMES:
            regions.append([(lo, hi)])
            continue
        windows = [(s, s + WINDOW_FRAMES) for s in range(lo, hi - WINDOW_FRAMES + 1, HOP_FRAMES)]
        if windows[-1][1] < hi - HOP_FRAMES // 2:
            windows.append((hi - WINDOW_FRAMES, hi))
        regions.append(windows)
    return regions


def _spans_for_region(region: Sequence[tuple[int, int]], labels: Sequence[int]) -> list[tuple[int, int, int]]:
    """`[(label, first_frame, last_frame)]` tiling one speech region's frames.

    Each frame goes to the window whose centre is nearest, so a turn change
    lands midway between the two windows that disagree and the spans neither
    overlap nor leave a hole. Merging same-label windows end-to-end instead
    would overlap them by a hop — ambiguity exactly where the turn changes.
    """
    lo, hi = region[0][0], region[-1][1]
    centres = np.array([(s + e) / 2 for s, e in region])
    frames = np.arange(lo, hi)
    # Centres are sorted, so the nearest one is a binary search against their
    # midpoints. The obvious `abs(frames[:, None] - centres).argmin(1)` builds a
    # frames x windows matrix — 1.1 GB on a 12-minute speech region, which one
    # unbroken stretch of a meeting easily is.
    owner = np.searchsorted((centres[:-1] + centres[1:]) / 2, frames, side="left")
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
        placed: list[tuple[AudioClip, list[tuple[int, int]]]] = []

        # Embed clip by clip: the fbank of a whole clip is transient, the vectors
        # it reduces to are 2 KB a window.
        for clip in clips:
            regions = speech_windows(clip.samples, vad=self._vad)
            if not regions:
                continue
            feats = fbank(clip.samples)
            windows = [w for region in regions for w in region]
            vectors.append(self._embedder.embed([feats[lo:hi] for lo, hi in windows]))
            placed.extend((clip, region) for region in regions)

        if not vectors:
            return DiarizationResult(engine=self.engine, took_ms=_ms_since(started))

        labels = cluster_voices(
            np.concatenate(vectors), threshold=self._threshold, max_speakers=self._max_speakers
        )
        voices: dict[str, list] = {}
        at = 0
        for clip, region in placed:
            window_labels = labels[at : at + len(region)]
            at += len(region)
            for label, lo, hi in _spans_for_region(region, window_labels):
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


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
