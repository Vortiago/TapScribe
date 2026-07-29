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
  * it frees the session's job slot after deleting, which nothing asserted.
  * its failure path answers 500 with a `delete failed:` detail, which nothing
    exercised.

Deliberately NOT pinned — the implementer's call: whether the tail becomes one
helper or several, whether it is a function / decorator / context manager, what
it is called, and whether `absorb` (today the only one that does NOT offload to
a thread) starts doing so.
"""

from __future__ import annotations

import ast
import builtins
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe.app import _ops_log, app, get_recorder
from tapscribe.recorder import JobState, Recorder

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


def _func_defs() -> dict[str, ast.AST]:
    from tapscribe import app as app_module

    tree = ast.parse(Path(app_module.__file__).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _route_defs() -> dict[str, ast.AST]:
    return {name: node for name, node in _func_defs().items() if name in DESTRUCTIVE_ROUTES}


def _httpexception_raises(node: ast.AST) -> list[ast.Raise]:
    return [
        r
        for r in ast.walk(node)
        if isinstance(r, ast.Raise)
        and isinstance(r.exc, ast.Call)
        and isinstance(r.exc.func, ast.Name)
        and r.exc.func.id == "HTTPException"
    ]


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
    assert not _httpexception_raises(_route_defs()["api_session_delete"]), (
        "api_session_delete still translates its failure to HTTPException by hand"
    )


# ---------------------------------------------------------------------------
# The scoped-OUT sibling, pinned POSITIVELY.
# ---------------------------------------------------------------------------


def test_stripped_delete_is_deliberately_left_alone() -> None:
    """`api_session_stripped_delete` sits inside the blast radius but OUTSIDE
    the scope: it is not one of `DESTRUCTIVE_ROUTES`, #368 never names it, and
    it carries the same `raise HTTPException(500, f"delete failed: {e}")`
    literal `api_session_delete` is losing — so an unanchored replace_all on
    that literal silently rewrites it too. Every assertion above is about the
    ABSENCE of something; nothing notices a scoped-out sibling being swept
    along unless the sibling is pinned positively, so pin it here. Deliberate
    sweeping is fine — it just has to update this test and say so."""
    stripped = _func_defs()["api_session_stripped_delete"]
    assert _calls_named(stripped, "print"), (
        "api_session_stripped_delete's inline completion log was swept — out of #368's scope"
    )
    assert _httpexception_raises(stripped), (
        "api_session_stripped_delete's hand-rolled HTTPException(500) was swept — out of #368's scope"
    )


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


def test_session_delete_holds_the_job_slot_for_the_walk_and_frees_it_after(
    client: TestClient, recorder_under_test: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route brackets its teardown with the SAME slot the batch jobs use.

    Asserting only the after-state is vacuous: `_refuse_current_or_busy`
    already guarantees the slot is empty at request time, so `jobs.get(...)
    is None` afterwards passes even with the bracket deleted outright. The
    load-bearing half is the DURING-state — the slot is held while the tree
    is being unlinked, so a transcribe/strip that races the check-then-act
    pre-flight window gets a 409 instead of reading WAVs out of a folder
    being rmtree'd. The after-state then pins the release: the slot must not
    leak for a session id that no longer exists, or a later session reusing
    the id would 409 forever."""
    seed_session(recorder_under_test.recordings_dir, "doomed", ["20260101T000000Z__alice__abc.wav"])
    real_rmtree = shutil.rmtree
    held: list[JobState | None] = []

    def _observe(path: object, *a: object, **k: object) -> None:
        # `JobTracker.get` is a bare dict read, so it is safe from the worker
        # thread `asyncio.to_thread` runs this on. (`jobs.claim` is not — it
        # awaits a lock bound to the app's loop — so the during-state is
        # asserted on lock-free state, per #271.)
        held.append(recorder_under_test.jobs.get("doomed"))
        real_rmtree(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "rmtree", _observe)
    assert client.delete("/api/sessions/doomed").status_code == 200
    assert held, "the teardown never ran"
    # Not merely "the slot is populated" — it must be THIS route's bracket
    # holding it, so stray bookkeeping that fills the slot can't stand in for
    # the hold.
    assert held[0] is not None, "the session's job slot was not held for the teardown walk"
    assert held[0].kind == "delete", f"the slot was held by a {held[0].kind} job, not the delete bracket"
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
    # The folder survives a delete that never started. `rmtree` is replaced
    # wholesale here, so this pins THAT and not "a mid-walk failure leaves
    # nothing behind" — `shutil.rmtree` is not atomic, and a real EBUSY part
    # way through leaves a truncated folder (the same exposure
    # `delete_session_audio` has had since #207).
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


# ---------------------------------------------------------------------------
# The emitted line — the harm layer. Every scan above asserts the ABSENCE of an
# inline `print`, which a bare `def _ops_log(m): print(m)` satisfies while
# dropping both the `[tapscribe] ` prefix an operator greps for and the
# `flush=True` that keeps the line visible behind a pipe. The operator log IS
# the deliverable of the `log` limb, so pin what reaches stdout, per route.
# ---------------------------------------------------------------------------


def test_the_seam_owns_the_prefix_and_the_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    """`capsys` replaces stdout with a plain buffer, so flushing is not
    observable through it — assert on the call the seam makes instead."""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(builtins, "print", lambda *a, **k: calls.append((a, k)))
    _ops_log("something happened")
    ((args, kwargs),) = calls
    assert args == ("[tapscribe] something happened",)
    # `flush` only — NOT an exact-kwargs match, so the transport change this
    # seam exists to make cheap (a `file=`, an `end=`) does not redden a test
    # about the prefix.
    assert kwargs.get("flush") is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/api/sessions/s", "[tapscribe] deleted session: "),
        ("/api/sessions/s/audio", "[tapscribe] deleted audio from session s: 1 wavs, "),
        (
            "/api/wav/s/20260101T000000Z__alice__abc.wav",
            "[tapscribe] deleted wav 20260101T000000Z__alice__abc.wav (original) from session s: ",
        ),
    ],
    ids=["session", "audio", "wav"],
)
def test_single_session_delete_still_says_what_it_freed(
    client: TestClient,
    recorder_under_test: Recorder,
    capsys: pytest.CaptureFixture[str],
    url: str,
    expected: str,
) -> None:
    """One seeded session, one DELETE, one line — the three routes differ only
    in the URL and the prefix they owe the operator. (`absorb` needs a second
    session, so it stays its own test below.)"""
    seed_session(recorder_under_test.recordings_dir, "s", ["20260101T000000Z__alice__abc.wav"])
    assert client.delete(url).status_code == 200
    assert expected in capsys.readouterr().out


def test_absorb_still_says_what_it_moved(
    client: TestClient, recorder_under_test: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    root = recorder_under_test.recordings_dir
    seed_session(root, "tgt", ["20260101T000000Z__alice__abc.wav"])
    seed_session(root, "src", ["20260101T010000Z__bob__def.wav"])
    assert client.post("/api/sessions/tgt/absorb", json={"source": "src"}).status_code == 200
    assert "[tapscribe] absorbed src into tgt: 1 wavs, " in capsys.readouterr().out
