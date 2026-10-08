"""The live-server guard: a Recorder that dies must not leave the server behind.

`LiveChannel` starts `whisperlivekit-server` in a process group of its own, which
also puts it out of the macOS Bundle tray's reach (the tray reaps by killing ITS
group). `tapscribe.live_guard` sits in the server's group and ends it when the
Recorder is gone. These run real processes: a stand-in "server" that waits, and
for the headline case a stand-in Recorder that is SIGKILLed the way an OOM kill
or a Force Quit would end the real one. Every wait is on a real event (a process's
exit notice, a pipe turning readable), never a condition polled against a clock.
"""

from __future__ import annotations

import os
import select
import selectors
import signal
import subprocess
import sys

import pytest

from tapscribe.live import build_live_guard_cmd, spawn_live_guard

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

#: Blocks until a signal ends it. No timer: these processes end when a test says so.
PAUSER = [sys.executable, "-c", "import signal; signal.pause()"]

#: A Recorder in miniature: it starts a server the way `LiveChannel.start` does
#: (its own process group, no stdin), guards it, reports both pids, and waits.
STAND_IN_RECORDER = f"""
import signal, subprocess
from tapscribe.live import spawn_live_guard
server = subprocess.Popen({PAUSER!r}, process_group=0, stdin=subprocess.DEVNULL)
guard = spawn_live_guard(server)
print(server.pid, guard.pid if guard else 0, flush=True)
signal.pause()
"""

#: How long one exit may take before the test fails instead of hanging. It bounds a
#: single wait on the kernel's exit notification, not a poll: the guard looks once a
#: second (`live_guard.POLL_S`) and gives a server `TERM_GRACE_S` to honour SIGTERM.
EXIT_BUDGET_S = 15.0


class _ExitWatch:
    """The kernel's own notice that `pid` has exited: a pidfd on Linux, a kqueue
    `NOTE_EXIT` on macOS. Both fire on exit, before anyone reaps the process, so a
    zombie that nobody reaps (a re-parented process inside a container) counts as
    gone. Opened while the process is known to be alive, so a reused pid cannot
    answer for it later."""

    def __init__(self, pid: int) -> None:
        self._fd: int | None = None
        self._kq: select.kqueue | None = None
        if hasattr(os, "pidfd_open"):
            self._fd = os.pidfd_open(pid)
        else:
            self._kq = select.kqueue()
            self._kq.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )
        self._exited = False

    def exited(self, timeout: float = EXIT_BUDGET_S) -> bool:
        """Block until the exit notice arrives, or `timeout` passes without one.
        `timeout=0` asks without waiting."""
        if not self._exited:
            if self._fd is not None:
                ready, _, _ = select.select([self._fd], [], [], timeout)
            else:
                assert self._kq is not None
                ready = self._kq.control(None, 1, timeout)
            self._exited = bool(ready)
        return self._exited

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
        if self._kq is not None:
            self._kq.close()


def _read_pids(recorder: subprocess.Popen[str], timeout: float = EXIT_BUDGET_S) -> tuple[int, int]:
    """The stand-in Recorder's one line, waited for as a readable pipe: a child that
    hangs before printing fails the test instead of hanging the run."""
    assert recorder.stdout is not None
    with selectors.DefaultSelector() as sel:
        sel.register(recorder.stdout, selectors.EVENT_READ)
        assert sel.select(timeout), f"the stand-in Recorder printed nothing in {timeout}s"
    server_pid, guard_pid = (int(field) for field in recorder.stdout.readline().split())
    return server_pid, guard_pid


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
    watches: dict[int, _ExitWatch] = {}
    try:
        server_pid, guard_pid = _read_pids(recorder)
        assert guard_pid, "the guard did not start"
        # Both are the Recorder's children, not ours, so they cannot be waited on;
        # the exit notice is the event that can.
        server = watches[server_pid] = _ExitWatch(server_pid)
        guard = watches[guard_pid] = _ExitWatch(guard_pid)
        assert not server.exited(timeout=0), "the server was gone before its Recorder"

        os.kill(recorder.pid, signal.SIGKILL)
        assert recorder.wait() == -signal.SIGKILL

        assert server.exited(), "the server outlived its Recorder"
        assert guard.exited(), "the guard outlived its work"
    finally:
        # Only what has not exited: a pid already gone may name someone else now.
        for pid, watch in watches.items():
            if not watch.exited(timeout=0):
                _kill_quietly(pid)
            watch.close()
        if recorder.poll() is None:
            _kill_quietly(recorder.pid)
        recorder.wait()
        if recorder.stdout is not None:
            recorder.stdout.close()


@posix_only
def test_a_normal_stop_ends_the_guard_with_the_server():
    """`LiveChannel.stop` SIGTERMs the server's group. The guard is in it, so it
    goes with the same signal instead of lingering beside nothing."""
    server = subprocess.Popen(PAUSER, process_group=0, stdin=subprocess.DEVNULL)
    guard = spawn_live_guard(server)
    assert guard is not None
    server_exit, guard_exit = _ExitWatch(server.pid), _ExitWatch(guard.pid)
    try:
        os.killpg(server.pid, signal.SIGTERM)

        assert server_exit.exited(), "the server ignored its group's SIGTERM"
        assert server.wait() == -signal.SIGTERM
        # Ended BY that signal, not by noticing the server gone on a later poll
        # (which exits 0): the guard is in the group, so the SIGTERM is its end too.
        assert guard_exit.exited(), "the guard outlived its group's SIGTERM"
        assert guard.wait() == -signal.SIGTERM
    finally:
        if not server_exit.exited(timeout=0):
            _kill_quietly(server.pid)
        if not guard_exit.exited(timeout=0):
            _kill_quietly(guard.pid)
        server_exit.close()
        guard_exit.close()


@posix_only
def test_the_guard_leaves_when_the_server_exits_on_its_own():
    # The server exits when the test closes its stdin, not after a fixed sleep, so
    # it is the test and not the clock that decides the guard is already watching.
    server = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        process_group=0,
        stdin=subprocess.PIPE,
    )
    guard = spawn_live_guard(server)
    assert guard is not None
    server_exit, guard_exit = _ExitWatch(server.pid), _ExitWatch(guard.pid)
    try:
        assert server.stdin is not None
        server.stdin.close()
        assert server_exit.exited(), "the server did not exit when its stdin closed"
        # Reaped here, which is what the guard's next look sees as "gone".
        assert server.wait() == 0

        assert guard_exit.exited(), "the guard outlived the server it was guarding"
        assert guard.wait() == 0
    finally:
        if not server_exit.exited(timeout=0):
            _kill_quietly(server.pid)
        if not guard_exit.exited(timeout=0):
            _kill_quietly(guard.pid)
        server_exit.close()
        guard_exit.close()


def test_no_guard_for_a_child_that_is_not_a_real_process():
    """Tests stand lookalikes in for `subprocess.Popen`; their pids name nothing,
    or somebody else's process group, and a guard must never join that."""

    class Lookalike:
        pid = 12345

    assert spawn_live_guard(Lookalike()) is None


def test_the_guard_argv_is_a_module_and_two_integers():
    assert build_live_guard_cmd("/py", 11, 22) == ["/py", "-m", "tapscribe.live_guard", "11", "22"]
