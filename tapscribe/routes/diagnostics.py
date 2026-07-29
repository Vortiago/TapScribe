"""Liveness, readiness, and the browser error relay.

  POST  /api/client-errors  browser error beacon: log-and-drop, flood-guarded
  GET   /health             session dir + ok, the original probe
  GET   /healthz            richer liveness + readiness probe (auth-exempt)

`/healthz` is auth-exempt (`config.AUTH_EXEMPT_ROUTES`) so a monitor can scrape
it without dashboard credentials; the relay and `/health` are Basic-auth gated
like the rest of the surface.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque

from fastapi import (
    APIRouter,
    Depends,
    Request,
)
from fastapi.responses import (
    JSONResponse,
    Response,
)

from ..recorder import Recorder
from .deps import get_recorder

router = APIRouter()


# Client-error relay (wireErrorBar, tapscribe/web/js/lib/chrome.js): the
# dashboard beacons unhandled browser errors here so an operator (or an LLM
# session maintaining the app) can read them from the server log instead of
# needing the browser console. Storage-free by design — log-and-drop, capped
# and flood-guarded, mirroring the toolkit's serve.mjs endpoint.
_CLIENT_ERR_WINDOW_S = 60.0
_CLIENT_ERR_MAX_PER_WINDOW = 30
_client_err_times: deque[float] = deque()


@router.post("/api/client-errors", status_code=204)
async def client_errors(request: Request) -> Response:
    now = time.monotonic()
    while _client_err_times and now - _client_err_times[0] > _CLIENT_ERR_WINDOW_S:
        _client_err_times.popleft()
    if len(_client_err_times) >= _CLIENT_ERR_MAX_PER_WINDOW:
        # Silently drop past the cap: the relay is best-effort telemetry and a
        # crash-looping page must not turn into a log flood.
        return Response(status_code=204)
    _client_err_times.append(now)

    # sendBeacon posts text/plain, so parse the (size-capped) body by hand
    # instead of a JSON body model. Every field is untrusted browser input:
    # cap lengths and strip newlines so a crafted message can't forge extra
    # log lines (log injection).
    raw = (await request.body())[:8192]
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    def _field(name: str, cap: int) -> str:
        value = payload.get(name, "")
        text = value if isinstance(value, str) else str(value)
        # Explicit log-injection hardening: neutralize CR/LF and drop other
        # control chars, then collapse remaining whitespace and cap length.
        text = text.replace("\r", " ").replace("\n", " ")
        text = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in text)
        return " ".join(text.split())[:cap]

    logging.getLogger("tapscribe.client").warning(
        "client error [%s] at %s: %s (ua: %s)",
        _field("src", 40),
        _field("url", 200),
        _field("msg", 2000),
        _field("ua", 200),
    )
    return Response(status_code=204)


@router.get("/health")
async def health(recorder: Recorder = Depends(get_recorder)):
    return {"status": "ok", "session_dir": str(recorder.session_dir)}


@router.get("/healthz")
async def healthz(recorder: Recorder = Depends(get_recorder)):
    """Liveness + readiness probe for monitoring (k8s, systemd watchdog,
    plain curl). No auth required — must also be exempt in
    `config.AUTH_EXEMPT_ROUTES`. The richer shape vs `/health` lets an
    operator alert on meaningful state (recording paused unexpectedly,
    live channel stuck in `error`) without scraping `/api/state`."""
    from .. import __version__

    active_streams = await recorder.streams.snapshot()
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "recording_enabled": recorder.recording_enabled,
            "live_channel_state": recorder.live.info.get("state", ""),
            "active_taps": len(active_streams),
        }
    )
