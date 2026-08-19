"""`session-voices.json` — the per-session record of the Voices a diarization
run found in each multi-person tap (ADR-0021). Sibling of `test_roster.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tapscribe import voices
from tapscribe.sessions import read_session_meta, write_session_meta

SYSAUDIO = "sysaudio-tray-9f2c1b"


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "20260819T100000Z"
    d.mkdir()
    return d


def _at(second: int) -> datetime:
    return datetime(2026, 8, 19, 10, 0, second, tzinfo=UTC)


def test_write_then_read_round_trips_voices_and_their_spans(session_dir: Path) -> None:
    """Spans are absolute-time, and `session_merge` interval-joins against them,
    so they must survive the round-trip exactly."""
    voices.record_voices(
        session_dir,
        identity=SYSAUDIO,
        run_id="run-abc123",
        spans={
            "A": [(_at(0), _at(5)), (_at(12), _at(20))],
            "B": [(_at(5), _at(12))],
        },
    )

    v = voices.read_voices(session_dir)

    assert set(v) == {SYSAUDIO}
    entry = v[SYSAUDIO]
    assert entry["run_id"] == "run-abc123"
    assert set(entry["voices"]) == {"A", "B"}
    assert entry["voices"]["A"]["spans"] == [
        {"start": "2026-08-19T10:00:00+00:00", "end": "2026-08-19T10:00:05+00:00"},
        {"start": "2026-08-19T10:00:12+00:00", "end": "2026-08-19T10:00:20+00:00"},
    ]
    assert entry["voices"]["B"]["spans"] == [
        {"start": "2026-08-19T10:00:05+00:00", "end": "2026-08-19T10:00:12+00:00"},
    ]


def test_missing_sidecar_reads_as_empty(session_dir: Path) -> None:
    """An undiarized session is the normal case, so absence is `{}`."""
    assert voices.read_voices(session_dir) == {}


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        '"a string"',
        '{"ident": "not a dict"}',
        '{"ident": {"voices": "not a dict"}}',
        '{"ident": {"voices": {"A": {"spans": [{"start": "x"}]}}}}',
        '{"ident": {"voices": {"A": {"spans": []}}}}',
        '{"": {"voices": {"A": {"spans": [{"start": "a", "end": "b"}]}}}}',
    ],
    ids=["torn", "list", "scalar", "entry", "voices", "half-span", "no-spans", "blank-ident"],
)
def test_malformed_sidecar_degrades_to_empty(session_dir: Path, raw: str) -> None:
    """Junk degrades to "no Voices" (every segment keeps its plain identity
    key) rather than crashing the merge or the 500 ms poll."""
    (session_dir / "session-voices.json").write_text(raw, encoding="utf-8")
    assert voices.read_voices(session_dir) == {}


def test_recording_one_identity_leaves_a_siblings_run_id_untouched(session_dir: Path) -> None:
    """Re-diarizing one tap must not bump a sibling's stamp, or every mapping
    made against that sibling reads as superseded and stops applying."""
    voices.record_voices(session_dir, identity=SYSAUDIO, run_id="run-1", spans={"A": [(_at(0), _at(5))]})
    voices.record_voices(session_dir, identity="mic-alice", run_id="run-2", spans={"A": [(_at(0), _at(9))]})

    voices.record_voices(session_dir, identity=SYSAUDIO, run_id="run-3", spans={"A": [(_at(1), _at(4))]})

    v = voices.read_voices(session_dir)
    assert v[SYSAUDIO]["run_id"] == "run-3"
    assert v["mic-alice"]["run_id"] == "run-2", "re-diarizing sysaudio bumped a sibling's stamp"
    assert v["mic-alice"]["voices"]["A"]["spans"][0]["end"] == "2026-08-19T10:00:09+00:00"


# ---------------------------------------------------------------------------
# Absorb — the only operation that adds audio to a session's time range, so the
# only one that can invalidate a Voice. Deleting audio cannot: it removes the
# segments a span would have attributed, leaving the span inert.
# ---------------------------------------------------------------------------


def test_fold_keeps_both_sides_when_identities_are_disjoint() -> None:
    """Different people on each side: both survive, each with its own run."""
    target = {"sysaudio": {"run_id": "run-t", "voices": {"A": {"spans": [{"start": "a", "end": "b"}]}}}}
    source = {"mic-bob": {"run_id": "run-s", "voices": {"A": {"spans": [{"start": "c", "end": "d"}]}}}}

    merged, collided = voices.fold_voices(target, source)

    assert collided == set()
    assert set(merged) == {"sysaudio", "mic-bob"}
    assert merged["sysaudio"]["run_id"] == "run-t"
    assert merged["mic-bob"]["run_id"] == "run-s"


def test_fold_drops_both_sides_of_a_colliding_identity() -> None:
    """Each session's Voice `A` is a different human, so a collision drops the
    identity from both sides rather than guessing."""
    target = {"sysaudio": {"run_id": "run-t", "voices": {"A": {"spans": [{"start": "a", "end": "b"}]}}}}
    source = {"sysaudio": {"run_id": "run-s", "voices": {"A": {"spans": [{"start": "c", "end": "d"}]}}}}

    merged, collided = voices.fold_voices(target, source)

    assert collided == {"sysaudio"}
    assert merged == {}, "a collided identity must survive on neither side"


# ---------------------------------------------------------------------------
# The operator Voice->Person map rides on session-meta.json, whose read and
# write paths each carry their own allowlist. Both must know the key.
# ---------------------------------------------------------------------------


def test_voices_map_survives_a_session_meta_round_trip(recorder_under_test) -> None:
    """Widening only the writer stores the map and strips it on every read —
    no error, no failing test, feature silently dead."""
    write_session_meta(
        "sv1", {"voices": {"tray-sys#A": {"person_id": "p1", "run_id": "r1"}}, "label": "Standup"}
    )

    meta = read_session_meta("sv1")
    assert meta["voices"] == {"tray-sys#A": {"person_id": "p1", "run_id": "r1"}}
    assert meta["label"] == "Standup"


def test_voices_map_drops_entries_without_a_person(recorder_under_test) -> None:
    write_session_meta("sv2", {"voices": {"tray-sys#A": {"run_id": "r1"}, "": {"person_id": "p1"}}})

    assert read_session_meta("sv2").get("voices", {}) == {}


def test_editing_another_meta_field_preserves_the_voices_map(recorder_under_test) -> None:
    """write_session_meta is merge-preserving; renaming must not drop the map."""
    write_session_meta("sv3", {"voices": {"tray-sys#A": {"person_id": "p1", "run_id": "r1"}}})
    write_session_meta("sv3", {"label": "Renamed"})

    meta = read_session_meta("sv3")
    assert meta["label"] == "Renamed"
    assert meta["voices"] == {"tray-sys#A": {"person_id": "p1", "run_id": "r1"}}
