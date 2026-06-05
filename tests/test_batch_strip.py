"""Direct tests for tapscribe.batch_strip.

Same shape as test_batch_transcribe: the orchestration (JobTracker claim,
strip_one_wav loop, aggregation) is testable WITHOUT an HTTP TestClient —
`strip_session` is called straight against a tmpdir-rooted Recorder. The
silero detector is the conftest RMS stub, so the square-wave seed WAVs
strip into exactly one clip each.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe.batch_strip import StripSessionRequest, strip_session
from tapscribe.batch_transcribe import NoUsableWavs, SessionBusy
from tapscribe.recorder import JobState

WAV_NAME = "2026-01-01T01-00-00Z__alice__abc.wav"


async def test_strip_session_writes_clips_and_aggregates(recorder_under_test):
    """Happy path: each audible original yields a stripped clip; the
    response aggregates per-file results and the job slot is released."""
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])

    out = await strip_session(recorder_under_test, StripSessionRequest(session="s"))

    assert out["ok"] is True
    assert out["files_processed"] == 1
    assert out["files_written"] >= 1
    assert out["in_seconds"] > 0
    stripped = recorder_under_test.recordings_dir / "s" / "stripped"
    assert stripped.is_dir()
    assert any(stripped.glob("*.wav"))
    # The JobTracker slot must be free again — a second strip succeeds.
    out2 = await strip_session(recorder_under_test, StripSessionRequest(session="s"))
    assert out2["ok"] is True


async def test_strip_session_raises_no_usable_wavs_on_empty_session(recorder_under_test):
    (recorder_under_test.recordings_dir / "empty").mkdir()
    with pytest.raises(NoUsableWavs):
        await strip_session(recorder_under_test, StripSessionRequest(session="empty"))


async def test_strip_session_raises_session_busy_and_leaves_foreign_claim_alone(recorder_under_test):
    """When another job holds the session's slot, strip_session must raise
    SessionBusy WITHOUT releasing the foreign claim on its way out."""
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
    claimed = await recorder_under_test.jobs.claim(
        JobState(
            session="s", kind="transcribe", current=0, total=1, started_at=datetime.now(UTC), status="running"
        )
    )
    assert claimed

    with pytest.raises(SessionBusy):
        await strip_session(recorder_under_test, StripSessionRequest(session="s"))

    # The pre-existing transcribe claim must still be in place.
    assert recorder_under_test.jobs.snapshot()["s"].kind == "transcribe"
