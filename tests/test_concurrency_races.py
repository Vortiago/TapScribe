"""Chaos tests — race conditions and error-recovery paths the audit
flagged as untested.

These tests are heavier than the rest of the suite and exercise
background tasks, repeated mutations, and abnormal-close paths. They
are tagged `@pytest.mark.chaos` so a fast inner CI loop can skip them
with `pytest -m 'not chaos'`. The nightly job runs the whole suite.

They lean on `filterwarnings = ["error"]` in pyproject.toml — any
leaked asyncio task or unawaited coroutine will surface as a hard test
failure rather than a console warning.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tapscribe import hallucinations
from tapscribe import tap_fan_out as tfo
from tapscribe.live import LiveConfig
from tapscribe.recorder import ActiveStream, Recorder
from tapscribe.tap_fan_out import TapFanOut

pytestmark = pytest.mark.chaos


# A 20 ms frame of audible-ish PCM at 16 kHz mono int16 — same shape the
# real Bridge sends (see CONTEXT.md "Bridge" wire contract).
PCM_FRAME = b"\x10\x00" * 320


async def _wait_for(predicate, *, timeout: float = 2.0, interval: float = 0.005) -> None:
    """Wait until `predicate()` returns truthy or `timeout` seconds elapse,
    raising `TimeoutError` on the latter. Tight inner sleep so a quick
    flip resolves without burning real wall time."""

    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_wait(), timeout=timeout)


def _build_recorder(tmp_path: Path, port: int = 9999) -> Recorder:
    recordings = tmp_path / "recordings"
    config_dir = tmp_path / "config"
    recordings.mkdir()
    config_dir.mkdir()
    return Recorder(
        recordings_dir=recordings,
        config_dir=config_dir,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=port),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


def _build_recorder_with_running_live(tmp_path: Path, port: int) -> Recorder:
    r = _build_recorder(tmp_path, port=port)

    class _FakeProc:
        def poll(self):
            return None  # "alive"

    r.live._proc = _FakeProc()
    return r


# ---------------------------------------------------------------------------
# 1. Relay reconnect coalescing under backoff
# ---------------------------------------------------------------------------


async def test_relay_reconnect_attempts_are_bounded_by_real_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """When WlK is unreachable for several seconds and the bridge keeps
    streaming PCM frames, the fan-out must coalesce reconnect attempts
    via `RELAY_RECONNECT_BACKOFF_S` rather than fire one per frame.

    The existing test in test_tap_fan_out.py sets the backoff to 5 s and
    fires a burst inside that window. This one uses a small but realistic
    backoff (0.1 s) over a longer window so we exercise the per-attempt
    timer across multiple backoff slices, not just the "still inside the
    first window" branch.

    Audit gap: without this we only know the backoff guard fires once in
    the simple case; we don't know that repeated `_maybe_schedule` calls
    actually allow a new attempt once the window has elapsed."""
    BACKOFF = 0.1
    WINDOW_S = 0.5

    # Monkeypatch WlKRelay.connect to return False fast. On Windows a
    # bare unused-port `websockets.connect` doesn't return ECONNREFUSED
    # quickly — it sits until `open_timeout` — which would mask the
    # backoff behaviour we want to test (only one attempt fires inside
    # the whole window). Returning False immediately mimics a refused
    # connection without touching production code.
    from tapscribe import live_relay as _lr

    async def _fast_fail_connect(self) -> bool:
        return False

    monkeypatch.setattr(_lr.WlKRelay, "connect", _fast_fail_connect)

    r = _build_recorder_with_running_live(tmp_path, port=_unused_port())

    original_backoff = tfo.RELAY_RECONNECT_BACKOFF_S
    tfo.RELAY_RECONNECT_BACKOFF_S = BACKOFF
    try:
        async with await TapFanOut.open(
            r,
            identity="alice",
            name="Alice",
            utterance_id="utt-bounded-backoff",
            do_record=True,
            do_live=True,
        ) as fan_out:
            # The initial connect (in _open) failed via the patched
            # WlKRelay.connect, so _relay_alive starts False.
            assert fan_out._relay_alive is False

            # Drive write_frame for WINDOW_S real seconds. Each frame
            # checks the backoff and either schedules a reconnect or
            # is rate-limited.
            loop = asyncio.get_event_loop()
            deadline = loop.time() + WINDOW_S
            frames = 0
            while loop.time() < deadline:
                await fan_out.write_frame(PCM_FRAME)
                frames += 1
                # 20 ms frame cadence to match production.
                await asyncio.sleep(0.02)

            # Let any in-flight reconnect task settle so the attempt
            # counter reflects the final state.
            if fan_out._relay_reconnect_task is not None:
                with suppress_all():
                    await fan_out._relay_reconnect_task

            attempts = fan_out._relay_reconnect_attempts

            # Upper bound: window / backoff + 2 (rounding + one
            # in-flight at the boundary). With a 0.1 s backoff over a
            # 0.5 s window we expect ≤ 7 attempts even on a slow runner.
            # CRUCIALLY: it must NOT be one-per-frame.
            max_expected = int(WINDOW_S / BACKOFF) + 2
            assert attempts <= max_expected, (
                f"reconnect attempts {attempts} exceeded backoff bound "
                f"({max_expected}) — got close to one-per-frame instead of "
                f"coalesced. Drove {frames} frames in {WINDOW_S} s."
            )
            # And it must have tried more than once — otherwise the
            # test didn't actually exercise the per-window re-arming.
            assert attempts >= 2, (
                f"only {attempts} reconnect attempts in {WINDOW_S} s — "
                f"backoff {BACKOFF} s should have allowed several. "
                f"Drove {frames} frames."
            )
    finally:
        tfo.RELAY_RECONNECT_BACKOFF_S = original_backoff


def _unused_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


class suppress_all:
    """Bare-bones `contextlib.suppress(Exception)` shim that also covers
    `asyncio.CancelledError` (which is a BaseException on 3.10+ so the
    plain Exception suppress wouldn't catch it). Used when awaiting a
    cancellable background task purely to give it a chance to finish."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, (Exception, asyncio.CancelledError))


# ---------------------------------------------------------------------------
# 2. ActiveStream concurrent mutation
# ---------------------------------------------------------------------------


async def test_active_stream_concurrent_update_and_remove_is_safe(
    tmp_path: Path,
):
    """One coroutine races `update_bytes`/`update_lag` against another
    coroutine calling `remove`. The ActiveStreams API guards with an
    asyncio.Lock; the post-condition is:

      - no exception leaks out of either coroutine,
      - the entry is gone from the registry at the end,
      - no `update_bytes`-after-`remove` re-registers the stream (the
        existing `update_bytes` no-ops on unknown conn_id; we pin that
        behaviour here),
      - no asyncio warnings (`filterwarnings = ["error"]` in pyproject
        catches stray tasks / unawaited coroutines)."""
    r = _build_recorder(tmp_path)

    from datetime import datetime, timezone

    stream = ActiveStream(
        conn_id="conn-1",
        identity="alice",
        name="Alice",
        filename="x.wav",
        started_at=datetime.now(timezone.utc),
    )
    await r.streams.register(stream)

    async def hammer_updates() -> None:
        for i in range(200):
            await r.streams.update_bytes("conn-1", i * 64, level=0.5)
            await r.streams.update_lag("conn-1", float(i) / 100)
            # Brief yield so the remover gets a fair crack at the lock.
            await asyncio.sleep(0)

    async def remover() -> None:
        # Let a few updates land first so the lock is genuinely contested.
        for _ in range(20):
            await asyncio.sleep(0)
        await r.streams.remove("conn-1")
        # Now keep yielding so the updater is still racing post-remove
        # — those calls must no-op rather than re-create the entry.
        for _ in range(20):
            await asyncio.sleep(0)

    # asyncio.gather propagates the first exception; if either coroutine
    # raises, this line surfaces it.
    await asyncio.gather(hammer_updates(), remover())

    # Final state: the stream is gone, and no updates after remove
    # re-registered it.
    final = await r.streams.snapshot()
    assert final == [], f"expected empty registry after concurrent update/remove, got {final}"


# ---------------------------------------------------------------------------
# 4. Live channel rapid restart
# ---------------------------------------------------------------------------


async def test_live_channel_rapid_restart_settles_to_one_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Simulate an operator hammering Apply (restart) 5× in quick
    succession. After the burst the live channel must own EXACTLY ONE
    `_proc` handle, every prior child must have been `.terminate()`-d
    cleanly, and no asyncio warnings should fire.

    We don't actually spawn whisperlivekit-server (heavy, hardware-
    dependent). Instead we mock `LiveChannel._find_exe` to return a
    sentinel path and stub `subprocess.Popen` to a tiny lookalike that
    blocks-on-stdout so the supervisory pump thread doesn't immediately
    reap the child. The orchestration around it (`stop()` → `start()`,
    `info` updates, lock contention) is real."""
    import threading

    import tapscribe.live as live_mod

    r = _build_recorder(tmp_path)

    spawned: list = []
    terminated: list = []

    class _BlockingStdout:
        """Yields nothing until the proc is terminated, then ends the
        iteration so the pump thread's `for line in proc.stdout` exits
        cleanly via StopIteration. Without this, an empty iterable
        would cause the pump thread to immediately call proc.wait() in
        its finally, which marks the proc dead before the test can
        observe its 'running' state for a second start() call."""

        def __init__(self, owner):
            self._owner = owner

        def __iter__(self):
            return self

        def __next__(self):
            # Block until terminate() flips _alive False, then end the
            # iteration. Polled rather than event-based so the test
            # doesn't deadlock if terminate is missed.
            while self._owner._alive:
                self._owner._alive_event.wait(timeout=0.05)
            raise StopIteration

    class _StubProc:
        def __init__(self, *args, **kwargs):
            self.pid = 10000 + len(spawned)
            self._alive = True
            self._alive_event = threading.Event()
            self.stdout = _BlockingStdout(self)
            spawned.append(self)

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False
            self._alive_event.set()
            terminated.append(self)

        def kill(self):
            self._alive = False
            self._alive_event.set()
            terminated.append(self)

        def wait(self, timeout=None):
            self._alive_event.wait(timeout=timeout or 1.0)
            return 0

    monkeypatch.setattr(live_mod.LiveChannel, "_find_exe", staticmethod(lambda: "/fake/wlk"))
    monkeypatch.setattr(live_mod.subprocess, "Popen", _StubProc)

    # On POSIX, LiveChannel.stop() terminates the child via
    # os.killpg(pid, SIGTERM) rather than proc.terminate(). Route that
    # path back into the stub so `terminated` is populated on every
    # platform, not just Windows where the else-branch hits proc.terminate.
    def _fake_killpg(pid, _sig):
        for sp in spawned:
            if sp.pid == pid and sp._alive:
                sp.terminate()
                return
        raise ProcessLookupError(pid)

    monkeypatch.setattr(live_mod.os, "killpg", _fake_killpg, raising=False)

    # 5 back-to-back start-with-different-model calls. Each one is a
    # restart because the model changes, so it exercises the full
    # stop()→start() sequence the API handler runs.
    models = ["tiny.en", "small.en", "tiny.en", "small.en", "tiny.en"]
    for m in models:
        if r.live.running():
            r.live.stop(timeout=1.0)
        ok, _msg = r.live.start(model=m)
        assert ok, f"start({m}) failed"

    # After the burst: exactly one proc owned by LiveChannel, all others
    # terminated. The most recent spawn is the surviving one.
    assert r.live._proc is not None
    assert r.live._proc is spawned[-1], "LiveChannel should hold the latest spawn"
    assert len(spawned) == len(models), (
        f"expected one spawn per start, got {len(spawned)} for {len(models)} calls"
    )
    # Every prior spawn should have been terminated by a stop() call.
    for old in spawned[:-1]:
        assert old in terminated, f"prior spawn pid={old.pid} was not terminated before being replaced"

    # Clean up: stop the surviving proc so the test doesn't leave a
    # "running" LiveChannel behind.
    r.live.stop(timeout=1.0)
    assert r.live._proc is None


# ---------------------------------------------------------------------------
# 5. Malformed hallucinations.txt
# ---------------------------------------------------------------------------


async def test_hallucinations_malformed_input_handling(
    tmp_config_dir: Path,
):
    """One test, two stanzas — both about how the hallucinations parser
    copes with bad operator input.

    Stanza 1: a hand-rolled file with a mix of bad rules
    (invalid regex, ReDoS-shaped regex, oversize regex pattern, blank
    lines, leading comment) plus one good substring rule. The parser
    currently SILENTLY drops bad rules; we pin that here so a future
    refactor that surfaces parse errors (a UX improvement) breaks this
    test loudly. The good `amara.org` rule survives every flavour of
    malformed neighbour.

    Stanza 2: non-UTF-8 bytes in the file. `read_text_file` catches
    UnicodeDecodeError and returns "" (same contract as the OSError
    fallback), so a malformed bytes paste is a "no rules apply" no-op
    rather than a wedged pipeline."""
    # ---- Stanza 1: mixed bad + good rules in valid UTF-8 ----
    (tmp_config_dir / "hallucinations.txt").write_text(
        # blank line + comment first to confirm they're stripped
        "\n# leading comment — must be ignored\n"
        # invalid regex: unclosed group
        "re:[unclosed\n"
        # ReDoS-shaped (rejected by _regex_is_safe before re.compile)
        "re:(a+)+$\n"
        # over the 256-char length cap
        "re:" + ("foo|" * 100) + "\n"
        # whitespace-only line (treated as blank)
        "   \n"
        # the one good rule that should survive
        "amara.org\n",
        encoding="utf-8",
    )
    rules = hallucinations.parse_rules()
    assert [r["raw"] for r in rules] == ["amara.org"], (
        f"expected only the good rule to survive, got {[r['raw'] for r in rules]}"
    )
    assert hallucinations.match("Subtitles by Amara.org", rules) == "amara.org"

    # ---- Stanza 2: non-UTF-8 bytes — the bad file reads as empty
    # (read_text_file catches UnicodeDecodeError), so parse_rules
    # returns no rules rather than raising into every transcribe job. ----
    (tmp_config_dir / "hallucinations.txt").write_bytes(b"valid line\n\xff\xfe\xfd not utf-8\namara.org\n")
    rules = hallucinations.parse_rules()
    assert rules == [], f"expected no rules from a non-UTF-8 file, got {[r['raw'] for r in rules]}"
