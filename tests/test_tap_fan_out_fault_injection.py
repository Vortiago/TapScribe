"""Fault-injection tests for the WAV write/finalize path (#264).

The chaos suite (`test_tap_fan_out_chaos.py`) covers abnormal WS close and
resume-across-rotation; the lifecycle-safety suite (`test_tap_fan_out_lifecycle_safety.py`,
#196) covers a failure during `_open` (WAV open, roster, relay). Neither
injects an `OSError` into the WAV *write* or *finalize* calls themselves
(`wave.Wave_write.writeframes` / `.close`), so whether a disk-full mid-utterance
or at finalize crashes the tap loop, leaks the `UtteranceIndex` entry, or
wedges the `ActiveStreams` row was unpinned.

Seam under test: `TapFanOut` as a whole (the same public boundary the `/tap`
route drives: `write_frame` + the async context manager), with the WAV
writer faked via `tapscribe.tap_fan_out.open_recorder_wav` so no real
disk-full has to be simulated. Asserted at the aggregation layer
(`UtteranceIndex.snapshot()`, `recorder.streams.snapshot()`), matching the
convention `test_tap_fan_out_lifecycle_safety.py` established for #196.

Tagged `@pytest.mark.chaos` like its sibling files."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # pytest puts tests/ on sys.path so `from conftest import` resolves the project's tests/conftest.py
    build_tap_recorder,
)

from tapscribe.recorder import Recorder
from tapscribe.tap_fan_out import TapFanOut

pytestmark = pytest.mark.chaos

PCM_FRAME = b"\x10\x00" * 320  # 20 ms @ 16 kHz mono int16


class _FakeWaveWriter:
    """Stand-in for `wave.Wave_write` covering only what `TapFanOut` touches:
    `writeframes` (called per frame in `write_frame`) and `close` (called once
    in `_close` to finalize/patch the RIFF header). Either can be told to
    raise, injecting a fault at that exact seam without touching a real disk."""

    def __init__(
        self,
        *,
        fail_write_after: int | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._fail_write_after = fail_write_after
        self._close_error = close_error
        self.frames_written = 0
        self.close_called = False

    def writeframes(self, buf: bytes) -> None:  # noqa: ARG002 - fake signature mirrors wave.Wave_write
        if self._fail_write_after is not None and self.frames_written >= self._fail_write_after:
            raise OSError(28, "No space left on device")
        self.frames_written += 1

    def close(self) -> None:
        self.close_called = True
        if self._close_error is not None:
            raise self._close_error


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    return build_tap_recorder(tmp_path)


def _open_records(recorder: Recorder) -> list[str]:
    """utterance_ids whose UtteranceRecord is still marked open=True."""
    return [uid for uid, rec in recorder.utterances.snapshot().items() if rec.open]


# ---------------------------------------------------------------------------
# Finalize-close OSError (disk-full patching the RIFF/data header)
# ---------------------------------------------------------------------------


async def test_finalize_close_oserror_does_not_leak_utterance_or_stream(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """A disk-full while `wave.Wave_write.close()` patches the RIFF/data-size
    header at finalize time (AFTER every `writeframes()` call already
    succeeded) must not abort the rest of `_close`'s teardown. Pins that the
    `UtteranceIndex` entry is released and the `ActiveStreams` row is dropped
    even when finalize itself raises."""
    fake_wf = _FakeWaveWriter(close_error=OSError(28, "No space left on device"))
    monkeypatch.setattr("tapscribe.tap_fan_out.open_recorder_wav", lambda path: fake_wf)  # noqa: ARG005

    fan_out = await TapFanOut.open(
        recorder,
        identity="grace",
        name="Grace",
        utterance_id="utt-finalize-fail",
        do_record=True,
        do_live=False,
    )
    await fan_out.write_frame(PCM_FRAME)

    # __aexit__ must not propagate the finalize OSError: the /tap route has
    # no wrapper around the `async with` exit, so an escaping exception here
    # would surface as an unhandled ASGI error instead of a clean WS close.
    await fan_out.__aexit__(None, None, None)

    assert fake_wf.close_called is True, "the close() seam was never reached; injection is stale"
    assert _open_records(recorder) == [], (
        "an UtteranceRecord was left open=True after a finalize-close OSError: "
        "it can never be released or resumed again"
    )
    assert await recorder.streams.snapshot() == [], (
        "a phantom ActiveStream row survived a finalize-close OSError: the "
        "dashboard would show a live tap for the process lifetime"
    )


async def test_finalize_close_oserror_still_leaves_no_leaked_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Companion to the chaos suite's task-leak check: a finalize-close
    OSError must not strand the relay's background tasks either."""
    from conftest import FakeAliveProc

    r = build_tap_recorder(tmp_path, port=9998, live_running=True)
    fake_wf = _FakeWaveWriter(close_error=OSError(28, "No space left on device"))
    monkeypatch.setattr("tapscribe.tap_fan_out.open_recorder_wav", lambda path: fake_wf)  # noqa: ARG005
    # live_running=True already sets a FakeAliveProc; keep the import so
    # ruff doesn't flag it as unused if the fixture changes shape later.
    assert isinstance(r.live._proc, FakeAliveProc)

    tasks_before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    fan_out = await TapFanOut.open(
        r,
        identity="grace",
        name="Grace",
        utterance_id="utt-finalize-fail-tasks",
        do_record=True,
        do_live=True,
    )
    await fan_out.write_frame(PCM_FRAME)
    await fan_out.__aexit__(None, None, None)

    await asyncio.sleep(0)
    tasks_after = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    pending_leaked = [t for t in (tasks_after - tasks_before) if not t.done()]
    assert pending_leaked == [], (
        f"finalize-close OSError left {len(pending_leaked)} pending task(s): "
        f"{[t.get_name() for t in pending_leaked]}"
    )


