"""RED contract for #358 — the bulk-reclaim check-then-act race.

`POST /api/sessions/bulk-reclaim-audio` freezes a single `busy` snapshot
(`recorder.jobs` + `recorder.streams`) and hands it, as `exclude_sessions`, to
the recorder-free walk `reclaim_audio_older_than`. A job that claims a session
*after* that snapshot but *before* the walk unlinks it is invisible to the frozen
set — so its WAVs get deleted out from under the running job. This is the same
check-then-act race the single-session `DELETE /audio` route already closed in
#353 (it now holds the session's JobTracker slot for the unlink walk); the bulk
route still has the old shape.

The fix must make a session that becomes busy *mid-walk* be SKIPPED, not deleted.
Two fix architectures satisfy that (the route claiming/releasing a JobTracker slot
per eligible session, or the walk consulting a live-busy callback at delete time),
so this contract is pinned at the ROUTE boundary and asserts on the OUTCOME (the
raced session's WAVs survive; it is neither reclaimed nor a delete-failure) — never
on which mechanism, so both fixes pass.

The race is modelled deterministically (no threads, no sleeps): a wrapper around
`delete_session_audio` marks the *other* eligible session busy in `recorder.jobs`
the instant the first reclaim begins — exactly "a job claimed it after the
snapshot, while the walk was mid-flight."
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]  # tests/ on sys.path

from tapscribe import app as app_module
from tapscribe import session_maintenance, session_paths
from tapscribe.app import app, get_recorder
from tapscribe.recorder import JobState

# 2020 is older than any realistic cutoff → both seeded sessions are age-eligible.
_OLD_WAV = "2020-01-01T00-00-00Z__alice__abcd1234.wav"


@pytest.fixture
def client(recorder_under_test):
    """TestClient wired to the tmpdir recorder (mirrors
    test_session_maintenance_bulk_reclaim.py). The override + app.state are torn
    down in the finalizer pytest always runs, so a failing assertion never leaks
    the override onto the shared module-level `app`."""
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_eligible(root: Path, name: str) -> Path:
    """Seed an OLD + transcribed session (age-eligible for reclaim). Returns its
    dir. Mirrors the sibling route test's `_seed_eligible`."""
    sd = seed_session(root, name, [_OLD_WAV])
    (sd / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "old"}')
    return sd


def _busy_state(session: str) -> JobState:
    return JobState(
        session=session,
        kind="strip",
        current=0,
        total=1,
        started_at=datetime.now(UTC),
        status="running",
    )


def _arm_midwalk_claim(
    monkeypatch: pytest.MonkeyPatch,
    recorder,
    eligible: set[str],
) -> None:
    """Make the FIRST `delete_session_audio` call — i.e. the first per-session
    reclaim the walk performs — mark every *other* eligible session busy in
    `recorder.jobs`, modelling a transcribe/strip job that claimed it after the
    route's busy-snapshot, while the walk is mid-flight.

    Patches BOTH bindings of the primitive: `session_maintenance`'s module global
    (the walk calls it late-bound) AND `app`'s `from ... import`-ed local (a
    route-driven per-session fix would call that one), so the injection fires
    whichever architecture the fix takes.

    Busy state is poked straight into `recorder.jobs._by_session` (not via the
    async `claim()`) to stay synchronous inside the `asyncio.to_thread` worker and
    to avoid using the JobTracker's main-loop `asyncio.Lock` from another loop.
    """
    original = session_maintenance.delete_session_audio
    fired: list[bool] = []

    def wrapper(session: str, **kwargs):
        if not fired:
            fired.append(True)
            for other in eligible - {session}:
                recorder.jobs._by_session[other] = _busy_state(other)
        return original(session, **kwargs)

    monkeypatch.setattr(session_maintenance, "delete_session_audio", wrapper)
    monkeypatch.setattr(app_module, "delete_session_audio", wrapper)


def test_execute_skips_session_that_becomes_busy_mid_walk(client, recorder_under_test, monkeypatch):
    """A session that a job claims *after* the route's busy-snapshot but *before*
    its own unlink must be SKIPPED — its WAVs survive the bulk execute. Currently
    the frozen `exclude_sessions` can't see the late claim, so the walk deletes it
    anyway (RED). Fix-agnostic: asserted on the surviving WAVs, not the mechanism.
    """
    recorder_under_test.session_start = "current-live"
    root = recorder_under_test.recordings_dir
    names = {"reclaim-a", "reclaim-b"}
    for n in names:
        _seed_eligible(root, n)
    _arm_midwalk_claim(monkeypatch, recorder_under_test, names)

    r = client.post("/api/sessions/bulk-reclaim-audio", json={"older_than_days": 30, "execute": True})

    assert r.status_code == 200
    body = r.json()
    # Exactly one of the two eligible sessions was still idle when the walk reached
    # it; the other was claimed mid-walk and must have been spared.
    surviving = [n for n in names if (root / n / _OLD_WAV).is_file()]
    assert len(surviving) == 1, (
        f"the session claimed mid-walk was deleted out from under the job "
        f"(survivors={surviving}, reclaimed={[s['session'] for s in body['sessions']]})"
    )
    reclaimed = {s["session"] for s in body["sessions"]}
    assert len(reclaimed) == 1
    # The spared session is a clean SKIP — neither reclaimed nor a delete-failure.
    assert surviving[0] not in reclaimed
    assert body["failed"] == []


def test_execute_reclaims_both_idle_sessions(client, recorder_under_test):
    """Guardrail: with NO mid-walk claim, both eligible sessions reclaim normally —
    the fix must not over-skip (e.g. mistaking the busy-recheck for "skip on any
    job anywhere"). Green on current code and after the fix."""
    recorder_under_test.session_start = "current-live"
    root = recorder_under_test.recordings_dir
    names = {"idle-a", "idle-b"}
    for n in names:
        _seed_eligible(root, n)

    r = client.post("/api/sessions/bulk-reclaim-audio", json={"older_than_days": 30, "execute": True})

    assert r.status_code == 200
    body = r.json()
    assert {s["session"] for s in body["sessions"]} == names
    assert body["failed"] == []
    assert [n for n in names if (root / n / _OLD_WAV).is_file()] == []  # both freed
