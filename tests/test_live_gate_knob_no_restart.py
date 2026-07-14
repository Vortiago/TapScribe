"""RED contract for #224 — a SpeechGate-knob change must NOT restart the
WhisperLiveKit child.

The four gate knobs (gate_speech_threshold / gate_hangover_ms / gate_pre_roll_ms
/ gate_min_speech_ms) configure the Recorder-side per-tap SpeechGate — NOT the
supervised whisperlivekit child (live.py says so explicitly). Yet they ride
LiveConfig (the child's config) and are compared in `WhisperLiveKitChannel.matches()`,
so `POST /api/live/start` treats a gate-knob nudge as a config change and RESTARTS
the child: a 10-30 s model reload + caption outage + relay reconnect churn — the
most expensive operation in the system, for the cheapest possible tuning action.

#224 relocates the gate knobs so a change applies to the next `/tap` open without
touching the child. `matches()` shrinks back to the child-side config it actually
guards: **model / language / gate_kind / conf**. Note the boundary precisely —
`conf` (confidence_validation) is a CHILD-side setting and STAYS in matches(); only
the four SpeechGate knobs leave. This contract pins that boundary at the route (the
design-agnostic observable), not at matches()'s internal signature (which the fix
changes).

What this file pins (all via the real `POST /api/live/start` restart decision):
  * DISCRIMINATOR (RED at base): a change to ONLY a gate knob (model/language/
    gate_kind/conf all unchanged) against a running child does NOT restart it —
    `stop()`/`start()` are never called. RED at base: matches() sees the knob
    differ, so the route respawns the child.
  * GUARDRAIL — model change still restarts (green->green): the child-side model
    change must still force a restart. Control.
  * GUARDRAIL — conf change still restarts (green->green): `confidence_validation`
    is child-side and STAYS in matches(), so a conf-only change must STILL restart.
    This pins that the relocation is scoped to the four gate knobs and does NOT
    over-broaden matches() into ignoring conf too.

OUT OF THIS GATE (named in the plan-spec, verified by code-review): that the
relocated knob actually TAKES EFFECT on the next tap (the gate factory
`tapscribe.tap_relay.build_gate_for_config` is monkeypatchable, so the reviewer can
confirm the next `_attach` builds a gate with the new value rather than silently
dropping it) — a naive fix that drops the knobs from matches() but never applies
them elsewhere passes the no-restart discriminator while breaking tuning. Also out:
the frontend live-channel.js formValues() comment (which this fix finally makes
true) and the now-superseded matches() unit contracts (tests/test_live_matches_noop.py,
tests/test_live_cmd.py) which pin the OLD "gate knob differs -> restart" and are
updated by the fix, not this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]  # pytest puts tests/ on sys.path
    FakeAliveProc,
    repoint_config_files,
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A tmpdir Recorder with auth + auto-start disabled so the lifespan never
    spawns whisperlivekit-server (mirrors tests/test_routes.py's fixture)."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()
    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


@pytest.fixture
def client(recorder_under_test: Recorder):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _running_child(recorder, monkeypatch) -> dict[str, list]:
    """Mark the live child 'running' and spy on the (subprocess) restart calls.

    Returns a dict recording every start()/stop() invocation so a test can assert
    a restart did or did not happen. start()/stop() are replaced with no-op spies
    so no whisperlivekit-server is ever spawned."""
    recorder.live._proc = FakeAliveProc()
    assert recorder.live.running() is True  # guard: the child really is 'alive'
    calls: dict[str, list] = {"start": [], "stop": []}
    monkeypatch.setattr(recorder.live, "start", lambda **k: (calls["start"].append(k), (True, "started"))[1])
    monkeypatch.setattr(recorder.live, "stop", lambda **k: (calls["stop"].append(k), (True, "stopped"))[1])
    return calls


# (gate-knob JSON field, a value that DIFFERS from the LiveConfig default) — one
# per knob so the discriminator pins EVERY relocated site, not just one (a fix
# that drops one knob from matches() but leaves another must still fail).
_GATE_KNOBS = [
    ("gate_speech_threshold", 0.9),  # default 0.5
    ("gate_hangover_ms", 650),  # default 400
    ("gate_pre_roll_ms", 500),  # default 300
    ("gate_min_speech_ms", 175),  # default 0
]


@pytest.mark.parametrize("knob, changed", _GATE_KNOBS)
def test_gate_knob_only_change_does_not_restart_the_child(
    knob, changed, recorder_under_test, client, monkeypatch
):
    calls = _running_child(recorder_under_test, monkeypatch)
    cfg = recorder_under_test.live.config
    # Every child-side field matches the running config; ONLY this one gate knob changes.
    resp = client.post(
        "/api/live/start",
        json={
            "model": cfg.model,
            "language": cfg.language,
            "gate_kind": cfg.gate_kind,
            "confidence_validation": cfg.confidence_validation,
            knob: changed,  # the sole change
        },
    )
    assert resp.status_code == 200, resp.text
    assert calls["stop"] == [] and calls["start"] == [], (
        f"changing only the SpeechGate knob {knob!r} must NOT respawn the whisperlivekit child "
        "(a 10-30 s caption outage) — the gate is Recorder-side and applies on the next tap"
    )


def test_model_change_still_restarts_the_child(recorder_under_test, client, monkeypatch):
    # Control (green before and after): the model is child-side, so a change to it
    # must still restart. Guards against the fix over-shrinking matches().
    calls = _running_child(recorder_under_test, monkeypatch)
    resp = client.post("/api/live/start", json={"model": "base.en", "language": "en"})
    assert resp.status_code == 200, resp.text
    assert calls["start"], "a model change is child-side and must still restart the whisperlivekit child"


def test_confidence_validation_change_still_restarts_the_child(recorder_under_test, client, monkeypatch):
    # #224 relocates the FOUR gate knobs only. confidence_validation is child-side
    # and STAYS in matches() — a conf-only change must STILL restart. Pins that the
    # relocation is scoped and does not accidentally make conf a no-restart knob too.
    calls = _running_child(recorder_under_test, monkeypatch)
    cfg = recorder_under_test.live.config
    resp = client.post(
        "/api/live/start",
        json={
            "model": cfg.model,
            "language": cfg.language,
            "gate_kind": cfg.gate_kind,
            "confidence_validation": not cfg.confidence_validation,  # the sole change
        },
    )
    assert resp.status_code == 200, resp.text
    assert calls["start"], (
        "confidence_validation is a child-side setting that stays in matches() — "
        "a conf change must still restart the child"
    )
