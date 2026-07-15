"""Route-boundary + edge tests for the bulk audio-retention op (#207).

The committed contract ``test_bulk_reclaim_audio.py`` pins the correctness-bearing
eligibility TAXONOMY at the function level. This file covers what that gate scoped
out — the HTTP route (its JSON-body call convention + response envelope) and the
edges the happy-path fixtures never seed:

  * the route reads a JSON BODY (not query params) and returns ``{ok, sessions,
    total_bytes, failed}``
  * ``older_than_days <= 0`` (or missing) is rejected with 400
  * a non-current session with a transcribe/strip job in flight, or a live tap
    writing to it, is EXCLUDED — its WAVs survive an execute (the busy-guard the
    single-item DELETE /audio route enforces via ``_refuse_current_or_busy``)
  * a delete-failing session lands in ``failed[]`` and the walk continues
  * a session mixing old + recent WAVs takes its age from the LATEST (max) WAV
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio.from_thread
import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import session_maintenance, session_paths
from tapscribe.app import app, get_recorder
from tapscribe.recorder import ActiveStream, JobState
from tapscribe.text import build_recorder_wav_name

_OLD_WAV = "2020-01-01T00-00-00Z__alice__abcd1234.wav"


@pytest.fixture
def client(recorder_under_test):
    """TestClient wired to the tmpdir recorder. The override + app.state are
    torn down in the fixture's finalizer, which pytest always runs — so an
    assertion failure inside a `with client` block never leaks the override
    onto the shared module-level `app`."""
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _claim_job(recorder, session: str) -> None:
    """Pre-claim a job slot the way the sibling inflight-job route tests do —
    JobTracker.claim is async, driven via anyio's sync→async portal."""
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder.jobs.claim,
            JobState(
                session=session,
                kind="strip",
                current=0,
                total=1,
                started_at=datetime.now(UTC),
                status="running",
            ),
        )


def _register_tap(recorder, session: str) -> None:
    """Register a live tap writing into `session` (an ActiveStream), the way
    test_session_active_tap_guard.py seeds the active-tap guard."""
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder.streams.register,
            ActiveStream(
                conn_id="conn-1",
                identity="alice",
                name="Alice",
                filename=_OLD_WAV,
                started_at=datetime.now(UTC),
                session=session,
            ),
        )


def _seed_eligible(root, name: str):
    """Seed an OLD + transcribed session (eligible for reclaim). Returns its dir."""
    sd = seed_session(root, name, [_OLD_WAV])
    (sd / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "old"}')
    return sd


# ---------------------------------------------------------------------------
# Preview edge (function-level, genuinely-new vs the committed contract)
# ---------------------------------------------------------------------------


def test_preview_includes_eligible_session_with_zero_bytes(recorder_under_test):
    """An eligible session whose WAV has been zeroed (but still exists on disk)
    appears with bytes_freed=0, not silently skipped."""
    current_session = "current-empty"
    recorder_under_test.session_start = current_session
    root = recorder_under_test.recordings_dir

    sd = _seed_eligible(root, "zero-bytes-sess")
    (sd / _OLD_WAV).write_bytes(b"")  # zero the WAV but leave it on disk

    result = session_maintenance.reclaim_audio_older_than(current_session, older_than_days=30, execute=False)

    assert [s["session"] for s in result["sessions"]] == ["zero-bytes-sess"]
    assert result["sessions"][0]["bytes_freed"] == 0


# ---------------------------------------------------------------------------
# Route boundary — JSON body call convention + response envelope
# ---------------------------------------------------------------------------


def test_route_reads_json_body_and_returns_envelope(client, recorder_under_test):
    """The route reads a JSON BODY (not query params) and returns the documented
    {ok, sessions, total_bytes, failed} envelope. A body-only POST — which a
    query-param endpoint would 422 on — must succeed and preview the eligible
    session without deleting anything."""
    recorder_under_test.session_start = "current-live"
    root = recorder_under_test.recordings_dir
    sd = _seed_eligible(root, "old-transcribed")

    r = client.post("/api/sessions/bulk-reclaim-audio", json={"older_than_days": 30, "execute": False})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body) == {"ok", "sessions", "total_bytes", "failed"}
    assert {s["session"] for s in body["sessions"]} == {"old-transcribed"}
    assert body["total_bytes"] > 0
    assert body["failed"] == []
    assert list(sd.glob("*.wav"))  # preview deleted nothing


