"""Tests for the Recorder's sub-components — ActiveStreams, JobTracker,
LiveTranscripts, SecretFile.

These are small concurrency primitives that encapsulate a dict/lock or
deque so route handlers don't have to acquire locks manually. Each is
unit-testable without the FastAPI app or any real model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tapscribe.recorder import (
    ActiveStream,
    ActiveStreams,
    JobState,
    JobTracker,
    LiveTranscripts,
    SecretFile,
)

# ---------------------------------------------------------------------------
# ActiveStreams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_streams_register_appears_in_snapshot():
    streams = ActiveStreams()
    s = ActiveStream(
        conn_id="abc-1234",
        identity="alice",
        name="Alice",
        filename="x.wav",
        started_at=datetime.now(timezone.utc),
    )
    await streams.register(s)
    snap = await streams.snapshot()
    assert len(snap) == 1
    assert snap[0].conn_id == "abc-1234"
    assert snap[0].bytes_received == 0


@pytest.mark.asyncio
async def test_active_streams_remove_drops_entry():
    streams = ActiveStreams()
    await streams.register(
        ActiveStream(conn_id="a", identity="i", name="n", filename="f", started_at=datetime.now(timezone.utc))
    )
    await streams.remove("a")
    assert (await streams.snapshot()) == []


@pytest.mark.asyncio
async def test_active_streams_update_bytes_increments():
    streams = ActiveStreams()
    await streams.register(
        ActiveStream(conn_id="a", identity="i", name="n", filename="f", started_at=datetime.now(timezone.utc))
    )
    await streams.update_bytes("a", 640)
    snap = await streams.snapshot()
    assert snap[0].bytes_received == 640


@pytest.mark.asyncio
async def test_active_streams_update_bytes_unknown_id_is_noop():
    streams = ActiveStreams()
    # Should not raise — the WS handler races against close
    await streams.update_bytes("unknown", 100)


# ---------------------------------------------------------------------------
# JobTracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_tracker_claim_returns_true_when_free():
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc)
    )
    assert await tracker.claim(state) is True


@pytest.mark.asyncio
async def test_job_tracker_claim_returns_false_when_already_claimed():
    """The 'one job per session' rule lives in JobTracker, not in each
    route handler."""
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc)
    )
    await tracker.claim(state)
    second = JobState(session="s1", kind="strip", current=0, total=3, started_at=datetime.now(timezone.utc))
    assert await tracker.claim(second) is False


@pytest.mark.asyncio
async def test_job_tracker_release_allows_reclaim():
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc)
    )
    await tracker.claim(state)
    await tracker.release("s1")
    assert await tracker.claim(state) is True


@pytest.mark.asyncio
async def test_job_tracker_update_modifies_fields():
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc)
    )
    await tracker.claim(state)
    await tracker.update("s1", current=3, current_file="x.wav")
    got = tracker.get("s1")
    assert got is not None
    assert got.current == 3
    assert got.current_file == "x.wav"


@pytest.mark.asyncio
async def test_job_tracker_get_missing_returns_none():
    tracker = JobTracker()
    assert tracker.get("nonexistent") is None


# ---------------------------------------------------------------------------
# LiveTranscripts
# ---------------------------------------------------------------------------


def test_live_transcripts_append_and_snapshot():
    feed = LiveTranscripts()
    feed.append({"text": "hello"})
    feed.append({"text": "world"})
    snap = feed.snapshot()
    assert [e["text"] for e in snap] == ["hello", "world"]


def test_live_transcripts_clear_resets():
    feed = LiveTranscripts()
    feed.append({"text": "hello"})
    feed.clear()
    assert feed.snapshot() == []


def test_live_transcripts_bounded_by_maxlen():
    feed = LiveTranscripts(max_entries=3)
    for i in range(10):
        feed.append({"text": str(i)})
    snap = feed.snapshot()
    assert len(snap) == 3
    assert [e["text"] for e in snap] == ["7", "8", "9"]


# ---------------------------------------------------------------------------
# SecretFile — used for the dashboard password and the /tap bearer token.
# Parametrized over the two real callsites' labels so a regression in
# either kicks in.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["password", "tap token"])
def test_secret_file_generates_value_when_file_missing(tmp_path: Path, label: str):
    f = tmp_path / ".secret"
    s = SecretFile.load_or_create(f, label=label)
    assert s.value
    assert f.is_file()
    assert f.read_text(encoding="utf-8").strip() == s.value


@pytest.mark.parametrize("label", ["password", "tap token"])
def test_secret_file_reads_existing_value(tmp_path: Path, label: str):
    f = tmp_path / ".secret"
    f.write_text("preset-value", encoding="utf-8")
    s = SecretFile.load_or_create(f, label=label)
    assert s.value == "preset-value"


@pytest.mark.parametrize("label", ["password", "tap token"])
def test_secret_file_rotate_generates_new_distinct_value(tmp_path: Path, label: str):
    f = tmp_path / ".secret"
    s = SecretFile.load_or_create(f, label=label)
    original = s.value
    s.rotate()
    assert s.value != original
    assert f.read_text(encoding="utf-8").strip() == s.value
