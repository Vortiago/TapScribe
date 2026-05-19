"""Tests for tapscribe.live_relay — the Recorder-side WS client to
WhisperLiveKit.

We spin up a tiny in-process WS server that pretends to be
whisperlivekit-server: it records every PCM frame it receives so the
test can assert bytes round-trip, and on demand it emits the
realistic FrontData JSON shape (`{"lines": [...], "buffer_transcription": ...}`)
so we can verify settled-line consumption against the wire format the
production WlK actually sends. See whisperlivekit/timed_objects.py
(`FrontData.to_dict`) for the source of truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
import websockets

from tapscribe.live_relay import WlKRelay

# ---------------------------------------------------------------------------
# Test wait helpers — event-based replacements for "await asyncio.sleep(...)"
# ---------------------------------------------------------------------------


class _SignalList(list):
    """A list that exposes an asyncio.Event firing whenever entries land,
    so tests can `await wait_count(n)` instead of `await asyncio.sleep(...)`.

    Used in place of bare `lines: list[str] = []` collectors. The relay's
    `on_settled_line` callback runs synchronously from inside the consumer
    task, so signalling the event in `append` is sufficient — the test
    coroutine resumes as soon as the consumer hands us the new line.
    """

    def __init__(self) -> None:
        super().__init__()
        self._event = asyncio.Event()

    def append(self, item) -> None:  # type: ignore[override]
        super().append(item)
        self._event.set()

    async def wait_count(self, n: int, *, timeout: float = 1.0) -> None:
        """Block until the list has at least `n` items, or raise TimeoutError."""

        async def _wait() -> None:
            while len(self) < n:
                self._event.clear()
                if len(self) >= n:
                    return
                await self._event.wait()

        await asyncio.wait_for(_wait(), timeout=timeout)


async def _wait_for(predicate, *, timeout: float = 1.0, interval: float = 0.005) -> None:
    """Poll-based fallback for state that doesn't have a natural event hook
    (e.g. `fake_wlk.received` is mutated by the fake server's own loop in
    a different thread — we can't intercept the append cheaply). Tight
    polling interval so tests don't burn real wall time when the condition
    flips quickly."""

    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_wait(), timeout=timeout)


# ---------------------------------------------------------------------------
# Fake WhisperLiveKit
# ---------------------------------------------------------------------------


class _FakeWlk:
    """One-connection WS server. Captures every received message and lets
    the test push FrontData-shaped JSON back at will.

    The real WhisperLiveKit emits a CUMULATIVE snapshot of all committed
    lines on every update — the relay is responsible for de-duplicating.
    `push_lines_snapshot()` mirrors that semantics; `push_committed()`
    is a thin convenience that wraps a single new tail line in a snapshot
    that grows by one each call (so a sequence of pushes simulates a
    typical streaming session)."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self._connections: list[Any] = []
        self._port = _free_port()
        self._server: Any = None
        self._stop_event = asyncio.Event()
        self._committed_lines: list[dict[str, Any]] = []

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, "localhost", self._port)

    async def stop(self) -> None:
        for c in list(self._connections):
            with contextlib.suppress(Exception):
                await c.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def push_lines_snapshot(
        self,
        lines: list[dict[str, Any]],
        *,
        buffer_transcription: str = "",
    ) -> None:
        """Emit a FrontData-shaped snapshot. `lines` is the FULL cumulative
        list as the real WlK sends — the relay must dedupe."""
        msg = json.dumps(
            {
                "status": "active_transcription",
                "lines": lines,
                "buffer_transcription": buffer_transcription,
                "buffer_diarization": "",
                "buffer_translation": "",
                "remaining_time_transcription": 0,
                "remaining_time_diarization": 0,
            }
        )
        for c in list(self._connections):
            with contextlib.suppress(Exception):
                await c.send(msg)

    async def push_committed(self, text: str) -> None:
        """Convenience: append `text` as a new committed line and push the
        cumulative snapshot. Mirrors a typical streaming session where each
        new settled line arrives in a fresh snapshot containing all prior
        lines too. The line's `start`/`end` advance with each call so each
        committed line has a stable identity."""
        idx = len(self._committed_lines)
        self._committed_lines.append(
            {
                "text": text,
                "speaker": 1,
                "start": float(idx),
                "end": float(idx) + 1.0,
            }
        )
        await self.push_lines_snapshot(list(self._committed_lines))

    async def _handler(self, ws) -> None:
        self._connections.append(ws)
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.received.append(msg)
        finally:
            if ws in self._connections:
                self._connections.remove(ws)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


