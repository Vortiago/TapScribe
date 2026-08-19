"""People Registry — the canonical, cross-session Person model (ADR-0009).

people.json is the single source of truth for *names* and *groupings* (which
Identities are the same human). Defaults — a Person's name before the operator
renames them — are NOT stored here: a blank stored name means "fall back to the
bridge/roster default", resolved by the name layer that has roster access. So
this module is purely grouping + chosen names: auto-bind, rename, merge, detach.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tapscribe import config
from tapscribe.people import (
    PEOPLE_JSON,
    IdentityNotAMember,
    InvalidMergeRequest,
    PeopleRegistry,
    PersonNotFound,
    _coerce_people,
)


@pytest.fixture
def recordings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    return tmp_path


def test_empty_when_no_file(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    assert reg.as_list() == []


def test_sync_auto_binds_one_person_per_new_identity(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    assert reg.sync(["alice", "bob"]) is True
    assert {p["name"] for p in reg.as_list()} == {""}  # blank = use default
    assert reg.person_for_identity("alice") is not None
    assert reg.person_for_identity("bob") is not None
    # Each auto-Person owns exactly its one identity.
    assert reg.person_for_identity("alice")["identities"] == ["alice"]


def test_sync_is_idempotent(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["alice"])
    assert reg.sync(["alice"]) is False  # nothing new → no change
    assert len(reg.as_list()) == 1


def test_rename_sets_the_chosen_name(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["alice"])
    pid = reg.person_for_identity("alice")["id"]
    reg.rename(pid, "Alice Havso")
    assert reg.person_for_identity("alice")["name"] == "Alice Havso"


def test_rename_unknown_id_raises(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    with pytest.raises(PersonNotFound):
        reg.rename("p_nope", "X")


def test_merge_joins_identities_survivor_name_wins(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["alice-laptop", "alice-office"])
    survivor = reg.person_for_identity("alice-laptop")
    absorbed = reg.person_for_identity("alice-office")
    reg.rename(survivor["id"], "Alice")
    reg.rename(absorbed["id"], "Alice (office)")
    reg.merge(survivor["id"], absorbed["id"])
    # One Person now owns both identities; the survivor's name wins.
    p = reg.person_for_identity("alice-laptop")
    assert p["id"] == survivor["id"]
    assert p["name"] == "Alice"
    assert set(p["identities"]) == {"alice-laptop", "alice-office"}
    # The absorbed identity now resolves to the survivor.
    assert reg.person_for_identity("alice-office")["id"] == survivor["id"]
    # Exactly one Person remains.
    assert len(reg.as_list()) == 1


def test_merge_rejects_same_and_unknown(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["a"])
    pid = reg.person_for_identity("a")["id"]
    with pytest.raises(InvalidMergeRequest):
        reg.merge(pid, pid)
    with pytest.raises(PersonNotFound):
        reg.merge(pid, "p_nope")


def test_detach_pulls_one_identity_into_its_own_person(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["x", "y"])
    survivor = reg.person_for_identity("x")
    reg.merge(survivor["id"], reg.person_for_identity("y")["id"])
    reg.rename(survivor["id"], "Combined")
    # Now detach y back out.
    new_person = reg.detach(survivor["id"], "y")
    assert new_person["id"] != survivor["id"]
    assert new_person["identities"] == ["y"]
    assert new_person["name"] == ""  # reverts to default
    # The survivor keeps its id, name, and remaining identity.
    p = reg.person_for_identity("x")
    assert p["id"] == survivor["id"]
    assert p["name"] == "Combined"
    assert p["identities"] == ["x"]
    # Two distinct Persons again.
    assert len({p["id"] for p in reg.as_list()}) == 2


def test_detach_identity_not_in_person_raises(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["x", "y"])
    pid = reg.person_for_identity("x")["id"]
    with pytest.raises(IdentityNotAMember):
        reg.detach(pid, "y")  # y belongs to a different person


def test_save_load_round_trip(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["alice", "bob"])
    reg.rename(reg.person_for_identity("alice")["id"], "Alice")
    reg.save()
    assert (recordings_dir / PEOPLE_JSON).exists()
    reloaded = PeopleRegistry.load()
    assert reloaded.person_for_identity("alice")["name"] == "Alice"
    assert reloaded.person_for_identity("bob")["name"] == ""


def test_each_identity_belongs_to_exactly_one_person(recordings_dir: Path) -> None:
    reg = PeopleRegistry.load()
    reg.sync(["a", "b", "c"])
    reg.merge(reg.person_for_identity("a")["id"], reg.person_for_identity("b")["id"])
    seen: list[str] = []
    for p in reg.as_list():
        seen.extend(p["identities"])
    assert sorted(seen) == ["a", "b", "c"]  # no duplicates, none lost


def test_torn_file_loads_empty(recordings_dir: Path) -> None:
    (recordings_dir / PEOPLE_JSON).write_text("{ broken", encoding="utf-8")
    assert PeopleRegistry.load().as_list() == []
    (recordings_dir / PEOPLE_JSON).write_text(json.dumps([1, 2]), encoding="utf-8")
    assert PeopleRegistry.load().as_list() == []


def test_detach_sole_identity_is_a_no_op_and_keeps_the_name(recordings_dir: Path) -> None:
    """Detaching a Person's only Identity must not destroy the Person.

    It used to drop the emptied Person and mint a blank-named replacement with
    a NEW id, so `POST /api/people/{id}/detach` silently discarded the
    operator's chosen name with no undo — the information loss ADR-0009
    decision 7 rules out for merge. people.js already documents the no-op
    contract client-side and merely hides the ✕, so a scripted call or a UI
    regression reached it unguarded.
    """
    reg = PeopleRegistry.load()
    reg.sync(["solo"])
    pid = reg.person_for_identity("solo")["id"]
    reg.rename(pid, "Alice Havso")

    result = reg.detach(pid, "solo")

    assert result["id"] == pid, "the Person id must survive"
    assert result["name"] == "Alice Havso", "the operator's chosen name must survive"
    assert result["identities"] == ["solo"]
    assert len(reg.as_list()) == 1
    assert reg.person_for_identity("solo")["name"] == "Alice Havso"


# ---- A Person can own no Identity (ADR-0021) -------------------------------


def test_a_named_person_with_no_identities_survives_a_round_trip() -> None:
    """Mapping a Voice by typing a name creates one. The load path used to drop
    identity-less rows, so the Person vanished on the next read."""
    people = _coerce_people({"people": [{"id": "p1", "name": "Alice", "identities": []}]})

    assert [(x["id"], x["name"]) for x in people] == [("p1", "Alice")]


def test_an_unnamed_person_with_no_identities_is_still_dropped() -> None:
    """Nothing reaches it and nothing names it — junk, not a Person."""
    assert _coerce_people({"people": [{"id": "p1", "name": "", "identities": []}]}) == []


def test_create_mints_one_named_person_reachable_by_id() -> None:
    reg = PeopleRegistry([])

    person = reg.create("Alice Andersen")

    assert person["identities"] == []
    assert reg.get(person["id"])["name"] == "Alice Andersen"


def test_create_does_not_put_the_person_in_the_identity_index() -> None:
    """It owns no Identity, so voice mapping must resolve through
    `get(person_id)` rather than the identity index."""
    reg = PeopleRegistry([])
    person = reg.create("Alice")

    assert reg.person_for_identity(person["id"]) is None


def test_rename_and_merge_still_work_on_an_identity_less_person() -> None:
    reg = PeopleRegistry([])
    voice_person = reg.create("Alice")
    reg.sync(["tray-mic-1"])
    owner = reg.person_for_identity("tray-mic-1")

    reg.rename(voice_person["id"], "Alice A")
    assert reg.get(voice_person["id"])["name"] == "Alice A"

    reg.merge(owner["id"], voice_person["id"])
    assert reg.get(voice_person["id"]) is None
    assert reg.person_for_identity("tray-mic-1")["id"] == owner["id"]
