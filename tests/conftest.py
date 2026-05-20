"""Shared pytest fixtures.

The package is imported only after we redirect the recording / config dirs
to a per-session tmpdir — otherwise importing tapscribe.config from a CI
runner would create `recordings/` next to the repo and pollute the
worktree.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import websockets

# Make the in-tree package importable when pytest is invoked from the repo
# root without an editable install (CI's most common shape).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the package's CONFIG_DIR + the three config files at a tmpdir.

    Tests that want to exercise prompt/hotwords/hallucinations reads write
    files into this dir directly.
    """
    from tapscribe import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "PROMPT_FILE", cfg / "prompt.txt")
    monkeypatch.setattr(config, "HOTWORDS_FILE", cfg / "hotwords.txt")
    monkeypatch.setattr(config, "HALLUCINATIONS_FILE", cfg / "hallucinations.txt")
    return cfg


# ---------------------------------------------------------------------------
# Fake whisperlivekit-server (used by /tap and TapFanOut relay tests)
# ---------------------------------------------------------------------------


class FakeWlkThread:
    """Minimal whisperlivekit-server stand-in. Runs its own event loop in
    a daemon thread so synchronous and asyncio callers alike can drive a
    relay against it.

    Shutdown contract: `stop()` signals an in-loop `asyncio.Event` rather
    than calling `loop.stop()`. The serve coroutine then awaits the event,
    closes the websocket server + open connections, and returns — at which
    point `run_until_complete` returns naturally and the loop drains its
    own pending tasks. Stopping via `loop.stop()` mid-`await` (the prior
    shape) left the websockets server's close path scheduling tasks onto
    an already-stopped loop on Python 3.13, which surfaced as
    PytestUnraisableExceptionWarning noise on every test that touched
    /tap or the relay.

    Lives in conftest.py because both the end-to-end /tap tests and the
    TapFanOut unit tests need to construct a real WlKRelay against
    something that speaks the WlK wire shape."""

    def __init__(self) -> None:
        # `received` is the flat aggregate across all connections — kept
        # so existing single-conn callers (`b"".join(fake_wlk.received)`)
        # keep working unchanged. Per-connection state lives in the
        # parallel lists `connections` / `received_by_connection` /
        # `_lines_acc_by_connection`, indexed in connect order.
        self.received: list[bytes] = []
        self.connections: list = []
        self.received_by_connection: list[list[bytes]] = []
        self._lines_acc_by_connection: list[list[dict]] = []
        self._port = _free_port()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=2.0), "fake WlK didn't start"

    def stop(self) -> None:
        # Idempotent: tests that simulate a WlK restart call stop() to
        # tear down the original fake, then the fixture's teardown
        # calls it again on exit. Without this guard the second call
        # crashes with "Event loop is closed" because serve() already
        # returned and the loop is shut down.
        if not self._thread.is_alive():
            return
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                # Loop closed between the is_alive check and here —
                # extremely narrow race, but the thread is on its way
                # out either way.
                pass
        self._thread.join(timeout=2.0)

    def terminate(self) -> None:
        """Simulate a WhisperLiveKit child crash: yank the server out
        from under any open relay without a graceful drain.

        `stop()` triggers the stop_event and lets the serve coroutine
        close connections cooperatively. `terminate()` short-circuits
        that: it forces sockets shut from the WlK loop's thread before
        signalling stop, so any open relay sees an abrupt close rather
        than a polite goodbye. Crash/restart tests need that shape.
        Idempotent and safe to follow with a teardown `stop()`."""
        if not self._thread.is_alive():
            return
        loop = self._loop

        async def _kill():
            # Force-close every open connection without awaiting their
            # close handshake, then close the listening server.
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    transport = getattr(c, "transport", None)
                    if transport is not None:
                        transport.abort()
                    else:
                        await c.close(code=1011)
            if self._server is not None:
                self._server.close()
            if self._stop_event is not None:
                self._stop_event.set()

        if loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(_kill(), loop)
                with contextlib.suppress(Exception):
                    fut.result(timeout=2.0)
            except RuntimeError:
                # Loop already closed; nothing to do.
                pass
        self._thread.join(timeout=2.0)

    def push_committed(self, text: str, *, connection_index: int | None = None) -> None:
        """Schedule a settled-line broadcast on the WlK loop's thread.

        Mirrors the real WlK wire format: each call appends a new line to
        the cumulative list and broadcasts the FrontData-shaped snapshot.
        See whisperlivekit/timed_objects.py FrontData.to_dict for the
        source of truth on the keys.

        With `connection_index=None` (default) the line is broadcast to
        every connected relay, with each relay's own cumulative line
        list advanced independently — the legacy single-conn behaviour
        is the N=1 case of this. Pass an int to push to a single
        specific connection (in connect order) for tests that need to
        diverge the two relays.

        Waits for the broadcast to complete on the WlK loop so callers can
        assert on relay-side state right after — no implicit sleep needed."""
        if self._loop is None:
            return

        if connection_index is None:
            targets = list(range(len(self.connections)))
        else:
            targets = [connection_index]

        async def _push():
            for i in targets:
                if i < 0 or i >= len(self.connections):
                    continue
                acc = self._lines_acc_by_connection[i]
                idx = len(acc)
                acc.append(
                    {
                        "text": text,
                        "speaker": 1,
                        "start": float(idx),
                        "end": float(idx) + 1.0,
                    }
                )
                msg = json.dumps(
                    {
                        "status": "active_transcription",
                        "lines": list(acc),
                        "buffer_transcription": "",
                        "buffer_diarization": "",
                        "buffer_translation": "",
                        "remaining_time_transcription": 0,
                        "remaining_time_diarization": 0,
                    }
                )
                with contextlib.suppress(Exception):
                    await self.connections[i].send(msg)

        fut = asyncio.run_coroutine_threadsafe(_push(), self._loop)
        with contextlib.suppress(Exception):
            fut.result(timeout=2.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handler, "localhost", self._port)
        self._stop_event = asyncio.Event()
        self._ready.set()
        try:
            await self._stop_event.wait()
        finally:
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    await c.close()
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws) -> None:
        self.connections.append(ws)
        per_conn_received: list[bytes] = []
        self.received_by_connection.append(per_conn_received)
        self._lines_acc_by_connection.append([])
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.received.append(msg)
                    per_conn_received.append(msg)
        finally:
            if ws in self.connections:
                idx = self.connections.index(ws)
                # Keep parallel lists aligned: drop this connection's
                # per-conn state alongside its slot in `connections`.
                self.connections.pop(idx)
                self.received_by_connection.pop(idx)
                self._lines_acc_by_connection.pop(idx)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_wlk() -> Iterator[FakeWlkThread]:
    wlk = FakeWlkThread()
    wlk.start()
    try:
        yield wlk
    finally:
        wlk.stop()


# ---------------------------------------------------------------------------
# Lightweight transcriber stub — shared across route + cache tests
# ---------------------------------------------------------------------------


class TranscriberStub:
    """A minimal Transcriber-protocol stub. Returns one canned segment per
    `transcribe()` call. Parameterise `backend`, `model`, and `text` to
    distinguish multiple stubs in the same test."""

    name = "fake"
    device = "test-device"

    def __init__(
        self,
        *,
        backend: str = "fake-backend",
        model: str = "fake-model",
        text: str | None = None,
    ) -> None:
        self.backend = backend
        self.model_name = model
        self._text = text if text is not None else f"text from {backend} {model}"
        self.calls: list[Path] = []

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
        from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment

        self.calls.append(path)
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text=self._text,
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=self._text),),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={},
        )