@pytest.fixture
async def fake_wlk() -> AsyncIterator[_FakeWlk]:
    wlk = _FakeWlk()
    await wlk.start()
    try:
        yield wlk
    finally:
        await wlk.stop()


# ---------------------------------------------------------------------------
# WlKRelay tests
# ---------------------------------------------------------------------------


async def test_relay_connect_then_send_round_trips_bytes(fake_wlk: _FakeWlk):
    lines: list[str] = []
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
    )
    assert await relay.connect() is True
    assert await relay.send(b"\x00\x01" * 320) is True  # one 20 ms PCM frame
    # Wait for the fake server's handler to receive the bytes (event-based
    # via the fake's own connection list — see _wait_for above).
    await _wait_for(lambda: fake_wlk.received == [b"\x00\x01" * 320])
    assert fake_wlk.received == [b"\x00\x01" * 320]
    await relay.close()


async def test_relay_consumes_lines_into_callback_with_close_drain(fake_wlk: _FakeWlk):
    """The real WlK emits a CUMULATIVE `lines` snapshot on each tick. The
    relay holds the in-flight tail (latest line) until either a newer
    line appears after it or the relay closes — ensuring each settled
    line is emitted exactly once regardless of how many snapshots
    contained it."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
    )
    await relay.connect()
    await fake_wlk.push_committed("hello world")
    await fake_wlk.push_committed("second line")
    # Event-based: wait until the first finalized line has reached the callback.
    await lines.wait_count(1)
    # After two snapshots, "hello world" is finalized (a newer line
    # exists after it); "second line" is still the tail.
    assert list(lines) == ["hello world"]
    await relay.close()
    # close() drains: the remaining tail line gets emitted.
    assert list(lines) == ["hello world", "second line"]


async def test_relay_dedupes_lines_across_repeated_snapshots(fake_wlk: _FakeWlk):
    """WlK re-sends the full lines list on every tick. The relay must
    NOT re-emit a line just because it appeared in three consecutive
    snapshots — the dashboard would fill with duplicates."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
    )
    await relay.connect()
    base = [
        {"text": "first", "speaker": 1, "start": 0.0, "end": 1.0},
        {"text": "second", "speaker": 1, "start": 1.0, "end": 2.0},
        {"text": "third (in-flight)", "speaker": 1, "start": 2.0, "end": 3.0},
    ]
    # The same snapshot delivered three times should yield each
    # finalized line exactly once. After the first push the two
    # non-tail lines should land; the subsequent pushes are de-duped.
    for _ in range(3):
        await fake_wlk.push_lines_snapshot(base)
    # Wait for the two non-tail lines to make it through the consumer.
    await lines.wait_count(2)
    assert list(lines) == ["first", "second"]
    await relay.close()


