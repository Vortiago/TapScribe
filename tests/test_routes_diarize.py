"""The diarization HTTP surface: run it, read the Voices, map one to a Person.

The mapping PUT lives on `routes/people.py` rather than here, because it can
CREATE a Person and `people.json` is mutated in exactly two places — those
routes and the `/api/state` sync. Its tests live here anyway: what it means is
a Voice mapping, and splitting the assertions from the rest of the flow would
hide the run-stamp contract they share.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import voices
from tapscribe.app import app, get_recorder
from tapscribe.people import PeopleRegistry
from tapscribe.session_paths import FILENAME_ROSTER_JSON
from tapscribe.sessions import read_session_meta
from tapscribe.tap_mode import TAP_MODE_MULTI

T0 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
def client(recorder_under_test):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def diarized(recorder_under_test) -> Path:
    """A session as a finished diarization leaves it: one multi-person tap with
    two Voices, no mapping yet."""
    session_dir = seed_session(
        recorder_under_test.recordings_dir,
        "s",
        [f"{T0.strftime('%Y-%m-%dT%H-%M-%SZ')}_them_sysaudio_0000abcd.wav"],
    )
    (session_dir / FILENAME_ROSTER_JSON).write_text(
        json.dumps(
            {
                "sysaudio": {
                    "name": "Them",
                    "source": "recorded",
                    "slug": "them",
                    "wavs": [],
                    "mode": TAP_MODE_MULTI,
                }
            }
        ),
        encoding="utf-8",
    )
    voices.record_voices(
        session_dir,
        identity="sysaudio",
        run_id="run-1",
        spans={
            "A": [(T0, T0 + timedelta(seconds=30))],
            "B": [(T0 + timedelta(seconds=30), T0 + timedelta(seconds=45))],
        },
    )
    return session_dir


def test_the_voices_body_names_the_tap_and_its_voices(client, diarized) -> None:
    body = client.get("/api/sessions/s/voices").json()

    assert body["session"] == "s"
    (tap,) = body["identities"]
    assert tap["identity"] == "sysaudio"
    assert tap["name"] == "Them"
    assert tap["run_id"] == "run-1"
    assert [v["key"] for v in tap["voices"]] == ["sysaudio#A", "sysaudio#B"]
    assert tap["voices"][0]["seconds"] == pytest.approx(30.0)
    assert tap["voices"][0]["person_id"] == ""


def test_an_undiarized_session_has_no_voices(client, recorder_under_test) -> None:
    seed_session(recorder_under_test.recordings_dir, "plain", [])

    body = client.get("/api/sessions/plain/voices").json()

    assert body["identities"] == []


def test_the_voices_body_reports_the_mapping_and_when_it_went_stale(client, diarized) -> None:
    """A mapping stamped with a superseded run is NOT applied, so the panel has
    to say so — otherwise the operator sees `Speaker A` back with no explanation
    and no reason to re-map."""
    client.put("/api/sessions/s/voices", json={"key": "sysaudio#A", "name": "Dana"})

    (tap,) = client.get("/api/sessions/s/voices").json()["identities"]
    voice = next(v for v in tap["voices"] if v["key"] == "sysaudio#A")
    assert voice["person_id"]
    assert voice["stale"] is False

    voices.record_voices(diarized, identity="sysaudio", run_id="run-2", spans={"A": [(T0, T0)]})

    (tap,) = client.get("/api/sessions/s/voices").json()["identities"]
    voice = next(v for v in tap["voices"] if v["key"] == "sysaudio#A")
    assert voice["stale"] is True, "a mapping from the previous run reads as current"


def test_mapping_a_voice_to_a_person_stamps_the_current_run(client, diarized) -> None:
    """A mapping is only applied while its stamp matches the sidecar's, so the
    route — not the client — is what puts the run on it."""
    registry = PeopleRegistry.load()
    person = registry.create("Dana")
    registry.save()

    r = client.put("/api/sessions/s/voices", json={"key": "sysaudio#A", "person_id": person["id"]})

    assert r.status_code == 200
    stored = read_session_meta("s")["voices"]["sysaudio#A"]
    assert stored == {"person_id": person["id"], "run_id": "run-1"}


def test_mapping_by_name_creates_the_person(client, diarized) -> None:
    """The enrollment point (ADR-0021): the operator reads `Speaker A` and types
    who it is. There is no bare create on the wire — a Person is never left
    unattached."""
    r = client.put("/api/sessions/s/voices", json={"key": "sysaudio#B", "name": "Robin"})

    assert r.status_code == 200
    person_id = read_session_meta("s")["voices"]["sysaudio#B"]["person_id"]
    assert PeopleRegistry.load().get(person_id)["name"] == "Robin"


def test_mapping_by_a_name_that_exists_still_creates_a_person(client, diarized) -> None:
    """Two people share a name more often than one person is typed twice, and
    silently folding a Voice into a namesake attributes their words to a
    stranger. Picking the existing Person is a different gesture — the dropdown."""
    PeopleRegistry.load().create("Robin")
    PeopleRegistry.load().save()

    client.put("/api/sessions/s/voices", json={"key": "sysaudio#A", "name": "Robin"})
    client.put("/api/sessions/s/voices", json={"key": "sysaudio#B", "name": "Robin"})

    meta = read_session_meta("s")["voices"]
    assert meta["sysaudio#A"]["person_id"] != meta["sysaudio#B"]["person_id"]


def test_clearing_a_mapping_removes_it(client, diarized) -> None:
    client.put("/api/sessions/s/voices", json={"key": "sysaudio#A", "name": "Dana"})

    r = client.put("/api/sessions/s/voices", json={"key": "sysaudio#A"})

    assert r.status_code == 200
    assert "sysaudio#A" not in read_session_meta("s").get("voices", {})


def test_an_unknown_person_is_a_404_not_a_dangling_pointer(client, diarized) -> None:
    r = client.put("/api/sessions/s/voices", json={"key": "sysaudio#A", "person_id": "nope"})

    assert r.status_code == 404
    assert "voices" not in read_session_meta("s")


def test_a_key_the_run_does_not_know_is_a_404(client, diarized) -> None:
    """The sidecar is the allowlist: it is what says which Voices exist and
    under which run, so a key it doesn't carry cannot be stamped correctly."""
    r = client.put("/api/sessions/s/voices", json={"key": "sysaudio#Z", "name": "Dana"})

    assert r.status_code == 404
    assert "voices" not in read_session_meta("s")


