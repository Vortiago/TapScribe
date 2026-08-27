"""The dashboard's poll endpoint.

  GET  /api/state  sessions + active taps + live channel + operator config

One route, deliberately alone in its module: the payload is the State view
(`tapscribe/state_view.py`), and what is left here is the part that needs a
Recorder and a Request. Three steps, in this order for a reason:

  1. snapshot everything recorder-owned (streams, jobs) on the event loop;
  2. thread hop for the disk walk (`gather_sessions`), then the people
     mutation back on the loop (load, sync, save), serialised with
     /api/people so the two writers of people.json cannot race;
  3. assemble one `StateInputs`, thread hop for the projection +
     serialization + ETag, then the 304 branch.

ADR-0013 is why this is a poll at all rather than a push.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

# `tapscribe.live`, not `routes.live`: a level-2 relative import reaches the
# package, so this is not the sibling-router import `test_route_surface` forbids.
from .. import tap_mode
from ..live import LiveSnapshot
from ..name_resolution import attach_people_mutation
from ..recorder import Recorder, open_wav_names
from ..runtime_probe import available_backend_strs
from ..sessions import gather_sessions
from ..state_view import StateInputs, active_rows, build_state_blob, live_identities_of
from .deps import get_recorder

router = APIRouter()


@router.get("/api/state")
async def api_state(req: Request, recorder: Recorder = Depends(get_recorder)):
    active_streams = await recorder.streams.snapshot()
    # One read of the live session id for the whole tick: reading it again inside
    # the thread hop below (as this route used to, through a closure) let a
    # rotation land between the two, so the listing and the payload's
    # `current_session` could disagree for one poll.
    session_start = recorder.session_start
    jobs_snapshot = {k: asdict(v) for k, v in recorder.jobs.snapshot().items()}
    open_wavs = open_wav_names(active_streams)

    # Active rows with the tap_settings overlay: cheap, stays on the event loop.
    active = active_rows(active_streams, recorder.tap_settings.get, tap_mode.overrides())
    # The People mutation below needs the live-identity set before the projection
    # object exists, so it comes off the same rows the payload ships —
    # `StateInputs.live_identities` derives its own through this same function,
    # which is what makes the two agree by construction.
    live_identities = live_identities_of(active)

    # Thread hop 1: gather_sessions (disk walk, off the loop)
    sessions_list = await asyncio.to_thread(
        gather_sessions,
        current_session=session_start,
        jobs=jobs_snapshot,
        open_wavs=open_wavs,
    )

    # Mutation: load → sync → save (on event loop, serialised with /api/people)
    registry, occs = attach_people_mutation(sessions_list, live_identities=live_identities)

    # One frozen record of this instant. Every read of something the Recorder
    # mutates happens HERE, on the event loop, so nothing the worker thread
    # below touches is still moving. How much log a tick ships, and how a
    # channel is read at all, belong to `LiveSnapshot` — not to this route.
    inputs = StateInputs(
        current_session=session_start,
        active=active,
        sessions_list=sessions_list,
        registry=registry,
        occs=occs,
        live_feed=recorder.transcripts.snapshot(),
        live=LiveSnapshot.capture(recorder.live),
        recording_enabled=recorder.recording_enabled,
        backend=recorder.backend,
        available_backends=sorted(available_backend_strs()),
    )

    # Thread hop 2: config reads + people joins + payload build + serialize + ETag
    body, etag = await asyncio.to_thread(build_state_blob, inputs)

    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if req.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
