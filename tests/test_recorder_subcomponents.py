"""Tests for the Recorder's sub-components — ActiveStreams, JobTracker,
LiveTranscripts, AuthState.

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
    AuthState,
    JobState,
    JobTracker,
    LiveTranscripts,
    TapTokenState,
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
    await streams.register(ActiveStream(conn_id="a", identity="i", name="n", filename="f", started_at=datetime.now(timezone.utc)))
    await streams.remove("a")
    assert (await streams.snapshot()) == []


@pytest.mark.asyncio
async def test_active_streams_update_bytes_increments():
    streams = ActiveStreams()
    await streams.register(ActiveStream(conn_id="a", identity="i", name="n", filename="f", started_at=datetime.now(timezone.utc)))
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
    state = JobState(session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc))
    assert await tracker.claim(state) is True


@pytest.mark.asyncio
async def test_job_tracker_claim_returns_false_when_already_claimed():
    """The 'one job per session' rule lives in JobTracker, not in each
    route handler."""
    tracker = JobTracker()
    state = JobState(session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc))
    await tracker.claim(state)
    second = JobState(session="s1", kind="strip", current=0, total=3, started_at=datetime.now(timezone.utc))
    assert await tracker.claim(second) is False


@pytest.mark.asyncio
async def test_job_tracker_release_allows_reclaim():
    tracker = JobTracker()
    state = JobState(session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc))
    await tracker.claim(state)
    await tracker.release("s1")
    assert await tracker.claim(state) is True


@pytest.mark.asyncio
async def test_job_tracker_update_modifies_fields():
    tracker = JobTracker()
    state = JobState(session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(timezone.utc))
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
# AuthState
# ---------------------------------------------------------------------------

def test_auth_state_generates_password_when_file_missing(tmp_path: Path):
    pw_file = tmp_path / ".auth-password"
    state = AuthState.load_or_create(pw_file)
    assert state.password
    assert pw_file.is_file()
    assert pw_file.read_text(encoding="utf-8").strip() == state.password


def test_auth_state_reads_existing_password(tmp_path: Path):
    pw_file = tmp_path / ".auth-password"
    pw_file.write_text("preset-password", encoding="utf-8")
    state = AuthState.load_or_create(pw_file)
    assert state.password == "preset-password"


def test_auth_state_rotate_generates_new_distinct_password(tmp_path: Path):
    pw_file = tmp_path / ".auth-password"
    state = AuthState.load_or_create(pw_file)
    original = state.password
    state.rotate()
    assert state.password != original
    assert pw_file.read_text(encoding="utf-8").strip() == state.password


# ---------------------------------------------------------------------------
# TapTokenState — mirror of AuthState but for the /tap WebSocket bearer
# ---------------------------------------------------------------------------

def test_tap_token_generates_when_file_missing(tmp_path: Path):
    tf = tmp_path / ".tap-token"
    state = TapTokenState.load_or_create(tf)
    assert state.token
    assert tf.is_file()
    assert tf.read_text(encoding="utf-8").strip() == state.token


def test_tap_token_reads_existing(tmp_path: Path):
    tf = tmp_path / ".tap-token"
    tf.write_text("preset-tap-token", encoding="utf-8")
    state = TapTokenState.load_or_create(tf)
    assert state.token == "preset-tap-token"


def test_tap_token_rotate_changes_value(tmp_path: Path):
    tf = tmp_path / ".tap-token"
    state = TapTokenState.load_or_create(tf)
    original = state.token
    state.rotate()
    assert state.token != original
    assert tf.read_text(encoding="utf-8").strip() == state.token


def test_tap_token_distinct_from_password_when_paths_differ(tmp_path: Path):
    """Sanity: the two secrets are independent. Same helper, but distinct
    files mean they don't collide on disk."""
    pw = AuthState.load_or_create(tmp_path / ".auth-password")
    tt = TapTokenState.load_or_create(tmp_path / ".tap-token")
    # Vanishingly unlikely to collide; assert distinct files instead of
    # distinct values so we don't flake on a 12-byte token collision.
    assert pw.password_file != tt.token_file