# ---------------------------------------------------------------------------
# Mid-tap writeframes() OSError (disk-full while a frame is being written)
# ---------------------------------------------------------------------------


async def _run_tap_until_write_failure(recorder: Recorder, fake_wf: _FakeWaveWriter, *, utterance_id: str):
    """Mirror the `/tap` route's own shape: `write_frame` has no try/except
    of its own, so a mid-stream OSError propagates straight out of it. The
    real route (`routes/tap.py`) catches it with a broad `except Exception`, logs,
    and lets the `async with` body finish normally, so cleanup then runs via
    a CLEAN `__aexit__` (no exc info), not one carrying the OSError. Reproduce
    that exact shape here rather than asserting on `pytest.raises` at the
    `async with` boundary, since that is NOT how the production caller
    observes this failure."""
    fan_out = await TapFanOut.open(
        recorder,
        identity="heidi",
        name="Heidi",
        utterance_id=utterance_id,
        do_record=True,
        do_live=False,
    )
    async with fan_out:
        try:
            while True:
                await fan_out.write_frame(PCM_FRAME)
        except OSError:
            pass  # the route's own broad `except Exception` swallows this identically
    return fan_out


async def test_mid_tap_writeframes_oserror_after_frames_releases_kept_utterance(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """Half A: some frames already succeeded (bytes_received > 0) before a
    disk-full OSError hits `writeframes()` on a later frame. The record must
    still be released with `kept=True` (resumable within the window), and the
    ActiveStream row must be gone: no leak just because the LAST frame of an
    otherwise-successful utterance failed."""
    fake_wf = _FakeWaveWriter(fail_write_after=2)
    monkeypatch.setattr("tapscribe.tap_fan_out.open_recorder_wav", lambda path: fake_wf)  # noqa: ARG005

    await _run_tap_until_write_failure(recorder, fake_wf, utterance_id="utt-write-fail-kept")

    assert fake_wf.frames_written == 2, "expected exactly 2 successful writes before the injected failure"
    rec = recorder.utterances.snapshot()["utt-write-fail-kept"]
    assert rec.open is False, "the record was left open=True after a mid-tap write failure"
    assert rec.bytes_received == len(PCM_FRAME) * 2
    assert await recorder.streams.snapshot() == [], (
        "a phantom ActiveStream row survived a mid-tap writeframes() OSError"
    )


async def test_mid_tap_writeframes_oserror_on_first_frame_drops_empty_utterance(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """Half B: the very FIRST frame fails (bytes_received stays 0). The
    record must be dropped entirely (not left open, not left "closed" for a
    phantom resume), matching the zero-frame branch of the abnormal-close
    chaos test."""
    fake_wf = _FakeWaveWriter(fail_write_after=0)
    monkeypatch.setattr("tapscribe.tap_fan_out.open_recorder_wav", lambda path: fake_wf)  # noqa: ARG005

    await _run_tap_until_write_failure(recorder, fake_wf, utterance_id="utt-write-fail-empty")

    assert fake_wf.frames_written == 0
    assert "utt-write-fail-empty" not in recorder.utterances.snapshot(), (
        "an empty utterance's record survived a first-frame writeframes() OSError"
    )
    assert await recorder.streams.snapshot() == []
