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

WlK semantics: every response carries a CUMULATIVE `lines` list — i.e.
each tick re-sends every line emitted so far in the session, not just
the new ones. The last entry may still be growing as new tokens arrive.
The relay treats a line as finalized when (a) a newer line appears
after it (so the position is stable), or (b) the relay closes (drain) —
in which case the in-flight tail is also flushed so a short utterance
that produced exactly one line isn't lost.

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
        # How many entries from the cumulative `lines` snapshot we've
        # already passed to the callback. Each tick we emit
        # `snapshot[_emitted_count : len(snapshot) - 1]` (skipping the
        # in-flight tail); on close-drain we also emit the tail.
        self._emitted_count: int = 0
        self._last_snapshot: list[dict] = []
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
        except (OSError, asyncio.TimeoutError, ConnectionClosed, InvalidHandshake) as e:
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
        """Close the WS and drain the consumer so any tail settled-lines
        for audio we already sent get appended before we return.

        After the consumer task has drained, we flush the in-flight tail
        from the most-recent snapshot — the line that was being held
        because no newer line had appeared after it. Without this, a
        short utterance that produced exactly one line would never reach
        the dashboard.

        The tail flush is wrapped in a `finally` so that even if the
        outer task is being cancelled mid-close (TestClient does this on
        WS exit; some ASGI servers do under shutdown), the held-back
        line still reaches the callback."""
        try:
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
                self._ws = None
            if self._consumer is not None:
                try:
                    await asyncio.wait_for(self._consumer, timeout=self._drain_timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._consumer.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._consumer
                self._consumer = None
        finally:
            self._flush_tail()

    async def _consume(self) -> None:
        """Read WlK's response stream and route settled lines to the callback.

        WlK emits rolling JSON whose `lines` field is a CUMULATIVE
        snapshot of every committed line so far in the session. The last
        entry may still be growing — we only emit lines whose position
        has been superseded by a newer entry. The remaining tail is
        emitted on close (`_flush_tail`).

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
                if not isinstance(snapshot, list):
                    continue
                self._last_snapshot = snapshot
                # Emit everything strictly before the tail. The tail (last
                # entry) may still be growing; hold it until either a newer
                # line appears in a future snapshot, or close() drains.
                upto = max(0, len(snapshot) - 1)
                while self._emitted_count < upto:
                    self._emit_line(snapshot[self._emitted_count])
                    self._emitted_count += 1
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as e:
            print(f"[tapscribe] WlK relay consumer error: {e}", flush=True)

    def _emit_line(self, line: dict) -> None:
        """Push one settled line to the callback. Skips entries with no
        text (e.g. silence segments WlK emits with `speaker == -2`)."""
        if not isinstance(line, dict):
            return
        text = (line.get("text") or "").strip()
        if text:
            self._on_settled_line(text)

    def _flush_tail(self) -> None:
        """Emit any held-back tail lines from the last snapshot, plus any
        in-flight `buffer_transcription` that didn't make it into a
        committed line before close. Plus rescues any non-empty
        `buffer_transcription` as a final settled line — without
        this, the trailing word(s) of every utterance that hadn't
        cleared LocalAgreement-2 by close time would silently drop.
        """
        snapshot = self._last_snapshot
        while self._emitted_count < len(snapshot):
            self._emit_line(snapshot[self._emitted_count])
            self._emitted_count += 1
        tail = (self._last_buffer or "").strip()
        if tail:
            self._on_settled_line(tail)
            self._last_buffer = ""
            if self._on_buffer is not None:
                with contextlib.suppress(Exception):
                    self._on_buffer("")
