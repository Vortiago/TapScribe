"""Roster — the per-session durable record of which Identity appeared, with
the bridge display name, recorded/live source, and the WAV(s) it produced.

The Roster is what makes the FULL bridge identity recoverable for a recorded
occurrence (the WAV filename only carries the lossy `safe_name(identity)[:10]`
slug), so it's the cross-session join key the People Registry resolves through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tapscribe import roster
from tapscribe.session_paths import FILENAME_ROSTER_JSON


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "20260626T100000Z"
    d.mkdir()
    return d


def test_missing_roster_reads_as_empty(session_dir: Path) -> None:
    assert roster.read_roster(session_dir) == {}


def test_record_then_read_round_trips_full_identity(session_dir: Path) -> None:
    roster.record_occurrence(
        session_dir,
        identity="alice-livekit-9f2c1b",
        name="Alice Havso",
        recorded=True,
        wav="2026-06-26T10-00-00Z_Alice_Havso_alicelivek_ab12cd34.wav",
    )
    r = roster.read_roster(session_dir)
    assert set(r) == {"alice-livekit-9f2c1b"}
    e = r["alice-livekit-9f2c1b"]
    assert e["name"] == "Alice Havso"
    assert e["source"] == "recorded"
    # The slug is the bridge between the slug-keyed transcript and the
    # identity-keyed registry — it's recovered from the WAV filename.
    assert e["slug"] == "Alice_Havso"
    assert e["wavs"] == ["2026-06-26T10-00-00Z_Alice_Havso_alicelivek_ab12cd34.wav"]


def test_several_identities_coexist_in_one_session(session_dir: Path) -> None:
    roster.record_occurrence(
        session_dir, identity="alice", name="Alice", recorded=True, wav="a_Alice_alice_1.wav"
    )
    roster.record_occurrence(
        session_dir, identity="system", name="Them", recorded=True, wav="a_Them_system_1.wav"
    )
    assert set(roster.read_roster(session_dir)) == {"alice", "system"}


def test_repeated_recorded_wavs_accrue_and_dedupe(session_dir: Path) -> None:
    for wav in ("t_Bob_bob_1.wav", "t_Bob_bob_2.wav", "t_Bob_bob_1.wav"):
        roster.record_occurrence(session_dir, identity="bob", name="Bob", recorded=True, wav=wav)
    assert roster.read_roster(session_dir)["bob"]["wavs"] == ["t_Bob_bob_1.wav", "t_Bob_bob_2.wav"]


def test_live_only_occurrence_has_no_wav(session_dir: Path) -> None:
    roster.record_occurrence(session_dir, identity="carol", name="Carol", recorded=False)
    e = roster.read_roster(session_dir)["carol"]
    assert e["source"] == "live"
    assert e["wavs"] == []


def test_recorded_never_downgrades_to_live(session_dir: Path) -> None:
    roster.record_occurrence(
        session_dir, identity="dave", name="Dave", recorded=True, wav="t_Dave_dave_1.wav"
    )
    # A later live-only presence (record off) must not erase the recorded source.
    roster.record_occurrence(session_dir, identity="dave", name="Dave", recorded=False)
    assert roster.read_roster(session_dir)["dave"]["source"] == "recorded"


def test_a_nonempty_name_updates_a_blank_one(session_dir: Path) -> None:
    roster.record_occurrence(session_dir, identity="erin", name="", recorded=False)
    roster.record_occurrence(session_dir, identity="erin", name="Erin", recorded=False)
    assert roster.read_roster(session_dir)["erin"]["name"] == "Erin"


def test_torn_or_garbage_file_reads_as_empty(session_dir: Path) -> None:
    (session_dir / FILENAME_ROSTER_JSON).write_text("{ not json", encoding="utf-8")
    assert roster.read_roster(session_dir) == {}
    # A non-dict top level is also ignored rather than crashing the poll.
    (session_dir / FILENAME_ROSTER_JSON).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert roster.read_roster(session_dir) == {}
