"""E2E-specific fixtures.

The `running_recorder` fixture stands up a real uvicorn server pointing
at a per-test Recorder. The `fake_wlk` fixture is inherited from
`tests/conftest.py` and gives us a stand-in whisperlivekit-server so the
relay path is fully exercised.
"""

from __future__ import annotations

import sys as _sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path as _Path

import pytest

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from conftest import (
    FakeWlkThread,  # type: ignore[import-not-found]  # noqa: E402  # NeMo ships an installed `tests` package — explicit sys.path insertion picks up the project's tests/conftest.py
)

from .harness import RecorderServer


@dataclass
class RunningRecorder:
    """Bundle of everything an E2E test usually needs: the running
    server, the Recorder instance it owns, and the fake
    whisperlivekit-server its relay points at."""

    server: RecorderServer
    recorder: Recorder
    fake_wlk: FakeWlkThread

    @property
    def base_url(self) -> str:
        return self.server.base_url

    @property
    def ws_base_url(self) -> str:
        return self.server.ws_base_url


class _FakeAliveProc:
    """LiveChannel.running() returns True iff `_proc.poll() is None`,
    so any object with a poll-returning-None satisfies it."""

    def poll(self):
        return None


@pytest.fixture
def running_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_wlk: FakeWlkThread,
) -> Iterator[RunningRecorder]:
    """Build a Recorder under tmp_path, attach it to the global FastAPI
    `app`, and serve it via real uvicorn on a free port. Auth off so
    bridges don't have to juggle subprotocols (auth has its own tests).
    LiveChannel is marked running via a fake proc so TapFanOut opens a
    relay, but AUTO_START_LIVE is off so the lifespan doesn't try to
    spawn the real whisperlivekit-server."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path / "config")
    # The per-file constants were computed from CONFIG_DIR at import time, so
    # repointing the dir alone leaves them aimed at the REPO's config/ — a
    # test that saves global config through the API would pollute the working
    # tree AND leak state into the next test run. Repoint them all, like the
    # unit-suite fixtures do.
    cfg = tmp_path / "config"
    monkeypatch.setattr(_config, "PROMPT_FILE", cfg / "prompt.txt")
    monkeypatch.setattr(_config, "LIVE_PROMPT_FILE", cfg / "live-prompt.txt")
    monkeypatch.setattr(_config, "LIVE_MODEL_FILE", cfg / "live-model.txt")
    monkeypatch.setattr(_config, "BATCH_MODEL_FILE", cfg / "batch-model.txt")
    monkeypatch.setattr(_config, "SUMMARIZER_CONFIG_FILE", cfg / "summarizer.json")
    monkeypatch.setattr(_config, "HOTWORDS_FILE", cfg / "hotwords.txt")
    monkeypatch.setattr(_config, "HALLUCINATIONS_FILE", cfg / "hallucinations.txt")
    (tmp_path / "config").mkdir()
    (tmp_path / "recordings").mkdir()

    recorder = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=fake_wlk.port),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    recorder.live._proc = _FakeAliveProc()

    app.state.recorder = recorder
    app.dependency_overrides[get_recorder] = lambda: recorder

    server = RecorderServer(app)
    server.start()
    try:
        yield RunningRecorder(server=server, recorder=recorder, fake_wlk=fake_wlk)
    finally:
        server.stop()
        app.dependency_overrides.clear()
        if hasattr(app.state, "recorder"):
            del app.state.recorder
