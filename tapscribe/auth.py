"""HTTP Basic auth middleware + the /tap WebSocket subprotocol gate.

Two secrets live on the Recorder, both `SecretFile` instances:

  - `recorder.auth.value` — Basic auth for the dashboard / REST API.
  - `recorder.tap.value`  — bearer token for the /tap WebSocket,
                            carried in `Sec-WebSocket-Protocol`.

The Basic middleware here covers HTTP. The /tap gate is a pure helper
(`pick_tap_subprotocol`) called from the WS route handler — middleware
of this class can't intercept the WS upgrade.
"""

from __future__ import annotations

import base64
import hmac
from collections.abc import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config

# The bridge prepends this to the tap token and sends the joined string
# as a WebSocket subprotocol value. Versioned so we can add a second
# scheme later (e.g. signed JWT) without breaking older bridges.
TAP_SUBPROTOCOL_PREFIX: str = "tapscribe.v1.tap."


def pick_tap_subprotocol(offered: Iterable[str] | None, expected_token: str) -> str | None:
    """Return the subprotocol the server should echo back, or None when
    no offered protocol carries the right tap token. Constant-time
    compare so timing can't be used to guess the token character-by-
    character."""
    if not expected_token:
        return None
    for proto in offered or ():
        proto = proto.strip()
        if not proto.startswith(TAP_SUBPROTOCOL_PREFIX):
            continue
        offered_token = proto[len(TAP_SUBPROTOCOL_PREFIX):]
        if hmac.compare_digest(offered_token, expected_token):
            return proto
    return None


async def basic_auth_middleware(request: Request, call_next):
    """HTTP Basic auth gate. Skips WebSocket upgrades (FastAPI middlewares
    of this kind don't see WS) and the routes in `AUTH_EXEMPT_ROUTES`.
    Constant-time comparison so the response time can't be used to guess
    characters.

    The /tap WebSocket has its own auth path (a bearer token in
    `Sec-WebSocket-Protocol`, validated by `pick_tap_subprotocol` above
    and called from the WS route handler).
    """
    if not config.AUTH_ENABLED:
        return await call_next(request)
    # CORS preflight: browsers never send Basic-auth credentials on OPTIONS.
    # If auth blocked preflight, the actual cross-origin POST from the
    # bridge (spatial.chat → recorder) would never fire. Let
    # CORSMiddleware handle these unconditionally.
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    if (request.method.upper(), request.url.path) in config.AUTH_EXEMPT_ROUTES:
        return await call_next(request)

    realm_header = {"WWW-Authenticate": 'Basic realm="TapScribe recorder"'}
    auth_header = request.headers.get("authorization") or ""
    if not auth_header.lower().startswith("basic "):
        return JSONResponse({"detail": "Authentication required"}, status_code=401, headers=realm_header)
    try:
        decoded = base64.b64decode(auth_header.split(" ", 1)[1].strip(), validate=False).decode("utf-8")
    except Exception:
        return JSONResponse({"detail": "Malformed Authorization header"}, status_code=401, headers=realm_header)
    user, _, pw = decoded.partition(":")

    # Recorder may not be attached yet (e.g. transient state during boot
    # before app.state.recorder is set). Refuse the request in that case
    # rather than crashing the middleware.
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        return JSONResponse({"detail": "Recorder not ready"}, status_code=503)

    user_ok = hmac.compare_digest(user, config.AUTH_USER)
    pass_ok = hmac.compare_digest(pw, recorder.auth.value)
    if not (user_ok and pass_ok):
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401, headers=realm_header)
    return await call_next(request)
