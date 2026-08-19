"""`session-voices.json` — the per-session, machine-written record of the Voices
a diarization run found inside each multi-person tap's identity (ADR-0021).

Sibling of `test_roster.py`: same per-session sidecar shape, same "a bad file
degrades to empty, never crashes the poll" contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tapscribe import voices

SYSAUDIO = "sysaudio-tray-9f2c1b"


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "20260819T100000Z"
    d.mkdir()
    return d


def _at(second: int) -> datetime:
    return datetime(2026, 8, 19, 10, 0, second, tzinfo=UTC)


def test_write_then_read_round_trips_voices_and_their_spans(session_dir: Path) -> None:
    """Two Voices under one identity, each with its speech spans in ABSOLUTE
    session time — the join in `session_merge` is an interval join against
    these, so the times must survive the disk round-trip exactly."""
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
    """An undiarized session is the NORMAL case — most sessions have no
    multi-person tap at all — so absence is `{}`, never an error."""
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
    """Every shape failure degrades to "no Voices", which means every segment
    keeps its plain identity key. A bad sidecar must never crash the merge or
    the 500 ms poll — the file is regenerable by re-running diarize."""
    (session_dir / "session-voices.json").write_text(raw, encoding="utf-8")
    assert voices.read_voices(session_dir) == {}


def test_recording_one_identity_leaves_a_siblings_run_id_untouched(session_dir: Path) -> None:
    """`run_id` is stamped PER IDENTITY. Re-diarizing one tap must not disturb
    another's stamp, or every Voice→Person mapping made against the sibling
    would read as superseded and silently stop being applied."""
    voices.record_voices(session_dir, identity=SYSAUDIO, run_id="run-1", spans={"A": [(_at(0), _at(5))]})
    voices.record_voices(session_dir, identity="mic-alice", run_id="run-2", spans={"A": [(_at(0), _at(9))]})

    voices.record_voices(session_dir, identity=SYSAUDIO, run_id="run-3", spans={"A": [(_at(1), _at(4))]})

    v = voices.read_voices(session_dir)
    assert v[SYSAUDIO]["run_id"] == "run-3"
    assert v["mic-alice"]["run_id"] == "run-2", "re-diarizing sysaudio bumped a sibling's stamp"
    assert v["mic-alice"]["voices"]["A"]["spans"][0]["end"] == "2026-08-19T10:00:09+00:00"


# ---------------------------------------------------------------------------
# Absorb — the one operation that ADDS audio to a session's time range, and so
# the one that can invalidate a Voice. (Deleting audio cannot: a span only ever
# attributes segments that fall inside it, so removing the segments leaves the
# span inert rather than wrong.)
# ---------------------------------------------------------------------------


def test_fold_keeps_both_sides_when_identities_are_disjoint() -> None:
    """Two sessions that recorded different people merge cleanly — each
    identity keeps its own Voices and its own `run_id`."""
    target = {"sysaudio": {"run_id": "run-t", "voices": {"A": {"spans": [{"start": "a", "end": "b"}]}}}}
    source = {"mic-bob": {"run_id": "run-s", "voices": {"A": {"spans": [{"start": "c", "end": "d"}]}}}}

    merged, collided = voices.fold_voices(target, source)

    assert collided == set()
    assert set(merged) == {"sysaudio", "mic-bob"}
    assert merged["sysaudio"]["run_id"] == "run-t"
    assert merged["mic-bob"]["run_id"] == "run-s"


def test_fold_drops_both_sides_of_a_colliding_identity() -> None:
    """`Speaker A` is SESSION-LOCAL. Two sessions can both hold `sysaudio`'s
    Voice A for entirely different humans, and nothing in the file says so —
    merging the labels would silently attribute one person's words to another.
    So a collision drops the identity from BOTH sides and reports it, leaving
    the tap unattributed until someone re-diarizes the merged session."""
    target = {"sysaudio": {"run_id": "run-t", "voices": {"A": {"spans": [{"start": "a", "end": "b"}]}}}}
    source = {"sysaudio": {"run_id": "run-s", "voices": {"A": {"spans": [{"start": "c", "end": "d"}]}}}}

    merged, collided = voices.fold_voices(target, source)

    assert collided == {"sysaudio"}
    assert merged == {}, "a collided identity must survive on neither side"
