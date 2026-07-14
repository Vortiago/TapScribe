"""Regression: a gate-knob-only change must apply the new value WITHOUT
flipping the child's state or restarting. Pinned alongside the RED
contract (test_live_gate_knob_no_restart.py) which only checks that
stop()/start() aren't called — this file checks that the knob actually
takes effect: it lands in `config`, mirrors into `info` (/api/state), reaches
the next `/tap`'s gate, and does NOT quantize a sub-precision stored threshold
on an unchanged dashboard re-POST."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]
    GATE_KNOB_TEST_VALUES,
    FakeAliveProc,
    repoint_config_files,
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder
from tapscribe.tap_relay import RelayHandlers, TapRelay

# All four relocated knobs, each with a value that DIFFERS from the LiveConfig
# default (0.5 / 400 / 300 / 0) — so a fix that drops any one from apply can't
# ship green. Shared with the #224 route contract via conftest so the two tables
# can't drift. `info` mirrors the threshold at GATE_THRESHOLD_DECIMALS=2 (kept as
# an explicit literal below, not derived, so it independently pins the render).
CHANGED = GATE_KNOB_TEST_VALUES
EXPECTED_INFO = {
    "gate_speech_threshold": "0.90",
    "gate_hangover_ms": "650",
    "gate_pre_roll_ms": "500",
    "gate_min_speech_ms": "175",
}


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


def _start_body(cfg: LiveConfig, **overrides) -> dict:
    """A /api/live/start body whose child-side fields (model/language/gate_kind/
    conf) match `cfg` — so matches() returns True and the route takes the
    no-restart apply path — plus any gate-knob overrides."""
    return {
        "model": cfg.model,
        "language": cfg.language,
        "gate_kind": cfg.gate_kind,
        "confidence_validation": cfg.confidence_validation,
        **overrides,
    }


def test_gate_knob_apply_updates_all_config_and_info_without_restart(recorder_with_running_child, client):
    """POSTing all four changed gate knobs against a running child must apply
    EVERY one to `config` and mirror EVERY one into `info` (/api/state), while
    the child stays 'running' (no restart). Pins sweep-site (all four, not just
    the threshold) + the info round-trip so a dropped knob or a dropped
    _mirror_gate_info() can't ship green."""
    live = recorder_with_running_child.live
    resp = client.post("/api/live/start", json=_start_body(live.config, **CHANGED))
    assert resp.status_code == 200, resp.text
    assert live.info["state"] == "running"  # never restarted
    for field, value in CHANGED.items():
        assert getattr(live.config, field) == value, field
    for field, rendered in EXPECTED_INFO.items():
        assert live.info[field] == rendered, field


async def test_applied_gate_knobs_reach_the_next_tap_gate(recorder_with_running_child, monkeypatch):
    """Harm layer: after apply_gate_knobs, the NEXT /tap open must build its
    gate from the updated config. Drive the real TapRelay._attach seam (via the
    default `tapscribe.tap_relay.build_gate_for_config`, monkeypatched to
    capture the cfg it receives) and assert every changed knob reaches it — a
    fix that updates matches() but drops the knob before the gate would pass
    the no-restart discriminator yet silently break tuning."""
    live = recorder_with_running_child.live
    live.apply_gate_knobs(**CHANGED)

    captured: dict[str, LiveConfig] = {}

    def _capture(cfg: LiveConfig):
        captured["cfg"] = cfg
        return None  # no real SpeechGate — we only care about the cfg passed

    monkeypatch.setattr("tapscribe.tap_relay.build_gate_for_config", _capture)

    class _FakeRelay:
        async def connect(self) -> bool:
            return True

        async def send(self, data: bytes) -> bool:
            return True

        async def close(self) -> None:
            return None

    async def _noop_metrics(_lag: float) -> None:
        return None

    handlers = RelayHandlers(
        on_settled_line=lambda _s: None,
        on_metrics=_noop_metrics,
        on_buffer=lambda _s: None,
    )
    relay = TapRelay(
        live,
        do_live=True,
        handlers=handlers,
        relay_factory=lambda _cfg, _h: _FakeRelay(),
    )
    await relay.open()

    cfg = captured["cfg"]
    for field, value in CHANGED.items():
        assert getattr(cfg, field) == value, field


def test_gate_knob_apply_preserves_sub_precision_threshold(recorder_with_running_child, client):
    """A >2-decimal stored threshold must survive an unchanged dashboard
    re-POST. The dashboard renders 0.567 as "0.57" and re-POSTs that rounded
    value on every Apply; the no-restart apply path must round-compare at the
    display precision so the stored 0.567 is NOT quantized down to 0.57
    (the #238 display-precision guarantee, on the apply path this slice added).
    """
    live = recorder_with_running_child.live
    live.config = replace(live.config, gate_speech_threshold=0.567)

    resp = client.post(
        "/api/live/start",
        json=_start_body(live.config, gate_speech_threshold=0.57),  # display-rounded re-POST
    )
    assert resp.status_code == 200, resp.text
    assert live.config.gate_speech_threshold == 0.567  # not clobbered to 0.57
