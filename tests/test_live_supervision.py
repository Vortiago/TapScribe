"""Tests for WhisperLiveKitChannel process supervision — spawn,
stop-escalation, exe discovery, and the `_pump_logs` state machine
(issue #235).

`build_live_cmd`'s pure argv construction and the equality-based
`matches()` contract already have dedicated coverage in
`test_live_cmd.py` / `test_live_matches_noop.py`. This file covers the
child-process orchestration those files deliberately don't exercise:

  * `begin_transition`'s knob validation/replacement body
  * `start()`'s exe-missing and spawn-failure error paths
  * `stop()`'s SIGTERM->SIGKILL escalation ladder (both the POSIX
    `killpg` path and the non-POSIX `terminate`/`kill` path)
  * `_find_exe`'s venv-bin fallback
  * the `_pump_logs` loop: "starting"->"running" promotion on the
    uvicorn banner, the accelerator-line device overwrite, crash ->
    state="error" + last_error-tail capture, and the stale-pump
    identity guard

No real whisperlivekit-server (or any other real subprocess) is
spawned — every child is a small fake object standing in for
`subprocess.Popen`'s return value, so these tests run instantly and
need no model download or WhisperLiveKit install.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest

import tapscribe.live as live_mod

LiveConfig = live_mod.LiveConfig
WhisperLiveKitChannel = live_mod.WhisperLiveKitChannel


def _make_channel(*, gate_kind: str = "tapscribe", **overrides) -> WhisperLiveKitChannel:
    """One tiny LiveConfig + channel per test, port=0 (ephemeral) so
    start()-exercising tests don't collide on a fixed port."""
    cfg_kwargs: dict = dict(
        model="tiny.en",
        language="en",
        host="127.0.0.1",
        port=0,
        gate_kind=gate_kind,
    )
    cfg_kwargs.update(overrides)
    cfg = LiveConfig(**cfg_kwargs)
    return WhisperLiveKitChannel(config=cfg, use_mlx=False)


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.005) -> None:
    """Event-driven busy-poll: returns as soon as `predicate()` is
    true, raises if it never is. Used instead of a fixed sleep so
    these tests don't pay (or risk under-paying) a hardcoded delay
    while a background pump thread catches up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


class _QueueStdout:
    """Stand-in for `Popen(text=True).stdout`: an iterator that blocks
    on `push`ed lines and raises StopIteration once `close`d (EOF).
    Lets a test drive `_pump_logs` (running on a background thread)
    line-by-line from the main thread instead of racing a real pipe."""

    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def push(self, line: str) -> None:
        self._q.put(line)

    def close(self) -> None:
        self._q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _FakeChildProc:
    """Minimal Popen lookalike for `_pump_logs` tests: `stdout` is a
    `_QueueStdout` the test drives directly; `wait()` (called from the
    method's `finally`, after the stdout iterator raises
    StopIteration on `close()`) returns the fixed exit code the test
    configured."""

    def __init__(self, *, pid: int = 4242, rc: int = 0) -> None:
        self.pid = pid
        self.stdout = _QueueStdout()
        self._rc = rc

    def poll(self):
        return None  # not consulted by _pump_logs directly

    def wait(self, timeout=None):
        return self._rc


@contextmanager
def _pumping(chan: WhisperLiveKitChannel, proc: _FakeChildProc):
    """Run `chan._pump_logs(proc)` on a background thread for the body
    of the `with`, then EOF the fake stdout and join — the shared
    scaffolding of every _pump_logs test. Post-`with` assertions run
    after the pump has fully exited (its `finally` included)."""
    t = threading.Thread(target=chan._pump_logs, args=(proc,), daemon=True)
    t.start()
    try:
        yield
    finally:
        proc.stdout.close()
        t.join(timeout=2)
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# _pump_logs — starting->running promotion, device overwrite, crash capture
# ---------------------------------------------------------------------------


def test_pump_logs_promotes_starting_to_running_on_uvicorn_banner():
    chan = _make_channel()
    chan.info["state"] = "starting"
    proc = _FakeChildProc(rc=0)
    chan._proc = proc  # mirrors what start() would have set
    with _pumping(chan, proc):
        proc.stdout.push("INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)")
        _wait_until(lambda: chan.info["state"] == "running")
    # rc=0 on exit overwrites the promoted "running" with a graceful stop.
    assert chan.info["state"] == "stopped"


def test_pump_logs_promotes_on_application_startup_complete_line_too():
    """The promotion check ORs two uvicorn phrasings — pin both."""
    chan = _make_channel()
    proc = _FakeChildProc(rc=0)
    chan._proc = proc
    with _pumping(chan, proc):
        proc.stdout.push("INFO:     Application startup complete.")
        _wait_until(lambda: chan.info["state"] == "running")


def test_pump_logs_overwrites_seeded_device_with_child_accelerator_report():
    chan = _make_channel()
    chan.info["device"] = "CPU"  # the parent's seeded prediction
    proc = _FakeChildProc(rc=0)
    chan._proc = proc
    with _pumping(chan, proc):
        proc.stdout.push("  Accelerator: CUDA (NVIDIA A100)")
        _wait_until(lambda: chan.info["device"] == "CUDA (NVIDIA A100)")


def test_pump_logs_sets_error_state_with_last_error_tail_on_nonzero_exit():
    chan = _make_channel()
    proc = _FakeChildProc(rc=1)
    chan._proc = proc
    lines = [
        "booting",
        "ERROR: something broke",
        "Traceback (most recent call last):",
        "  File x, line 1",
        "RuntimeError: boom",
    ]
    with _pumping(chan, proc):
        for line in lines:
            proc.stdout.push(line)
        _wait_until(lambda: len(chan.log) == len(lines))

    assert chan.info["state"] == "error"
    assert chan.info["last_error"] == " | ".join(lines)


def test_pump_logs_falls_back_to_exit_code_message_when_log_is_empty():
    """If the child dies before printing anything (e.g. instant crash
    on import), the tail-join is empty and last_error must still be
    informative rather than a blank string."""
    chan = _make_channel()
    proc = _FakeChildProc(rc=17)
    chan._proc = proc
    with _pumping(chan, proc):
        pass  # push nothing — the child "exits" without a single log line
    assert chan.info["state"] == "error"
    assert chan.info["last_error"] == "exited with code 17"


def test_pump_logs_skips_info_update_when_proc_was_replaced():
    """A stale pump thread whose proc has been superseded by a fresh
    start() must not clobber the newer proc's info — otherwise a fast
    Apply-model click flaps the dashboard's state back to "error"
    behind the new child's back."""
    chan = _make_channel()
    old_proc = _FakeChildProc(pid=1, rc=1)
    new_proc = _FakeChildProc(pid=2, rc=0)
    chan._proc = old_proc
    chan.info["state"] = "running"

    with _pumping(chan, old_proc):
        # Simulate a fresh start() swapping in a new proc while the stale
        # pump is still draining the old child's tail; the EOF at `with`
        # exit is the old child "exiting" with rc=1.
        chan._proc = new_proc

    assert chan.info["state"] == "running"


