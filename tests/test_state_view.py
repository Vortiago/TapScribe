"""Unit contract for the State view (#229): the /api/state read model.

`GET /api/state` is the dashboard's ~2 Hz poll and the payload it returns was
assembled inline in `app.py`: config reads, the People join, the
default-override counts, the active-row overlay, the JSON serialization and the
ETag. Extracted into `tapscribe/state_view.py` so the projection is testable
without a route, a Recorder or a TestClient: everything it needs arrives as
snapshotted arguments (the route owns the thread hops and the 304 branch).

What is pinned here is the part `test_routes.py` cannot see cheaply: the ETag
contract (equal inputs give the same tag, ANY changed input gives a different
one, because the tag is a digest of the body) and the two derivations that have
no other owner (override counts, the active-row overlay with its byte
bucketing).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from conftest import repoint_config_files  # type: ignore[import-not-found]

from tapscribe.people import PeopleRegistry
from tapscribe.recorder import ActiveStream, TapSetting
from tapscribe.state_view import TAP_BYTES_BUCKET, active_rows, build_state_blob


def _blob(**overrides):
    """`build_state_blob` with every argument defaulted to an empty snapshot."""
    kwargs = dict(
        current_session="20260101T000000Z",
        active=[],
        sessions_list=[],
        registry=PeopleRegistry([]),
        occs=[],
        live_identities=set(),
        live_feed=[],
        live_info={"state": "stopped"},
        live_log=[],
        live_supports_native_vad=False,
        recording_enabled=True,
        backend="cpu",
        available_backends=["cpu"],
    )
    kwargs.update(overrides)
    return build_state_blob(**kwargs)


def test_state_blob_is_json_with_an_etag_over_its_own_bytes(tmp_path, monkeypatch):
    repoint_config_files(monkeypatch, tmp_path)
    body, etag = _blob()
    payload = json.loads(body)
    assert payload["current_session"] == "20260101T000000Z"
    assert payload["recording_enabled"] is True
    assert etag.startswith('W/"') and etag.endswith('"')
    # Same inputs, same tag: the poll's 304 path depends on this being stable
    # across ticks when nothing changed.
    assert _blob()[1] == etag


def test_state_blob_etag_changes_when_any_input_changes(tmp_path, monkeypatch):
    repoint_config_files(monkeypatch, tmp_path)
    _, baseline = _blob()
    assert _blob(recording_enabled=False)[1] != baseline
    assert _blob(current_session="20260101T010000Z")[1] != baseline
    assert _blob(live_info={"state": "running"})[1] != baseline
    assert _blob(backend="cuda")[1] != baseline


def test_state_blob_counts_per_session_default_overrides(tmp_path, monkeypatch):
    """`default_override_counts` tells the config card how many sessions
    override each global default. A summarizer override is EITHER a source or a
    prompt, so a session that sets both still counts once."""
    repoint_config_files(monkeypatch, tmp_path)
    sessions = [
        {"session": "a", "session_meta": {"prompt": "hi"}},
        {"session": "b", "session_meta": {"prompt": "yo", "hotwords": "TapScribe"}},
        {"session": "c", "session_meta": {"summary_source": "local", "summary_prompt": "x"}},
        {"session": "d", "session_meta": {}},
        {"session": "e"},
    ]
    # `occs` is parallel to `sessions_list` (one roster per session); empty
    # rosters keep the People join a no-op so the counts are what's under test.
    body, _ = _blob(sessions_list=sessions, occs=[{} for _ in sessions])
    assert json.loads(body)["default_override_counts"] == {
        "prompt": 2,
        "hotwords": 1,
        "summarizer": 1,
    }


def test_active_rows_overlay_tap_settings_and_bucket_bytes():
    """Each open tap's row carries the operator's per-identity record/live
    preference (not the value captured at WS open) and a bucketed byte count:
    the raw counter is bumped per 20 ms frame, so shipping it verbatim would
    bust the response ETag on every poll of a quiet-but-open tap (#217)."""
    streams = [
        ActiveStream(
            conn_id="c1",
            identity="alice",
            name="Alice",
            filename="a.wav",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            session="s1",
            record=True,
            live=True,
            level=0.123456,
            bytes_received=TAP_BYTES_BUCKET + 10,
        )
    ]
    rows = active_rows(streams, {"alice": TapSetting(record=False, live=True)}.get)
    assert len(rows) == 1
    assert rows[0]["record"] is False
    assert rows[0]["live"] is True
    assert rows[0]["level"] == 0.12
    assert rows[0]["bytes_received"] == TAP_BYTES_BUCKET


def test_active_rows_bucketing_rounds_to_nearest():
    """Round to NEAREST bucket, not down: the constant is centred on
    TAP_BYTES_BUCKET // 2 so a tap just past the halfway mark reports the next
    bucket rather than under-reporting by almost a whole one."""

    def row(byte_count):
        stream = ActiveStream(
            conn_id="c1",
            identity="a",
            name="A",
            filename="a.wav",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            session="s",
            record=True,
            live=True,
            level=0.0,
            bytes_received=byte_count,
        )
        return active_rows([stream], lambda _identity: TapSetting(record=True, live=True))[0]

    assert row(0)["bytes_received"] == 0
    assert row(TAP_BYTES_BUCKET // 2 - 1)["bytes_received"] == 0
    assert row(TAP_BYTES_BUCKET // 2)["bytes_received"] == TAP_BYTES_BUCKET
    assert row(TAP_BYTES_BUCKET * 3)["bytes_received"] == TAP_BYTES_BUCKET * 3
