"""RED contract for #257 — the prune-vs-tap invariant becomes STRUCTURAL.

`_rotate_and_prune` (routes/tap.py) documents its own correctness argument in a
comment: prune runs synchronously on the event loop AND `TapFanOut._open` is
await-free between `mkdir` and the WAV open — "keep it synchronous ... or that race
reopens". The second half of that invariant lives in a different module with nothing
at the site naming it, and no test pins either half. A future edit that inserts an
await in that segment (offloading the WAV open to a thread, awaiting the roster
write) silently reopens a delete-during-write race.

THE WINDOW, precisely: `_open` does `mkdir` → `utterances.register_new` →
`open_recorder_wav`. Between the mkdir and the WAV open the session directory EXISTS
and is EMPTY, so `session_is_empty` is True and `prune_empty_sessions` will `rmtree`
it — while the tap is about to write into it. Verified against base: driving a prune
into that window makes the WAV open die with

    FileNotFoundError: .../recordings/<session>/<session>_Alice_alice_<uuid>.wav

i.e. the tap's audio is lost and the /tap connection dies mid-meeting.

WHY THE ISSUE'S OWN SUGGESTED FIX IS NOT WHAT THIS PINS. #257 proposes "skip pruning
any session dir matching an ActiveStream's session". That guard is VACUOUS:
`recorder.streams.register(...)` runs ~40 lines AFTER `open_recorder_wav`, so by the
time an ActiveStream exists a `*.wav` exists too, `session_is_empty` is already False,
and prune skips the directory anyway. {sessions with an ActiveStream} ⊆ {non-empty}.
Such a guard would satisfy a naive "register a stream, run prune, assert the dir
survives" test while leaving the real window exactly as open as before. (The issue
text calls the pre-WAV step `register_new`, but that is
`utterances.register_new` — the UtteranceIndex — not `streams.register`.)

WHAT MUST ACTUALLY CHANGE: the session must be marked in-flight BEFORE the `mkdir`,
and `prune_empty_sessions` must honour that mark. Only a mark taken before the
directory exists covers the window that races. This is deliberately the hot-path
restructure the issue lists third; the two cheaper options do not close the race.

The mark's shape is NOT pinned here — no registry name, no signature. Every test
below asserts through observable behaviour (does the directory survive / is it
prunable afterwards), so any sound implementation passes.

THE LEAK IS THE DANGER. A mark that is taken and never released makes that session
directory permanently un-prunable — silently worse than the race it fixes. `_open`
has several partial-init exits (WAV open raises, roster raises, relay open raises,
the `__probe__` identity skips the WAV entirely). Per the #196 lesson every one is
pinned, and each is asserted at the harm layer: after the failure, a later prune of
that now-empty directory must SUCCEED.

Out of scope, stated rather than silently skipped: the `try_resume` reconnect path
takes no mkdir and appends to an EXISTING WAV, so its directory is never empty and
prune already skips it — there is no window there to guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import build_tap_recorder  # type: ignore[import-not-found]  # tests/ on sys.path

from tapscribe import roster, tap_fan_out
from tapscribe.recorder import Recorder
from tapscribe.routes.tap import _rotate_and_prune
from tapscribe.session_maintenance import prune_empty_sessions
from tapscribe.tap_fan_out import PROBE_IDENTITY, TapFanOut

PCM_FRAME = b"\x10\x00" * 320  # 20 ms @ 16 kHz mono int16

# A current-session name that is never seeded, so the tap's own session is always a
# prune CANDIDATE. `rotate_session` is idempotent while the current session is
# untouched, so rotating would leave the empty session still current — and still
# skipped — which would make every leak pin below vacuously green.
_OTHER_CURRENT = "current-session-not-seeded"


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A tmpdir-rooted Recorder with `config.RECORDINGS_DIR` repointed too —
    `build_tap_recorder` alone leaves the global pointing at the repo, and the
    maintenance functions walk THAT, not the recorder."""
    rec = build_tap_recorder(tmp_path)
    from tapscribe import config

    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "recordings")
    return rec


class _FakeRelay:
    """Stand-in for TapRelay covering only what `_open`/`_close` touch, so the
    relay step can be made to raise at its exact seam."""

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
        raise AssertionError("feed() is not exercised by the prune-race contract")


def _sessions_on_disk(recorder: Recorder) -> list[str]:
    from tapscribe import config

    return sorted(p.name for p in config.RECORDINGS_DIR.glob("*") if p.is_dir())