# ---------------------------------------------------------------------------
# start() wired to a real background pump thread (integration of the two)
# ---------------------------------------------------------------------------


def test_start_wires_a_background_pump_that_promotes_state_to_running():
    chan = _make_channel()
    proc = _FakeChildProc(pid=24680, rc=0)

    with (
        patch.object(WhisperLiveKitChannel, "_find_exe", return_value="/fake/whisperlivekit-server"),
        patch("tapscribe.live.subprocess.Popen", return_value=proc),
    ):
        ok, _msg = chan.start()
    assert ok is True
    assert chan.info["state"] == "starting"

    proc.stdout.push("INFO:     Uvicorn running on http://127.0.0.1:8000")
    _wait_until(lambda: chan.info["state"] == "running")

    proc.stdout.close()
    _wait_until(lambda: chan.info["state"] == "stopped")


# ---------------------------------------------------------------------------
# begin_transition — knob validation + replacement body
# ---------------------------------------------------------------------------


def test_begin_transition_rejects_invalid_gate_kind_and_leaves_state_untouched():
    chan = _make_channel()
    before_config = chan.config
    before_state = chan.info["state"]
    with pytest.raises(ValueError, match="gate_kind must be 'tapscribe' or 'backend'"):
        chan.begin_transition(gate_kind="bogus")
    assert chan.config == before_config
    assert chan.info["state"] == before_state


def test_begin_transition_applies_every_knob_and_flips_to_starting():
    chan = _make_channel()
    chan.begin_transition(
        model="small.en",
        language="no",
        gate_kind="backend",
        conf=False,
        gate_speech_threshold=0.75,
        gate_hangover_ms=999,
        gate_pre_roll_ms=111,
        gate_min_speech_ms=55,
    )
    assert chan.config.gate_kind == "backend"
    assert chan.config.confidence_validation is False
    assert chan.config.gate_speech_threshold == 0.75
    assert chan.config.gate_hangover_ms == 999
    assert chan.config.gate_pre_roll_ms == 111
    assert chan.config.gate_min_speech_ms == 55
    assert chan.info["state"] == "starting"
    assert chan.info["last_error"] == ""
    assert chan.info["model"] == "small.en"
    assert chan.info["language"] == "no"


