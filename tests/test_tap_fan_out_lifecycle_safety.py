"""RED contract for issue #196 — TapFanOut lifecycle must be
exception/cancellation-safe: a failure in `open()` must unwind ALL partial
state, and `_close` must remove the ActiveStream row even when the awaited
relay teardown is cancelled.

The `/tap` route composes `async with await TapFanOut.open(...)` (app.py) — the
`await` is OUTSIDE the context manager, so if `_open` raises after partial
setup, `__aexit__`/`_close` never runs and the partial state leaks for the whole
process lifetime. `_open` opens the WAV, indexes the `UtteranceRecord`
(open=True), writes the roster occurrence, registers the `ActiveStream` row, and
LAST builds/opens the live relay — so a raise at any of those later points
strands the earlier ones: an `UtteranceRecord` stuck open=True and, once the row
is registered, a phantom ActiveStream that `/api/state` and `/healthz` report as
a live tap forever (no removal path).

Separately, `_close` runs its sync cleanup (WAV finalize + utterance release)
first, then `await tap_relay.close()` (which can block on the relay drain), and
`streams.remove(conn_id)` is the LAST await AFTER it. A `CancelledError`
delivered during the relay teardown (server shutdown; the TestClient does it
routinely on WS exit) skips the removal and strands the row.

These tests pin the OPERATOR-observable harm at the aggregation layer — the
`ActiveStreams` snapshot (the phantom live tap) and the `UtteranceIndex`
snapshot (the record stuck open) — NEVER the proximate `_wf` handle or the
presence of a try/except. Failures are injected at EVERY partial-init seam the
cleanup path branches on — the relay open (after full registration), the roster
(after the WAV+index but before the ActiveStream), and — the subtle ones — the
WAV open itself on BOTH the fresh and the resume paths: `register_new` /
`try_resume` mark the `UtteranceRecord` open=True BEFORE `self._wf`/`self._record`
are assigned, so if `open_recorder_wav` (fresh) or the resume reopen raises, a
release coupled to the WAV-finalize guard would strand the record open=True
forever. These force a GENERAL unwind rather than a relay-specific catch; a
guardrail pins that a normal open+close still registers-then-removes so a
degenerate "hide/remove everything" fix can't pass.

Hermetic: the live leg is a hand-built `_FakeRelay` (the `open()` classmethod
already accepts `tap_relay=`), so no real WhisperLiveKit child or socket is
touched; the Recorder is the shared tmp-rooted fixture with the live channel
stopped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # pytest puts tests/ on sys.path so `from conftest import` resolves the project's tests/conftest.py
    build_tap_recorder,
)

from tapscribe.audio import open_recorder_wav
from tapscribe.recorder import Recorder, UtteranceRecord
from tapscribe.tap_fan_out import TapFanOut


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    """A Recorder with the live channel stopped — the fan-out's relay is
    injected per test, so the real WlK path is never attempted."""
    return build_tap_recorder(tmp_path)


class _FakeRelay:
    """Stand-in for TapRelay covering only what the lifecycle touches:
    `open()` (called last in `_open`) and `close()` (awaited in `_close`). Each
    can be told to raise, to inject a failure at that exact seam."""

    def __init__(
        self,
        *,
        open_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._open_error = open_error
        self._close_error = close_error
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True
        if self._open_error is not None:
            raise self._open_error

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _open_records(recorder: Recorder) -> list[str]:
    """utterance_ids whose UtteranceRecord is still marked open=True."""
    return [uid for uid, rec in recorder.utterances.snapshot().items() if rec.open]


async def test_open_failure_after_registration_unwinds_stream_and_utterance(recorder: Recorder):
    """Harm case — the relay's `open()` (the LAST step of `_open`) raises after
    the WAV is opened, the UtteranceRecord indexed, the roster written, and the
    ActiveStream row registered. `TapFanOut.open` must re-raise AND leave NO
    phantom ActiveStream row and NO utterance stuck open=True.

    Asserted at the aggregation layer (the two snapshots), so any general unwind
    passes without the test knowing whether it's a try/except in `_open`, an
    `__aenter__` restructure, etc."""
    relay = _FakeRelay(open_error=OSError("disk full attaching the live relay"))

    with pytest.raises(OSError):
        async with await TapFanOut.open(
            recorder,
            identity="alice",
            name="Alice",
            utterance_id="utt-leak-late",
            do_record=True,
            do_live=True,
            tap_relay=relay,
        ):
            pass  # pragma: no cover — open() raises before the body runs

    assert relay.opened is True, "the relay open() seam was never reached — injection is stale"
    assert await recorder.streams.snapshot() == [], (
        "a phantom ActiveStream row survived a failed open() — /api/state and "
        "/healthz would report a live tap for the whole process lifetime"
    )
    assert _open_records(recorder) == [], (
        "an UtteranceRecord was left open=True after a failed open() — it never "
        "gets released and its WAV handle is unfinalised until GC"
    )


async def test_open_failure_before_stream_registration_releases_utterance(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """Adversarial companion — force the failure EARLIER, at the roster step,
    which runs after the WAV open + UtteranceRecord index but BEFORE the
    ActiveStream is registered. A relay-specific catch would miss this entirely;
    only a general unwind releases the half-registered utterance. No ActiveStream
    was ever registered here, so the harm is the stranded open utterance."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("read-only fs writing the roster occurrence")

    monkeypatch.setattr("tapscribe.roster.record_occurrence", _boom)
    relay = _FakeRelay()

    with pytest.raises(OSError):
        async with await TapFanOut.open(
            recorder,
            identity="dave",
            name="Dave",
            utterance_id="utt-leak-early",
            do_record=True,
            do_live=True,
            tap_relay=relay,
        ):
            pass  # pragma: no cover — open() raises before the body runs

    assert relay.opened is False, "relay open() should not be reached when the roster step fails first"
    assert _open_records(recorder) == [], (
        "an UtteranceRecord was left open=True after a failure BEFORE the "
        "ActiveStream was registered — the unwind is relay-specific, not general"
    )
    assert await recorder.streams.snapshot() == [], "no stream should exist on this path"


