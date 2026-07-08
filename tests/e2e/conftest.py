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
from tapscribe.recorder import Recorder

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # explicit sys.path insertion picks up the project's tests/conftest.py
    FakeAliveProc,  # noqa: F401 — re-exported: e2e files import it from .conftest (see test_dashboard_ui)
    FakeWlkThread,
    all_probe_modules,
    build_tap_recorder,
    repoint_config_files,
)

from .harness import RecorderServer


@pytest.fixture(autouse=True)
def _e2e_probes_installed():
    """Present a fully set-up machine to the e2e server: mark every backend
    probe installed (mirrors the unit `_force_all_probes_installed`). Without
    this a backend-less CI box is "first run", so GET / now redirects to /setup
    and the dashboard tests would break. The /setup smoke is robust either way."""
    from tapscribe.transcribers.catalog import set_installed_modules_for_testing

    set_installed_modules_for_testing(all_probe_modules())
    try:
        yield
    finally:
        set_installed_modules_for_testing(None)


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
    # Repoints CONFIG_DIR AND every per-file constant under it — the
    # constants were computed from CONFIG_DIR at import time, so repointing
    # the dir alone leaves them aimed at the REPO's config/ (a test saving
    # global config through the API would pollute the working tree and leak
    # state into the next run).
    repoint_config_files(monkeypatch, tmp_path / "config")

    recorder = build_tap_recorder(tmp_path, port=fake_wlk.port, live_running=True)

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
