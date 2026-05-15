"""Tests for tapscribe.live_relay — the Recorder-side WS client to
WhisperLiveKit.

We spin up a tiny in-process WS server that pretends to be
whisperlivekit-server: it records every PCM frame it receives so the
test can assert bytes round-trip, and on demand it emits a
`committed_lines` JSON message so we can verify settled-line consumption.
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
# Fake WhisperLiveKit
# ---------------------------------------------------------------------------

class _FakeWlk:
    """One-connection WS server. Captures every received message and lets
    the test push `committed_lines` JSON back at will."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self._connections: list[Any] = []
        self._port = _free_port()
        self._server: Any = None
        self._stop_event = asyncio.Event()

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

    async def push_committed(self, text: str) -> None:
        """Emit a settled-line JSON on every active connection."""
        msg = json.dumps({"committed_lines": [{"text": text}]})
        for c in list(self._connections):
            with contextlib.suppress(Exception):
                await c.send(msg)

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
        host="localhost", port=fake_wlk.port, language="en",
        on_settled_line=lines.append,
    )
    assert await relay.connect() is True
    assert await relay.send(b"\x00\x01" * 320) is True  # one 20 ms PCM frame
    # Allow the fake server's handler to receive the bytes
    await asyncio.sleep(0.05)
    assert fake_wlk.received == [b"\x00\x01" * 320]
    await relay.close()


async def test_relay_consumes_committed_lines_into_callback(fake_wlk: _FakeWlk):
    lines: list[str] = []
    relay = WlKRelay(
        host="localhost", port=fake_wlk.port, language="en",
        on_settled_line=lines.append,
    )
    await relay.connect()
    await fake_wlk.push_committed("hello world")
    await fake_wlk.push_committed("second line")
    # Give the consumer task a tick to process the pushed messages
    await asyncio.sleep(0.05)
    assert lines == ["hello world", "second line"]
    await relay.close()


async def test_relay_ignores_committed_lines_without_text(fake_wlk: _FakeWlk):
    """WlK sometimes emits empty committed_lines entries on near-silent
    frames. The consumer should skip them rather than appending empty
    strings to LiveTranscripts."""
    lines: list[str] = []
    relay = WlKRelay(
        host="localhost", port=fake_wlk.port, language="en",
        on_settled_line=lines.append,
    )
    await relay.connect()
    # Push a malformed-looking message and an empty-text line
    for c in fake_wlk._connections:
        await c.send(json.dumps({"committed_lines": [{"text": ""}, {"text": "   "}]}))
        await c.send(json.dumps({"foo": "bar"}))  # no committed_lines key
        await c.send("not even json {{{")
    await asyncio.sleep(0.05)
    assert lines == []
    await relay.close()


async def test_relay_connect_returns_false_on_unreachable_target():
    """When WlK isn't running, connect() returns False without raising.
    The /tap handler relies on this to decide whether to attempt sends."""
    relay = WlKRelay(
        host="localhost", port=_free_port(),  # nobody's listening
        language="en", on_settled_line=lambda _t: None,
    )
    assert await relay.connect() is False


async def test_relay_send_after_failed_connect_returns_false():
    """send() should be safe to call even when connect failed — caller can
    keep the call site uniform without branching."""
    relay = WlKRelay(
        host="localhost", port=_free_port(),
        language="en", on_settled_line=lambda _t: None,
    )
    await relay.connect()
    assert await relay.send(b"\x00" * 10) is False


async def test_relay_send_returns_false_after_server_closes(fake_wlk: _FakeWlk):
    """If WlK dies mid-stream, send() reports the failure so the /tap
    handler can stop trying without spamming errors."""
    relay = WlKRelay(
        host="localhost", port=fake_wlk.port, language="en",
        on_settled_line=lambda _t: None,
    )
    await relay.connect()
    await fake_wlk.stop()
    # Give the server time to actually close the connection
    await asyncio.sleep(0.05)
    # The first send may succeed (buffered) or fail; one or two more should
    # definitely fail. Loop until we observe the closure.
    failed = False
    for _ in range(5):
        if await relay.send(b"\x00" * 10) is False:
            failed = True
            break
        await asyncio.sleep(0.02)
    assert failed
    await relay.close()


async def test_relay_close_drains_tail_settled_lines(fake_wlk: _FakeWlk):
    """After the Bridge disconnects, the relay should give WlK a moment
    to emit any settled lines for audio already sent (drain timeout)."""
    lines: list[str] = []
    relay = WlKRelay(
        host="localhost", port=fake_wlk.port, language="en",
        on_settled_line=lines.append,
        drain_timeout=0.5,
    )
    await relay.connect()
    # Push the settled line, then close — the drain window must let the
    # consumer process it before the close completes.
    await fake_wlk.push_committed("tail caption")
    await relay.close()
    assert "tail caption" in lines
