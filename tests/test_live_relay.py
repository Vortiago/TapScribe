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
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # pytest puts tests/ on sys.path so `from conftest import` resolves the project's tests/conftest.py
    wait_for,
)

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
    # Wait for the fake server's handler to receive the bytes (poll-based
    # via the shared conftest wait_for — the fake's receive list has no
    # event hook to await).
    await wait_for(lambda: fake_wlk.received == [b"\x00\x01" * 320])
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
    snapshots — the dashboard would fill with duplicates.

    With the tail-stability flush (see `_TAIL_STABLE_SNAPSHOTS`), the
    third entry DOES eventually settle once it's been stable for
    enough ticks — but it must still emit exactly once across the
    repeated snapshots, never duplicated."""
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
    # Push enough identical snapshots to cross the stability threshold.
    for _ in range(5):
        await fake_wlk.push_lines_snapshot(base)
    # All three lines should land — first two on the first snapshot
    # (non-tail), third after the stability window.
    await lines.wait_count(3)
    assert list(lines) == ["first", "second", "third (in-flight)"]
    # Pushing the same snapshot again must not re-emit any of them.
    for _ in range(3):
        await fake_wlk.push_lines_snapshot(base)
    # Give the consumer a chance to process; assert no duplicates appeared.
    await asyncio.sleep(0.05)
    assert list(lines) == ["first", "second", "third (in-flight)"]
    await relay.close()
    # Close-drain must be idempotent against already-emitted lines.
    assert list(lines) == ["first", "second", "third (in-flight)"]


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


async def test_relay_pushes_buffer_transcription_to_on_buffer_callback(fake_wlk: _FakeWlk):
    """WlK's response carries the in-flight (uncommitted) hypothesis text
    in `buffer_transcription` on every snapshot. The relay forwards this
    to `on_buffer` so the dashboard's per-tap row can render "what
    Whisper is currently transcribing" before it commits to `lines`.
    """
    bufs: list[str] = []
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lambda _t: None,
        on_buffer=bufs.append,
    )
    await relay.connect()
    await fake_wlk.push_lines_snapshot([], buffer_transcription="hello in flight")
    await wait_for(lambda: bufs == ["hello in flight"])
    await fake_wlk.push_lines_snapshot([], buffer_transcription="hello world in flight")
    await wait_for(lambda: bufs == ["hello in flight", "hello world in flight"])
    await relay.close()


async def test_relay_emits_empty_buffer_when_text_commits_to_lines(fake_wlk: _FakeWlk):
    """Once Whisper commits its in-flight text to `lines`, WlK sets
    `buffer_transcription` back to "". The relay must surface that
    transition so the dashboard's in-flight indicator clears."""
    bufs: list[str] = []
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lambda _t: None,
        on_buffer=bufs.append,
    )
    await relay.connect()
    await fake_wlk.push_lines_snapshot([], buffer_transcription="growing tail")
    await wait_for(lambda: bufs == ["growing tail"])
    # Now the tail is committed and the buffer empties.
    await fake_wlk.push_lines_snapshot(
        [{"text": "growing tail", "speaker": 1, "start": 0.0, "end": 1.0}],
        buffer_transcription="",
    )
    await wait_for(lambda: bufs == ["growing tail", ""])
    await relay.close()


async def test_relay_close_flushes_trailing_buffer_transcription(fake_wlk: _FakeWlk):
    """The bug we're fixing: on `/tap` close, the trailing words still
    in `buffer_transcription` were getting dropped on the floor.
    `_flush_tail` now also emits the non-empty buffer as a final
    settled line so those words reach the chat log."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
        on_buffer=lambda _t: None,
    )
    await relay.connect()
    # Send a snapshot with one committed line and an in-flight tail.
    # The committed line is the relay's "held tail" (in-flight position
    # in `lines`) — but here we also have buffer_transcription content
    # that's not yet in `lines`.
    await fake_wlk.push_lines_snapshot(
        [{"text": "committed sentence", "speaker": 1, "start": 0.0, "end": 1.5}],
        buffer_transcription="trailing words that didn't commit",
    )
    # No lines emitted yet — single committed entry is the held tail.
    await asyncio.sleep(0.02)
    assert list(lines) == []
    # Close → flush. Both the held tail AND the buffer_transcription
    # land in `lines`.
    await relay.close()
    assert "committed sentence" in lines
    assert "trailing words that didn't commit" in lines


async def test_relay_close_with_empty_buffer_does_not_emit_phantom_line(fake_wlk: _FakeWlk):
    """When `buffer_transcription` is empty at close time, no phantom
    empty line should appear — that would clutter LiveTranscripts with
    blank entries every time the gate sees clean speech→silence."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
        on_buffer=lambda _t: None,
    )
    await relay.connect()
    await fake_wlk.push_lines_snapshot(
        [{"text": "tail", "speaker": 1, "start": 0.0, "end": 1.0}],
        buffer_transcription="",
    )
    await asyncio.sleep(0.02)
    await relay.close()
    # Only the held tail line; no extra empty.
    assert list(lines) == ["tail"]


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


