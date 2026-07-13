"""MoonshineWindow — the backend-agnostic rolling-chunk pseudo-streaming
state machine shared by the MLX and ONNX-CPU Moonshine live engines.

Moonshine (see PRD #120) is fed the speech bursts TapScribe's own
`SpeechGate` already cuts, over a single long-lived `/asr` WebSocket
connection per `/tap` utterance (mirroring `WlKRelay`'s wire contract —
see `tapscribe.moonshine_live`). Unlike a batch `Transcriber`, there is no
whole file to split into windows up front: PCM arrives frame-by-frame in
real time, so the windowing has to be incremental.

The loop, mirroring the upstream `live_captions` reference
(`MAX_SPEECH_SECS` / `MIN_REFRESH_SECS`):

  1. Accumulate incoming PCM into a buffer for the CURRENT open line.
  2. No more often than every `refresh_s` seconds of NEW audio, re-run
     `generate_fn` on the buffer and update the open line's text in place
     — this is what lets `WlKRelay._consider_emit_line` treat it as a
     single growing line and emit only the new suffix.
  3. Once the open line's span exceeds `chunk_s` seconds, every further
     `generate_fn` call would exceed Moonshine's recommended sub-30s clip
     length. Roll over: freeze the current line's text, keep only the
     last `overlap_s` seconds of PCM for context continuity, and open a
     NEW line whose `start` is offset by the retained overlap — mirroring
     the timestamp-offset stitching `transcribers/_chunked.py` does for
     batch Parakeet, but incrementally instead of on a precomputed split.
  4. `close()` forces one final refresh (bypassing the refresh cadence)
     so the trailing words of a short utterance that ends before the next
     scheduled refresh aren't silently dropped.

Each returned "lines" snapshot has the SAME shape `WlKRelay._line_key`
already parses — `{"speaker": int, "start": float, "end": float, "text":
str}` — so the `/asr` server built on top of this (moonshine_live.py) can
hand its snapshot straight to a WS client speaking the WlKRelay contract
with zero translation.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from ..audio import RECORDER_SAMPLE_RATE
from ..config import env_float

# Chunk/overlap/refresh defaults. `chunk_s` stays comfortably under
# Moonshine's recommended sub-30s single-clip length even after accounting
# for a slow refresh tick landing just past the boundary. `refresh_s` is
# short for low perceived latency (the PRD's whole point) while still
# giving each `generate_fn` call enough new audio to be worth re-running.
# `overlap_s` carries a little trailing context into the next window so a
# word split exactly at the rollover isn't orphaned with zero context.
_DEFAULT_CHUNK_S = 25.0
_DEFAULT_OVERLAP_S = 3.0
_DEFAULT_REFRESH_S = 0.5

ENV_CHUNK_S = "TAPSCRIBE_MOONSHINE_CHUNK_S"
ENV_OVERLAP_S = "TAPSCRIBE_MOONSHINE_OVERLAP_S"
ENV_REFRESH_S = "TAPSCRIBE_MOONSHINE_REFRESH_S"

_CHUNK_S_BOUNDS = (1.0, 29.0)  # Moonshine's documented single-clip ceiling is ~30s.
_OVERLAP_S_BOUNDS = (0.0, 10.0)
_REFRESH_S_BOUNDS = (0.05, 10.0)


def chunk_s_from_env() -> float:
    return env_float(ENV_CHUNK_S, _DEFAULT_CHUNK_S, min_value=_CHUNK_S_BOUNDS[0], max_value=_CHUNK_S_BOUNDS[1])


def overlap_s_from_env() -> float:
    return env_float(
        ENV_OVERLAP_S, _DEFAULT_OVERLAP_S, min_value=_OVERLAP_S_BOUNDS[0], max_value=_OVERLAP_S_BOUNDS[1]
    )


def refresh_s_from_env() -> float:
    return env_float(
        ENV_REFRESH_S, _DEFAULT_REFRESH_S, min_value=_REFRESH_S_BOUNDS[0], max_value=_REFRESH_S_BOUNDS[1]
    )


GenerateFn = Callable[[np.ndarray], str]


class MoonshineWindow:
    """One per live `/asr` connection (one `/tap` utterance). NOT
    reusable across connections — construct a fresh instance per
    connection, same convention as `WlKRelay`.

    `generate_fn` is injected so tests never load a real model — it takes
    a 1-D float32 16 kHz numpy array and returns decoded text (see
    `MlxMoonshineEngine.generate` / the ONNX-CPU engine for real
    implementations).
    """

    def __init__(
        self,
        *,
        generate_fn: GenerateFn,
        chunk_s: float | None = None,
        overlap_s: float | None = None,
        refresh_s: float | None = None,
        sample_rate: int = RECORDER_SAMPLE_RATE,
        speaker: int = 0,
    ) -> None:
        self._generate_fn = generate_fn
        self._chunk_s = chunk_s if chunk_s is not None else chunk_s_from_env()
        self._overlap_s = overlap_s if overlap_s is not None else overlap_s_from_env()
        self._refresh_s = refresh_s if refresh_s is not None else refresh_s_from_env()
        self._sample_rate = sample_rate
        self._speaker = speaker

        self._buffer = bytearray()
        # `_total_elapsed_s` is the ABSOLUTE session-relative clock — total
        # seconds of PCM ever fed, monotonically increasing for the life of
        # this connection. It must NOT be derived from `len(self._buffer)`:
        # a rollover truncates `_buffer` down to just the retained overlap
        # tail, so buffer length alone silently "rewinds" the clock the
        # moment a chunk boundary is crossed, corrupting every line's
        # start/end and the refresh-cadence bookkeeping from then on.
        self._total_elapsed_s: float = 0.0
        self._last_refresh_at: float = 0.0
        self._window_start_s: float = 0.0
        # Closed (frozen) lines, oldest first, plus the current open line
        # (if any) as the last entry — the WlK-shaped cumulative snapshot.
        self._lines: list[dict] = []
        self._has_open_line: bool = False

    def feed_pcm(self, pcm: bytes) -> None:
        """Append recorder-format PCM (16-bit mono LE) to the buffer and
        advance the absolute session clock. A no-op call with `b""` is
        harmless (SpeechGate may hand back an empty tuple of frames)."""
        if not pcm:
            return
        self._buffer.extend(pcm)
        self._total_elapsed_s += len(pcm) / 2 / self._sample_rate

    def maybe_refresh(self) -> list[dict] | None:
        """Run inference on the current window if the refresh cadence has
        elapsed since the last call AND new audio has arrived. Returns the
        full cumulative `lines` snapshot on refresh, or None when skipped
        (nothing to show yet / too soon / no new audio)."""
        if self._total_elapsed_s <= self._last_refresh_at:
            return None
        if self._total_elapsed_s - self._last_refresh_at < self._refresh_s:
            return None
        return self._refresh(force=False)

    def close(self) -> list[dict]:
        """Force a final refresh bypassing the cadence check, so a short
        utterance's trailing words aren't lost. No-op (returns whatever
        snapshot already exists) if no audio was ever fed."""
        if self._total_elapsed_s <= 0.0 and not self._lines:
            return list(self._lines)
        self._refresh(force=True)
        return list(self._lines)

    @property
    def lines(self) -> list[dict]:
        return list(self._lines)

    def _refresh(self, *, force: bool) -> list[dict]:
        now = self._total_elapsed_s
        if now <= self._last_refresh_at and not force:
            return list(self._lines)
        self._last_refresh_at = now

        window_len = now - self._window_start_s
        rolled_over = False
        if window_len > self._chunk_s and self._has_open_line:
            keep_s = min(self._overlap_s, window_len)
            keep_samples = int(keep_s * self._sample_rate)
            keep_bytes = keep_samples * 2
            self._buffer = self._buffer[-keep_bytes:] if keep_bytes else bytearray()
            self._window_start_s = now - keep_s
            rolled_over = True

        arr = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        text = (self._generate_fn(arr) or "").strip()

        if rolled_over or not self._has_open_line:
            self._lines.append(
                {
                    "speaker": self._speaker,
                    "start": self._window_start_s,
                    "end": now,
                    "text": text,
                }
            )
            self._has_open_line = True
        else:
            self._lines[-1]["text"] = text
            self._lines[-1]["end"] = now
        return list(self._lines)
