"""Chaos tests focused on TapFanOut lifecycle edge cases.

Covers two scenarios the audit flagged as untested:

  - Abnormal /tap close mid-stream (client transport drops, server-side
    exception). The async context manager must still finalize the WAV,
    release the UtteranceIndex entry, close the relay, and leave no
    orphan asyncio tasks behind.
  - Utterance resume across a session rotation. The "one utterance = one
    WAV" invariant in CONTEXT.md doesn't explicitly cover what happens
    when the session_dir changes between the original /tap WS and the
    bridge's reconnect with the same utterance_id. This test pins
    today's behaviour so the gap is documented.

Tagged `@pytest.mark.chaos` so a fast CI lane can skip with `-m 'not
chaos'`. Leans on `filterwarnings = ["error"]` in pyproject.toml: any
leaked asyncio task surfaces as a test failure rather than a console
warning."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder
from tapscribe.tap_fan_out import TapFanOut
from tests.conftest import FakeWlkThread

pytestmark = pytest.mark.chaos


PCM_FRAME = b"\x10\x00" * 320  # 20 ms @ 16 kHz mono int16


def _build_recorder(tmp_path: Path, port: int = 9999) -> Recorder:
    recordings = tmp_path / "recordings"
    config_dir = tmp_path / "config"
    recordings.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    return Recorder(
        recordings_dir=recordings,
        config_dir=config_dir,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=port),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


def _build_recorder_with_relay(tmp_path: Path, fake_wlk: FakeWlkThread) -> Recorder:
    r = _build_recorder(tmp_path, port=fake_wlk.port)

    class _FakeProc:
        def poll(self):
            return None

    r.live._proc = _FakeProc()
    return r


# ---------------------------------------------------------------------------
# 3. Abnormal /tap close mid-stream — cleanup must complete
# ---------------------------------------------------------------------------


async def test_abnormal_tap_close_finalizes_or_unlinks_and_leaks_no_tasks(
    tmp_path: Path,
    fake_wlk: FakeWlkThread,
):
    """Two halves, one test — the cleanup contract of the async context
    manager has the same shape whether or not any audio arrived.

    Half A (frames received): the receive loop blows up after two
    frames. __aexit__ must finalize the WAV non-empty, release the
    UtteranceIndex entry with kept=True (so a resume within the window
    still finds it), close the relay (drains the consumer task), remove
    the ActiveStream row, and leave no extra asyncio tasks behind.

    Half B (empty utterance): the receive loop blows up before any
    frame is written. __aexit__ must unlink the empty WAV (no phantom
    zero-byte files on disk for the operator to wonder about), drop
    the UtteranceIndex entry entirely, and again leave no leaked tasks.

    The task-leak check matters because pyproject's
    `filterwarnings = ["error"]` would turn any "task was destroyed but
    pending" into a test failure — but those warnings can also fire
    during interpreter shutdown after the test passes. We snapshot
    `asyncio.all_tasks()` at the boundary so a leak surfaces here, not
    later in some unrelated test."""
    # ---- Half A: frames received, then a mid-stream RuntimeError ----
    r = _build_recorder_with_relay(tmp_path, fake_wlk)
    tasks_before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    fan_out = await TapFanOut.open(
        r,
        identity="alice",
        name="Alice",
        utterance_id="utt-abnormal-close",
        do_record=True,
        do_live=True,
    )
    try:
        async with fan_out:
            await fan_out.write_frame(PCM_FRAME)
            await fan_out.write_frame(PCM_FRAME)
            # Simulate the /tap handler's receive loop blowing up
            # mid-utterance — e.g. a ConnectionResetError bubbling from
            # the ASGI transport. __aexit__ must still finalize.
            raise RuntimeError("simulated abnormal /tap close mid-stream")
    except RuntimeError as e:
        assert "simulated" in str(e)

    # WAV finalized non-empty.
    wavs = list(r.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected exactly one finalized WAV, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnframes() == 640
    # No orphan files (e.g. a .partial / .tmp lingering).
    siblings = sorted(p.name for p in r.session_dir.iterdir())
    assert siblings == [wavs[0].name], f"unexpected leftover files: {siblings}"
    # UtteranceIndex entry kept (non-empty -> resumable within window).
    rec = r.utterances.snapshot()["utt-abnormal-close"]
    assert rec.open is False
    assert rec.bytes_received == len(PCM_FRAME) * 2
    # ActiveStream row removed.
    assert await r.streams.snapshot() == []

    # One scheduler tick so cancelled background tasks finish exiting.
    await asyncio.sleep(0)
    tasks_after = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    pending_leaked = [t for t in (tasks_after - tasks_before) if not t.done()]
    assert pending_leaked == [], (
        f"half A abnormal close left {len(pending_leaked)} pending task(s): "
        f"{[t.get_name() for t in pending_leaked]}"
    )

    # ---- Half B: zero frames, abnormal close before any audio ----
    r2 = _build_recorder(tmp_path / "b")
    tasks_before2 = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    fan_out2 = await TapFanOut.open(
        r2,
        identity="alice",
        name="Alice",
        utterance_id="utt-abnormal-empty",
        do_record=True,
        do_live=False,
    )
    try:
        async with fan_out2:
            raise RuntimeError("abnormal close before any frame written")
    except RuntimeError:
        pass

    assert list(r2.session_dir.glob("*.wav")) == []
    assert "utt-abnormal-empty" not in r2.utterances.snapshot()
    assert await r2.streams.snapshot() == []

    await asyncio.sleep(0)
    tasks_after2 = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    pending_leaked2 = [t for t in (tasks_after2 - tasks_before2) if not t.done()]
    assert pending_leaked2 == [], (
        f"half B (empty) abnormal close left {len(pending_leaked2)} pending task(s): "
        f"{[t.get_name() for t in pending_leaked2]}"
    )


# ---------------------------------------------------------------------------
# 6. Resume across session rotation
# ---------------------------------------------------------------------------


async def test_resume_across_session_rotation_pins_current_behaviour(
    tmp_path: Path,
):
    """Write /tap utterance X under session A. Rotate the session to B.
    Reconnect with utterance_id=X. The "one utterance = one WAV"
    invariant in CONTEXT.md doesn't say where the resumed WAV should
    land when session_dir changed between the two halves.

    This test PINS what happens today so a future change is forced to
    update the test (and document the choice). It also asserts the
    invariant the audit cares about: a fresh WAV in session B, with the
    old session-A WAV either left intact or appended-to — never
    silently lost.

    Today's behaviour (as of writing): `UtteranceIndex.try_resume`
    returns the existing record because the file path it remembers
    still exists on disk — that path is in session A. The fan-out then
    appends to the session-A WAV even though `recorder.session_dir`
    now points at B. This means a reconnect after a session rotation
    silently writes into the previous session's folder, which violates
    the operator's mental model of "one session = one folder of
    utterances."

    Documents the gap and pins the current behaviour so any future
    fix breaks this test loudly."""
    r = _build_recorder(tmp_path)
    utt = "utt-cross-session"

    # ----- session A: write the first half -----
    session_a_dir = r.session_dir
    async with await TapFanOut.open(
        r,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)
        await fan_out.write_frame(PCM_FRAME)

    wavs_a = list(session_a_dir.glob("*.wav"))
    assert len(wavs_a) == 1, "session A should hold the original WAV"
    original_wav_path = wavs_a[0]
    original_frames = 320 * 2

    # ----- rotate to session B (operator clicks "new session") -----
    # `rotate_session` returns (prev, new) and flips recorder.session_dir
    # without touching UtteranceIndex. Bump the strftime resolution by
    # hand so the rotation actually produces a different folder even on
    # second-resolution clocks (the unit tests note this is a no-op
    # within the same second).
    import time

    time.sleep(1.05)
    prev_session, new_session = r.rotate_session()
    assert prev_session != new_session, "session rotation produced no new id"
    session_b_dir = r.session_dir
    assert session_b_dir != session_a_dir

    # ----- session B: reconnect with the same utterance_id -----
    async with await TapFanOut.open(
        r,
        identity="alice",
        name="Alice",
        utterance_id=utt,
        do_record=True,
        do_live=False,
    ) as fan_out:
        await fan_out.write_frame(PCM_FRAME)

    # Audit-flagged behaviour: the resume path consults UtteranceIndex,
    # finds the still-on-disk WAV in session A, and APPENDS to it.
    # session_b_dir gets nothing.
    wavs_b = list(session_b_dir.glob("*.wav"))
    wavs_a_after = list(session_a_dir.glob("*.wav"))

    # Pinning today's behaviour:
    if not wavs_b and len(wavs_a_after) == 1 and wavs_a_after[0] == original_wav_path:
        # Resume reused the session-A WAV — the documented gap. Confirm
        # the file actually grew by one frame so we know the append
        # really happened (rather than silently dropping the second half).
        with wave.open(str(original_wav_path), "rb") as w:
            assert w.getnframes() == original_frames + 320, (
                "resumed-into-session-A WAV did not grow by the appended frame — "
                "silent data loss across session rotation"
            )
        # Document the gap loudly in the test output so anyone reading
        # CI logs notices.
        pytest.xfail(
            "Documented audit gap: utterance resume after rotate_session() "
            "appends to the OLD session's WAV instead of starting fresh in "
            "the new session_dir. UtteranceIndex.try_resume only checks "
            "file existence, not session_dir parent. See CONTEXT.md "
            "'one utterance = one WAV' — invariant doesn't pin which "
            "session folder the WAV lives in."
        )
    else:
        # Future-fix branch: a fresh WAV landed in session B. Assert the
        # invariant in its strict form — the new session has its own WAV
        # and the old session's WAV is untouched.
        assert len(wavs_b) == 1, (
            f"expected exactly one fresh WAV in session B after rotation, "
            f"got {[w.name for w in wavs_b]}"
        )
        # Old session unchanged.
        assert len(wavs_a_after) == 1
        assert wavs_a_after[0] == original_wav_path
        with wave.open(str(original_wav_path), "rb") as w:
            assert w.getnframes() == original_frames, (
                "session-A WAV was modified by a session-B reconnect"
            )
