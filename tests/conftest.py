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

    Lives in conftest.py because both the end-to-end /tap tests and the
    TapFanOut unit tests need to construct a real WlKRelay against
    something that speaks the WlK wire shape."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.connections: list = []
        self._port = _free_port()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._lines_acc: list[dict] = []

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=2.0), "fake WlK didn't start"

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)

    def push_committed(self, text: str) -> None:
        """Schedule a settled-line broadcast on the WlK loop's thread.

        Mirrors the real WlK wire format: each call appends a new line to
        the cumulative list and broadcasts the FrontData-shaped snapshot.
        See whisperlivekit/timed_objects.py FrontData.to_dict for the
        source of truth on the keys."""
        if self._loop is None:
            return
        idx = len(self._lines_acc)
        self._lines_acc.append({
            "text": text, "speaker": 1,
            "start": float(idx), "end": float(idx) + 1.0,
        })
        msg = json.dumps({
            "status": "active_transcription",
            "lines": list(self._lines_acc),
            "buffer_transcription": "",
            "buffer_diarization": "",
            "buffer_translation": "",
            "remaining_time_transcription": 0,
            "remaining_time_diarization": 0,
        })

        async def _push():
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    await c.send(msg)

        asyncio.run_coroutine_threadsafe(_push(), self._loop)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handler, "localhost", self._port)
        self._ready.set()
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
        finally:
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    await c.close()
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws) -> None:
        self.connections.append(ws)
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.received.append(msg)
        finally:
            if ws in self.connections:
                self.connections.remove(ws)


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
