"""The live-server guard: a Recorder that dies must not leave the server behind.

`LiveChannel` starts `whisperlivekit-server` in a process group of its own, which
also puts it out of the macOS Bundle tray's reach (the tray reaps by killing ITS
group). `tapscribe.live_guard` sits in the server's group and ends it when the
Recorder is gone. These run real processes: a stand-in "server" that sleeps, and
for the headline case a stand-in Recorder that is SIGKILLed the way an OOM kill
or a Force Quit would end the real one.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from unittest.mock import patch

import pytest

from tapscribe.live import LiveConfig, WhisperLiveKitChannel, build_live_guard_cmd, spawn_live_guard

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]

#: A Recorder in miniature: it starts a server the way `LiveChannel.start` does
#: (its own process group, no stdin), guards it, reports both pids, and waits.
STAND_IN_RECORDER = f"""
import subprocess, sys, time
from tapscribe.live import spawn_live_guard
server = subprocess.Popen({SLEEPER!r}, process_group=0, stdin=subprocess.DEVNULL)
guard = spawn_live_guard(server)
print(server.pid, guard.pid if guard else 0, flush=True)
time.sleep(120)
"""


def _running(pid: int) -> bool:
    """Alive and not a zombie. A process whose parent died is reaped by whoever
    adopts it, and inside a container that can be nobody, so an ENDED process can
    linger as a zombie that `os.kill(pid, 0)` still finds."""
    stat = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _eventually(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def _kill_quietly(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        # Already gone, which is what every test here expects by its end; this
        # is only the cleanup for a test that failed before getting there.
        return


@posix_only
def test_the_server_dies_with_a_recorder_that_is_killed():
    recorder = subprocess.Popen([sys.executable, "-c", STAND_IN_RECORDER], stdout=subprocess.PIPE, text=True)
    server_pid = guard_pid = 0
    try:
        assert recorder.stdout is not None
        server_pid, guard_pid = (int(field) for field in recorder.stdout.readline().split())
        assert guard_pid, "the guard did not start"
        assert _running(server_pid)

        os.kill(recorder.pid, signal.SIGKILL)
        recorder.wait(timeout=10)

        assert _eventually(lambda: not _running(server_pid)), "the server outlived its Recorder"
        assert _eventually(lambda: not _running(guard_pid)), "the guard outlived its work"
    finally:
        _kill_quietly(recorder.pid)
        _kill_quietly(server_pid)
        _kill_quietly(guard_pid)
        if recorder.stdout is not None:
            recorder.stdout.close()
        recorder.wait(timeout=10)


@posix_only
def test_a_normal_stop_ends_the_guard_with_the_server():
    """`LiveChannel.stop` SIGTERMs the server's group. The guard is in it, so it
    goes with the same signal instead of lingering beside nothing."""
    server = subprocess.Popen(SLEEPER, process_group=0, stdin=subprocess.DEVNULL)
    guard = spawn_live_guard(server)
    try:
        assert guard is not None

        os.killpg(server.pid, signal.SIGTERM)

        server.wait(timeout=10)
        assert guard.wait(timeout=10) is not None
    finally:
        _kill_quietly(server.pid)
        _kill_quietly(guard.pid if guard else 0)


@posix_only
def test_the_guard_leaves_when_the_server_exits_on_its_own():
    server = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        process_group=0,
        stdin=subprocess.DEVNULL,
    )
    guard = spawn_live_guard(server)
    try:
        assert guard is not None

        server.wait(timeout=10)

        assert guard.wait(timeout=10) == 0
    finally:
        _kill_quietly(guard.pid if guard else 0)


def test_no_guard_for_a_child_that_is_not_a_real_process():
    """Tests stand lookalikes in for `subprocess.Popen`; their pids name nothing,
    or somebody else's process group, and a guard must never join that."""

    class Lookalike:
        pid = 12345

    assert spawn_live_guard(Lookalike()) is None


def test_the_guard_argv_is_a_module_and_two_integers():
    assert build_live_guard_cmd("/py", 11, 22) == ["/py", "-m", "tapscribe.live_guard", "11", "22"]


@posix_only
def test_the_server_gets_a_process_group_not_a_session():
    """The guard joins the server's group, and a process can only join a group in
    its own session: `start_new_session` would make every guard spawn fail."""
    seen: dict[str, object] = {}

    class _Stdout:
        def __iter__(self):
            return iter(())

    class _Child:
        pid = 0
        stdout = _Stdout()

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return _Child()

    chan = WhisperLiveKitChannel(
        config=LiveConfig(model="tiny.en", language="en", host="127.0.0.1", port=0),
        use_mlx=False,
    )
    with (
        patch.object(WhisperLiveKitChannel, "_find_exe", return_value="/fake/whisperlivekit-server"),
        patch("tapscribe.live.subprocess.Popen", side_effect=fake_popen),
    ):
        ok, _ = chan.start()

    assert ok
    assert seen.get("process_group") == 0
    assert "start_new_session" not in seen
    assert seen.get("stdin") == subprocess.DEVNULL
