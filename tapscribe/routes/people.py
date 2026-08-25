"""People Registry (ADR-0009): the canonical cross-session Person model.

  GET   /api/people                    one row per Person, the same shape /api/state ships
  PUT   /api/people/{person_id}        rename
  POST  /api/people/merge              fold one Person into another
  POST  /api/people/{person_id}/detach split an identity back out
  PUT   /api/sessions/{session}/voices map one Voice to a Person, by id or name

The registry view also rides on `/api/state` (`people`); these routes are the
explicit fetch plus the mutations. people.json is mutated ONLY here and in the
/api/state sync, both on the event loop, so they cannot race. A person_id /
identity from the body is validated against the loaded registry (KeyError → 404)
before anything is written; nothing here builds a filesystem path from request
input (people.json is a fixed path).

The Voice mapping sits here despite its `/api/sessions/*` prefix because mapping
by NAME creates a Person — a third people.json writer would break the invariant
above to satisfy a URL prefix. ADR-0018 groups by concern; `routes/diarize.py`
owns the rest of the diarization surface. The voice KEY travels in the body, not
the path: it carries a `#`, and a request-shaped path segment is a class of risk
this route has no reason to take on.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..name_resolution import attach_people
from ..people import PeopleRegistry
from ..recorder import Recorder
from ..session_paths import resolve_session_dir
from ..sessions import gather_sessions, read_session_meta, write_session_meta
from ..text import split_voice_key
from ..voices import read_voices
from .body import json_body, require_json_object_body, require_str
from .deps import get_recorder

router = APIRouter()


async def _people_view(recorder: Recorder) -> list[dict[str, Any]]:
    active_streams = await recorder.streams.snapshot()
    live_identities = {s.identity for s in active_streams}
    sessions = await asyncio.to_thread(gather_sessions, current_session=recorder.session_start, jobs={})
    return attach_people(sessions, live_identities=live_identities)


@router.get("/api/people")
async def api_people_get(recorder: Recorder = Depends(get_recorder)):
    """The cross-session People view: one row per Person with name, member
    identities, sessions, recorded/live source. Same shape /api/state ships."""
    return {"people": await _people_view(recorder)}


@router.put("/api/people/{person_id}")
async def api_people_rename(person_id: str, req: Request, recorder: Recorder = Depends(get_recorder)):
    # Strict parse + a REQUIRED `name`: a blank stored name is how the registry
    # says "fall back to the roster default", so a body that never parsed — or
    # one without the key — must not read as a deliberate rename-to-blank and
    # DESTROY the operator's chosen name. Only an explicit `{"name": ""}` clears.
    body = await require_json_object_body(req, allow_empty=False)
    name = require_str(body.get("name"), "name")
    registry = PeopleRegistry.load()
    person = registry.get(person_id)
    # A Person owning no Identity is reachable only BY name: `_coerce_people`
    # drops an unnamed one as torn-file junk, so clearing the name here deletes
    # the Person on the next load and orphans every `session_meta.voices` pointer
    # at it. There is nothing to fall back to either — `_default_name` derives
    # from Identities, and this Person has none.
    if person is not None and not name.strip() and not person["identities"]:
        raise HTTPException(
            400, "this Person owns no Identity — its name is how it is found, so it cannot be cleared"
        )
    registry.rename(person_id, name.strip())
    registry.save()
    return {"ok": True, "people": await _people_view(recorder)}


@router.post("/api/people/merge")
async def api_people_merge(req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await json_body(req)
    survivor = body.get("survivor")
    absorbed = body.get("absorbed")
    if not isinstance(survivor, str) or not isinstance(absorbed, str) or not survivor or not absorbed:
        raise HTTPException(400, "survivor and absorbed person ids are required")
    registry = PeopleRegistry.load()
    registry.merge(survivor, absorbed)
    registry.save()
    return {"ok": True, "people": await _people_view(recorder)}


@router.post("/api/people/{person_id}/detach")
async def api_people_detach(person_id: str, req: Request, recorder: Recorder = Depends(get_recorder)):
    body = await json_body(req)
    identity = body.get("identity")
    if not isinstance(identity, str) or not identity:
        raise HTTPException(400, "identity is required")
    registry = PeopleRegistry.load()
    new_person = registry.detach(person_id, identity)
    registry.save()
    return {"ok": True, "detached": new_person["id"], "people": await _people_view(recorder)}


@router.put("/api/sessions/{session}/voices")
async def api_session_voice_mapping(session: str, req: Request, recorder: Recorder = Depends(get_recorder)):
    """Map one Voice to a Person: `{"key": "identity#A", "person_id": …}`, or
    `{"key": …, "name": …}` to create the Person as part of the mapping, or the
    key alone to clear it.

    A `name` always CREATES, even when one Person already has it. Two people
    share a name more often than one is typed twice, and folding a Voice into a
    namesake attributes their words to a stranger; picking the existing Person
    is the dropdown, a different gesture. There is deliberately no bare create
    on the wire (ADR-0021) — a Person is never left unattached.

    The session's Voice sidecar is the allowlist: the key must be a Voice it
    carries, and the run stamped on the mapping is the one it names. A mapping
    whose stamp no longer matches is not applied, so letting the client choose
    it would let a stale mapping look current.
    """
    body = await json_body(req)
    key = require_str(body.get("key"), "key")
    identity, label = split_voice_key(key)
    entry = read_voices(resolve_session_dir(session)).get(identity)
    if entry is None or not label or label not in entry["voices"]:
        raise HTTPException(404, f"no Voice {key!r} in this session's diarization")

    person_id = body.get("person_id")
    name = body.get("name")
    registry = PeopleRegistry.load()
    created = None
    if isinstance(name, str) and name.strip():
        created = registry.create(name.strip())
        person_id = created["id"]
    elif isinstance(person_id, str) and person_id:
        if registry.get(person_id) is None:
            raise HTTPException(404, f"no Person {person_id!r}")
    else:
        person_id = ""

    mapping = dict(read_session_meta(session).get("voices") or {})
    if person_id:
        mapping[key] = {"person_id": person_id, "run_id": entry["run_id"]}
    else:
        mapping.pop(key, None)
    # The mapping lands FIRST, and only then does the new Person become durable:
    # a Person the meta write never referenced is unreachable — no Identity, no
    # pointer, no delete verb — so committing it ahead of the write leaves one
    # behind on every failed attempt. Still no `await` between load and save, so
    # the "people.json has two serialised writers" invariant holds.
    write_session_meta(session, {"voices": mapping})
    if created is not None:
        registry.save()
    return {"ok": True, "voices": mapping, "people": await _people_view(recorder)}
