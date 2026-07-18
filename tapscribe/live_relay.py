"""WhisperLiveKit relay — the Recorder's client-side WS to its supervised
whisperlivekit-server child.

Each open `/tap` WebSocket from a Bridge spawns its own `WlKRelay`. The
relay holds:

  - One outbound WS to `ws://<host>:<port>/asr?language=<lang>` (the
    `WhisperLiveKit` child). The Recorder writes Bridge bytes to this
    stream; WlK responds with rolling JSON transcripts (see
    `whisperlivekit/timed_objects.py FrontData.to_dict` for the source
    of truth on the wire shape).
  - One background consumer task that parses those responses and calls
    `on_settled_line(text)` for each newly-finalized line. The handler
    typically appends the result to `recorder.transcripts` so the
    dashboard sees it.

WlK semantics: every response carries a CUMULATIVE `lines` list — each
tick re-sends every line emitted so far in the session, not just the
new ones. Same-speaker consecutive segments get merged inside WlK
(`segments[-1].text += segment.text` per `tokens_alignment.py`), so a
line's text CAN grow between snapshots; we identify lines by
`(speaker, start)` and emit only the new suffix on growth.

Emission policy:

  1. Every snapshot, emit all non-tail entries via the key-aware
     `_consider_emit_line` (dedupes against previous emissions of the
     same `(speaker, start)`).
  2. The tail (last entry) is held back as long as it may still grow
     — but once `buffer_transcription` is empty AND the tail's text
     has been stable across `_TAIL_STABLE_SNAPSHOTS` consecutive
     snapshots, we flush it. This makes short utterances ("Hello.")
     reach the LiveFeed without waiting for /tap close, while still
     handling the merger case correctly (a later snapshot whose tail
     grows the previously-flushed line emits only the new suffix).
  3. On close, anything still unemitted (the most recent tail + any
     non-empty `buffer_transcription`) is flushed so no audio's
     transcript is lost when the bridge disconnects.

Lifecycle: `connect()` returns False on any failure (WlK not running,
host down, etc.) — callers branch on the bool rather than catching
exceptions. `send()` returns False after the connection has gone away
so callers can stop spending CPU on bytes nobody is reading. `close()`
drains the consumer with `drain_timeout` so tail settled-lines for
audio already sent don't get dropped when the Bridge disconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

# How many consecutive snapshots a tail must hold the same text (with
# `buffer_transcription` empty) before we flush it as settled. WlK's
# tick rate is ~2–5 Hz; 3 snapshots buys ~600 ms – 1.5 s of
# confirmation, which is short enough that short utterances reach the
# dashboard quickly but long enough that a brief mid-snapshot pause
# doesn't flush a still-growing line. The merger edge case (a
# previously-flushed tail later grows via same-speaker merge) is
# handled by suffix-only re-emission in `_consider_emit_line`.
_TAIL_STABLE_SNAPSHOTS: int = 3


class WlKRelay:
    """One-shot relay from the Recorder to its WhisperLiveKit child.

    Not reusable: after `close()`, construct a new instance for the
    next /tap WS. This keeps lifecycle reasoning tractable — there's
    no "is this still connected?" mode to worry about beyond `connect()`.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        language: str,
        on_settled_line: Callable[[str], None],
        on_metrics: Callable[[float], Awaitable[None]] | None = None,
        on_buffer: Callable[[str], None] | None = None,
        drain_timeout: float = 1.0,
    ):
        self._host = host
        self._port = port
        self._language = language
        self._on_settled_line = on_settled_line
        self._on_metrics = on_metrics
        # Fired with WlK's `buffer_transcription` only on change
        # (including the transition to "" when text commits out).
        self._on_buffer = on_buffer
        self._drain_timeout = drain_timeout
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._consumer: asyncio.Task | None = None
        self._last_snapshot: list[dict] = []
        # Per-line emission state, keyed by `(speaker, start)` from the
        # WlK wire format. Tracks the text we last forwarded to the
        # callback so a re-sent snapshot doesn't double-emit, and so a
        # line whose text grows between snapshots (WlK merges
        # consecutive same-speaker segments — see
        # `tokens_alignment.py`) only emits the new suffix.
        self._emitted_by_key: dict[tuple, str] = {}
        # Non-tail entries are immutable in WlK's wire format — the
        # merger case only ever modifies the CURRENT tail in place;
        # once a newer entry appears after a position, that position
        # is frozen. Cache the lower bound of the scan so we don't
        # re-walk hundreds of stable lines on every snapshot for long
        # sessions.
        self._last_emit_scan_upto: int = 0
        # Tail-stability bookkeeping: counts how many consecutive
        # snapshots the tail's `(key, text)` has matched while the
        # buffer was empty. Reset whenever the tail changes or buffer
        # has in-flight text. Once it crosses `_TAIL_STABLE_SNAPSHOTS`
        # the tail is flushed via `_consider_emit_line`.
        self._tail_stable_key: tuple | None = None
        self._tail_stable_text: str = ""
        self._tail_stable_count: int = 0
        # Used to dedupe on_buffer calls and to rescue residual
        # uncommitted text on close (see _flush_tail).
        self._last_buffer: str = ""
        # Latest `remaining_time_transcription` value WlK reported on this
        # connection. Whisperlivekit's internal `lag=`/`internal_buffer=`
        # heartbeat is log-only and has no connection id; this field is
        # the only per-tap lag signal that arrives over the wire. Stale
        # between snapshots (WlK emits one every few hundred ms).
        self.lag_s: float | None = None

    async def connect(self) -> bool:
        """Open the relay. Returns False on any failure — callers branch
        on this rather than catching, so the /tap handler stays tidy.

        The exception list intentionally covers both transport-level
        problems (OSError, timeout) AND handshake-level rejections
        (InvalidHandshake — InvalidStatus, InvalidUpgrade, etc.).
        Without InvalidHandshake, an HTTP 5xx from a half-down WlK (e.g.
        port held by a process that won't speak WebSockets) escapes the
        relay, crashes the /tap ASGI handler, and the recording-vs-live
        independence invariant breaks."""
        url = f"ws://{self._host}:{self._port}/asr?language={self._language}"
        try:
            self._ws = await websockets.connect(url, open_timeout=2.0)
        except (TimeoutError, OSError, ConnectionClosed, InvalidHandshake) as e:
            print(f"[tapscribe] WlK relay connect failed: {e}", flush=True)
            return False
        self._consumer = asyncio.create_task(self._consume())
        return True

    async def send(self, data: bytes) -> bool:
        """Forward bytes to WlK. Returns False once the connection has
        gone away (server died, network glitch). Callers should treat
        that as "stop relaying for the rest of this /tap WS" without
        crashing."""
        if self._ws is None:
            return False
        try:
            await self._ws.send(data)
            return True
        except (ConnectionClosed, OSError):
            self._ws = None
            return False

    async def close(self) -> None:
        """Signal end-of-audio, drain the consumer, close the WS — in that
        order, so tail settled-lines for audio we already sent get
        appended before we return.

        The end-of-audio signal is an empty binary frame — the same wire
        signal whisperlivekit's own web client sends on stop: the server
        flushes its remaining PCM, sends the final results, and replies
        `{"type": "ready_to_stop"}` (see whisperlivekit's
        `audio_processor.process_audio` / `basic_server`). TapScribe's
        Moonshine `/asr` server speaks the same protocol. Sending it
        BEFORE the WS close is what lets the peer deliver its close-time
        final snapshot at all — once the close handshake starts, the
        peer's sends can only fail, which used to truncate the tail of
        essentially every Moonshine utterance (PR #334 finding #5). The
        consumer ends on `ready_to_stop` (or the peer's own close); the
        `drain_timeout` bounds the wait against a peer that never
        answers, in which case we fall back to cancelling — exactly the
        pre-signal behaviour.

        After the consumer has drained, we flush the in-flight tail from
        the most-recent snapshot — the line that was being held because
        no newer line had appeared after it. Without this, a short
        utterance that produced exactly one line would never reach the
        dashboard.

        The tail flush is wrapped in a `finally` so that even if the
        outer task is being cancelled mid-close (TestClient does this on
        WS exit; some ASGI servers do under shutdown), the held-back
        line still reaches the callback."""
        try:
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.send(b"")  # end-of-audio: request the final snapshot
            if self._consumer is not None:
                try:
                    await asyncio.wait_for(self._consumer, timeout=self._drain_timeout)
                except (TimeoutError, asyncio.CancelledError):
                    self._consumer.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._consumer
                self._consumer = None
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
                self._ws = None
        finally:
            self._flush_tail()

    async def _consume(self) -> None:
        """Read WlK's response stream and route settled lines to the callback.

        WlK emits rolling JSON whose `lines` field is a CUMULATIVE
        snapshot of every committed line so far in the session. Each
        entry's text can grow between snapshots when WlK merges
        consecutive same-speaker segments, so emissions are
        `(speaker, start)`-keyed and re-send only the new suffix on
        growth.

        Tail handling: every non-tail entry is forwarded immediately.
        The tail is held until either (a) a newer line appears after it
        (it becomes a non-tail and follows the immediate path) or (b)
        we observe `_TAIL_STABLE_SNAPSHOTS` consecutive snapshots where
        the tail's text is unchanged and `buffer_transcription` is
        empty — that's WlK's "no more in-flight tokens" signal, so
        flushing is safe. Anything still unemitted at close time goes
        through `_flush_tail`.

        Malformed payloads, snapshots that shrink (shouldn't happen in
        practice), and entries with no usable text are dropped silently.
        """
        if self._ws is None:
            return
        try:
            async for msg in self._ws:
                try:
                    data = json.loads(msg)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                raw_lag = data.get("remaining_time_transcription")
                if isinstance(raw_lag, (int, float)):
                    self.lag_s = float(raw_lag)
                    if self._on_metrics is not None:
                        with contextlib.suppress(Exception):
                            await self._on_metrics(self.lag_s)
                # In-flight buffer (uncommitted hypothesis). Surface it
                # only on change so the dashboard doesn't get hammered
                # with redundant updates when WlK ticks at 2 Hz with the
                # same text in flight.
                raw_buf = data.get("buffer_transcription")
                if isinstance(raw_buf, str) and raw_buf != self._last_buffer:
                    self._last_buffer = raw_buf
                    if self._on_buffer is not None:
                        with contextlib.suppress(Exception):
                            self._on_buffer(raw_buf)
                snapshot = data.get("lines")
                if isinstance(snapshot, list):
                    self._last_snapshot = snapshot
                    # Emit every newly-non-tail entry. Non-tail positions
                    # are immutable in WlK's wire format, so we only need
                    # to scan from the last upto we processed. The dedup
                    # in `_consider_emit_line` covers a tail-becoming-
                    # non-tail whose text already settled (no re-emit) and
                    # the rare same-key text-change case (emits the diff).
                    upto = max(0, len(snapshot) - 1)
                    for i in range(self._last_emit_scan_upto, upto):
                        self._consider_emit_line(snapshot[i])
                    if upto > self._last_emit_scan_upto:
                        self._last_emit_scan_upto = upto
                    # Tail-stability flush: once the tail text has held
                    # steady (with the in-flight buffer empty) for enough
                    # consecutive snapshots, WlK has nothing more to
                    # commit into it and we can flush without losing
                    # context — short utterances reach the LiveFeed
                    # without waiting for the bridge to disconnect.
                    self._maybe_flush_stable_tail(snapshot)
                # `ready_to_stop` is the peer's answer to our end-of-audio
                # signal (see `close()`): everything is delivered, so end
                # the drain immediately instead of waiting out
                # drain_timeout. Checked AFTER the snapshot handling above
                # — the Moonshine server ships its final lines in the same
                # message (real WlK sends it as a bare, lines-less
                # message; either shape ends the drain).
                if data.get("type") == "ready_to_stop":
                    break
        except (ConnectionClosed, asyncio.CancelledError):
            # Both are normal ends of the drain, not faults: ConnectionClosed
            # when the peer (WlK child / Moonshine server) tears the WS down
            # under us, CancelledError when `close()` cancels this consumer
            # task after the drain window. Swallowing CancelledError is safe
            # here — the coroutine ends immediately after this handler, so
            # cancellation still completes. What's lost: only the distinction
            # between a clean and an abrupt end, which has no consumer;
            # settled lines already flushed keep flowing via on_settled_line.
            pass
        except Exception as e:
            print(f"[tapscribe] WlK relay consumer error: {e}", flush=True)

    @staticmethod
    def _line_key(line: dict) -> tuple:
        """Stable identity for a `lines[]` entry. WlK doesn't ship an
        explicit id, but `(speaker, start)` is unique within a session
        — `start` is the segment's start timestamp from the audio, and
        WlK only assigns one segment per (speaker, start) point."""
        return (line.get("speaker"), line.get("start"))

    def _consider_emit_line(self, line: dict) -> None:
        """Emit a line — or the new suffix if its text grew since the
        last emission — guarding against duplicates from re-sent
        snapshots. Silence segments (`speaker == -2`) and other
        empty-text entries are silently skipped."""
        if not isinstance(line, dict):
            return
        text = (line.get("text") or "").strip()
        if not text:
            return
        key = self._line_key(line)
        prev = self._emitted_by_key.get(key, "")
        if text == prev:
            return
        if prev and text.startswith(prev):
            # Growth (same-speaker merger): forward only the new tail.
            # `lstrip()` to drop the boundary whitespace WlK inserts
            # when concatenating segments.
            suffix = text[len(prev) :].lstrip()
            if not suffix:
                return
            self._on_settled_line(suffix)
        else:
            # Either first emit of this key, or text changed in a way
            # that isn't a clean prefix-extension (rare — would mean
            # WlK rewrote a committed line). Forward as-is rather than
            # try to guess the diff.
            self._on_settled_line(text)
        self._emitted_by_key[key] = text

    def _maybe_flush_stable_tail(self, snapshot: list[dict]) -> None:
        """Track tail stability and flush once the threshold's reached.
        Resets the counter whenever the tail key changes, the text
        changes, or the in-flight buffer is non-empty."""
        if not snapshot:
            self._tail_stable_key = None
            self._tail_stable_text = ""
            self._tail_stable_count = 0
            return
        tail = snapshot[-1]
        if not isinstance(tail, dict):
            return
        key = self._line_key(tail)
        text = (tail.get("text") or "").strip()
        buf_empty = not (self._last_buffer or "").strip()
        if not text or not buf_empty:
            self._tail_stable_key = key
            self._tail_stable_text = text
            self._tail_stable_count = 0
            return
        if key == self._tail_stable_key and text == self._tail_stable_text:
            self._tail_stable_count += 1
        else:
            self._tail_stable_key = key
            self._tail_stable_text = text
            self._tail_stable_count = 1
        if self._tail_stable_count >= _TAIL_STABLE_SNAPSHOTS:
            # `_consider_emit_line` is idempotent on (key, text), so if
            # we've already flushed this tail (e.g. via close-drain
            # racing), this call is a no-op.
            self._consider_emit_line(tail)

    def _flush_tail(self) -> None:
        """Close-time drain: emit anything still unemitted from the
        most-recent snapshot, plus any non-empty `buffer_transcription`
        as a final settled line. Without this, the trailing word(s) of
        every utterance that hadn't cleared LocalAgreement-2 by close
        time — or a single-line short utterance whose tail never
        stabilised long enough to auto-flush — would silently drop.
        """
        for line in self._last_snapshot:
            self._consider_emit_line(line)
        tail = (self._last_buffer or "").strip()
        if tail:
            self._on_settled_line(tail)
            self._last_buffer = ""
            if self._on_buffer is not None:
                with contextlib.suppress(Exception):
                    self._on_buffer("")
