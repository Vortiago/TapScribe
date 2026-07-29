"""#368 (destructive-session step) — the four destructive routes stop
re-implementing the same `resolve -> offload -> log -> spread` tail, and the
whole-session delete joins the domain-error seam.

The People step (PR #394) collapsed the CRUD trio. This is #368's second
checkbox: `api_session_audio_delete`, `api_session_absorb`, `api_session_delete`
and `api_wav_delete` each carry their own copy of the completion tail, and
`api_session_delete` additionally hand-rolls `shutil.rmtree` behind
`HTTPException(500, f"delete failed: {e}")` — duplicating what
`session_maintenance` already does properly, and bypassing the
`SessionDeleteError -> 500` registration that #228 put in `_DOMAIN_ERROR_STATUS`
(registered since #228, and until now reachable by no route at all).

WHY THIS CONTRACT IS MOSTLY GUARDRAIL: the four routes look alike but are NOT
alike, and the whole risk of "factor the shared tail" is a helper that flattens
the differences. `test_routes.py` already pins a good deal of their behaviour
(the current-session refusal table, the audio-delete job-slot hold, absorb's
alias merge, the wav source variants) and must keep passing. What it does NOT
pin — and what a uniform helper would silently break — is pinned here:

  * `api_session_delete` answers `{"ok": True, "deleted": <session>}`. It is the
    ONE route that does not spread a summary. A `{"ok": True, **summary}` helper
    is the natural collapse and would change the API behind a green gate. This
    is the same un-gated-response-key trap the People step hit with `detached`.
  * it releases the session's job slot after deleting, which nothing asserts.
  * its failure path answers 500 with a `delete failed:` detail, which nothing
    exercises.

Deliberately NOT pinned — the implementer's call: whether the tail becomes one
helper or several, whether it is a function / decorator / context manager, what
it is called, and whether `absorb` (today the only one that does NOT offload to
a thread) starts doing so.
"""

from __future__ import annotations

import ast
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe.app import app, get_recorder
from tapscribe.recorder import Recorder

DESTRUCTIVE_ROUTES = (
    "api_session_audio_delete",
    "api_session_absorb",
    "api_session_delete",
    "api_wav_delete",
)


@pytest.fixture
def client(recorder_under_test: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _route_defs() -> dict[str, ast.AST]:
    from tapscribe import app as app_module

    tree = ast.parse(Path(app_module.__file__).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in DESTRUCTIVE_ROUTES
    }


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        c
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name
    ]


def test_all_four_destructive_routes_are_present() -> None:
    """Guards every scan below: a renamed handler would make them vacuous."""
    assert set(_route_defs()) == set(DESTRUCTIVE_ROUTES)


# ---------------------------------------------------------------------------
# The residue itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", DESTRUCTIVE_ROUTES)
def test_route_does_not_hand_roll_its_own_completion_log(route: str) -> None:
    """Each route ends with its own multi-line `print("[tapscribe] ...")`. That
    is the `log` limb of the duplicated tail — the one piece all four share
    verbatim in shape. Routing it through the shared seam is what makes the
    other limbs worth factoring."""
    assert not _calls_named(_route_defs()[route], "print"), (
        f"{route} still builds its own completion log inline"
    )


def test_session_delete_uses_the_domain_error_seam() -> None:
    """`api_session_delete` is the only destructive route that still raises
    `HTTPException` for a FAILURE (as opposed to the request-shape 400s, which
    stay with the routes). `SessionDeleteError` is already registered at 500 in
    `_DOMAIN_ERROR_STATUS`; the route should use it rather than translating by
    hand — that is the whole point of the map #228 introduced."""
    raises = [
        r
        for r in ast.walk(_route_defs()["api_session_delete"])
        if isinstance(r, ast.Raise)
        and isinstance(r.exc, ast.Call)
        and isinstance(r.exc.func, ast.Name)
        and r.exc.func.id == "HTTPException"
    ]
    assert not raises, "api_session_delete still translates its failure to HTTPException by hand"


# ---------------------------------------------------------------------------
# The un-gated surface a uniform helper would flatten.
# ---------------------------------------------------------------------------


def test_session_delete_response_shape_is_not_a_summary_spread(
    client: TestClient, recorder_under_test: Recorder
) -> None:
    """The ONE route whose body is not `{"ok": True, **summary}`. Nothing in the
    suite asserts this today, so a helper that returns a uniform spread changes
    the API with every gate green."""
    seed_session(recorder_under_test.recordings_dir, "doomed", ["20260101T000000Z__alice__abc.wav"])
    r = client.delete("/api/sessions/doomed")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "deleted": "doomed"}
    assert not (recorder_under_test.recordings_dir / "doomed").exists()


