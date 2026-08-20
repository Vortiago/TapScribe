"""Disk → `/api/state` names, with nothing hand-fed.

Every other Voice test drives ONE link with its neighbours passed in by hand,
which is how two wiring bugs shipped green: the voice branch read the WAV-slug
list instead of the transcript's keys, and nothing noticed because every unit
test supplied `speaker_keys` itself. This file writes real files and asserts on
the far end, so a mis-wired link fails here even when each unit still passes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe import sessions, voices
from tapscribe.name_resolution import attach_people
from tapscribe.roster import record_occurrence
from tapscribe.session_paths import FILENAME_META_JSON, FILENAME_TRANSCRIPT_JSON

SESSION = "20260101T010000Z"
IDENTITY = "tray-system-audio-0011223344"
SLUG = "sysaudio"
PERSON = "p_voicetest"


@pytest.fixture(autouse=True)
def _clear_poll_caches():
    sessions._WAV_DESC_CACHE.clear()
    sessions._SESSION_JSON_CACHE.clear()
    yield
    sessions._WAV_DESC_CACHE.clear()
    sessions._SESSION_JSON_CACHE.clear()


@pytest.fixture
def rec_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # `RECORDINGS_DIR` is the whole isolation: `PeopleRegistry` resolves
    # `people.json` under it, and `attach_people` SAVES (sync auto-binds the
    # tap identity), so a fixture that missed this would rewrite the developer's
    # real registry.
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    return tmp_path


def _seed(root: Path, *, run_id: str, mapped_run: str | None) -> Path:
    """A diarized session as the pipeline would leave it on disk."""
    sd = root / SESSION
    sd.mkdir()
    seed_wav(sd / "2026-01-01T01-00-00Z_sysaudio_id01_u1.wav")
    record_occurrence(sd, identity=IDENTITY, name="System audio", recorded=True, wav="x.wav", mode="multi")
    # The Roster's slug is what maps the transcript key onto the identity.
    r = json.loads((sd / "session-roster.json").read_text(encoding="utf-8"))
    r[IDENTITY]["slug"] = SLUG
    (sd / "session-roster.json").write_text(json.dumps(r), encoding="utf-8")

    voices.record_voices(sd, identity=IDENTITY, run_id=run_id, spans={"A": []})
    # `record_voices` drops label-less entries, so write the shape directly when
    # the test needs a run stamp without spans.
    (sd / "session-voices.json").write_text(
        json.dumps({IDENTITY: {"run_id": run_id, "voices": {"A": {"spans": []}}}}), encoding="utf-8"
    )
    (sd / FILENAME_TRANSCRIPT_JSON).write_text(
        json.dumps({"transcribed_at": "2026-01-01T02:00:00Z", "speakers": [f"{SLUG}#A"], "segments": []}),
        encoding="utf-8",
    )
    meta: dict = {"label": "Standup"}
    if mapped_run is not None:
        meta["voices"] = {f"{IDENTITY}#A": {"person_id": PERSON, "run_id": mapped_run}}
    (sd / FILENAME_META_JSON).write_text(json.dumps(meta), encoding="utf-8")
    return sd


def _names(root: Path) -> dict[str, str]:
    (root / "people.json").write_text(
        json.dumps({"people": [{"id": PERSON, "name": "Alice Andersen", "identities": []}]}),
        encoding="utf-8",
    )
    listing = sessions.gather_sessions(current_session=SESSION)
    attach_people(listing, live_identities=set())
    return next(s for s in listing if s["session"] == SESSION)["names"]


def test_a_mapped_voice_reaches_api_state_as_its_person(rec_root: Path) -> None:
    """The whole chain: sidecar + meta map + transcript keys on disk → a name."""
    _seed(rec_root, run_id="r1", mapped_run="r1")

    assert _names(rec_root)[f"{SLUG}#A"] == "Alice Andersen"


def test_an_unmapped_voice_reaches_api_state_as_a_speaker_label(rec_root: Path) -> None:
    _seed(rec_root, run_id="r1", mapped_run=None)

    assert _names(rec_root)[f"{SLUG}#A"] == "Speaker A"


def test_a_mapping_stamped_against_a_superseded_run_is_not_applied(rec_root: Path) -> None:
    """The staleness check needs `voice_runs` to have survived the poll
    projection — hand-feeding it in a unit test proves nothing about that."""
    _seed(rec_root, run_id="r2", mapped_run="r1")

    assert _names(rec_root)[f"{SLUG}#A"] == "Speaker A"


def test_the_poll_projects_run_ids_off_the_real_sidecar(rec_root: Path) -> None:
    """`voice_runs` is read from disk by `gather_sessions`; every other test
    passes it in by hand."""
    _seed(rec_root, run_id="r7", mapped_run=None)

    listing = sessions.gather_sessions(current_session=SESSION)

    assert next(s for s in listing if s["session"] == SESSION)["voice_runs"] == {IDENTITY: "r7"}


def test_voice_keys_never_mint_a_person_across_a_poll(rec_root: Path) -> None:
    """`attach_people` PERSISTS what it syncs, twice a second."""
    _seed(rec_root, run_id="r1", mapped_run=None)
    listing = sessions.gather_sessions(current_session=SESSION)

    people = attach_people(listing, live_identities=set())

    assert not [p for p in people if "#" in "".join(p.get("identities") or [])]
