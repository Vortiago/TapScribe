"""Contract for issue #238 — WhisperLiveKitChannel.matches() must treat a
supplied gate/confidence value that EQUALS the running child's config as
"already satisfied" (no restart), and force a restart only when a supplied
value DIFFERS.

Today matches() forces a restart whenever conf or any gate_* knob is non-None
(it requires `is None`), so the dashboard — which pre-fills every gate input
with the current value and always POSTs concrete numbers (live-channel.js
formValues) — can never reach api_live_start's "already running with
requested config -> no-op" branch (app.py). Every start/apply click respawns
the WhisperLiveKit child (10-30 s caption outage + reconnect churn) even when
nothing changed. The equality comparison already exists for model / language
/ gate_kind; the fix extends it to conf + the four gate_* numerics, which also
makes the route docstring and the client comment true.

RED drivers (matches() returns False on origin/main today):
  every "<knob> equal to config -> matches() True" case, including the
  all-knobs-equal dashboard scenario.
Green controls (must STAY green after the fix):
  every "<knob> differs -> False", not-running -> False, and model /
  language / gate_kind differ -> False (the pre-existing equality checks
  must not be loosened).

This file is the pinned contract — do NOT weaken it. (The corrected
semantics also make the deliberately-opt-in assertion + comment in
tests/test_live_cmd.py obsolete; that stale assertion is updated by the fix,
not this file.)
"""

from __future__ import annotations

import pytest

from tapscribe.live import LiveConfig, WhisperLiveKitChannel


class _Alive:
    """Stand-in for a live child whose process is still running. matches()
    gates on self.running(), which polls self._proc."""

    def poll(self) -> None:
        return None


# A fully-specified config: every dashboard-tunable knob carries a concrete,
# NON-default value, so "supply the same value" is a real equality test rather
# than accidentally matching a field default.
CFG = LiveConfig(
    model="tiny.en",
    language="en",
    host="localhost",
    port=8000,
    gate_kind="tapscribe",
    gate_speech_threshold=0.6,
    gate_hangover_ms=450,
    gate_pre_roll_ms=250,
    gate_min_speech_ms=80,
    confidence_validation=True,
)

# The "no override supplied" base: matches() then reduces to "is a child
# running with this model/language?" — the untouched-request case.
BASE = dict(model=None, language=None, gate_kind=None, conf=None)

# (matches() kwarg, value EQUAL to CFG, value that DIFFERS from CFG).
# `conf` maps to config.confidence_validation; the four gate_* map 1:1.
KNOBS = [
    ("conf", True, False),
    ("gate_speech_threshold", 0.6, 0.9),
    ("gate_hangover_ms", 450, 600),
    ("gate_pre_roll_ms", 250, 500),
    ("gate_min_speech_ms", 80, 200),
]


def _running_channel(cfg: LiveConfig = CFG) -> WhisperLiveKitChannel:
    chan = WhisperLiveKitChannel(config=cfg, use_mlx=False)
    chan._proc = _Alive()
    assert chan.running() is True  # fixture guard: the child really is "running"
    return chan


@pytest.mark.parametrize("knob, equal, _differ", KNOBS)
def test_supplied_value_equal_to_config_is_a_noop(knob, equal, _differ):
    """A supplied knob whose value EQUALS the running child's config must not
    force a restart (matches() True). RED today: matches() requires the knob
    to be None, so any supplied value — even an equal one — returns False."""
    chan = _running_channel()
    # Merge into one kwargs dict (the knob overrides BASE's value) so
    # supplying `conf` doesn't collide with BASE's conf=None.
    assert chan.matches(**{**BASE, knob: equal}) is True


@pytest.mark.parametrize("knob, _equal, differ", KNOBS)
def test_supplied_value_differing_from_config_forces_restart(knob, _equal, differ):
    """A supplied knob whose value DIFFERS from config forces a restart
    (matches() False). Control — green today and after the fix."""
    chan = _running_channel()
    assert chan.matches(**{**BASE, knob: differ}) is False


def test_all_knobs_supplied_equal_is_the_dashboard_noop():
    """The real dashboard request: every gate input is pre-filled with the
    current value, so formValues() POSTs all of them as concrete numbers equal
    to config. matches() must return True so api_live_start no-ops instead of
    respawning the WhisperLiveKit child. RED today."""
    chan = _running_channel()
    assert (
        chan.matches(
            model="tiny.en",
            language="en",
            gate_kind="tapscribe",
            conf=True,
            gate_speech_threshold=0.6,
            gate_hangover_ms=450,
            gate_pre_roll_ms=250,
            gate_min_speech_ms=80,
        )
        is True
    )


def test_untouched_request_matches_running_child():
    """No overrides supplied + same model/language → True. Control (green
    today): the all-None reduction must not regress."""
    chan = _running_channel()
    assert chan.matches(**BASE) is True


def test_not_running_never_matches():
    """No child running → always False, even when every supplied value equals
    config. Control: the running() guard is unconditional."""
    chan = WhisperLiveKitChannel(config=CFG, use_mlx=False)
    assert chan.running() is False
    assert (
        chan.matches(
            model="tiny.en",
            language="en",
            gate_kind="tapscribe",
            conf=True,
            gate_speech_threshold=0.6,
            gate_hangover_ms=450,
            gate_pre_roll_ms=250,
            gate_min_speech_ms=80,
        )
        is False
    )


@pytest.mark.parametrize(
    "field, value",
    [("model", "small.en"), ("language", "no"), ("gate_kind", "backend")],
)
def test_core_identity_change_still_forces_restart(field, value):
    """A differing model / language / gate_kind still forces a restart.
    Control — these equality checks already existed; guard against the fix
    accidentally loosening them while adding the gate/conf comparisons."""
    chan = _running_channel()
    assert chan.matches(**{**BASE, field: value}) is False
