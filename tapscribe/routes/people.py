"""People Registry (ADR-0009): the canonical cross-session Person model.

  GET   /api/people                    one row per Person, the same shape /api/state ships
  PUT   /api/people/{person_id}        rename
  POST  /api/people/merge              fold one Person into another
  POST  /api/people/{person_id}/detach split an identity back out

The registry view also rides on `/api/state` (`people`); these routes are the
explicit fetch plus the mutations. people.json is mutated ONLY here and in the
/api/state sync, both on the event loop, so they cannot race. A person_id /
identity from the body is validated against the loaded registry (KeyError → 404)
before anything is written; nothing here builds a filesystem path from request
input (people.json is a fixed path).
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
from ..sessions import gather_sessions
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