def test_begin_transition_leaves_unspecified_knobs_at_their_prior_values():
    chan = _make_channel(gate_kind="tapscribe")
    before = chan.config
    chan.begin_transition(gate_speech_threshold=0.9)
    # Every field except the one supplied knob must be untouched —
    # compared against a snapshot, not hardcoded defaults, so this test
    # keeps pinning the "leave unspecified knobs alone" property even if
    # LiveConfig's defaults change.
    assert chan.config == replace(before, gate_speech_threshold=0.9)


def test_begin_transition_clears_a_prior_last_error():
    chan = _make_channel()
    chan.info["last_error"] = "boom from a previous crash"
    chan.begin_transition()
    assert chan.info["last_error"] == ""
    assert chan.info["state"] == "starting"


# ---------------------------------------------------------------------------
# start() — exe-missing and spawn-failure error paths
# ---------------------------------------------------------------------------


def test_start_reports_error_when_exe_not_found():
    chan = _make_channel()
    with (
        patch.object(WhisperLiveKitChannel, "_find_exe", return_value=None),
        patch("tapscribe.live.subprocess.Popen") as popen,
    ):
        ok, msg = chan.start()
    assert ok is False
    assert "not found on PATH" in msg
    assert chan.info["state"] == "error"
    assert chan.info["last_error"] == msg
    assert chan._proc is None
    popen.assert_not_called()


def test_start_reports_error_when_popen_raises():
    chan = _make_channel()
    with (
        patch.object(WhisperLiveKitChannel, "_find_exe", return_value="/fake/whisperlivekit-server"),
        patch("tapscribe.live.subprocess.Popen", side_effect=OSError("no such file")),
    ):
        ok, msg = chan.start()
    assert ok is False
    assert "spawn failed" in msg
    assert "no such file" in msg
    assert chan.info["state"] == "error"
    assert chan.info["last_error"] == msg
    assert chan._proc is None


# ---------------------------------------------------------------------------
# _find_exe — PATH hit, venv-bin fallback, and the "nowhere" case
# ---------------------------------------------------------------------------


def test_find_exe_returns_path_result_when_shutil_which_finds_it(monkeypatch):
    monkeypatch.setattr(live_mod.shutil, "which", lambda name: "/usr/local/bin/whisperlivekit-server")
    assert WhisperLiveKitChannel._find_exe() == "/usr/local/bin/whisperlivekit-server"


def test_find_exe_falls_back_to_venv_bin_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mod.shutil, "which", lambda name: None)
    venv_root = tmp_path / "venv"
    bin_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    exe_name = "whisperlivekit-server.exe" if os.name == "nt" else "whisperlivekit-server"
    exe_path = bin_dir / exe_name
    exe_path.write_text("#!/bin/sh\n")
    monkeypatch.setattr(live_mod.sys, "prefix", str(venv_root))
    assert WhisperLiveKitChannel._find_exe() == str(exe_path)


def test_find_exe_returns_none_when_missing_from_path_and_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(live_mod.sys, "prefix", str(tmp_path / "no-such-venv"))
    assert WhisperLiveKitChannel._find_exe() is None


# ---------------------------------------------------------------------------
# stop() — no-op / already-exited fast paths
# ---------------------------------------------------------------------------


def test_stop_is_a_noop_when_never_started():
    chan = _make_channel()
    ok, msg = chan.stop()
    assert (ok, msg) == (True, "not running")
    assert chan.info["state"] == "stopped"


def test_stop_detects_a_child_that_already_exited():
    chan = _make_channel()

    class _AlreadyDead:
        pid = 999

        def poll(self):
            return 0

    chan._proc = _AlreadyDead()
    ok, msg = chan.stop()
    assert (ok, msg) == (True, "already exited")
    assert chan._proc is None
    assert chan.info["pid"] == ""
    assert chan.info["state"] == "stopped"


# ---------------------------------------------------------------------------
# stop() — SIGTERM -> SIGKILL escalation ladder
# ---------------------------------------------------------------------------


