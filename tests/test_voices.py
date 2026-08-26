"""`session-voices.json` — the per-session record of the Voices a diarization
run found in each multi-person tap (ADR-0021). Sibling of `test_roster.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tapscribe import config, voices
from tapscribe.session_paths import FILENAME_META_JSON
from tapscribe.sessions import read_session_meta, repoint_voice_person, write_session_meta

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


# ---------------------------------------------------------------------------
# `repoint_voice_person` — the cross-session half of removing a Person (#445).
# A Voice-mapped Person owns no Identity, so the pointer is the only route to
# them; the walk keeps merge from stranding it.
# ---------------------------------------------------------------------------


def test_repoint_moves_every_session_that_named_the_old_person(recorder_under_test) -> None:
    write_session_meta("rp1", {"voices": {"tray-sys#A": {"person_id": "p_old", "run_id": "r1"}}})
    write_session_meta("rp2", {"voices": {"laptop#B": {"person_id": "p_old", "run_id": "r2"}}})

    assert repoint_voice_person("p_old", "p_new") == ["rp1", "rp2"]
    assert read_session_meta("rp1")["voices"]["tray-sys#A"]["person_id"] == "p_new"
    assert read_session_meta("rp2")["voices"]["laptop#B"]["person_id"] == "p_new"


def test_repoint_keeps_the_run_stamp_and_the_pointers_it_does_not_name(recorder_under_test) -> None:
    """The stamp is what makes a mapping apply (ADR-0021): drop it on the
    rewrite and the Voice the repoint just saved goes unnamed anyway. A sibling
    Voice mapped to somebody else is not this merge's business."""
    write_session_meta(
        "rp3",
        {
            "voices": {
                "tray-sys#A": {"person_id": "p_old", "run_id": "r1"},
                "tray-sys#B": {"person_id": "p_other", "run_id": "r1"},
            }
        },
    )

    assert repoint_voice_person("p_old", "p_new") == ["rp3"]
    assert read_session_meta("rp3")["voices"] == {
        "tray-sys#A": {"person_id": "p_new", "run_id": "r1"},
        "tray-sys#B": {"person_id": "p_other", "run_id": "r1"},
    }


def test_repoint_preserves_the_rest_of_the_meta_it_rewrites(recorder_under_test) -> None:
    """The walk re-emits the whole meta, so every operator-owned field on a
    touched session rides along. A repoint that ate the label would be a worse
    bug than the one it fixes."""
    write_session_meta(
        "rp4",
        {
            "label": "Standup",
            "aliases": {"tray-sys#A": "Chair"},
            "prompt": "meeting notes",
            "languages": ["no"],
            "voices": {"tray-sys#A": {"person_id": "p_old", "run_id": "r1"}},
        },
    )

    repoint_voice_person("p_old", "p_new")

    meta = read_session_meta("rp4")
    assert meta["label"] == "Standup"
    assert meta["aliases"] == {"tray-sys#A": "Chair"}
    assert meta["prompt"] == "meeting notes"
    assert meta["languages"] == ["no"]
    assert meta["voices"]["tray-sys#A"]["person_id"] == "p_new"


def test_repoint_writes_nothing_for_a_session_it_does_not_name(recorder_under_test) -> None:
    """A merge must not rewrite the archive: no `voices` map, or a map naming
    somebody else, means no write."""
    write_session_meta("rp5", {"label": "No voices here"})
    write_session_meta("rp6", {"voices": {"tray-sys#A": {"person_id": "p_other", "run_id": "r1"}}})
    root = config.RECORDINGS_DIR
    before = {s: (root / s / FILENAME_META_JSON).stat().st_ino for s in ("rp5", "rp6")}

    assert repoint_voice_person("p_old", "p_new") == []
    # `atomic_write_text` replaces the file, so a write is visible as a new
    # inode even when the bytes are unchanged.
    assert {s: (root / s / FILENAME_META_JSON).stat().st_ino for s in ("rp5", "rp6")} == before


def test_repoint_leaves_an_empty_archive_alone(recorder_under_test) -> None:
    assert repoint_voice_person("p_old", "p_new") == []


# ---------------------------------------------------------------------------
# Value-level coercion. `merge_session` PARSES these instants inline, so a
# shape-only check leaves a torn sidecar failing a whole transcribe job.
# ---------------------------------------------------------------------------

_SPAN = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:01:00Z"}


def test_a_span_whose_instants_do_not_parse_is_dropped(tmp_path: Path) -> None:
    bad = {"start": "not-a-time", "end": "nor-this"}
    voices.write_voices(
        tmp_path,
        {"ident": {"run_id": "r1", "voices": {"A": {"spans": [bad, _SPAN]}}}},
    )

    assert voices.read_voices(tmp_path)["ident"]["voices"]["A"]["spans"] == [_SPAN]


def test_a_non_string_run_id_reads_as_unstamped_on_both_paths() -> None:
    """`run_ids` is the poll's shortcut past `coerce_voices`; a truthy non-string
    leaking through it would silently discard a valid Voice→Person mapping."""
    raw = {"ident": {"run_id": 123, "voices": {"A": {"spans": [_SPAN]}}}}

    assert voices.run_ids(raw) == {"ident": ""}
    assert voices.coerce_voices(raw)["ident"]["run_id"] == ""