def test_a_key_for_an_identity_that_was_never_diarized_is_a_404(client, diarized) -> None:
    r = client.put("/api/sessions/s/voices", json={"key": "mic-alice#A", "name": "Dana"})

    assert r.status_code == 404


def test_a_missing_key_is_a_400(client, diarized) -> None:
    assert client.put("/api/sessions/s/voices", json={"name": "Dana"}).status_code == 400


def _row(client, session: str) -> dict:
    return next(s for s in client.get("/api/state").json()["sessions"] if s["session"] == session)


def test_the_poll_stamps_the_diarization_run(client, diarized) -> None:
    """The dashboard's only signal that a diarize landed: the runs themselves
    are a join input `/api/state` consumes and drops, so without this projection
    the Voices panel has nothing to key its lazy body on."""
    before = _row(client, "s")["voices_sig"]

    voices.record_voices(diarized, identity="sysaudio", run_id="run-2", spans={"A": [(T0, T0)]})

    assert before
    assert _row(client, "s")["voices_sig"] != before


def test_an_undiarized_session_has_an_empty_stamp(client, recorder_under_test) -> None:
    seed_session(recorder_under_test.recordings_dir, "plain", [])

    assert _row(client, "plain")["voices_sig"] == ""


def test_the_poll_never_ships_the_runs_themselves(client, diarized) -> None:
    """A join input, not payload — shipping it would put every identity's stamp
    in each 500 ms body."""
    assert "voice_runs" not in _row(client, "s")


def test_the_trigger_runs_the_stage(client, recorder_under_test, monkeypatch) -> None:
    import tapscribe.routes.diarize as route

    seen: list[str] = []

    async def _fake(recorder, req):  # noqa: ARG001
        seen.append(req.session)
        return {"ok": True, "session": req.session, "identities": [], "skipped": []}

    monkeypatch.setattr(route, "diarize_session", _fake)

    r = client.post("/api/sessions/s/diarize")

    assert r.status_code == 200
    assert seen == ["s"]
    assert r.json()["ok"] is True


def test_an_unfetched_model_is_a_400(client, recorder_under_test, monkeypatch) -> None:
    """An install problem the operator fixes by running preflight — not a 500
    from onnxruntime failing to open a path."""
    import tapscribe.routes.diarize as route
    from tapscribe.diarizers.base import DiarizerUnavailable

    async def _fake(recorder, req):  # noqa: ARG001
        raise DiarizerUnavailable("the speaker-embedding model is not at …")

    monkeypatch.setattr(route, "diarize_session", _fake)

    assert client.post("/api/sessions/s/diarize").status_code == 400


def test_an_engine_failure_is_a_502(client, recorder_under_test, monkeypatch) -> None:
    import tapscribe.routes.diarize as route
    from tapscribe.diarizers.base import DiarizerFailed

    async def _fake(recorder, req):  # noqa: ARG001
        raise DiarizerFailed("the model produced no embedding")

    monkeypatch.setattr(route, "diarize_session", _fake)

    assert client.post("/api/sessions/s/diarize").status_code == 502
