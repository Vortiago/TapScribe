"""Regression: a gate-knob-only change must apply the new value WITHOUT
flipping the child's state or restarting. Pinned alongside the RED
contract (test_live_gate_knob_no_restart.py) which only checks that
stop()/start() aren't called — this file checks that the knob actually
takes effect."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]
    FakeAliveProc,
    repoint_config_files,
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder


@pytest.fixture
def recorder_with_running_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A Recorder with a faked running child and spied start/stop."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()
    rec = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=cfg,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    rec.live._proc = FakeAliveProc()
    assert rec.live.running() is True
    # Seed a realistic running state (the log pump would normally set this).
    rec.live.info["state"] = "running"
    monkeypatch.setattr(
        rec.live,
        "start",
        lambda **k: (True, "started"),
    )
    monkeypatch.setattr(rec.live, "stop", lambda **k: (True, "stopped"))
    return rec


@pytest.fixture
def client(recorder_with_running_child: Recorder):
    app.dependency_overrides[get_recorder] = lambda: recorder_with_running_child
    app.state.recorder = recorder_with_running_child
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_gate_knob_apply_preserves_running_state(recorder_with_running_child, client):
    """A gate-knob-only change must NOT flip info['state'] to 'starting'
    — the child never restarted, so it should stay 'running'."""
    cfg = recorder_with_running_child.live.config
    resp = client.post(
        "/api/live/start",
        json={
            "model": cfg.model,
            "language": cfg.language,
            "gate_kind": cfg.gate_kind,
            "confidence_validation": cfg.confidence_validation,
            "gate_speech_threshold": 0.9,  # changed from default 0.5
        },
    )
    assert resp.status_code == 200, resp.text
    assert recorder_with_running_child.live.info["state"] == "running"
    assert recorder_with_running_child.live.config.gate_speech_threshold == 0.9
