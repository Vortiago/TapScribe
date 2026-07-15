"""Tests for the bulk audio-retention operation (#207).

Seam under test: ``reclaim_audio_older_than`` (the maintenance function) and
``POST /api/sessions/bulk-reclaim-audio`` (the HTTP boundary), using the same
``recorder_under_test`` + ``seed_session`` pattern as
``test_session_maintenance_fault_injection.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from wav_builders import seed_session  # type: ignore[import-not-found]  # noqa: E402

from tapscribe import session_maintenance, session_paths


@pytest.fixture
def rec_root(recorder_under_test) -> str:  # noqa: ARG001
    """Expose the recordings root dir name so the maintenance functions
    walk against the monkeypatched RECORDINGS_DIR."""
    return recorder_under_test.recordings_dir


# ---------------------------------------------------------------------------
# Preview (execute=False)
# ---------------------------------------------------------------------------


def test_preview_lists_only_eligible_and_deletes_nothing(
    recorder_under_test,  # noqa: ARG001
):
    """Preview mode: only eligible sessions appear, no audio is deleted,
    total_bytes reflects the sum of reclaimable bytes across eligible sessions."""
    # Four sessions with different eligibility profiles:
    #   current-abc    – the live session → excluded
    #   old-transcribed – old WAVs + merged transcript → eligible
    #   new-transcribed – recent WAVs + merged transcript → too young
    #   old-no-transcript – old WAVs, no transcript → no transcript gate
    current_session = "current-abc"
    recorder_under_test.session_start = current_session

    old_transcribed = seed_session(
        recorder_under_test.recordings_dir,
        "old-transcribed",
        ["2020-01-01T00-00-00Z__alice__abcd1234.wav", "2020-01-01T01-00-00Z__bob__efgh5678.wav"],
    )
    new_transcribed = seed_session(
        recorder_under_test.recordings_dir,
        "new-transcribed",
        ["2026-07-14T00-00-00Z__alice__ijkl9012.wav"],
    )
    seed_session(
        recorder_under_test.recordings_dir, "old-no-transcript", ["2020-02-01T12-00-00Z__carol__mnop3456.wav"]
    )

    # Seed merged transcripts for eligible + ineligible-but-transcribed sessions.
    (old_transcribed / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "old"}')
    (new_transcribed / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "new"}')

    result = session_maintenance.reclaim_audio_older_than(
        current_session,
        older_than_days=30,
        execute=False,
    )

    # Only the old-transcribed session is eligible.
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["session"] == "old-transcribed"
    assert result["sessions"][0]["bytes_freed"] > 0
    assert result["total_bytes"] > 0

    # All four session directories are untouched (current-abc isn't created
    # but it wouldn't be deleted anyway — it's the current_session).
    assert not any(d.name == "current-abc" for d in Path(recorder_under_test.recordings_dir).iterdir())
    assert (recorder_under_test.recordings_dir / "old-transcribed").exists()
    assert (recorder_under_test.recordings_dir / "new-transcribed").exists()
    assert (recorder_under_test.recordings_dir / "old-no-transcript").exists()


def test_preview_includes_eligible_session_with_zero_bytes(
    recorder_under_test,  # noqa: ARG001
):
    """An eligible session whose WAVs have been manually zeroed (but still
    exist on disk) should appear with bytes_freed=0, not be silently skipped."""
    current_session = "current-empty"
    recorder_under_test.session_start = current_session

    # Seed a session with one WAV, then zero the WAV content (simulating
    # external truncation) — but leave the transcript so the session still
    # passes the "has audio" gate (the WAV file is on disk).
    seed_session(
        recorder_under_test.recordings_dir, "zero-bytes-sess", ["2020-01-01T00-00-00Z__alice__abcd1234.wav"]
    )
    wav_path = (
        Path(recorder_under_test.recordings_dir)
        / "zero-bytes-sess"
        / "2020-01-01T00-00-00Z__alice__abcd1234.wav"
    )
    wav_path.write_bytes(b"")  # zero the WAV but leave it on disk
    (
        Path(recorder_under_test.recordings_dir) / "zero-bytes-sess" / session_paths.FILENAME_TRANSCRIPT_JSON
    ).write_text('{"text": "ghost"}')

    result = session_maintenance.reclaim_audio_older_than(
        current_session,
        older_than_days=30,
        execute=False,
    )

    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["session"] == "zero-bytes-sess"
    assert result["sessions"][0]["bytes_freed"] == 0


# ---------------------------------------------------------------------------
# Execute (execute=True)
# ---------------------------------------------------------------------------


def test_execute_reclaims_only_eligible_and_keeps_transcripts(
    recorder_under_test,  # noqa: ARG001
):
    """Execute mode: eligible sessions' WAVs and stripped/ are deleted,
    merged transcripts and meta survive, non-eligible sessions are untouched."""
    current_session = "current-xyz"
    recorder_under_test.session_start = current_session

    old_transcribed = seed_session(
        recorder_under_test.recordings_dir,
        "old-transcribed",
        ["2020-01-01T00-00-00Z__alice__abcd1234.wav"],
    )
    new_transcribed = seed_session(
        recorder_under_test.recordings_dir,
        "new-transcribed",
        ["2026-07-14T00-00-00Z__alice__ijkl9012.wav"],
    )
    seed_session(
        recorder_under_test.recordings_dir, "old-no-transcript", ["2020-02-01T12-00-00Z__carol__mnop3456.wav"]
    )

    # Add merged transcripts + meta to sessions that have audio.
    (old_transcribed / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "old"}')
    (old_transcribed / session_paths.FILENAME_META_JSON).write_text('{"label": "old"}')
    (new_transcribed / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"text": "new"}')
    (new_transcribed / session_paths.FILENAME_META_JSON).write_text('{"label": "new"}')

    result = session_maintenance.reclaim_audio_older_than(
        current_session,
        older_than_days=30,
        execute=True,
    )

    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["session"] == "old-transcribed"
    assert result["sessions"][0]["bytes_freed"] > 0
    assert result["total_bytes"] > 0

    # Old-transcribed WAVs are gone.
    wav_files = list((recorder_under_test.recordings_dir / "old-transcribed").glob("*.wav"))
    assert wav_files == []

    # Merged transcript + meta survive.
    assert (old_transcribed / session_paths.FILENAME_TRANSCRIPT_JSON).exists()
    assert (old_transcribed / session_paths.FILENAME_META_JSON).exists()

    # Non-eligible sessions are untouched.
    assert len(list((recorder_under_test.recordings_dir / "new-transcribed").glob("*.wav"))) == 1
    assert len(list((recorder_under_test.recordings_dir / "old-no-transcript").glob("*.wav"))) == 1


def test_execute_refuses_negative_days(recorder_under_test):  # noqa: ARG001
    """older_than_days=0 or negative should not be accepted by the route."""
    from fastapi.testclient import TestClient

    from tapscribe.app import app, get_recorder

    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        resp = c.post("/api/sessions/bulk-reclaim-audio?older_than_days=0")
        assert resp.status_code == 400

        resp = c.post("/api/sessions/bulk-reclaim-audio?older_than_days=-5")
        assert resp.status_code == 400
    app.dependency_overrides.clear()
