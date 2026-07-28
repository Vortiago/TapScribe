"""Unit tests for `live_control` — the FastAPI-free live-channel reconcile
seam (`plan_live` / `apply_live`). The `/api/live/start` route and the boot
auto-start both drive these; the pure `plan_live` is the primary test
surface here (no subprocess, no engine, no TestClient), and `apply_live`'s
choreography is pinned with a recording fake. The HTTP wiring + status
mapping stays pinned by the /api/live/start tests in test_routes.py.
"""

from __future__ import annotations

import pytest

from tapscribe.live import LiveConfig
from tapscribe.live_control import (
    DesiredLiveState,
    GateKindUnsupported,
    LiveModelUnknown,
    LivePlan,
    LiveReconcileError,
    LiveStartFailed,
    apply_live,
    plan_live,
)
from tapscribe.moonshine_live import MoonshineLiveChannel


def _config(model: str = "small.en", **kw) -> LiveConfig:
    return LiveConfig(model=model, language="en", host="127.0.0.1", port=0, **kw)


class FakeChannel:
    """A minimal `LiveChannel` stand-in that records the lifecycle calls
    `apply_live` makes, so the transition choreography is assertable
    without a real subprocess/engine. NOT a `MoonshineLiveChannel`, so
    `resolve_live_channel_for_model` treats it as the Whisper family."""

    def __init__(
        self,
        *,
        config: LiveConfig | None = None,
        running: bool = False,
        matches: bool = False,
        start_result: tuple[bool, str] = (True, "started"),
        supports_native_vad: bool = True,
    ) -> None:
        self.config = config if config is not None else _config()
        self._running = running
        self._matches = matches
        self._start_result = start_result
        self.supports_native_vad = supports_native_vad
        self.info = {"state": "running" if running else "stopped", "model": self.config.model}
        self.calls: list[str] = []

    def running(self) -> bool:
        return self._running

    def matches(self, **kw) -> bool:
        return self._matches

    def apply_gate_knobs(self, **kw) -> None:
        self.calls.append("apply_gate_knobs")

    def begin_transition(self, **kw) -> None:
        self.calls.append("begin_transition")
        self.info["state"] = "starting"

    def stop(self, **kw) -> tuple[bool, str]:
        self.calls.append("stop")
        self._running = False
        self.info["state"] = "stopped"
        return True, "stopped"

    def start(self, **kw) -> tuple[bool, str]:
        self.calls.append("start")
        ok, msg = self._start_result
        if ok:
            self._running = True
            self.info["state"] = "running"
        return ok, msg


# ---------------------------------------------------------------------------
# plan_live — family swap resolution (incl. #259)
# ---------------------------------------------------------------------------


def test_plan_resolves_family_swap_even_when_model_string_unchanged():
    """#259: a persisted Moonshine default at boot leaves config.model
    unchanged, yet the always-WhisperLiveKit boot channel still needs a
    swap to a Moonshine engine. The swap must be resolved UNCONDITIONALLY,
    not gated on a changed model string — here `desired.model` is None
    (reuse), so no catalog check runs, and the swap must still be found."""
    current = FakeChannel(config=_config(model="moonshine-tiny"))
    plan = plan_live(current, DesiredLiveState(), use_mlx=False)
    assert plan.swap is True
    assert isinstance(plan.target, MoonshineLiveChannel)
    assert plan.no_restart is False  # a swap always restarts


def test_plan_no_swap_when_family_already_matches():
    current = FakeChannel(config=_config(model="small.en"), running=True, matches=True)
    plan = plan_live(current, DesiredLiveState(), use_mlx=False)
    assert plan.swap is False
    assert plan.target is current


# ---------------------------------------------------------------------------
# plan_live — no_restart fast path
# ---------------------------------------------------------------------------


def test_plan_no_restart_when_running_channel_matches():
    current = FakeChannel(running=True, matches=True)
    plan = plan_live(current, DesiredLiveState(gate_hangover_ms=500), use_mlx=False)
    assert plan.no_restart is True


def test_plan_restart_when_running_channel_does_not_match():
    current = FakeChannel(running=True, matches=False)
    plan = plan_live(current, DesiredLiveState(model="small.en"), use_mlx=False)
    assert plan.no_restart is False