async def test_relay_ignores_lines_without_text(fake_wlk: _FakeWlk):
    """WlK can emit speaker-only / silence segments with empty text.
    Those should be skipped rather than appending empty strings to
    LiveTranscripts."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
    )
    await relay.connect()
    # Snapshot with a no-text entry, a whitespace-only entry, then a
    # valid line. The valid line will be the tail so it won't emit yet.
    snap = [
        {"text": "", "speaker": -2, "start": 0.0, "end": 0.5},
        {"text": "   ", "speaker": 1, "start": 0.5, "end": 1.0},
        {"text": "real line", "speaker": 1, "start": 1.0, "end": 2.0},
    ]
    await fake_wlk.push_lines_snapshot(snap)
    # Push another snapshot that adds a real line so the prior real
    # line becomes finalized.
    snap2 = snap + [{"text": "newer", "speaker": 1, "start": 2.0, "end": 3.0}]
    await fake_wlk.push_lines_snapshot(snap2)
    # And another raw malformed payload — must not crash.
    for c in fake_wlk._connections:
        await c.send(json.dumps({"foo": "bar"}))
        await c.send("not even json {{{")
    # Event-based: wait until the one expected finalized line has landed.
    await lines.wait_count(1)
    # Empty-text entries are skipped; only "real line" makes it through
    # (the in-flight "newer" tail is held until close).
    assert list(lines) == ["real line"]
    await relay.close()


async def test_relay_connect_returns_false_on_unreachable_target():
    """When WlK isn't running, connect() returns False without raising.
    The /tap handler relies on this to decide whether to attempt sends."""
    relay = WlKRelay(
        host="localhost",
        port=_free_port(),  # nobody's listening
        language="en",
        on_settled_line=lambda _t: None,
    )
    assert await relay.connect() is False


async def test_relay_send_after_failed_connect_returns_false():
    """send() should be safe to call even when connect failed — caller can
    keep the call site uniform without branching."""
    relay = WlKRelay(
        host="localhost",
        port=_free_port(),
        language="en",
        on_settled_line=lambda _t: None,
    )
    await relay.connect()
    assert await relay.send(b"\x00" * 10) is False


async def test_relay_send_returns_false_after_server_closes(fake_wlk: _FakeWlk):
    """If WlK dies mid-stream, send() reports the failure so the /tap
    handler can stop trying without spamming errors."""
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lambda _t: None,
    )
    await relay.connect()
    await fake_wlk.stop()
    # Give the server time to actually close the connection.
    # real-time timeout test — sleep is intentional (closure propagation
    # over a TCP socket is a real-clock event with no in-process hook).
    await asyncio.sleep(0.05)
    # The first send may succeed (buffered) or fail; one or two more should
    # definitely fail. Loop until we observe the closure.
    failed = False
    for _ in range(5):
        if await relay.send(b"\x00" * 10) is False:
            failed = True
            break
        # real-time timeout test — sleep is intentional (backoff between retries).
        await asyncio.sleep(0.02)
    assert failed
    await relay.close()


async def test_relay_close_drains_tail_settled_lines(fake_wlk: _FakeWlk):
    """After the Bridge disconnects, the relay should give WlK a moment
    to emit any settled lines for audio already sent (drain timeout).
    Even a single in-flight tail line gets emitted on close — otherwise
    a short utterance that produced exactly one line would be lost
    every time."""
    lines: list[str] = []
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
        drain_timeout=0.5,
    )
    await relay.connect()
    # Push the settled line, then close — the drain window must let the
    # consumer process it before the close completes.
    await fake_wlk.push_committed("tail caption")
    await relay.close()
    assert "tail caption" in lines


async def test_relay_extracts_remaining_time_into_on_metrics(fake_wlk: _FakeWlk):
    """WlK's per-tick `remaining_time_transcription` is the only per-tap
    backlog signal that arrives over the wire (its `lag=`/`internal_buffer=`
    heartbeat is log-only and not connection-attributable). The relay
    exposes it both as a `lag_s` attribute and via an `on_metrics` async
    callback so the dashboard can render a per-tap indicator."""
    seen: list[float] = []
    metric_event = asyncio.Event()

    async def on_metrics(v: float) -> None:
        seen.append(v)
        metric_event.set()

    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lambda _t: None,
        on_metrics=on_metrics,
    )
    await relay.connect()
    # push_lines_snapshot hard-codes remaining_time_transcription=0; push
    # a custom payload so we observe a non-zero value.
    for c in fake_wlk._connections:
        await c.send(
            json.dumps(
                {
                    "status": "active_transcription",
                    "lines": [],
                    "buffer_transcription": "",
                    "remaining_time_transcription": 1.25,
                    "remaining_time_diarization": 0,
                }
            )
        )
    # Event-based: the consumer fires on_metrics from inside _consume.
    await asyncio.wait_for(metric_event.wait(), timeout=1.0)
    assert seen == [1.25]
    assert relay.lag_s == 1.25
    await relay.close()


async def test_relay_emits_finalized_lines_immediately_not_just_on_close(fake_wlk: _FakeWlk):
    """Sanity guard against a regression where the relay only flushed at
    close: in a multi-line utterance, settled lines must reach the
    dashboard's live-transcripts feed in real time, not all at once when
    the bridge disconnects."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
    )
    await relay.connect()
    await fake_wlk.push_committed("one")
    await fake_wlk.push_committed("two")
    await fake_wlk.push_committed("three")
    # Event-based: wait for both finalized lines to land at the callback.
    await lines.wait_count(2)
    # Without close: "one" and "two" are finalized, "three" is the tail.
    assert list(lines) == ["one", "two"]
    await relay.close()