def test_session_delete_releases_the_job_slot(client: TestClient, recorder_under_test: Recorder) -> None:
    """`recorder.jobs.release(session)` runs after the tree is gone — the slot
    would otherwise leak for a session id that no longer exists, and a later
    session reusing the id would 409 forever. Unasserted until now."""
    seed_session(recorder_under_test.recordings_dir, "doomed", ["20260101T000000Z__alice__abc.wav"])
    assert client.delete("/api/sessions/doomed").status_code == 200
    assert recorder_under_test.jobs.get("doomed") is None


def test_session_delete_failure_is_a_500_that_says_what_failed(
    client: TestClient, recorder_under_test: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure path no route test exercises. Whether it travels as
    `HTTPException` or as `SessionDeleteError` through `_DOMAIN_ERROR_STATUS`,
    the operator must still get 500 and a detail naming the failure."""
    seed_session(recorder_under_test.recordings_dir, "locked", ["20260101T000000Z__alice__abc.wav"])

    def _boom(*a: object, **k: object) -> None:
        raise OSError("device or resource busy")

    monkeypatch.setattr(shutil, "rmtree", _boom)
    r = client.delete("/api/sessions/locked")
    assert r.status_code == 500, r.text
    assert "delete failed" in r.json()["detail"]
    # The folder survives a failed delete — no partial teardown.
    assert (recorder_under_test.recordings_dir / "locked").exists()


# ---------------------------------------------------------------------------
# The three routes that DO spread a summary must keep doing so, with their own
# keys. A helper that normalises the payload would erase these.
# ---------------------------------------------------------------------------


def test_audio_delete_still_spreads_its_summary(client: TestClient, recorder_under_test: Recorder) -> None:
    sd = seed_session(recorder_under_test.recordings_dir, "s", ["20260101T000000Z__alice__abc.wav"])
    (sd / "session-transcript.json").write_text('{"merged": true}')
    body = client.delete("/api/sessions/s/audio").json()
    assert body["ok"] is True
    assert body["wavs_deleted"] == 1
    assert "bytes_freed" in body


def test_wav_delete_still_spreads_its_summary(client: TestClient, recorder_under_test: Recorder) -> None:
    seed_session(
        recorder_under_test.recordings_dir,
        "s",
        ["20260101T000000Z__alice__abc.wav", "20260101T010000Z__bob__def.wav"],
    )
    body = client.delete("/api/wav/s/20260101T000000Z__alice__abc.wav").json()
    assert body["ok"] is True
    assert "bytes_freed" in body


def test_absorb_still_spreads_its_summary(client: TestClient, recorder_under_test: Recorder) -> None:
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    seed_session(root, "src", ["20260101T010000Z__bob__def.wav"])
    body = client.post("/api/sessions/tgt/absorb", json={"source": "src"}).json()
    assert body["ok"] is True
    assert body["wavs_moved"] == 1
    assert "stripped_moved" in body
    assert "aliases_added" in body


# ---------------------------------------------------------------------------
# Request-shape validation belongs to the routes — no domain error covers it,
# and collapsing the tail must not take it along.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{}, {"source": ""}, {"source": 7}, {"source": None}],
    ids=["missing", "blank", "non-string", "null"],
)
def test_absorb_rejects_a_bad_source_with_400(
    client: TestClient, recorder_under_test: Recorder, body: dict
) -> None:
    seed_session(recorder_under_test.recordings_dir, "tgt", ["20260101T000000Z__alice__abc.wav"])
    assert client.post("/api/sessions/tgt/absorb", json=body).status_code == 400


def test_absorb_rejects_absorbing_a_session_into_itself(
    client: TestClient, recorder_under_test: Recorder
) -> None:
    seed_session(recorder_under_test.recordings_dir, "tgt", ["20260101T000000Z__alice__abc.wav"])
    r = client.post("/api/sessions/tgt/absorb", json={"source": "tgt"})
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot absorb a session into itself"


def test_wav_delete_rejects_an_unknown_source_with_400(
    client: TestClient, recorder_under_test: Recorder
) -> None:
    """`resolve_source_dir` owns this check — the path seam, not the route."""
    seed_session(recorder_under_test.recordings_dir, "s", ["20260101T000000Z__alice__abc.wav"])
    r = client.delete("/api/wav/s/20260101T000000Z__alice__abc.wav?source=bogus")
    assert r.status_code == 400
