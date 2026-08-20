"""People Registry — the canonical, cross-session Person model (ADR-0009;
CONTEXT.md: Person · Identity · Roster · People Registry).

`people.json` at the recordings root is the single source of truth for *names*
and *groupings* — which Identities are the same human. It is NOT the source of
the *default* name (the bridge-sent name a Person carries before the operator
renames them): a blank stored name means "fall back to the roster default",
resolved by the name layer (`name_resolution.py`) that has roster access. So
this module is purely grouping + chosen names.

The view (who appears in which sessions, live vs recorded) is derived by
aggregating the rosters and overlaying this registry — it is not stored here.

Invariant: every Identity belongs to **exactly one** Person. `sync` auto-binds
each newly-seen Identity to its own Person (blank name); `merge` joins two
Persons (survivor's name wins); `detach` pulls one Identity back into its own
Person; `rename` sets the chosen name.

Concurrency mirrors the Roster: every mutator is a synchronous read-modify-write
the caller follows with `save()`, with no `await` between load and save, so the
single asyncio event loop can't interleave two mutations. `atomic_write_text`
adds crash-safety.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from . import config
from .text import atomic_write_text, file_stat_sig

PEOPLE_JSON = "people.json"


# --- Domain errors — FastAPI-free, translated by routes/errors.py ------------


class PersonNotFound(Exception):
    """No Person with that id (→ 404)."""

    def __init__(self, person_id: str) -> None:
        super().__init__("person not found")
        self.person_id = person_id


class InvalidMergeRequest(Exception):
    """The merge is malformed — e.g. a Person into itself (→ 400)."""


class IdentityNotAMember(Exception):
    """The Identity does not belong to the Person named (→ 400)."""


# Single-slot memoisation cache for `load()`. Stores `(sig, raw_data)` where
# `raw_data` is the raw dict from `json.loads`. On a hit, `_coerce_people`
# builds a fresh PeopleRegistry from the cached snapshot — independence is
# inherent, zero `deepcopy` needed (a caller's mutation touches the output,
# never the cached raw data). A dict slot (the shape every cache in this
# repo uses — _CONFIG_TEXT_CACHE, _RULES_CACHE, _FIND_SPEC_CACHE) rather
# than a rebindable module global: mutation needs no `global` statement and
# CodeQL's unused-global query false-positives on function-rebound globals.
_PEOPLE_CACHE: dict[str, tuple[tuple | None, Any]] = {}


def _new_person_id() -> str:
    # Server-generated, opaque, stable. Never derived from the Identity: a
    # derived id would collide when the seed Identity is later detached.
    return "p_" + uuid4().hex


def _coerce_people(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("people"), list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_idents: set[str] = set()
    for row in data["people"]:
        if not isinstance(row, dict):
            continue
        pid = row.get("id")
        name = row.get("name")
        idents = row.get("identities")
        if not isinstance(pid, str) or pid in seen_ids or not isinstance(idents, list):
            continue
        # Enforce the one-Identity-one-Person invariant on read too: a hand-
        # edited or torn file can't smuggle in a duplicate that would make
        # resolution ambiguous.
        clean_idents = [i for i in idents if isinstance(i, str) and i and i not in seen_idents]
        # An identity-less Person is legitimate when it carries a NAME: a Voice
        # mapped by typing one creates exactly that, and a session's `voices`
        # map reaches it by `person_id` (ADR-0021). Unnamed AND unreachable is
        # still junk — and so is a row the dedup above just EMPTIED, which is a
        # duplicate-identity repair, not a Voice-mapped Person. Without that
        # second half, a torn people.json's duplicate row survives forever as a
        # named ghost owning nothing.
        emptied_by_dedup = any(isinstance(i, str) and i for i in idents)
        if not clean_idents and (emptied_by_dedup or not (isinstance(name, str) and name.strip())):
            continue
        seen_ids.add(pid)
        seen_idents.update(clean_idents)
        out.append({"id": pid, "name": name if isinstance(name, str) else "", "identities": clean_idents})
    return out


class PeopleRegistry:
    """In-memory view of `people.json` with the auto-bind / rename / merge /
    detach operations. Load → mutate → `save()`."""

    def __init__(self, people: list[dict[str, Any]]) -> None:
        self._people = people
        self._reindex()

    # ---- persistence ------------------------------------------------------

    @classmethod
    def load(cls) -> PeopleRegistry:
        path = config.RECORDINGS_DIR / PEOPLE_JSON
        sig = file_stat_sig(path, include_path=True)
        hit = _PEOPLE_CACHE.get("_slot")
        if hit is not None and hit[0] == sig and sig is not None:
            cached_data = hit[1]
            return cls(_coerce_people(cached_data))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Missing or torn file → empty registry; nothing is lost, the
            # people list is rebuildable from session rosters via sync().
            data = None
        _PEOPLE_CACHE["_slot"] = (sig, data)
        return cls(_coerce_people(data))

    def save(self) -> None:
        atomic_write_text(
            config.RECORDINGS_DIR / PEOPLE_JSON,
            json.dumps({"people": self._people}, indent=2, ensure_ascii=False),
        )
        # Structural invalidation: our own write must never be served stale.
        # load() would normally see a new stat signature anyway (os.replace →
        # new inode), but the route handlers read back within the same request
        # (load → mutate → save → _people_view), so don't bank on the
        # filesystem — force the next load() to re-read what we wrote.
        _PEOPLE_CACHE.pop("_slot", None)

    # ---- queries ----------------------------------------------------------

    def as_list(self) -> list[dict[str, Any]]:
        return self._people

    def get(self, person_id: str) -> dict[str, Any] | None:
        return next((p for p in self._people if p["id"] == person_id), None)

    def person_for_identity(self, identity: str) -> dict[str, Any] | None:
        return self._by_identity.get(identity)

    def name_for_identity(self, identity: str) -> str | None:
        """The operator-chosen name for `identity`, or None when the Person is
        unknown or still default-named (caller falls back to the roster name)."""
        p = self._by_identity.get(identity)
        return p["name"] if p and p["name"] else None

    # ---- mutations --------------------------------------------------------

    def sync(self, identities: object) -> bool:
        """Auto-bind every Identity in `identities` not already owned by a
        Person to a fresh blank-named Person. Returns True iff anything was
        added (so the caller can skip a no-op save on the hot poll path)."""
        changed = False
        for identity in identities:
            if not isinstance(identity, str) or not identity:
                continue
            if identity not in self._by_identity:
                person = {"id": _new_person_id(), "name": "", "identities": [identity]}
                self._people.append(person)
                self._by_identity[identity] = person
                changed = True
        return changed

    def create(self, name: str) -> dict[str, Any]:
        """Mint a named Person owning no Identity — what mapping a Voice to a
        typed name produces (ADR-0021). Not exposed as a bare route verb: the
        mapping write creates it, so a Person is never left unattached."""
        person = {"id": _new_person_id(), "name": name, "identities": []}
        self._people.append(person)
        return person

    def rename(self, person_id: str, name: str) -> dict[str, Any]:
        person = self.get(person_id)
        if person is None:
            raise PersonNotFound(person_id)
        person["name"] = name
        return person

    def merge(self, survivor_id: str, absorbed_id: str) -> dict[str, Any]:
        if survivor_id == absorbed_id:
            raise InvalidMergeRequest("cannot merge a Person into itself")
        survivor = self.get(survivor_id)
        absorbed = self.get(absorbed_id)
        if survivor is None:
            raise PersonNotFound(survivor_id)
        if absorbed is None:
            raise PersonNotFound(absorbed_id)
        for identity in absorbed["identities"]:
            if identity not in survivor["identities"]:
                survivor["identities"].append(identity)
        self._people.remove(absorbed)
        self._reindex()
        return survivor

    def detach(self, person_id: str, identity: str) -> dict[str, Any]:
        """Pull `identity` out of its Person into a fresh blank-named Person
        (the undo for an over-eager merge). Returns the resulting Person.

        Detaching a Person's SOLE Identity is a no-op that returns it
        unchanged. There is nothing to separate it from, and the alternative —
        dropping the emptied Person and minting a blank-named replacement —
        silently discarded the operator's chosen name and changed the person
        id, with no undo. That is the information loss ADR-0009 decision 7
        rules out for merge, and it reached `POST /api/people/{id}/detach`
        unguarded; `web/js/next/views/people.js` already documents the intended
        contract ("detaching a sole identity would be a no-op, so no ✕ then")
        and only hides the button.
        """
        person = self.get(person_id)
        if person is None:
            raise PersonNotFound(person_id)
        if identity not in person["identities"]:
            raise IdentityNotAMember(f"{identity!r} is not a member of {person_id!r}")
        if person["identities"] == [identity]:
            return person
        person["identities"].remove(identity)
        new_person = {"id": _new_person_id(), "name": "", "identities": [identity]}
        self._people.append(new_person)
        self._reindex()
        return new_person

    # ---- internals --------------------------------------------------------

    def _reindex(self) -> None:
        self._by_identity: dict[str, dict[str, Any]] = {}
        for person in self._people:
            for identity in person["identities"]:
                self._by_identity[identity] = person
