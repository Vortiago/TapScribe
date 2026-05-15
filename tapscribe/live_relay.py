"""WhisperLiveKit relay — the Recorder's client-side WS to its supervised
whisperlivekit-server child.

Each open `/tap` WebSocket from a Bridge spawns its own `WlKRelay`. The
relay holds:

  - One outbound WS to `ws://<host>:<port>/asr?language=<lang>` (the
    `WhisperLiveKit` child). The Recorder writes Bridge bytes to this
    stream; WlK responds with rolling JSON transcripts.
  - One background consumer task that parses those responses and calls
    `on_settled_line(text)` for every entry in `committed_lines`. The
    handler typically appends the result to `recorder.transcripts` so
    the dashboard sees it.

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
from collections.abc import Callable

import websockets
from websockets.exceptions import ConnectionClosed


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
        drain_timeout: float = 1.0,
    ):
        self._host = host
        self._port = port
        self._language = language
        self._on_settled_line = on_settled_line
        self._drain_timeout = drain_timeout
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._consumer: asyncio.Task | None = None

    async def connect(self) -> bool:
        """Open the relay. Returns False on any failure — callers branch
        on this rather than catching, so the /tap handler stays tidy."""
        url = f"ws://{self._host}:{self._port}/asr?language={self._language}"
        try:
            self._ws = await websockets.connect(url, open_timeout=2.0)
        except (OSError, asyncio.TimeoutError, ConnectionClosed) as e:
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
        for audio we already sent get appended before we return."""
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._consumer is not None:
            try:
                await asyncio.wait_for(self._consumer, timeout=self._drain_timeout)
            except asyncio.TimeoutError:
                self._consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._consumer
            self._consumer = None

    async def _consume(self) -> None:
        """Read WlK's response stream and route settled lines to the callback.

        WlK emits rolling JSON with both `committed_lines` (settled) and
        `buffer_transcription` (still being refined). We only fire the
        callback for committed entries; malformed payloads and lines with
        no usable text are dropped silently.
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
                lines = data.get("committed_lines")
                if not isinstance(lines, list):
                    continue
                for line in lines:
                    if not isinstance(line, dict):
                        continue
                    text = (line.get("text") or "").strip()
                    if text:
                        self._on_settled_line(text)
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as e:
            print(f"[tapscribe] WlK relay consumer error: {e}", flush=True)
