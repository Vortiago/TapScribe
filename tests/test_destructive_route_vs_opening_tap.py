"""RED contract for #408: a threaded destructive route must not destroy the bytes
of a tap that is opening into the same session.

#405 let the destructive preflight read the in-flight tap registry, which NARROWS
this race but cannot close it. `refuse_current_or_busy` is check-then-act, and both
routes below finish inside `asyncio.to_thread(...)`, so the window between the
guard's read and the worker thread's first destructive syscall is unbounded: a tap
that takes its mark one instruction after the read still loses its audio.

WHAT IS PINNED, and why exactly these two routes. The sweep is "guarded by
`refuse_current_or_busy` AND finishing its destruction on a worker thread AND
destroying a tap's ORIGINAL bytes":

  * `DELETE /api/sessions/{s}`        -> to_thread(shutil.rmtree, session_dir)
  * `DELETE /api/sessions/{s}/audio`  -> to_thread(delete_session_audio, session)

Deliberately OUT, so the two above do not read as under-pinning:

  * `POST /api/sessions/{t}/absorb` calls `absorb_session` SYNCHRONOUSLY on the
    event loop, so it cannot interleave with the tap's await-free segment at all —
    the same property #257's argument rests on for `prune_empty_sessions`.
  * `DELETE /api/wav/{s}/{name}` is threaded, but destroys one NAMED pre-existing
    WAV. A tap opening a fresh file is not in its path.
  * `DELETE /api/sessions/{s}/stripped` removes the derived `stripped/` folder and
    deliberately carries no tap guard (see its docstring) — clearing a derived
    artefact while a tap writes originals is legitimate.

HOW THE ASSERTION STAYS IMPLEMENTATION-AGNOSTIC. It does not check the response
status, and it does not check the filesystem after the route returns. It records
whether the tap's freshly-opened WAV was still on disk after the destructive worker
had a real chance to run, WHILE THE TAP WAS STILL OPEN. That is the harm the issue
describes, and it holds for either fix the issue weighs: a lock spanning the tap's
open window (the route blocks and destroys nothing until the tap is done) and an
abort-on-mark re-check inside the worker (the route wakes, sees the mark, walks
away) both leave the bytes intact. Asserting 409, or asserting the directory still
exists once the route has finished, would fail a correct lock-based fix.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from pathlib import Path

import httpx
import pytest
from conftest import build_tap_recorder  # type: ignore[import-not-found]
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe import tap_fan_out, tap_registry
from tapscribe.app import app, get_recorder
from tapscribe.recorder import Recorder, SessionBusy
from tapscribe.routes import sessions as sessions_routes

_SEEDED_WAV = "20260101T000000Z__bob__seed.wav"
# Never seeded, so the tap's own target session is always a legitimate delete
# candidate and the preflight has nothing to refuse on.
_TARGET = "20260102T000000Z-detached"


def session_dir_of(recorder: Recorder) -> Path:
    return recorder.recordings_dir / _TARGET


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """One tmpdir-rooted Recorder shared by the HTTP routes and the tap, so the
    race is between two callers of the same state rather than two fixtures.
    `config.RECORDINGS_DIR` is repointed too — the maintenance helpers walk THAT,
    not the recorder (same reason test_prune_vs_tap_race.py's fixture does it)."""
    rec = build_tap_recorder(tmp_path)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    app.dependency_overrides[get_recorder] = lambda: rec
    app.state.recorder = rec
    yield rec
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is a process-global refcount dict, so a mark leaked by one test
    makes every later test's route answer 409. Clear-only, not asserting: these
    tests drive the real `TapFanOut` open/close pair, but a run that fails
    mid-window legitimately leaves a mark behind and that must not cascade."""
    tap_registry._tap_open_sessions.clear()
    yield
    tap_registry._tap_open_sessions.clear()


class _Race:
    """Bookkeeping for one forced interleaving."""

    def __init__(self) -> None:
        self.worker_entered = threading.Event()  # the destructive worker reached its first syscall
        self.worker_released = threading.Event()  # the tap's WAV is open; let the worker run
        self.wav_survived: bool | None = None  # the contract
        self.dir_survived: bool | None = None
        self.tap_error: Exception | None = None
        # Where the tap ACTUALLY opened. Checked by every harm test: `session` and
        # `session_dir` are independent, so a tap pointed at the wrong directory makes
        # the whole race vacuous while still looking red for the wrong reason.
        self.tap_dir: Path | None = None


async def _race_destructive_route_against_an_opening_tap(
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    *,
    request: str,
    leaf_owner: object,
    leaf_name: str,
) -> _Race:
    """Park `leaf_owner.leaf_name` (the route's destructive worker) one instruction
    before it destroys anything, open a real tap into the same session, and record
    whether the tap's bytes were still there after the worker had its chance."""
    race = _Race()
    real_leaf = getattr(leaf_owner, leaf_name)

    def parked_leaf(*args, **kwargs):
        race.worker_entered.set()
        race.worker_released.wait(20)
        return real_leaf(*args, **kwargs)

    monkeypatch.setattr(leaf_owner, leaf_name, parked_leaf)

    # `open_recorder_wav` is the LAST step of TapFanOut._open's await-free segment —
    # the mark is taken and the directory exists by the time it runs — so this is the
    # worst-case place to hand the destructive worker its turn.
    real_open = tap_fan_out.open_recorder_wav
    session_dir = recorder.recordings_dir / _TARGET

    def open_then_release(fpath):
        handle = real_open(fpath)
        # A fix that blocks the ROUTE before its thread hop never reaches parked_leaf,
        # so this wait is a bound, not a requirement.
        race.worker_entered.wait(5)
        race.worker_released.set()
        time.sleep(0.4)  # a real chance for the worker to do its damage
        race.tap_dir = Path(fpath).parent
        race.wav_survived = Path(fpath).is_file()
        race.dir_survived = session_dir.is_dir()
        return handle

    monkeypatch.setattr(tap_fan_out, "open_recorder_wav", open_then_release)

    seed_session(recorder.recordings_dir, _TARGET, [_SEEDED_WAV])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        route = asyncio.create_task(client.request("DELETE", request))
        await asyncio.sleep(0.2)  # let the route clear its preflight and hop to the thread
        fan = None
        try:
            fan = await tap_fan_out.TapFanOut.open(
                recorder,
                identity="alice",
                name="Alice",
                utterance_id="utt-408",
                do_record=True,
                do_live=False,
                session=_TARGET,
                # BOTH are required: `session` and `session_dir` are independent
                # (tap_fan_out.py:109-110) and the in-flight mark is taken from
                # `session_dir.name`. Passing only `session` leaves the tap writing
                # into the CURRENT session, so the race would be against a directory
                # nothing is opening into — a vacuously green contract.
                session_dir=recorder.recordings_dir / _TARGET,
            )
        # Every failure the race can produce is an Exception (SessionBusy, OSError), and
        # `CancelledError` must reach the event loop rather than be filed as a tap error.
        except Exception as exc:  # noqa: BLE001 — the recorded bytes are the contract
            race.tap_error = exc
        finally:
            race.worker_released.set()
            if fan is not None:
                # TapFanOut is an async context manager; releasing the in-flight mark
                # is __aexit__'s job, and leaving it held would make every later
                # test's route answer 409.
                await fan.__aexit__(None, None, None)
            await route
    return race


def assert_no_audio_was_silently_lost(race: _Race, recorder: Recorder, what: str) -> None:
    """The harm layer, and the ONLY thing these two routes are pinned on.

    Two outcomes are both correct, because the issue weighs mechanisms that differ in
    who yields:
      * the tap OPENS and its bytes survive the worker's window (a lock spanning the
        open window, or an abort-on-mark re-check inside the worker); or
      * the tap is CLEANLY REFUSED before it creates anything (a claim the destructive
        worker takes first, so a new tap into a session being destroyed is turned away).
        Nothing it wrote can be lost, because it wrote nothing.

    What is NOT acceptable is the base behaviour: the tap opens, starts writing, and its
    bytes are destroyed underneath it — a dead WS at best, a detached inode at worst.
    A raw OSError out of the open is the same defect wearing a different hat.

    Asserting `tap_error is None` instead would mandate that the TAP wins, which is a
    mechanism choice the issue explicitly leaves open.
    """
    if race.tap_error is not None:
        assert isinstance(race.tap_error, SessionBusy), (
            f"{what}: the tap failed with {race.tap_error!r} rather than a clean refusal. "
            "A SessionBusy before anything is created is a fine resolution; a filesystem "
            "error out of the open is the race itself."
        )
        return

    assert race.tap_dir == session_dir_of(recorder), (
        f"{what}: the tap opened into {race.tap_dir}, not the session under attack, so this "
        "test would pass or fail for the wrong reason. `session` and `session_dir` are "
        "independent arguments and the mark comes from session_dir.name."
    )
    assert race.wav_survived is not False, (
        f"{what}: the tap opened and its WAV was then destroyed while the tap was still live. "
        "Either keep the bytes or refuse the tap outright; destroying under an open handle is "
        "the defect."
    )


async def test_session_delete_cannot_rmtree_a_session_a_tap_is_opening_into(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """DELETE /api/sessions/{s} hops to `to_thread(shutil.rmtree, ...)`, so it can
    remove the directory out from under a tap that marked itself after the guard
    read. RED at base: the tap's WAV is gone (or its open raised FileNotFoundError)
    while the tap is still live."""
    race = await _race_destructive_route_against_an_opening_tap(
        recorder,
        monkeypatch,
        request=f"/api/sessions/{_TARGET}",
        leaf_owner=shutil,
        leaf_name="rmtree",
    )
    assert_no_audio_was_silently_lost(race, recorder, "session delete")
    if race.tap_error is None:
        assert race.dir_survived is not False, "the session directory was removed under the open tap"


async def test_session_audio_delete_cannot_unlink_the_wav_a_tap_just_opened(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """DELETE /api/sessions/{s}/audio hops to `to_thread(delete_session_audio, ...)`,
    whose walk unlinks every original WAV — including the one the tap created inside
    the window. Same race, different worker, so pinning only the rmtree route would
    ship a partial sweep."""
    race = await _race_destructive_route_against_an_opening_tap(
        recorder,
        monkeypatch,
        request=f"/api/sessions/{_TARGET}/audio",
        leaf_owner=sessions_routes,
        leaf_name="delete_session_audio",
    )
    assert_no_audio_was_silently_lost(race, recorder, "session-audio delete")


async def test_a_mark_taken_before_the_request_still_refuses(recorder: Recorder):
    """Guardrail, green before and after: #405's preflight read must survive. A tap
    that already holds its mark when the request arrives is refused outright, and
    that path must not regress into "the worker sorts it out"."""
    sd = seed_session(recorder.recordings_dir, _TARGET, [_SEEDED_WAV])
    tap_registry.mark_session_in_flight(_TARGET)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.request("DELETE", f"/api/sessions/{_TARGET}")

    assert r.status_code == 409
    assert sd.is_dir()
    assert (sd / _SEEDED_WAV).is_file()


async def test_an_unrelated_session_is_still_deletable_while_a_tap_is_opening(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """Guardrail, green before and after: whatever closes the race must key off the
    session actually being destroyed. A tap opening into one session must not make
    every other session undeletable — the failure mode a coarse global lock would
    introduce."""
    other = "20260103T000000Z-unrelated"
    seed_session(recorder.recordings_dir, other, [_SEEDED_WAV])
    tap_registry.mark_session_in_flight(_TARGET)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.request("DELETE", f"/api/sessions/{other}")

    assert r.status_code == 200, r.text
    assert not (recorder.recordings_dir / other).exists()
