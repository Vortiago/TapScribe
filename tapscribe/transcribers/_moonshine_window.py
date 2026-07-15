"""MoonshineWindow — the backend-agnostic rolling-chunk pseudo-streaming
state machine shared by the MLX and ONNX-CPU Moonshine live engines.

Moonshine (see PRD #120) is fed the speech bursts TapScribe's own
`SpeechGate` already cuts, over a single long-lived `/asr` WebSocket
connection per `/tap` utterance (mirroring `WlKRelay`'s wire contract —
see `tapscribe.moonshine_live`). Unlike a batch `Transcriber`, there is no
whole file to split into windows up front: PCM arrives frame-by-frame in
real time, so the windowing has to be incremental.

The window -> relay text contract (one connection = ONE line)
--------------------------------------------------------------------------
`WlKRelay._consider_emit_line` keys emissions by `(speaker, start)` and
emits only the new suffix when a known line's text grows; any non-prefix
rewrite makes it re-emit the ENTIRE line text (visible duplication in the
live feed). Moonshine re-decodes the whole buffer on every refresh and —
having no LocalAgreement of its own upstream — routinely revises earlier
words, so the contract this window guarantees is:

  * One connection produces exactly ONE line, keyed `(speaker, 0.0)` for
    its whole life — a rollover never re-keys it, so the relay can never
    see already-emitted words under a fresh key.
  * The line's `text` is APPEND-ONLY: it holds the *committed* words —
    the word-prefix two consecutive decodes agreed on (LocalAgreement-2,
    the same stabilisation WhisperLiveKit runs). Once shown, words are
    never retracted; a decode that revises words *already committed*
    keeps the committed spelling and appends only the genuinely new
    words. The live feed can therefore differ from the batch transcript
    — live captions are ephemeral (CONTEXT.md), the batch transcript is
    the durable record. That is the deliberate tradeoff for never
    duplicating a line.
  * The not-yet-committed tail of the latest decode is exposed as
    `buffer_text` — the `/asr` server ships it as `buffer_transcription`
    on every snapshot, so `WlKRelay._flush_tail` can rescue in-flight
    words if a close-time snapshot is ever lost.

The loop, adapted from the upstream `live_captions` reference
(`MAX_SPEECH_SECS` / `MIN_REFRESH_SECS`):

  1. Accumulate incoming PCM into the current window's buffer.
  2. No more often than every `refresh_s` seconds of NEW audio, re-run
     `generate_fn` on the buffer, commit the agreed word-prefix into the
     line, and park the rest in `buffer_text`.
  3. Once the window's span exceeds `chunk_s`, a further decode would
     exceed Moonshine's recommended sub-30s clip length. Roll over:
     decode the full window one final time, commit ALL of it (its audio
     context is about to shrink — no better decode of it will ever
     happen), then keep only the last `overlap_s` seconds of PCM for
     context continuity. The overlap's words re-appear in every decode
     of the new window; they are stitched away by dropping the longest
     word-prefix of the new decode that matches a word-suffix of the
     frozen window's final decode (a garbled boundary word can defeat
     the exact match, in which case a small duplication is possible —
     favoured over silently dropping real words).
  4. `close()` commits everything: a final forced decode when new audio
     arrived after the last refresh (so a burst's trailing words are
     never dropped), or a plain commit of the volatile words when
     nothing new was fed (no redundant inference).

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
# for the rollover decode running over the full window PLUS the refresh
# slack that accrued past the boundary. `refresh_s` is short for low
# perceived latency (the PRD's whole point) while still giving each
# `generate_fn` call enough new audio to be worth re-running. `overlap_s`
# carries a little trailing context into the next window so a word split
# exactly at the rollover isn't orphaned with zero context.
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
    return env_float(
        ENV_CHUNK_S, _DEFAULT_CHUNK_S, min_value=_CHUNK_S_BOUNDS[0], max_value=_CHUNK_S_BOUNDS[1]
    )


def overlap_s_from_env() -> float:
    return env_float(
        ENV_OVERLAP_S, _DEFAULT_OVERLAP_S, min_value=_OVERLAP_S_BOUNDS[0], max_value=_OVERLAP_S_BOUNDS[1]
    )


def refresh_s_from_env() -> float:
    return env_float(
        ENV_REFRESH_S, _DEFAULT_REFRESH_S, min_value=_REFRESH_S_BOUNDS[0], max_value=_REFRESH_S_BOUNDS[1]
    )


GenerateFn = Callable[[np.ndarray], str]


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    """Length of the longest common word-prefix of `a` and `b` — the
    LocalAgreement-2 primitive: words two consecutive decodes agree on
    are considered stable enough to commit."""
    n = 0
    for wa, wb in zip(a, b, strict=False):
        if wa != wb:
            break
        n += 1
    return n


def _strip_overlap_prefix(words: list[str], stitch: list[str]) -> list[str]:
    """Drop the longest word-prefix of `words` that matches a word-suffix
    of `stitch` (the frozen window's final decode). This is the textual
    dual of the retained-overlap PCM: the overlap audio's words lead
    every decode of the new window and have already been committed as
    the tail of the frozen text. Exact word match only — a re-decode
    that garbles the boundary word yields k=0 (nothing stripped), which
    risks a small duplication but never drops real words."""
    if not words or not stitch:
        return words
    for k in range(min(len(words), len(stitch)), 0, -1):
        if words[:k] == stitch[-k:]:
            return words[k:]
    return words


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
        # moment a chunk boundary is crossed, corrupting the line's end
        # timestamp and the refresh-cadence bookkeeping from then on.
        self._total_elapsed_s: float = 0.0
        self._last_refresh_at: float = 0.0
        self._window_start_s: float = 0.0
        # The single line this connection ever produces (None until the
        # first refresh). Its text holds the committed words only.
        self._line: dict | None = None
        self._committed_words: list[str] = []
        # How many words of the CURRENT window's (overlap-stripped) decode
        # are already committed — reset to 0 at every rollover.
        self._window_committed_n: int = 0
        # The previous decode of the CURRENT window, for LocalAgreement-2.
        # None right after a rollover (the window's content changed, so
        # the pre-rollover decode isn't comparable).
        self._prev_words: list[str] | None = None
        # The frozen window's final RAW decode — its word-suffix is what
        # the retained overlap audio re-decodes to, and is stripped from
        # every decode of the new window. None before the first rollover.
        self._stitch_words: list[str] | None = None
        # The volatile (uncommitted) tail of the latest decode.
        self._buffer_text: str = ""

    def feed_pcm(self, pcm: bytes) -> None:
        """Append recorder-format PCM (16-bit mono LE) to the buffer and
        advance the absolute session clock. A no-op call with `b""` is
        harmless (SpeechGate may hand back an empty tuple of frames)."""
        if not pcm:
            return
        self._buffer.extend(pcm)
        self._total_elapsed_s += len(pcm) / 2 / self._sample_rate

    @property
    def refresh_due(self) -> bool:
        """True when `maybe_refresh` would actually run inference — a
        cheap check the `/asr` server uses so it only pays a worker-
        thread dispatch when there's a decode to run."""
        return (
            self._total_elapsed_s > self._last_refresh_at
            and self._total_elapsed_s - self._last_refresh_at >= self._refresh_s
        )

    def maybe_refresh(self) -> list[dict] | None:
        """Run inference on the current window if the refresh cadence has
        elapsed since the last call AND new audio has arrived. Returns the
        full cumulative `lines` snapshot on refresh, or None when skipped
        (nothing to show yet / too soon / no new audio)."""
        if not self.refresh_due:
            return None
        return self._refresh(force=False)

    def close(self) -> list[dict]:
        """Commit everything so a burst's trailing words are never lost:
        a final forced decode when new audio arrived after the last
        refresh, or a plain commit of the already-decoded volatile words
        when nothing new was fed (no redundant inference). No-op (returns
        the empty snapshot) if no audio was ever fed."""
        if self._total_elapsed_s <= 0.0 and self._line is None:
            return self.lines
        if self._total_elapsed_s > self._last_refresh_at:
            self._refresh(force=True)
        self._commit_volatile()
        return self.lines

    @property
    def lines(self) -> list[dict]:
        return [dict(self._line)] if self._line is not None else []

    @property
    def buffer_text(self) -> str:
        """The uncommitted tail of the latest decode — the snapshot's
        `buffer_transcription`. Empty after `close()`."""
        return self._buffer_text

    def _refresh(self, *, force: bool) -> list[dict]:
        now = self._total_elapsed_s
        if now <= self._last_refresh_at and not force:
            return self.lines
        self._last_refresh_at = now

        rolled_over = now - self._window_start_s > self._chunk_s
        # Decode the FULL current buffer — on a rollover this is the
        # window's final decode, run BEFORE truncation so audio fed since
        # the previous refresh is never silently dropped (it may lie
        # outside the retained overlap when overlap_s is small).
        arr = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        raw_words = ((self._generate_fn(arr) or "").strip()).split()
        words = (
            _strip_overlap_prefix(raw_words, self._stitch_words)
            if self._stitch_words is not None
            else raw_words
        )

        if self._line is None:
            self._line = {
                "speaker": self._speaker,
                "start": 0.0,
                "end": now,
                "text": "",
            }

        if rolled_over:
            # Freeze this window's content: commit ALL of it — its audio
            # context is about to shrink to the overlap tail, so no
            # better decode of it will ever happen. Then retain only the
            # overlap PCM and reset the per-window bookkeeping.
            self._commit_words(words)
            self._stitch_words = raw_words
            keep_s = min(self._overlap_s, now - self._window_start_s)
            keep_bytes = int(keep_s * self._sample_rate) * 2
            self._buffer = self._buffer[-keep_bytes:] if keep_bytes else bytearray()
            self._window_start_s = now - keep_s
            self._window_committed_n = 0
            self._prev_words = None
            self._buffer_text = ""
        else:
            # LocalAgreement-2: commit the word-prefix this decode and the
            # previous one agree on; everything past the committed count
            # stays volatile in buffer_text.
            agreement_n = _common_prefix_len(self._prev_words, words) if self._prev_words is not None else 0
            if agreement_n > self._window_committed_n:
                self._commit_words(words[:agreement_n])
            self._buffer_text = " ".join(words[self._window_committed_n :])
            self._prev_words = words

        self._line["end"] = now
        return self.lines

    def _commit_words(self, words: list[str]) -> None:
        """Append `words[self._window_committed_n:]` to the committed line
        text. Word positions BELOW the committed count are never touched —
        once shown, words are not retracted (see the module docstring for
        the tradeoff)."""
        new = words[self._window_committed_n :]
        if new:
            self._committed_words.extend(new)
            self._window_committed_n += len(new)
            assert self._line is not None
            self._line["text"] = " ".join(self._committed_words)

    def _commit_volatile(self) -> None:
        """close()-time drain: promote whatever is still volatile from the
        latest decode into the committed line text."""
        if self._prev_words is not None and self._window_committed_n < len(self._prev_words):
            self._commit_words(self._prev_words)
        self._buffer_text = ""
        if self._line is not None:
            self._line["end"] = self._total_elapsed_s
