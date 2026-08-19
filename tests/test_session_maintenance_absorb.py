"""What `absorb_session` must carry across besides the WAVs.

Two things the WAV move alone loses:

  * **The Roster.** `session-roster.json` is the ONLY place a recorded
    occurrence's FULL bridge Identity survives — the filename carries just
    `safe_name(identity)[:10]` (`parse_wav_speaker_ident`), and the roster is
    written by the tap path alone (`roster.record_occurrence`), never rebuilt
    by transcribe/merge. Absorb ends in `shutil.rmtree(source_dir)`, so a
    roster left behind is destroyed while the WAVs it describes live on in the
    target — ADR-0009's cross-session join key gone irreversibly.
  * **The plain-text merged transcript.** `batch_transcribe` writes
    `session-transcript.json` AND `session-transcript.txt`; invalidating only
    the JSON leaves a stale `.txt` on disk that is missing every absorbed WAV,
    while the call reports `transcript_invalidated: True`.

Seam under test: `session_maintenance.absorb_session` directly (the same
boundary the route handler calls), with rosters seeded through the tap path's
own `roster.record_occurrence` rather than hand-written JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import session_maintenance, voices
from tapscribe.roster import read_roster, record_occurrence
from tapscribe.session_paths import FILENAME_TRANSCRIPT_JSON, FILENAME_TRANSCRIPT_TXT
from tapscribe.sessions import known_names_for_session

# Recorder-format filenames: <ISO stamp>_<speaker slug>_<ident[:10]>_<uuid8>.wav
ALICE_WAV = "2026-01-01T00-00-00Z_Alice_Andersen_sc-alice-_aaaaaaaa.wav"
BOB_WAV = "2026-01-01T01-00-00Z_Bob_Bergman_sc-bob-us_bbbbbbbb.wav"
BOB_WAV_2 = "2026-01-01T01-05-00Z_Bob_Bergman_sc-bob-us_cccccccc.wav"

# The untruncated bridge Identities. Both start with the same 10 characters the
# WAV filename would keep, so the filename slug cannot tell them apart — the
# exact collision ADR-0009 introduced the Roster to avoid.
ALICE_IDENTITY = "sc-alice-9f8e7d6c5b4a3210"
BOB_IDENTITY = "sc-bob-user-0011223344556677"


@pytest.fixture
def rec_root(recorder_under_test) -> Path:
    """`recorder_under_test` (tests/conftest.py) points `config.RECORDINGS_DIR`
    at a tmpdir; expose it so these direct-call tests can seed session folders
    the resolvers will find."""
    return Path(recorder_under_test.recordings_dir)


# ---------------------------------------------------------------------------
# Roster carry-over
# ---------------------------------------------------------------------------


def test_absorb_carries_the_source_roster_into_the_target(rec_root: Path):
    target = seed_session(rec_root, "tgt", [ALICE_WAV])
    source = seed_session(rec_root, "src", [BOB_WAV])
    record_occurrence(target, identity=ALICE_IDENTITY, name="Alice Andersen", recorded=True, wav=ALICE_WAV)
    record_occurrence(source, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV)

    result = session_maintenance.absorb_session("tgt", "src")

    merged = read_roster(target)
    assert set(merged) == {ALICE_IDENTITY, BOB_IDENTITY}, (
        "the source's full bridge Identity is unrecoverable once absorb rmtree's the source folder"
    )
    assert merged[BOB_IDENTITY]["name"] == "Bob Bergman"
    assert merged[BOB_IDENTITY]["wavs"] == [BOB_WAV]
    assert merged[BOB_IDENTITY]["slug"] == "Bob_Bergman"
    assert result["roster_merged"] == 1


def test_absorb_keeps_the_absorbed_speaker_resolvable_by_name(rec_root: Path):
    """The behaviour the roster exists for (ADR-0009): after the absorb, the
    target session still resolves the absorbed speaker to the bridge-sent
    display name rather than falling back to the truncated filename slug."""
    seed_session(rec_root, "tgt", [ALICE_WAV])
    source = seed_session(rec_root, "src", [BOB_WAV])
    record_occurrence(source, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV)

    session_maintenance.absorb_session("tgt", "src")

    assert "Bob Bergman" in known_names_for_session("tgt")


def test_absorb_unions_wav_lists_for_an_identity_present_in_both_sessions(rec_root: Path):
    """Same person recorded in both sessions: the target's own entry wins on
    the scalar fields (mirroring the alias merge), but the WAV lists must UNION
    — the source's WAVs physically live in the target now, so an entry that
    doesn't list them describes audio it no longer accounts for."""
    target = seed_session(rec_root, "tgt", [BOB_WAV])
    source = seed_session(rec_root, "src", [BOB_WAV_2])
    record_occurrence(target, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV)
    record_occurrence(source, identity=BOB_IDENTITY, name="bob (stale)", recorded=True, wav=BOB_WAV_2)

    result = session_maintenance.absorb_session("tgt", "src")

    entry = read_roster(target)[BOB_IDENTITY]
    assert entry["name"] == "Bob Bergman", "target wins on conflict, as for aliases"
    assert sorted(entry["wavs"]) == sorted([BOB_WAV, BOB_WAV_2])
    assert result["roster_merged"] == 1


