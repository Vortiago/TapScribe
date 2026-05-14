"""HTTP Basic auth middleware.

The password itself lives on the Recorder (`recorder.auth.password`,
loaded/persisted via `AuthState`). This module only contains the
FastAPI middleware that reads it via `request.app.state.recorder`.
"""

from __future__ import annotations

import base64
import hmac

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config


async def basic_auth_middleware(request: Request, call_next):
    """HTTP Basic auth gate. Skips WebSocket upgrades (FastAPI middlewares
    of this kind don't see WS) and the routes in `AUTH_EXEMPT_ROUTES`.
    Constant-time comparison so the response time can't be used to guess
    characters.

    Known gap: the /record WebSocket is NOT protected here because adding
    auth there requires the bridge extension to send the password during
    the WS handshake, which is a bigger plumbing change. In LAN mode the
    operator should still be cautious about what's running on the network.
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
    pass_ok = hmac.compare_digest(pw, recorder.auth.password)
    if not (user_ok and pass_ok):
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401, headers=realm_header)
    return await call_next(request)
