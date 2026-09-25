"""End the live-caption server when the Recorder that started it is gone.

`LiveChannel` starts `whisperlivekit-server` in a process group of its own, so
`stop()` can signal the whole tree (uvicorn → ASR worker) at once. That same
group is out of the Bundle tray's reach: on macOS the tray reaps its children by
killing ITS process group (ADR-0024), and the server is not in it. A normal stop
is unaffected — `LiveChannel.stop` ends the server itself. A Recorder that dies
HARD (out of memory, a native crash, Force Quit, the tray's watchdog SIGKILLing
the tray's group) used to leave the server running with its model loaded,
re-parented to launchd until logout: one leaked process per crash.

So on POSIX `LiveChannel` starts this watcher as its own child, INSIDE the
server's group (`live.spawn_live_guard`). It polls `getppid()`: the moment that
stops naming the Recorder, the Recorder is gone, and the watcher ends the group
the way `stop()` would — SIGTERM, a grace, then SIGKILL, which takes the watcher
with it. Polling is the one portable way to notice (macOS has no
`PR_SET_PDEATHSIG`), and a re-parented process's `getppid()` names its CURRENT
parent, so a reused pid cannot fool it. Living in the server's group means a
normal `stop()` ends the watcher with the same signal, and a server that exits
on its own is noticed on the next poll.

Stdlib-only and silent. It is started as `python -m tapscribe.live_guard` from
the Recorder's own interpreter, and its argv is two integers: it spawns nothing,
so nothing it is handed can become a command.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections.abc import Sequence

#: How often the watcher looks. A leaked server costs memory, not correctness,
#: so a second's lag is nothing, and each look is two syscalls.
POLL_S: float = 1.0

#: How long the group gets to honour SIGTERM before SIGKILL. `LiveChannel.stop`
#: takes its default from here, so a crash and a normal stop give the same budget.
TERM_GRACE_S: float = 5.0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tapscribe.live_guard")
    parser.add_argument("recorder_pid", type=int)
    parser.add_argument("server_pid", type=int)
    args = parser.parse_args(argv)
    return watch(args.recorder_pid, args.server_pid)


def watch(recorder_pid: int, server_pid: int) -> int:
    """Poll until the Recorder or the server is gone, and end the server's group
    if it was the Recorder."""
    while True:
        if os.getppid() != recorder_pid:
            _end_group(server_pid)
            return 0
        if not _alive(server_pid):
            return 0  # the server ended on its own: nothing is left to guard
        time.sleep(POLL_S)


def _end_group(server_pid: int) -> None:
    group = os.getpgrp()
    # This watcher is in the group it is about to signal, and must outlive the
    # SIGTERM to escalate for a server that ignores it. Ignoring is safe here
    # because nothing is spawned after it: the disposition has no one to leak to.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _signal_group(group, signal.SIGTERM)
    deadline = time.monotonic() + TERM_GRACE_S
    while _alive(server_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    # Unconditional: it also takes an ASR worker that outlived the server, and
    # this watcher, which has nothing left to do.
    _signal_group(group, signal.SIGKILL)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        # No process holds that pid, or one we may not signal does. The server was
        # started by our own Recorder, so the latter is a reused pid: gone either way.
        return False
    return True


def _signal_group(group: int, sig: signal.Signals) -> None:
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        # The group emptied between the last look and this signal: everything
        # that was about to be ended has ended already.
        return


if __name__ == "__main__":
    sys.exit(main())
