"""Tests for the destruction guard in the destructive-route vs opening-tap race.

This file covers the new destruction-guard primitives added to ``tap_registry``:
``register_tap``, ``unregister_tap``, ``try_claim_destruct``, and
``release_destruct``. It also asserts that the TapFanOut lifecycle correctly
acquires and releases the guard on both happy and failure paths.

WHAT IS PINNED, AND WHAT IS NOT. No implementation details about the reader
counter or lock shapes. Tests assert through observable behaviour — the guard
count, whether ``try_claim_destruct`` returns True/False, whether a tap is
refused during destruction — so a correct implementation passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import build_tap_recorder  # type: ignore[import-not-found]

from tapscribe import tap_fan_out, tap_registry
from tapscribe.recorder import Recorder, SessionBusy
from tapscribe.tap_fan_out import TapFanOut

PCM_FRAME = b"\x10\x00" * 320  # 20 ms @ 16 kHz mono int16

_TAP_TARGET = "20260102T000000Z-destruction"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_tap(
    recorder: Recorder,
    *,
    identity: str = "alice",
    name: str = "Alice",
    utterance_id: str = "utt-destroy",
    do_record: bool = True,
    do_live: bool = False,
    session: str | None = None,
    tap_relay=None,
):
    """`TapFanOut.open` with shared defaults for these tests."""
    params = {
        "identity": identity,
        "name": name,
        "utterance_id": utterance_id,
        "do_record": do_record,
        "do_live": do_live,
        "tap_relay": tap_relay,
    }
    if session is not None:
        params["session"] = session
        params["session_dir"] = recorder.recordings_dir / session
    return await TapFanOut.open(recorder, **params)


def _active_count(recorder: Recorder) -> int | None:
    """Return the destruction-guard count for the target session, or None
    if the key has been popped."""
    return tap_registry._tap_active_count.get(recorder.session_start)


# ---------------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the destruction-guard dicts after every test."""
    tap_registry._tap_active_count.clear()
    tap_registry._tap_active_locks.clear()
    yield
    tap_registry._tap_active_count.clear()
    tap_registry._tap_active_locks.clear()


async def test_guard_released_on_wav_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The destruction guard is released when `open_recorder_wav` raises
    during TapFanOut.open. The guard count should be empty after the
    failure unwinds."""
    rec = build_tap_recorder(tmp_path)
    session = rec.session_start
    session_dir = rec.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)

    # Patch open_recorder_wav to raise, so _close runs during the __aexit__
    # from the failed open.
    def boom(fpath):
        raise OSError("disk full opening the recorder WAV")

    monkeypatch.setattr(tap_fan_out, "open_recorder_wav", boom)

    with pytest.raises(OSError):
        await _open_tap(rec, session=session)

    # After the failure, the guard count for this session should be empty.
    assert session not in tap_registry._tap_active_count, (
        f"guard not released on WAV-open failure: count is {tap_registry._tap_active_count.get(session)}"
    )


async def test_guard_released_on_relay_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The destruction guard is released when the relay's `open()` raises.
    The guard count should be empty after the failure unwinds."""

    class _FakeRelay:
        def __init__(self, *, open_error: BaseException | None = None) -> None:
            self._open_error = open_error
            self.opened = False

        async def open(self) -> None:
            self.opened = True
            if self._open_error is not None:
                raise self._open_error

        async def close(self) -> None:
            return None

        async def feed(self, buf):  # pragma: no cover — not reached in these tests
            raise AssertionError("feed() is not exercised")

    rec = build_tap_recorder(tmp_path)
    session = rec.session_start
    session_dir = rec.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)

    relay = _FakeRelay(open_error=OSError("relay open failed"))

    with pytest.raises(OSError):
        await _open_tap(rec, do_live=True, tap_relay=relay, session=session)

    assert relay.opened is True, "the relay open() seam was never reached — injection is stale"
    assert session not in tap_registry._tap_active_count, (
        f"guard not released on relay-open failure: count is {tap_registry._tap_active_count.get(session)}"
    )


async def test_concurrent_taps_hold_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two concurrent taps into the same session don't deadlock; both
    increment the guard count. The guard count drops as taps close,
    and `try_claim_destruct` returns False while any tap is open."""
    rec = build_tap_recorder(tmp_path)
    session = rec.session_start
    session_dir = rec.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)

    # Use _NoFileWav so the session stays a legitimate prune candidate.
    monkeypatch.setattr(tap_fan_out, "open_recorder_wav", lambda fpath: _NoFileWav())

    async with await _open_tap(rec, session=session, utterance_id="utt-a", identity="alice"):
        assert tap_registry._tap_active_count.get(session) == 1, "first tap should have guard count 1"
        assert tap_registry.try_claim_destruct(session) is False, (
            "try_claim_destruct should be False while a tap is open"
        )

        async with await _open_tap(rec, session=session, utterance_id="utt-b", identity="bob"):
            assert tap_registry._tap_active_count.get(session) == 2, (
                "second tap should increment guard count to 2"
            )
            assert tap_registry.try_claim_destruct(session) is False, (
                "try_claim_destruct should still be False with two taps open"
            )

        # After inner async with ends, one tap is closed.
        assert tap_registry._tap_active_count.get(session) == 1, (
            "after closing one tap, guard count should drop to 1"
        )
        assert tap_registry.try_claim_destruct(session) is False, (
            "try_claim_destruct should still be False with one tap open"
        )

    # After outer async with ends, all taps are closed.
    assert session not in tap_registry._tap_active_count, (
        f"guard count should be empty after all taps close, got {tap_registry._tap_active_count.get(session)}"
    )
    # After all taps close, try_claim_destruct should succeed.
    assert tap_registry.try_claim_destruct(session) is True, (
        "try_claim_destruct should succeed after all taps close"
    )
    # Clean up the claim.
    tap_registry.release_destruct(session)


async def test_tap_refused_during_destruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A tap opening while a destruction is in progress is refused
    with SessionBusy. The guard count should not be incremented."""
    rec = build_tap_recorder(tmp_path)
    session = rec.session_start
    session_dir = rec.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)

    # Stub open_recorder_wav to avoid materialising files.
    monkeypatch.setattr(tap_fan_out, "open_recorder_wav", lambda fpath: _NoFileWav())

    # Claim destruction.
    assert tap_registry.try_claim_destruct(session) is True

    # Attempt to open a tap into the session.
    with pytest.raises(SessionBusy):
        await _open_tap(rec, session=session, utterance_id="utt-destroy", identity="alice")

    # The guard count should still reflect the destruction sentinel, not a tap.
    assert tap_registry._tap_active_count.get(session) == -1, (
        f"expected destruction sentinel (-1), got {tap_registry._tap_active_count.get(session)}"
    )

    # Clean up.
    tap_registry.release_destruct(session)


class _NoFileWav:
    """A `wave` writer stand-in that materialises NO file on disk.

    Used by tests that need the session to stay empty (a legitimate prune
    candidate) while exercising the destruction guard path."""

    def writeframes(self, data: bytes) -> None:
        return None

    def close(self) -> None:
        return None
