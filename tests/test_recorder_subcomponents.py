"""Tests for the Recorder's sub-components — ActiveStreams, JobTracker,
LiveTranscripts, SecretFile.

These are small concurrency primitives that encapsulate a dict/lock or
deque so route handlers don't have to acquire locks manually. Each is
unit-testable without the FastAPI app or any real model.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
        started_at=datetime.now(UTC),
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
        ActiveStream(conn_id="a", identity="i", name="n", filename="f", started_at=datetime.now(UTC))
    )
    await streams.remove("a")
    assert (await streams.snapshot()) == []


@pytest.mark.asyncio
async def test_active_streams_update_bytes_increments():
    streams = ActiveStreams()
    await streams.register(
        ActiveStream(conn_id="a", identity="i", name="n", filename="f", started_at=datetime.now(UTC))
    )
    await streams.update_bytes("a", 640)
    snap = await streams.snapshot()
    assert snap[0].bytes_received == 640


@pytest.mark.asyncio
async def test_active_streams_update_bytes_unknown_id_is_noop():
    streams = ActiveStreams()
    # Should not raise — the WS handler races against close
    await streams.update_bytes("unknown", 100)


@pytest.mark.asyncio
async def test_active_streams_update_bytes_carries_level_when_provided():
    """The dashboard's per-tap volume meter reads `level` off the active
    stream snapshot, populated by TapFanOut on each PCM frame. Update
    must accept the new field via the level kwarg and persist it; absent
    a value, the previous level survives so a transient missing kwarg
    doesn't reset the meter mid-utterance."""
    streams = ActiveStreams()
    await streams.register(
        ActiveStream(conn_id="a", identity="i", name="n", filename="f", started_at=datetime.now(UTC))
    )
    await streams.update_bytes("a", 640, level=0.42)
    snap = await streams.snapshot()
    assert snap[0].bytes_received == 640
    assert snap[0].level == pytest.approx(0.42)

    # A subsequent update without level should not clobber the stored
    # value — the recorder doesn't always have a fresh level to report
    # (e.g. resume path before the first new frame).
    await streams.update_bytes("a", 1280)
    snap = await streams.snapshot()
    assert snap[0].bytes_received == 1280
    assert snap[0].level == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_active_streams_update_buffer_transcription_persists_text():
    """The dashboard's per-tap in-flight indicator reads
    `buffer_transcription` off the active stream snapshot, populated by
    TapFanOut whenever WlK pushes a new buffer_transcription via the
    relay's on_buffer callback. Empty default + idempotent set + works
    on the same lock as the other update_* methods."""
    streams = ActiveStreams()
    await streams.register(
        ActiveStream(
            conn_id="a",
            identity="i",
            name="n",
            filename="f",
            started_at=datetime.now(UTC),
        )
    )
    snap = await streams.snapshot()
    assert snap[0].buffer_transcription == ""

    await streams.update_buffer_transcription("a", "hello world in flight")
    snap = await streams.snapshot()
    assert snap[0].buffer_transcription == "hello world in flight"

    # Subsequent set with the same value is a no-op (idempotent).
    await streams.update_buffer_transcription("a", "hello world in flight")
    snap = await streams.snapshot()
    assert snap[0].buffer_transcription == "hello world in flight"

    # Clearing back to "" (text committed to lines) is reflected.
    await streams.update_buffer_transcription("a", "")
    snap = await streams.snapshot()
    assert snap[0].buffer_transcription == ""


@pytest.mark.asyncio
async def test_active_streams_update_buffer_transcription_unknown_id_is_noop():
    """Same race semantics as update_bytes / update_lag: a tap whose
    WS handler raced against close() and called us after the entry was
    removed must not raise."""
    streams = ActiveStreams()
    await streams.update_buffer_transcription("nobody-home", "should not raise")


@pytest.mark.asyncio
async def test_active_streams_update_gate_open_persists_state():
    """The dashboard's per-tap row shows whether TapScribe is actively
    forwarding audio (gate open) or filtering silence (gate closed).
    The flag must round-trip via the dataclass field and be settable
    via the dedicated update method."""
    streams = ActiveStreams()
    await streams.register(
        ActiveStream(
            conn_id="g1",
            identity="i",
            name="n",
            filename="f",
            started_at=datetime.now(UTC),
        )
    )
    # Default — gate closed (no audio forwarded yet).
    snap = await streams.snapshot()
    assert snap[0].gate_open is False

    await streams.update_gate_open("g1", True)
    snap = await streams.snapshot()
    assert snap[0].gate_open is True

    await streams.update_gate_open("g1", False)
    snap = await streams.snapshot()
    assert snap[0].gate_open is False


@pytest.mark.asyncio
async def test_active_streams_update_gate_open_unknown_id_is_noop():
    streams = ActiveStreams()
    await streams.update_gate_open("nobody-home", True)


@pytest.mark.asyncio
async def test_active_streams_apply_rejects_unknown_field():
    """The `_apply` helper that backs every update_* method must
    reject typo'd kwargs at runtime so a misspelt field name doesn't
    silently set a phantom attribute on the dataclass."""
    streams = ActiveStreams()
    await streams.register(
        ActiveStream(
            conn_id="t1",
            identity="i",
            name="n",
            filename="f",
            started_at=datetime.now(UTC),
        )
    )
    with pytest.raises(AttributeError):
        await streams._apply("t1", gate_oppen=True)  # noqa: SLF001


# ---------------------------------------------------------------------------
# JobTracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_tracker_claim_returns_true_when_free():
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(UTC)
    )
    assert await tracker.claim(state) is True


@pytest.mark.asyncio
async def test_job_tracker_claim_returns_false_when_already_claimed():
    """The 'one job per session' rule lives in JobTracker, not in each
    route handler."""
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(UTC)
    )
    await tracker.claim(state)
    second = JobState(session="s1", kind="strip", current=0, total=3, started_at=datetime.now(UTC))
    assert await tracker.claim(second) is False


@pytest.mark.asyncio
async def test_job_tracker_release_allows_reclaim():
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(UTC)
    )
    await tracker.claim(state)
    await tracker.release("s1")
    assert await tracker.claim(state) is True


@pytest.mark.asyncio
async def test_job_tracker_update_modifies_fields():
    tracker = JobTracker()
    state = JobState(
        session="s1", kind="transcribe", current=0, total=5, started_at=datetime.now(UTC)
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