def _pruned(result: dict) -> list[str]:
    """The session names actually deleted. `_rotate_and_prune` NESTS
    `prune_empty_sessions`' whole dict under its own "pruned" key, while the bare
    function returns the list flat — accept either so a caller swap doesn't turn
    these assertions vacuous (`name in {...}` silently tests dict KEYS)."""
    inner = result["pruned"]
    return inner["pruned"] if isinstance(inner, dict) else inner


async def _open_tap(recorder: Recorder, **kw):
    """`TapFanOut.open` with the defaults these tests share."""
    params = {
        "identity": "alice",
        "name": "Alice",
        "utterance_id": "utt-race",
        "do_record": True,
        "do_live": False,
        "tap_relay": None,
    }
    params.update(kw)
    return await TapFanOut.open(recorder, **params)


# ---------------------------------------------------------------------------
# THE HARM — a prune driven into the mkdir → WAV-open window.
#
# Both prune CALLERS are pinned. `prune_empty_sessions` is recorder-free by design,
# so the in-flight mark has to reach it from two independent call sites; guarding
# only the rotate-then-prune path ships a partial sweep.
# ---------------------------------------------------------------------------


async def test_rotate_and_prune_during_the_window_spares_the_in_flight_session(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """`POST /api/new-session` (rotate, then prune) landing between the mkdir and
    the WAV open must not delete the directory the tap is opening into.

    RED at base with `FileNotFoundError` from the WAV open — the directory is gone
    by the time the tap writes to it."""
    real_open = tap_fan_out.open_recorder_wav
    fired: dict[str, object] = {}

    def prune_then_open(fpath):
        # Inside the window: mkdir has run, the WAV does not exist yet.
        session_dir = Path(fpath).parent
        fired["dir"] = session_dir.name
        fired["result"] = _rotate_and_prune(recorder)
        fired["survived"] = session_dir.exists()
        return real_open(fpath)

    monkeypatch.setattr(tap_fan_out, "open_recorder_wav", prune_then_open)

    async with await _open_tap(recorder) as fan:
        await fan.write_frame(PCM_FRAME)

    assert fired.get("dir"), "the WAV-open seam was never reached — the injection is stale"
    assert fired["survived"] is True, (
        "the in-flight session directory was pruned between mkdir and the WAV open — "
        "the tap's audio is lost and /tap dies mid-meeting"
    )
    assert fired["dir"] not in _pruned(fired["result"]), (
        "prune reported deleting the session a tap was opening into"
    )


async def test_prune_empty_endpoint_during_the_window_spares_the_in_flight_session(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """The SECOND caller: `POST /api/sessions/prune-empty` calls
    `prune_empty_sessions` directly, with no rotate of its own. The operator can hit
    it at any moment, including while a tap that captured the previous session at WS
    open is still materialising its folder."""
    real_open = tap_fan_out.open_recorder_wav
    fired: dict[str, object] = {}

    def prune_then_open(fpath):
        session_dir = Path(fpath).parent
        # Rotate so the tap's captured directory is no longer the CURRENT session
        # (the current-session skip would otherwise mask the missing guard), then
        # run the bare prune the endpoint runs.
        recorder.rotate_session()
        fired["dir"] = session_dir.name
        fired["result"] = prune_empty_sessions(recorder.session_start)
        fired["survived"] = session_dir.exists()
        return real_open(fpath)

    monkeypatch.setattr(tap_fan_out, "open_recorder_wav", prune_then_open)

    async with await _open_tap(recorder) as fan:
        await fan.write_frame(PCM_FRAME)

    assert fired.get("dir"), "the WAV-open seam was never reached — the injection is stale"
    assert fired["survived"] is True, "prune-empty deleted the directory an in-flight tap was opening into"


async def test_the_mark_is_taken_before_the_directory_exists(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
):
    """ANTI-VACUITY PIN — the earliest point in the window, not just the latest.

    `utterances.register_new` runs immediately after the mkdir, well before the WAV
    open. A guard installed late (anywhere at or after the WAV open, and in
    particular the ActiveStream registration the issue proposes) leaves this earlier
    slice of the window open while the previous two tests still pass. Only a mark
    taken BEFORE the mkdir survives an injection here."""
    real_register = recorder.utterances.register_new
    fired: dict[str, object] = {}

    def prune_then_register(record):
        session_dir = Path(record.path).parent if getattr(record, "path", None) else None
        fired["result"] = _rotate_and_prune(recorder)
        if session_dir is not None:
            fired["dir"] = session_dir.name
            fired["survived"] = session_dir.exists()
        return real_register(record)

    monkeypatch.setattr(recorder.utterances, "register_new", prune_then_register)

    async with await _open_tap(recorder) as fan:
        await fan.write_frame(PCM_FRAME)

    assert "survived" in fired, "the register_new seam was never reached — the injection is stale"
    assert fired["survived"] is True, (
        "a prune immediately after the mkdir still deleted the session directory — "
        "the in-flight mark must be taken BEFORE the mkdir to cover the whole window"
    )


# ---------------------------------------------------------------------------
# GUARDRAILS — the fix must not become "stop pruning".
# ---------------------------------------------------------------------------


async def test_an_idle_empty_session_is_still_pruned(recorder: Recorder):
    """The whole point of prune still works: an empty, non-current session with no
    tap in flight is deleted."""
    stale = recorder.session_dir
    stale.mkdir(parents=True, exist_ok=True)
    result = _rotate_and_prune(recorder)
    assert stale.name in _pruned(result), "an idle empty session must still be pruned"
    assert not stale.exists()


async def test_the_current_session_is_still_never_pruned(recorder: Recorder):
    current = recorder.session_start
    recorder.session_dir.mkdir(parents=True, exist_ok=True)
    assert current not in _pruned(prune_empty_sessions(current))


async def test_the_session_is_prunable_again_once_the_tap_closes(recorder: Recorder):
    """THE LEAK PIN, happy path: the mark is released on a normal close, so the
    (now empty — the tap wrote nothing) directory prunes on the NEXT sweep. A mark
    that is never released makes the directory permanently un-prunable."""
    # Deliberately NO frame written: the empty WAV is unlinked at close, leaving the
    # directory a legitimate prune candidate. That is what makes a leaked mark
    # visible — with a frame written the WAV survives and prune skips the directory
    # for an entirely different (and correct) reason.
    async with await _open_tap(recorder):
        pass
    session = recorder.session_start
    result = prune_empty_sessions(_OTHER_CURRENT)
    assert session in _pruned(result), (
        "the session stayed un-prunable after the tap closed — the in-flight mark leaked"
    )
    assert session not in _sessions_on_disk(recorder)


# ---------------------------------------------------------------------------
# THE LEAK, at EVERY partial-init exit of `_open` (#196's lesson: one injection
# point leaves the earlier/narrower windows unpinned). Asserted at the harm layer —
# a later prune must be able to delete the directory — so no mark API is assumed.
# ---------------------------------------------------------------------------


async def test_mark_released_when_the_wav_open_fails(recorder: Recorder, monkeypatch: pytest.MonkeyPatch):
    def boom(fpath):
        raise OSError("disk full opening the recorder WAV")

    monkeypatch.setattr(tap_fan_out, "open_recorder_wav", boom)

    with pytest.raises(OSError):
        async with await _open_tap(recorder):
            pass  # pragma: no cover — open() raises before the body runs

    session = recorder.session_start
    assert session in _pruned(prune_empty_sessions(_OTHER_CURRENT)), (
        "the in-flight mark leaked after a failed WAV open — that session can never be pruned"
    )


async def test_mark_released_when_the_roster_write_fails(recorder: Recorder, monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **kw):
        raise OSError("disk full writing the roster")

    monkeypatch.setattr(roster, "record_occurrence", boom)

    with pytest.raises(OSError):
        async with await _open_tap(recorder):
            pass  # pragma: no cover

    session = recorder.session_start
    assert session in _pruned(prune_empty_sessions(_OTHER_CURRENT)), (
        "the in-flight mark leaked after a failed roster write"
    )


async def test_mark_released_when_the_relay_open_fails(recorder: Recorder):
    relay = _FakeRelay(open_error=OSError("disk full attaching the live relay"))

    with pytest.raises(OSError):
        async with await _open_tap(recorder, do_live=True, tap_relay=relay):
            pass  # pragma: no cover

    assert relay.opened is True, "the relay open() seam was never reached — injection is stale"
    session = recorder.session_start
    assert session in _pruned(prune_empty_sessions(_OTHER_CURRENT)), (
        "the in-flight mark leaked after a failed relay open"
    )


async def test_the_probe_identity_leaves_no_mark(recorder: Recorder):
    """`__probe__` must leave NO durable and NO live-visible state — it skips the WAV
    open, the roster occurrence and the ActiveStream registration. An in-flight mark
    is durable state of exactly that kind, so it must not survive the probe either."""
    # The probe skips the mkdir along with the WAV, so seed the directory first —
    # otherwise there is nothing on disk and the assertion passes vacuously.
    recorder.session_dir.mkdir(parents=True, exist_ok=True)

    async with await _open_tap(recorder, identity=PROBE_IDENTITY, name="", utterance_id="utt-probe"):
        pass

    session = recorder.session_start
    assert session in _pruned(prune_empty_sessions(_OTHER_CURRENT)), (
        "a __probe__ tap left an in-flight mark behind — the probe must leave no durable state"
    )