async def test_open_failure_opening_wav_fresh_releases_indexed_utterance(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """Harm case — the WAV open on the FRESH path raises (disk full / bad path).
    `register_new` has already indexed the `UtteranceRecord` open=True BEFORE
    `self._wf`/`self._record` are assigned, so this is the exact #196 window a
    release coupled to the WAV-finalize guard leaves stranded. No ActiveStream is
    registered yet, so the harm is purely the record stuck open=True."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full opening the recorder WAV")

    monkeypatch.setattr("tapscribe.tap_fan_out.open_recorder_wav", _boom)
    relay = _FakeRelay()

    with pytest.raises(OSError):
        async with await TapFanOut.open(
            recorder,
            identity="frank",
            name="Frank",
            utterance_id="utt-wav-fail-fresh",
            do_record=True,
            do_live=True,
            tap_relay=relay,
        ):
            pass  # pragma: no cover — open() raises before the body runs

    assert relay.opened is False, "relay open() should not be reached when the WAV open fails first"
    assert _open_records(recorder) == [], (
        "an UtteranceRecord was left open=True after the fresh WAV open failed — "
        "register_new indexed it open=True before self._wf was set, and _close's "
        "WAV-finalize guard skipped the release"
    )
    assert await recorder.streams.snapshot() == [], "no stream should exist on this path"


async def test_open_failure_reopening_wav_on_resume_releases_indexed_utterance(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """Harm case — the resume path's WAV reopen raises. `try_resume` re-marks the
    existing record open=True (and transfers ownership to this tap) BEFORE
    `self._wf`/`self._record` are assigned, so a raise from the reopen is the same
    strand window as the fresh path — the second #196 failure point named in the
    issue. Seed a resumable record (a real prior WAV + a closed index entry), then
    fail the reopen; the record must be released (not left open=True)."""
    session_dir = recorder.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    prior = session_dir / "prior-erin.wav"
    wf = open_recorder_wav(prior)
    wf.writeframes(b"\x00\x00" * 100)  # non-empty so the record is kept (resumable)
    wf.close()
    seed = UtteranceRecord(
        utterance_id="utt-wav-fail-resume",
        identity="erin",
        name="Erin",
        filename="prior-erin.wav",
        path=prior,
        started_at=datetime.now(UTC),
        owner="seed-owner",
    )
    recorder.utterances.register_new(seed)  # marks open=True
    recorder.utterances.release(
        "utt-wav-fail-resume", owner="seed-owner", bytes_received=200, kept=True
    )  # open=False, within the resume window → resumable

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full reopening the WAV for append")

    monkeypatch.setattr("tapscribe.tap_fan_out.open_recorder_wav", _boom)
    relay = _FakeRelay()

    with pytest.raises(OSError):
        async with await TapFanOut.open(
            recorder,
            identity="erin",
            name="Erin",
            utterance_id="utt-wav-fail-resume",
            do_record=True,
            do_live=True,
            tap_relay=relay,
        ):
            pass  # pragma: no cover — open() raises before the body runs

    assert relay.opened is False, "relay open() should not be reached when the resume reopen fails"
    assert _open_records(recorder) == [], (
        "an UtteranceRecord was left open=True after the resume WAV reopen failed — "
        "try_resume re-marked it open=True before self._wf was set, and _close's "
        "WAV-finalize guard skipped the release"
    )
    assert await recorder.streams.snapshot() == [], "no stream should exist on this path"


async def test_close_removes_stream_even_when_relay_teardown_cancelled(recorder: Recorder):
    """Harm case — a `CancelledError` is delivered during the awaited relay
    teardown in `_close` (server shutdown / TestClient WS exit). The
    ActiveStream removal is the LAST await after it, so today it is skipped and
    the row is stranded. The removal must survive the cancellation (row gone),
    and the CancelledError must still propagate (cancellation is real)."""
    relay = _FakeRelay(close_error=asyncio.CancelledError())
    fan_out = await TapFanOut.open(
        recorder,
        identity="bob",
        name="Bob",
        utterance_id="utt-cancel",
        do_record=True,
        do_live=True,
        tap_relay=relay,
    )

    assert len(await recorder.streams.snapshot()) == 1, "the row should be registered while open"

    with pytest.raises(asyncio.CancelledError):
        await fan_out.__aexit__(None, None, None)

    assert await recorder.streams.snapshot() == [], (
        "the ActiveStream row survived a CancelledError during relay teardown — "
        "the dashboard shows a phantom active tap for the process lifetime"
    )
    assert _open_records(recorder) == [], (
        "the UtteranceRecord was left open=True through a cancelled teardown — "
        "utterances.release must run SYNC, before the awaited relay close, so a "
        "future refactor that moves it after the cancelling await is caught here"
    )


async def test_normal_open_close_registers_then_removes_stream(recorder: Recorder):
    """Guardrail — a clean open+close still registers the row while open and
    removes it on exit (and tears the relay down). Distinguishes a real unwind
    from a degenerate 'never register' or 'always remove everything' fix that
    would pass the harm cases while breaking live-tap visibility."""
    relay = _FakeRelay()

    async with await TapFanOut.open(
        recorder,
        identity="carol",
        name="Carol",
        utterance_id="utt-ok",
        do_record=True,
        do_live=True,
        tap_relay=relay,
    ):
        assert len(await recorder.streams.snapshot()) == 1, "the row should be live inside the context"

    assert await recorder.streams.snapshot() == [], "the row must be removed on a clean exit"
    assert relay.closed is True, "the relay must be torn down on a clean exit"


async def test_open_failure_at_wav_open_releases_the_indexed_utterance(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """Adversarial (the register_new -> _wf window, #196's headline case): the WAV
    open itself raises AFTER `register_new` has indexed the UtteranceRecord
    (open=True) but BEFORE `self._wf`/`self._record` are assigned — i.e. a
    disk-full fresh `open_recorder_wav` (the resume branch's `wave.open` reopen is
    the same window). `_close`'s WAV-finalize is guarded on `self._wf is not None`,
    so releasing the utterance must NOT be coupled to that guard — otherwise the
    indexed record is stranded open=True for the whole process lifetime, exactly
    the leak #196 is about. Asserted at the aggregation layer (`_open_records`)."""

    def _boom(path):
        raise OSError("disk full opening the recorder WAV")

    monkeypatch.setattr("tapscribe.tap_fan_out.open_recorder_wav", _boom)
    relay = _FakeRelay()

    with pytest.raises(OSError):
        async with await TapFanOut.open(
            recorder,
            identity="erin",
            name="Erin",
            utterance_id="utt-wav-open-fail",
            do_record=True,
            do_live=True,
            tap_relay=relay,
        ):
            pass  # pragma: no cover — open() raises before the body runs

    assert _open_records(recorder) == [], (
        "an indexed UtteranceRecord was stranded open=True after the WAV open "
        "raised — releasing the utterance must not be coupled to the _wf guard"
    )
    assert await recorder.streams.snapshot() == [], "no stream is registered before the WAV opens"
    assert relay.opened is False, "the relay open() seam is reached only after the WAV opens"