async def test_relay_connect_returns_false_on_http_handshake_rejection():
    """If WlK's port is held by something that speaks HTTP but not
    WebSockets (e.g. a half-down WlK returning 5xx, or a
    misconfigured proxy), the websockets handshake raises
    InvalidStatus — a subclass of InvalidHandshake, NOT of OSError.
    connect() must still return False rather than letting it escape,
    or the /tap ASGI handler crashes and breaks the recording-vs-live
    independence invariant."""
    import threading

    # Plain HTTP listener that returns 503 to any request — the cheapest
    # way to stage "port is bound by something that won't speak WS".
    # Sync sockets in a daemon thread keep the test free of asyncio
    # task-cancellation cleanup.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", 0))
    sock.listen(1)
    sock.settimeout(0.1)
    port = sock.getsockname()[1]
    stop_event = threading.Event()

    def serve_503() -> None:
        while not stop_event.is_set():
            try:
                conn, _ = sock.accept()
            except (TimeoutError, OSError):
                continue
            with contextlib.suppress(Exception):
                conn.settimeout(0.5)
                with contextlib.suppress(socket.timeout, OSError):
                    conn.recv(4096)
                conn.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            with contextlib.suppress(Exception):
                conn.close()

    thread = threading.Thread(target=serve_503, daemon=True)
    thread.start()
    try:
        relay = WlKRelay(
            host="localhost",
            port=port,
            language="en",
            on_settled_line=lambda _t: None,
        )
        assert await relay.connect() is False
    finally:
        stop_event.set()
        with contextlib.suppress(Exception):
            sock.close()
        thread.join(timeout=2.0)


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


async def test_relay_flushes_tail_after_stability_window_no_close_needed(fake_wlk: _FakeWlk):
    """The user-visible fix: short utterances reach the LiveFeed
    without waiting for /tap close. After the tail's text has been
    stable across `_TAIL_STABLE_SNAPSHOTS` consecutive snapshots (with
    `buffer_transcription` empty), it auto-flushes. Before this
    change, a one-line utterance would only land on stop/restart."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
    )
    await relay.connect()
    # One committed line, no in-flight buffer — simulates the user
    # said one short sentence and the gate closed.
    tail = [{"text": "hello there", "speaker": 1, "start": 0.0, "end": 1.5}]
    # Push enough times to cross the stability threshold (currently 3).
    for _ in range(4):
        await fake_wlk.push_lines_snapshot(tail, buffer_transcription="")
    await lines.wait_count(1)
    assert list(lines) == ["hello there"]
    await relay.close()
    # No phantom re-emit on close — the line was already flushed.
    assert list(lines) == ["hello there"]


async def test_relay_does_not_flush_tail_while_buffer_is_non_empty(fake_wlk: _FakeWlk):
    """The stability flush requires `buffer_transcription` to be empty
    — that's WlK's "no more in-flight tokens" signal. While buffer is
    non-empty (WlK is still consuming new audio), the tail might grow,
    so we hold it."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
        on_buffer=lambda _t: None,
    )
    await relay.connect()
    tail = [{"text": "growing tail", "speaker": 1, "start": 0.0, "end": 1.0}]
    # Send many snapshots with the same tail but a non-empty buffer.
    # Stability flush must NOT fire.
    for _ in range(6):
        await fake_wlk.push_lines_snapshot(tail, buffer_transcription="more incoming")
    await asyncio.sleep(0.05)
    assert list(lines) == []
    # Buffer empties and the tail stabilises → flush.
    for _ in range(4):
        await fake_wlk.push_lines_snapshot(tail, buffer_transcription="")
    await lines.wait_count(1)
    assert list(lines) == ["growing tail"]
    await relay.close()


async def test_relay_growing_tail_re_emits_only_new_suffix(fake_wlk: _FakeWlk):
    """If a stabilised+flushed tail later grows (WlK same-speaker
    merger), the relay must emit only the NEW suffix — never the
    whole grown text — or the dashboard ends up with both "hello" and
    "hello world" stacked up."""
    lines = _SignalList()
    relay = WlKRelay(
        host="localhost",
        port=fake_wlk.port,
        language="en",
        on_settled_line=lines.append,
        on_buffer=lambda _t: None,
    )
    await relay.connect()
    # First: short tail, stabilises and gets flushed.
    short = [{"text": "hello", "speaker": 1, "start": 0.0, "end": 0.5}]
    for _ in range(4):
        await fake_wlk.push_lines_snapshot(short, buffer_transcription="")
    await lines.wait_count(1)
    assert list(lines) == ["hello"]
    # Then: same key (speaker=1, start=0.0) but text grew — WlK
    # merged in more same-speaker content. Re-stabilise.
    grown = [{"text": "hello world", "speaker": 1, "start": 0.0, "end": 1.5}]
    for _ in range(4):
        await fake_wlk.push_lines_snapshot(grown, buffer_transcription="")
    await lines.wait_count(2)
    # Only the suffix ("world") emitted on the growth — never the
    # whole "hello world".
    assert list(lines) == ["hello", "world"]
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