# ---------------------------------------------------------------------------
# plan_live — domain validation (pure: raises before any side effect, #334)
# ---------------------------------------------------------------------------


def test_plan_rejects_changed_model_not_in_live_catalog():
    current = FakeChannel(config=_config(model="small.en"))
    with pytest.raises(LiveModelUnknown):
        plan_live(current, DesiredLiveState(model="totally-bogus-xyz"), use_mlx=False)


def test_plan_allows_resending_current_uncataloged_model():
    """Re-sending the current pinned model verbatim is exempt from the
    catalog allowlist — operator state, not new external input."""
    current = FakeChannel(config=_config(model="pinned-wlk-only"), running=True, matches=True)
    plan = plan_live(current, DesiredLiveState(model="pinned-wlk-only"), use_mlx=False)
    assert plan.swap is False  # no raise: the pinned model is allowed


def test_plan_rejects_invalid_gate_kind():
    with pytest.raises(GateKindUnsupported):
        plan_live(FakeChannel(), DesiredLiveState(gate_kind="bogus"), use_mlx=False)


def test_plan_rejects_backend_gate_kind_without_native_vad():
    current = FakeChannel(supports_native_vad=False)
    with pytest.raises(GateKindUnsupported):
        plan_live(current, DesiredLiveState(gate_kind="backend"), use_mlx=False)


def test_reconcile_errors_share_the_registered_base():
    """All three map through `LiveReconcileError` — the base whose
    subclasses `routes.errors.DOMAIN_ERROR_STATUS` registers."""
    for exc in (LiveModelUnknown, GateKindUnsupported, LiveStartFailed):
        assert issubclass(exc, LiveReconcileError)


# ---------------------------------------------------------------------------
# apply_live — no-restart path
# ---------------------------------------------------------------------------


def test_apply_no_restart_applies_gate_knobs_only():
    current = FakeChannel(running=True, matches=True)
    plan = plan_live(current, DesiredLiveState(gate_hangover_ms=500), use_mlx=False)
    installed: list = []
    result = apply_live(current, plan, set_live=installed.append)
    assert result["msg"] == "already running; any gate-knob change applied without restart"
    assert current.calls == ["apply_gate_knobs"]
    assert installed == []  # no swap → nothing installed


# ---------------------------------------------------------------------------
# apply_live — restart choreography (advisor blocker #2 / swap ordering)
# ---------------------------------------------------------------------------


def test_apply_restart_re_announces_after_stop():
    """The running channel is torn down and restarted; begin_transition
    fires BEFORE the stop AND again after it — stop() sets state='stopped',
    so without the re-announce a dashboard polling /api/state would show
    'stopped' through the multi-second reload instead of 'starting'."""
    current = FakeChannel(running=True, matches=False)
    plan = LivePlan(desired=DesiredLiveState(model="small.en"), target=current, swap=False, no_restart=False)
    installed: list = []
    result = apply_live(current, plan, set_live=installed.append)
    assert current.calls == ["begin_transition", "stop", "begin_transition", "start"]
    assert result["state"] == "running"
    assert installed == []


def test_apply_swap_stops_old_installs_target_then_starts_it():
    old = FakeChannel(running=True, matches=False)
    target = FakeChannel(running=False)
    plan = LivePlan(
        desired=DesiredLiveState(model="moonshine-tiny"), target=target, swap=True, no_restart=False
    )
    installed: list = []
    apply_live(old, plan, set_live=installed.append)
    assert old.calls == ["stop"]  # old engine torn down first
    assert installed == [target]  # then the sibling installed
    # target is fresh (not running) → announced + started, no double-stop
    assert target.calls == ["begin_transition", "start"]


def test_apply_raises_live_start_failed_on_start_error():
    current = FakeChannel(running=False, matches=False, start_result=(False, "weights missing"))
    plan = LivePlan(desired=DesiredLiveState(), target=current, swap=False, no_restart=False)
    with pytest.raises(LiveStartFailed, match="weights missing"):
        apply_live(current, plan, set_live=lambda ch: None)