def test_absorb_without_a_source_roster_leaves_the_target_roster_untouched(rec_root: Path):
    target = seed_session(rec_root, "tgt", [ALICE_WAV])
    seed_session(rec_root, "src", [BOB_WAV])
    record_occurrence(target, identity=ALICE_IDENTITY, name="Alice Andersen", recorded=True, wav=ALICE_WAV)

    result = session_maintenance.absorb_session("tgt", "src")

    assert set(read_roster(target)) == {ALICE_IDENTITY}
    assert result["roster_merged"] == 0


def test_absorb_adopts_the_source_roster_when_the_target_has_none(rec_root: Path):
    target = seed_session(rec_root, "tgt", [ALICE_WAV])
    source = seed_session(rec_root, "src", [BOB_WAV])
    record_occurrence(source, identity=BOB_IDENTITY, name="Bob Bergman", recorded=True, wav=BOB_WAV)

    result = session_maintenance.absorb_session("tgt", "src")

    assert set(read_roster(target)) == {BOB_IDENTITY}
    assert result["roster_merged"] == 1


# ---------------------------------------------------------------------------
# Plain-text merged transcript invalidation
# ---------------------------------------------------------------------------


def test_absorb_invalidates_the_plain_text_transcript_alongside_the_json(rec_root: Path):
    target = seed_session(rec_root, "tgt", [ALICE_WAV])
    seed_session(rec_root, "src", [BOB_WAV])
    (target / FILENAME_TRANSCRIPT_JSON).write_text('{"stale": true}', encoding="utf-8")
    (target / FILENAME_TRANSCRIPT_TXT).write_text("[00:00:00] Alice: stale\n", encoding="utf-8")

    result = session_maintenance.absorb_session("tgt", "src")

    assert result["transcript_invalidated"] is True
    assert not (target / FILENAME_TRANSCRIPT_JSON).exists()
    assert not (target / FILENAME_TRANSCRIPT_TXT).exists(), (
        "reporting transcript_invalidated while a stale .txt missing every absorbed WAV survives"
    )


# ---------------------------------------------------------------------------
# Voice carry-over (ADR-0021)
#
# Absorb is the one operation that ADDS audio to a session's time range, which
# makes it the one that can invalidate a Voice. It is also destructive: the
# source folder is rmtree'd, so a `session-voices.json` left behind is gone
# while the WAVs it describes live on in the target.
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)


def test_absorb_carries_voices_when_the_identities_are_disjoint(rec_root: Path):
    """Two sessions that diarized different taps merge cleanly — each identity
    keeps its own Voices and its own `run_id`."""
    target = seed_session(rec_root, "tgt", [ALICE_WAV])
    source = seed_session(rec_root, "src", [BOB_WAV])
    voices.record_voices(target, identity=ALICE_IDENTITY, run_id="run-t", spans={"A": [(_T0, _T1)]})
    voices.record_voices(source, identity=BOB_IDENTITY, run_id="run-s", spans={"A": [(_T0, _T1)]})

    result = session_maintenance.absorb_session("tgt", "src")

    merged = voices.read_voices(target)
    assert set(merged) == {ALICE_IDENTITY, BOB_IDENTITY}, (
        "the source's Voices are destroyed with its folder unless absorb carries them"
    )
    assert merged[ALICE_IDENTITY]["run_id"] == "run-t"
    assert merged[BOB_IDENTITY]["run_id"] == "run-s"
    assert result["voices_collided"] == []


def test_absorb_drops_a_colliding_identitys_voices_from_both_sides(rec_root: Path):
    """A Voice label is SESSION-LOCAL, so the target's Voice `A` for an identity
    and the source's are different humans with nothing on disk to say so.
    Keeping either would attribute one person's words to another; absorb drops
    both and reports the identity so the merged session can be re-diarized."""
    target = seed_session(rec_root, "tgt", [ALICE_WAV])
    source = seed_session(rec_root, "src", [BOB_WAV])
    voices.record_voices(target, identity=ALICE_IDENTITY, run_id="run-t", spans={"A": [(_T0, _T1)]})
    voices.record_voices(source, identity=ALICE_IDENTITY, run_id="run-s", spans={"A": [(_T0, _T1)]})

    result = session_maintenance.absorb_session("tgt", "src")

    assert voices.read_voices(target) == {}, "a collided identity must survive on neither side"
    assert result["voices_collided"] == [ALICE_IDENTITY]
