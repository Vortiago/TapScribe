"""GET /api/sessions/{s}/files must report the OPEN WAV, not just compute it.

The listing gained a per-WAV `open` flag for playback (#191, ADR-0017), but the
flag is only useful if the endpoint actually feeds the recorder's live-tap set
into the walk. `api_session_files` already depended on the Recorder and marked
it `# noqa: ARG001` because it never read it, so "the flag exists" and "the
flag is true while a tap is writing" are two different claims. This pins the
second one at the HTTP boundary the dashboard actually calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

import anyio.from_thread
import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe.app import app, get_recorder
from tapscribe.recorder import ActiveStream, Recorder

OPEN_WAV = "20260101T000000Z__alice__abc.wav"
CLOSED_WAV = "20260101T001500Z__bob__def.wav"


def _register_active_tap(recorder: Recorder, session: str, filename: str) -> None:
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder.streams.register,
            ActiveStream(
                conn_id="conn-1",
                identity="alice",
                name="Alice",
                filename=filename,
                started_at=datetime.now(UTC),
                session=session,
            ),
        )


@pytest.fixture
def client(recorder_under_test):
    """HTTP client over the shared conftest `recorder_under_test`."""
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_files_endpoint_reports_the_wav_a_live_tap_is_writing(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    seed_session(root, "detached", [OPEN_WAV, CLOSED_WAV])
    _register_active_tap(recorder_under_test, "detached", OPEN_WAV)

    r = client.get("/api/sessions/detached/files")

    assert r.status_code == 200
    files = {f["name"]: f for f in r.json()["files"]}
    assert files[OPEN_WAV]["open"] is True
    assert files[CLOSED_WAV]["open"] is False


def test_files_endpoint_scopes_open_to_the_session_being_listed(client, recorder_under_test):
    """A tap writing into ANOTHER session must not mark a same-named WAV open.

    Recorder filenames carry a UTC stamp + speaker + ident, so two sessions can
    legitimately hold the same name (a Bridge's bracketed meeting reconnecting
    into a detached session is the live case). Matching on filename alone would
    disable playback on an idle session's WAV.
    """
    root = recorder_under_test.recordings_dir
    seed_session(root, "listed", [OPEN_WAV])
    seed_session(root, "elsewhere", [OPEN_WAV])
    _register_active_tap(recorder_under_test, "elsewhere", OPEN_WAV)

    r = client.get("/api/sessions/listed/files")

    assert r.status_code == 200
    files = {f["name"]: f for f in r.json()["files"]}
    assert files[OPEN_WAV]["open"] is False
