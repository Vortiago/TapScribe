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
presence of a try/except. Two failure injections at DIFFERENT points in `_open`
(one after full registration, one before the ActiveStream is even registered)
force a GENERAL unwind rather than a relay-specific catch; a guardrail pins that
a normal open+close still registers-then-removes so a degenerate "hide/remove
everything" fix can't pass.

Hermetic: the live leg is a hand-built `_FakeRelay` (the `open()` classmethod
already accepts `tap_relay=`), so no real WhisperLiveKit child or socket is
touched; the Recorder is the shared tmp-rooted fixture with the live channel
stopped.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import (  # type: ignore[import-not-found]  # noqa: E402  # pytest puts tests/ on sys.path so `from conftest import` resolves the project's tests/conftest.py
    build_tap_recorder,
)

from tapscribe.recorder import Recorder
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
