"""Disk → `/api/state` names, with nothing hand-fed.

Every other Voice test drives ONE link with its neighbours passed in by hand,
which is how two wiring bugs shipped green: the voice branch read the WAV-slug
list instead of the transcript's keys, and nothing noticed because every unit
test supplied `speaker_keys` itself. This file writes real files and asserts on
the far end, so a mis-wired link fails here even when each unit still passes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe import sessions, voices
from tapscribe.app import app, get_recorder
from tapscribe.name_resolution import attach_people
from tapscribe.roster import record_occurrence
from tapscribe.session_paths import FILENAME_META_JSON, FILENAME_TRANSCRIPT_JSON

SESSION = "20260101T010000Z"
IDENTITY = "tray-system-audio-0011223344"
SLUG = "sysaudio"
PERSON = "p_voicetest"
SURVIVOR = "p_survivor"


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


def _resolved_names() -> dict[str, str]:
    """The session's speaker-key → display-name map, off disk. Same resolution
    the transcript pane, the exports and the summarizer input cross."""
    listing = sessions.gather_sessions(current_session=SESSION)
    attach_people(listing, live_identities=set())
    return next(s for s in listing if s["session"] == SESSION)["names"]


def _names(root: Path) -> dict[str, str]:
    (root / "people.json").write_text(
        json.dumps({"people": [{"id": PERSON, "name": "Alice Andersen", "identities": []}]}),
        encoding="utf-8",
    )
    return _resolved_names()


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


# ---------------------------------------------------------------------------
# Removing a Person, over the real HTTP surface (#445). The registry is a leaf
# that knows nothing about sessions, so a fix wired only there still leaves the
# operator's merge silently breaking every Voice the absorbed id named.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(recorder_under_test) -> Iterator[TestClient]:
    """The People mutations are routes; drive them as one. Seeding reads
    `_config.RECORDINGS_DIR` inside the test — `recorder_under_test` owns it
    here, not `rec_root`."""
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _write_people(root: Path, rows: list[dict]) -> None:
    (root / "people.json").write_text(json.dumps({"people": rows}), encoding="utf-8")


def _two_alices(root: Path) -> None:
    """The duplicate the operator merges away: `PERSON` was minted by typing a
    name onto a Voice and owns no Identity, `SURVIVOR` is the same human already
    tapped in under their own. Different names, so the assertion cannot pass on
    the absorbed row's name."""
    _write_people(
        root,
        [
            {"id": PERSON, "name": "Alice (dup)", "identities": []},
            {"id": SURVIVOR, "name": "Alice Andersen", "identities": ["alice-laptop"]},
        ],
    )


def test_merging_a_voice_mapped_person_keeps_the_voice_named(client: TestClient) -> None:
    """The gate. Unrepointed, `resolve_session_names` finds no Person for the
    dead id and the Voice reverts to `Speaker A` — asserted through the
    resolution the transcript crosses, not by reading session-meta back."""
    root = _config.RECORDINGS_DIR
    _seed(root, run_id="r1", mapped_run="r1")
    _two_alices(root)
    # Resolve once first: the mapping is live, and the poll now holds the
    # pre-merge meta in `_SESSION_JSON_CACHE` — the state a running dashboard is
    # always in when the operator hits merge.
    assert _resolved_names()[f"{SLUG}#A"] == "Alice (dup)"

    r = client.post("/api/people/merge", json={"survivor": SURVIVOR, "absorbed": PERSON})

    assert r.status_code == 200
    assert _resolved_names()[f"{SLUG}#A"] == "Alice Andersen"


def test_merging_reattributes_the_voice_mapped_session_to_the_survivor(client: TestClient) -> None:
    """`_sessions_by_voice_pointer` reads the same pointer, so the People view
    counts the meeting for the survivor instead of emitting a dead id."""
    root = _config.RECORDINGS_DIR
    _seed(root, run_id="r1", mapped_run="r1")
    _two_alices(root)

    rows = client.post("/api/people/merge", json={"survivor": SURVIVOR, "absorbed": PERSON}).json()["people"]

    assert SESSION in next(p for p in rows if p["id"] == SURVIVOR)["sessions"]
    assert not [p for p in rows if p["id"] == PERSON]


def test_detaching_an_identity_leaves_the_voice_mapping_named(client: TestClient) -> None:
    """Detach needs no mirror walk: the Person survives under the same id, so
    the pointer stays valid. Repointing on detach would be the bug."""
    root = _config.RECORDINGS_DIR
    _seed(root, run_id="r1", mapped_run="r1")
    _write_people(
        root,
        [{"id": PERSON, "name": "Alice Andersen", "identities": ["alice-laptop", "alice-office"]}],
    )

    r = client.post(f"/api/people/{PERSON}/detach", json={"identity": "alice-office"})

    assert r.status_code == 200
    assert _resolved_names()[f"{SLUG}#A"] == "Alice Andersen"
