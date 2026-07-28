"""#368 (People step) — PeopleRegistry raises named domain errors, and the three
People CRUD routes collapse to thin shims over `_DOMAIN_ERROR_STATUS`.

Today `PeopleRegistry.rename/merge/detach` raise builtin `KeyError`/`ValueError`,
so each of `api_people_rename` / `api_people_merge` / `api_people_detach` carries
its own `try / except KeyError -> 404 / except ValueError -> 400` ladder — the
per-route translation the batch quartet already deleted in #228 by registering
domain errors in `app._DOMAIN_ERROR_STATUS`.

This contract pins the migration. The taxonomy (names, module, statuses) is
fixed here because a test cannot ask for an exception it cannot name:

    tapscribe.people.PersonNotFound       -> 404   (no Person with that id)
    tapscribe.people.InvalidMergeRequest  -> 400   (merging a Person into itself)
    tapscribe.people.IdentityNotAMember   -> 400   (detaching a non-member identity)

They live in `people.py` beside the registry that raises them, matching the
repo's convention (`SessionBusy` in recorder.py, `SessionNotFound` in
session_paths.py) and keeping `people.py` FastAPI-free.

Everything else is the implementer's call: whether the errors subclass the
builtins they replace, whether the three routes share a helper, and how the
`load -> mutate -> save -> view` tail is expressed.

WHAT IS NOT PINNED HERE, deliberately: #368's second step — the destructive
session routes' `resolve -> offload -> log -> spread` tail — is out of scope for
this slice. Do not touch `api_session_audio_delete`, `api_session_absorb`,
`api_session_delete` or `api_wav_delete`.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tapscribe import config, people
from tapscribe.app import _DOMAIN_ERROR_STATUS, app, get_recorder
from tapscribe.people import (
    PEOPLE_JSON,
    IdentityNotAMember,
    InvalidMergeRequest,
    PeopleRegistry,
    PersonNotFound,
)
from tapscribe.recorder import Recorder

# The three route handlers this slice turns into thin shims.
PEOPLE_ROUTES = ("api_people_rename", "api_people_merge", "api_people_detach")


@pytest.fixture
def recordings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(recorder_under_test: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _two_people(reg: PeopleRegistry) -> tuple[str, str]:
    """Two auto-bound Persons; returns (alice_id, bob_id)."""
    reg.sync(["alice", "bob"])
    return reg.person_for_identity("alice")["id"], reg.person_for_identity("bob")["id"]


def _seed_two_people() -> tuple[str, str]:
    """Persist two Persons through the same file the routes read."""
    reg = PeopleRegistry.load()
    ids = _two_people(reg)
    reg.save()
    return ids


# ---------------------------------------------------------------------------
# The map is the one source of truth (mirrors the #228 block in test_routes.py).
#
# `_domain_error_handler` dispatches on `type(exc)` EXACTLY, so a base class
# registered in place of the concrete raised types silently falls through to 500.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_type", "status"),
    [
        (PersonNotFound, 404),
        (InvalidMergeRequest, 400),
        (IdentityNotAMember, 400),
    ],
    ids=lambda v: getattr(v, "__name__", v),
)
def test_people_domain_error_status_is_registered(exc_type, status) -> None:
    assert _DOMAIN_ERROR_STATUS[exc_type] == status


@pytest.mark.parametrize(
    "exc_type",
    [PersonNotFound, InvalidMergeRequest, IdentityNotAMember],
    ids=lambda v: getattr(v, "__name__", v),
)
def test_people_domain_errors_carry_no_status_code_attribute(exc_type) -> None:
    """#228's rule: the HTTP status lives in `_DOMAIN_ERROR_STATUS` and nowhere
    else. A per-class `status_code` duplicates the map and drifts from it."""
    assert not hasattr(exc_type, "status_code")


def test_people_module_stays_fastapi_free() -> None:
    """The registry is the domain layer — the whole point of raising domain
    errors is that it does not know about HTTP."""
    src = Path(people.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert not [m for m in imported if m.split(".")[0] in {"fastapi", "starlette"}]


# ---------------------------------------------------------------------------
# Registry level — EVERY branch that raises today.
#
# `merge` raises on THREE distinct branches (self, unknown survivor, unknown
# absorbed) and `detach` on two. Migrating only the first raise of each method
# leaves the rest as bare builtins, which reach the route as an unhandled
# exception (500) — invisible to a test that only exercises one branch per
# method.
# ---------------------------------------------------------------------------


def test_rename_unknown_person_raises_person_not_found(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    _two_people(reg)
    with pytest.raises(PersonNotFound):
        reg.rename("p_nope", "X")


def test_merge_into_self_raises_invalid_merge_request(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    alice, _ = _two_people(reg)
    with pytest.raises(InvalidMergeRequest):
        reg.merge(alice, alice)


def test_merge_unknown_survivor_raises_person_not_found(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    _, bob = _two_people(reg)
    with pytest.raises(PersonNotFound):
        reg.merge("p_nope", bob)


def test_merge_unknown_absorbed_raises_person_not_found(recordings_dir: Path) -> None:
    """The SECOND lookup in `merge`. A migration that converts only the survivor
    branch leaves this one raising a bare KeyError."""
    reg = PeopleRegistry.load()
    alice, _ = _two_people(reg)
    with pytest.raises(PersonNotFound):
        reg.merge(alice, "p_nope")


def test_merge_into_self_wins_over_unknown_id(recordings_dir: Path) -> None:
    """Precedence: the self-merge check runs BEFORE either lookup, so two
    identical UNKNOWN ids are an invalid request, not a missing Person. A
    reordering that hoists the lookups flips this 400 to a 404."""
    reg = PeopleRegistry.load()
    _two_people(reg)
    with pytest.raises(InvalidMergeRequest):
        reg.merge("p_nope", "p_nope")


def test_detach_unknown_person_raises_person_not_found(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    _two_people(reg)
    with pytest.raises(PersonNotFound):
        reg.detach("p_nope", "alice")


def test_detach_non_member_identity_raises_identity_not_a_member(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    alice, _ = _two_people(reg)
    with pytest.raises(IdentityNotAMember):
        reg.detach(alice, "bob")  # bob's identity belongs to another Person


# ---------------------------------------------------------------------------
# Behaviour preservation at the registry — this is a translation, not a redesign.
# ---------------------------------------------------------------------------


def test_detaching_a_sole_identity_stays_a_no_op(recordings_dir: Path) -> None:
    """ADR-0009 decision 7: detaching a Person's only Identity returns it
    unchanged rather than minting a blank-named replacement. It must NOT become
    an IdentityNotAMember now that detach has an error to raise."""
    reg = PeopleRegistry.load()
    reg.sync(["solo"])
    pid = reg.person_for_identity("solo")["id"]
    reg.rename(pid, "Solo")
    result = reg.detach(pid, "solo")
    assert result["id"] == pid
    assert result["name"] == "Solo"
    assert result["identities"] == ["solo"]
    assert len(reg.as_list()) == 1


def test_merge_and_rename_still_do_their_work(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    alice, bob = _two_people(reg)
    reg.rename(alice, "Alice")
    reg.merge(alice, bob)
    survivor = reg.person_for_identity("alice")
    assert survivor["id"] == alice
    assert survivor["name"] == "Alice"
    assert set(survivor["identities"]) == {"alice", "bob"}
    assert reg.person_for_identity("bob")["id"] == alice
    assert len(reg.as_list()) == 1


# ---------------------------------------------------------------------------
# Route level — the harm layer.
#
# The registry raising the right type proves nothing on its own: the operator
# sees a STATUS and a `detail`. Every branch is pinned through the real route so
# an unregistered type (500) or a dropped body-validation guard is caught here.
# ---------------------------------------------------------------------------


def test_route_rename_unknown_person_is_404(client: TestClient) -> None:
    _seed_two_people()
    r = client.put("/api/people/p_nope", json={"name": "X"})
    assert r.status_code == 404


def test_route_merge_into_self_is_400(client: TestClient) -> None:
    alice, _ = _seed_two_people()
    r = client.post("/api/people/merge", json={"survivor": alice, "absorbed": alice})
    assert r.status_code == 400


def test_route_merge_unknown_survivor_is_404(client: TestClient) -> None:
    _, bob = _seed_two_people()
    r = client.post("/api/people/merge", json={"survivor": "p_nope", "absorbed": bob})
    assert r.status_code == 404


def test_route_merge_unknown_absorbed_is_404(client: TestClient) -> None:
    alice, _ = _seed_two_people()
    r = client.post("/api/people/merge", json={"survivor": alice, "absorbed": "p_nope"})
    assert r.status_code == 404


def test_route_detach_unknown_person_is_404(client: TestClient) -> None:
    _seed_two_people()
    r = client.post("/api/people/p_nope/detach", json={"identity": "alice"})
    assert r.status_code == 404


def test_route_detach_non_member_identity_is_400(client: TestClient) -> None:
    alice, _ = _seed_two_people()
    r = client.post(f"/api/people/{alice}/detach", json={"identity": "bob"})
    assert r.status_code == 400


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/api/people/{alice}", {"name": 123}),
        ("post", "/api/people/merge", {"survivor": "only"}),
        ("post", "/api/people/merge", {"survivor": "", "absorbed": "x"}),
        ("post", "/api/people/merge", {"survivor": "a", "absorbed": 7}),
        ("post", "/api/people/{alice}/detach", {}),
        ("post", "/api/people/{alice}/detach", {"identity": ""}),
    ],
)
def test_route_body_validation_still_rejects_with_400(
    client: TestClient, method: str, path: str, body: dict
) -> None:
    """The request-shape guards are the routes' OWN job — no domain error covers
    them, and collapsing the handlers must not take them along. A dropped guard
    turns a malformed body into a 500 (or a rename to the string "123")."""
    alice, _ = _seed_two_people()
    r = getattr(client, method)(path.format(alice=alice), json=body)
    assert r.status_code == 400


def test_route_404_detail_still_says_not_found(client: TestClient) -> None:
    """Today the operator gets `{"detail": "person not found"}`. After the
    migration the handler returns `str(exc)`, so an error whose `__str__` is the
    inherited `KeyError` repr would answer `"'p_nope'"` — a 404 body that no
    longer says what went wrong. The id may be added; the meaning must survive."""
    _seed_two_people()
    detail = client.put("/api/people/p_nope", json={"name": "X"}).json()["detail"]
    assert isinstance(detail, str)
    assert "not found" in detail.lower()


def test_route_400_details_are_preserved_verbatim(client: TestClient) -> None:
    """Both 400s pass `str(e)` through today, so these exact strings are the
    current API contract."""
    alice, _ = _seed_two_people()
    merged = client.post("/api/people/merge", json={"survivor": alice, "absorbed": alice})
    assert merged.json()["detail"] == "cannot merge a Person into itself"

    detached = client.post(f"/api/people/{alice}/detach", json={"identity": "bob"})
    assert detached.json()["detail"] == f"'bob' is not a member of {alice!r}"


# ---------------------------------------------------------------------------
# The seam the refactor actually touches: load -> mutate -> save -> re-view.
# ---------------------------------------------------------------------------


def test_failed_mutation_persists_nothing(client: TestClient, tmp_path: Path) -> None:
    """`save()` runs only after the mutation succeeds, because it sits AFTER the
    `try` block that the domain errors escape from. Collapsing the ladder moves
    that call; a helper that saves in a `finally`, or one that saves before the
    error can propagate, turns every rejected request into a disk write."""
    alice, _ = _seed_two_people()
    before = json.loads((config.RECORDINGS_DIR / PEOPLE_JSON).read_text(encoding="utf-8"))

    assert client.post("/api/people/merge", json={"survivor": alice, "absorbed": "p_nope"}).status_code == 404
    assert client.post(f"/api/people/{alice}/detach", json={"identity": "bob"}).status_code == 400
    assert client.put("/api/people/p_nope", json={"name": "X"}).status_code == 404

    after = json.loads((config.RECORDINGS_DIR / PEOPLE_JSON).read_text(encoding="utf-8"))
    assert after == before


def test_successful_mutation_response_already_shows_it(client: TestClient) -> None:
    """The route saves and THEN re-renders the view, so the response a caller
    gets back is already current. `_people_view` re-loads through the stat-sig
    memo cache, so a refactor that defers the save (or hands the registry back
    for the caller to save) renders the OLD name here while the next GET is
    right — a stale dashboard for one round-trip."""
    alice, _ = _seed_two_people()
    r = client.put(f"/api/people/{alice}", json={"name": "Alice Havso"})
    assert r.status_code == 200
    row = next(p for p in r.json()["people"] if p["id"] == alice)
    assert row["name"] == "Alice Havso"


# ---------------------------------------------------------------------------
# The thin shim itself.
#
# Narrow on purpose: it forbids the two builtin translations this slice deletes,
# not error handling in general.
# ---------------------------------------------------------------------------


def _route_defs() -> dict[str, ast.AST]:
    from tapscribe import app as app_module

    tree = ast.parse(Path(app_module.__file__).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PEOPLE_ROUTES
    }


def test_all_three_people_routes_are_present() -> None:
    """Guards the scan below: a renamed handler would make it vacuously pass."""
    assert set(_route_defs()) == set(PEOPLE_ROUTES)


@pytest.mark.parametrize("route", PEOPLE_ROUTES)
def test_people_route_no_longer_translates_builtin_exceptions(route: str) -> None:
    caught = [
        name.id
        for handler in ast.walk(_route_defs()[route])
        if isinstance(handler, ast.ExceptHandler)
        for name in ast.walk(handler.type)
        if isinstance(name, ast.Name)
    ]
    assert not {"KeyError", "ValueError"} & set(caught), (
        f"{route} still translates builtin exceptions: {sorted(set(caught))}"
    )


def test_registering_the_people_errors_keeps_every_earlier_one() -> None:
    """This slice ADDS to `_DOMAIN_ERROR_STATUS`. Rewriting the literal instead
    of extending it would silently de-register the session/batch families, and
    `_domain_error_handler` answers 500 for anything unmapped — so the whole
    #228 migration would regress behind a green People gate."""
    from tapscribe.recorder import SessionBusy
    from tapscribe.session_paths import SessionNotFound, UnknownSource

    assert _DOMAIN_ERROR_STATUS[SessionBusy] == 409
    assert _DOMAIN_ERROR_STATUS[SessionNotFound] == 404
    assert _DOMAIN_ERROR_STATUS[UnknownSource] == 400
