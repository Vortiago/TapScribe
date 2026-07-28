"""The dashboard's poll endpoint.

  GET  /api/state  sessions + active taps + live channel + operator config

One route, deliberately alone in its module: the payload is the State view
(`tapscribe/state_view.py`), and what is left here is the part that needs a
Recorder and a Request. Three steps, in this order for a reason:

  1. snapshot everything recorder-owned (streams, jobs) on the event loop;
  2. thread hop for the disk walk (`gather_sessions`), then the people
     mutation back on the loop (load, sync, save), serialised with
     /api/people so the two writers of people.json cannot race;
  3. thread hop for the projection + serialization + ETag, then the 304 branch.

ADR-0013 is why this is a poll at all rather than a push.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from itertools import islice

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..name_resolution import attach_people_mutation
from ..recorder import Recorder, open_wav_names
from ..runtime_probe import available_backend_strs
from ..sessions import gather_sessions
from ..state_view import active_rows, build_state_blob
from .deps import get_recorder

router = APIRouter()


@router.get("/api/state")
async def api_state(req: Request, recorder: Recorder = Depends(get_recorder)):
    active_streams = await recorder.streams.snapshot()
    live_identities = {s.identity for s in active_streams}
    jobs_snapshot = {k: asdict(v) for k, v in recorder.jobs.snapshot().items()}
    open_wavs = open_wav_names(active_streams)

    # Active rows with tap_settings overlay (on loop, unchanged)
    active = active_rows(active_streams, recorder.tap_settings.get)

    # Thread hop 1: gather_sessions (disk walk, off the loop)
    sessions_list = await asyncio.to_thread(
        lambda: gather_sessions(
            current_session=recorder.session_start,
            jobs=jobs_snapshot,
            open_wavs=open_wavs,
        )
    )

    # Mutation: load → sync → save (on event loop, serialised with /api/people)
    registry, occs = attach_people_mutation(sessions_list, live_identities=live_identities)

    # Thread hop 2: config reads + people joins + payload build + serialize + ETag
    body, etag = await asyncio.to_thread(
        build_state_blob,
        recorder.session_start,
        active,
        sessions_list,
        registry,
        occs,
        live_identities,
        recorder.transcripts.snapshot(),
        dict(recorder.live.info),
        # Last 30 lines without copying the whole 200-entry deque on every
        # poll tick — islice walks straight to the tail.
        list(islice(recorder.live.log, max(0, len(recorder.live.log) - 30), None)),
        bool(getattr(recorder.live, "supports_native_vad", False)),
        recorder.recording_enabled,
        recorder.backend,
        sorted(available_backend_strs()),
    )

    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if req.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