class _FakeStoppableProc:
    """A running child under `stop()`'s control. `wait()` raises
    TimeoutExpired until something (a fake `terminate`/`kill`, or the
    fake `killpg` below) flips `_alive` False — mirroring a real
    process that either exits promptly on SIGTERM or must be SIGKILLed."""

    def __init__(self, *, pid: int, obeys_sigterm: bool) -> None:
        self.pid = pid
        self._alive = True
        self._obeys_sigterm = obeys_sigterm
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd=["fake"], timeout=timeout or 0)
        return 0

    def terminate(self):
        """Non-POSIX path only."""
        self.terminate_calls += 1
        if self._obeys_sigterm:
            self._alive = False

    def kill(self):
        """Non-POSIX path only."""
        self.kill_calls += 1
        self._alive = False


def _install_fake_killpg(monkeypatch, proc: _FakeStoppableProc) -> list[tuple[int, int]]:
    """Route `os.killpg` calls at `proc.pid` back onto the fake, the
    same way test_concurrency_races.py's rapid-restart test does —
    SIGTERM only succeeds if the fake was built with
    obeys_sigterm=True, SIGKILL always succeeds."""
    calls: list[tuple[int, int]] = []

    def _fake_killpg(pid, sig):
        calls.append((pid, sig))
        if pid != proc.pid:
            raise ProcessLookupError(pid)
        if sig == signal.SIGKILL:
            proc.kill()
        elif proc._obeys_sigterm:
            proc._alive = False

    monkeypatch.setattr(live_mod.os, "killpg", _fake_killpg, raising=False)
    return calls


@pytest.mark.skipif(os.name != "posix", reason="killpg escalation is POSIX-only")
def test_stop_escalates_to_sigkill_when_child_ignores_sigterm(monkeypatch):
    chan = _make_channel()
    proc = _FakeStoppableProc(pid=54321, obeys_sigterm=False)
    chan._proc = proc
    calls = _install_fake_killpg(monkeypatch, proc)

    ok, msg = chan.stop(timeout=0.01)

    assert (ok, msg) == (True, "stopped")
    assert proc.kill_calls == 1
    assert calls == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
    assert chan._proc is None
    assert chan.info["state"] == "stopped"
    assert chan.info["pid"] == ""


@pytest.mark.skipif(os.name != "posix", reason="killpg escalation is POSIX-only")
def test_stop_does_not_escalate_when_child_obeys_sigterm(monkeypatch):
    chan = _make_channel()
    proc = _FakeStoppableProc(pid=54322, obeys_sigterm=True)
    chan._proc = proc
    calls = _install_fake_killpg(monkeypatch, proc)

    ok, msg = chan.stop(timeout=1.0)

    assert (ok, msg) == (True, "stopped")
    assert proc.kill_calls == 0
    assert calls == [(proc.pid, signal.SIGTERM)]
    assert chan._proc is None


@pytest.mark.skipif(os.name != "posix", reason="killpg escalation is POSIX-only")
def test_stop_reports_failure_message_when_signal_delivery_raises(monkeypatch):
    chan = _make_channel()
    proc = _FakeStoppableProc(pid=1, obeys_sigterm=True)
    chan._proc = proc

    def _boom(pid, sig):
        raise OSError("weird kernel failure")

    monkeypatch.setattr(live_mod.os, "killpg", _boom, raising=False)

    ok, msg = chan.stop()

    assert ok is False
    assert msg == "stop failed: weird kernel failure"
    # stop() bailed out of the outer try before clearing the handle.
    assert chan._proc is proc


def test_stop_uses_terminate_and_kill_directly_on_non_posix(monkeypatch):
    chan = _make_channel()  # construct BEFORE faking os.name: __init__ must not run under it
    monkeypatch.setattr(live_mod.os, "name", "nt")
    proc = _FakeStoppableProc(pid=1, obeys_sigterm=False)
    chan._proc = proc

    ok, msg = chan.stop(timeout=0.01)

    assert (ok, msg) == (True, "stopped")
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert chan._proc is None


def test_stop_on_non_posix_does_not_escalate_when_child_obeys_terminate(monkeypatch):
    chan = _make_channel()  # construct BEFORE faking os.name: __init__ must not run under it
    monkeypatch.setattr(live_mod.os, "name", "nt")
    proc = _FakeStoppableProc(pid=1, obeys_sigterm=True)
    chan._proc = proc

    ok, msg = chan.stop(timeout=1.0)

    assert (ok, msg) == (True, "stopped")
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 0
    assert chan._proc is None
