"""A process-wide "observed state changed" signal, so tests wait on events, not clocks.

The e2e Recorder runs on uvicorn's event loop in one thread, the fake
whisperlivekit-server on its own loop in another, and the test on pytest's. A
test that needs "every /tap stream has been torn down" used to re-read that state
every 50 ms until a deadline. Now each place where watched state changes calls
`CHANGES.bump()`, and a waiter re-reads its condition only when that happens:

- `build_tap_recorder` (tests/conftest.py) wraps the public mutators of every
  Recorder it builds with `notify_after`, so the streams registry, the utterance
  index, tap settings, jobs, pipeline results, live transcripts and the recording
  toggle all signal after they change.
- `FakeWlkThread` signals when a relay connects, sends a frame, or goes away.

The version counter is what makes this free of lost wakeups. A waiter reads the
version BEFORE it evaluates its condition, then waits for the version to move
past it. A change that lands between the read and the wait has already moved
the version, so the wait returns at once.

A condition that reads state nothing here signals (the DOM, a file, a server not
built by `build_tap_recorder`) must wait on that state's own event instead.
Waiting here on it would only time out.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
from collections.abc import Callable
from typing import Any


class ChangeSignal:
    """A version counter that any thread may bump, and that any event loop may
    await a bump of."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Sync waiters (a test thread driving a TestClient) block on this.
        self._bumped = threading.Condition(self._lock)
        self._version = 0
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def bump(self) -> None:
        with self._lock:
            self._version += 1
            self._bumped.notify_all()
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # That waiter's loop has closed, and the waiter went with it;
                # there is nobody left to wake.
                continue

    async def changed_since(self, version: int) -> None:
        """Return once the version is no longer `version`, at once if it already isn't."""
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            if self._version != version:
                return
            self._waiters.add(waiter)
        try:
            await waiter[1].wait()
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    def wait_until(self, predicate: Callable[[], object], *, timeout: float) -> bool:
        """The blocking twin of the e2e `wait_until`, for sync tests: re-read
        `predicate` each time the signal is bumped, and return whether it became
        true. The predicate runs OUTSIDE the lock, since reading state may itself
        go through the app (a TestClient request) on another thread. `timeout`
        bounds a hang; it is not a polling deadline."""
        deadline = time.monotonic() + timeout
        while True:
            seen = self.version
            if predicate():
                return True
            with self._bumped:
                if not self._bumped.wait_for(
                    lambda seen=seen: self._version != seen, deadline - time.monotonic()
                ):
                    return bool(predicate())


#: The one signal every watched source bumps. Spurious wakeups are harmless (a
#: waiter just re-reads its condition), so one shared signal needs no routing.
CHANGES = ChangeSignal()


def _bump_after(method: Callable[..., Any], signal: ChangeSignal) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await method(*args, **kwargs)
            finally:
                signal.bump()

        return async_wrapper

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        finally:
            signal.bump()

    return wrapper


def notify_after(obj: object, *names: str, signal: ChangeSignal = CHANGES) -> None:
    """Make each named method of THIS instance bump `signal` once it returns.

    It wraps the bound method on the instance and leaves the class alone, so only
    the objects a test built carry the instrumentation."""
    for name in names:
        setattr(obj, name, _bump_after(getattr(obj, name), signal))