def test_route_refuses_nonpositive_or_missing_days(client):
    """older_than_days <= 0 posted as a body is rejected (value rejection, not a
    missing-field artifact), and a missing field fails loud with 400 too — so a
    query-style client that sends no body can't silently no-op."""
    for bad_body in ({"older_than_days": 0}, {"older_than_days": -5}, {}):
        r = client.post("/api/sessions/bulk-reclaim-audio", json=bad_body)
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Route boundary — busy/live-tap exclusion (the gate scoped the route out)
# ---------------------------------------------------------------------------


def test_execute_excludes_session_with_inflight_job(client, recorder_under_test):
    """A non-current, old+transcribed session with a transcribe/strip job in
    flight is EXCLUDED: an execute must not delete its WAVs out from under the
    running job."""
    recorder_under_test.session_start = "current-live"
    root = recorder_under_test.recordings_dir
    sd = _seed_eligible(root, "busy-old")
    _claim_job(recorder_under_test, "busy-old")

    r = client.post("/api/sessions/bulk-reclaim-audio", json={"older_than_days": 30, "execute": True})

    assert r.status_code == 200
    assert "busy-old" not in {s["session"] for s in r.json()["sessions"]}
    assert (sd / _OLD_WAV).is_file()  # WAVs survived the in-flight job


def test_execute_excludes_session_with_live_tap(client, recorder_under_test):
    """A non-current session with a live tap writing into it is EXCLUDED too —
    an execute must not delete WAVs a tap is appending to."""
    recorder_under_test.session_start = "current-live"
    root = recorder_under_test.recordings_dir
    sd = _seed_eligible(root, "tapped-old")
    _register_tap(recorder_under_test, "tapped-old")

    r = client.post("/api/sessions/bulk-reclaim-audio", json={"older_than_days": 30, "execute": True})

    assert r.status_code == 200
    assert "tapped-old" not in {s["session"] for s in r.json()["sessions"]}
    assert (sd / _OLD_WAV).is_file()  # WAVs survived the live tap


# ---------------------------------------------------------------------------
# Execute edges (function-level) — partial-failure resilience + age tie-break
# ---------------------------------------------------------------------------


def test_execute_collects_delete_failures_and_continues(recorder_under_test, monkeypatch):
    """A session whose delete raises SessionDeleteError (e.g. a locked stripped/
    dir) is collected into failed[] and the walk continues — it never lands in
    sessions[] and never aborts the reclaim of the healthy sessions."""
    current = "current-live"
    recorder_under_test.session_start = current
    root = recorder_under_test.recordings_dir

    good = _seed_eligible(root, "good-old")
    bad = seed_session(root, "bad-old", ["2020-01-02T00-00-00Z__bob__efgh5678.wav"])
    (bad / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "bad"}')

    real = session_maintenance.delete_session_audio

    def flaky(session, *, dry_run=False):
        if session == "bad-old":
            raise session_maintenance.SessionDeleteError("locked stripped/")
        return real(session, dry_run=dry_run)

    monkeypatch.setattr(session_maintenance, "delete_session_audio", flaky)

    result = session_maintenance.reclaim_audio_older_than(current, older_than_days=30, execute=True)

    assert {s["session"] for s in result["sessions"]} == {"good-old"}
    assert {f["session"] for f in result["failed"]} == {"bad-old"}
    assert result["failed"][0]["error"] == "delete failed"
    assert not list(good.glob("*.wav"))  # healthy session reclaimed
    assert list(bad.glob("*.wav"))  # failed session's WAVs untouched


def test_mixed_age_session_kept_by_latest_wav(recorder_under_test):
    """A session mixing an OLD and a RECENT WAV takes its age from the LATEST
    (max) start, so it reads as recent and is KEPT — never reclaim a session
    still being appended to."""
    current = "current-live"
    recorder_under_test.session_start = current
    root = recorder_under_test.recordings_dir

    recent = build_recorder_wav_name(datetime.now(UTC) - timedelta(days=1), "alice", "a")
    sd = seed_session(root, "mixed-age", [_OLD_WAV, recent])
    (sd / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "mixed"}')

    result = session_maintenance.reclaim_audio_older_than(current, older_than_days=30, execute=True)

    assert {s["session"] for s in result["sessions"]} == set()  # excluded
    assert len(list(sd.glob("*.wav"))) == 2  # both WAVs kept
