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

# The TAP-BEARER scheme matches every path under TAP_PREFIX/. Pre-joined
# once at import (the prefix is a constant) since the middleware tests it
# against every HTTP request path — no per-request concatenation.
_TAP_PREFIX_SLASH: str = config.TAP_PREFIX + "/"


def _utf8_compare_digest(a: str, b: str) -> bool:
    """`hmac.compare_digest` needs equal-type operands, but every credential
    here starts life as `str`. Centralizing the `.encode("utf-8")` in one
    place means a future credential-comparison site can't forget it and
    reintroduce the non-ASCII `TypeError` crash this was written to close
    (#194)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


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
        offered_token = proto[len(TAP_SUBPROTOCOL_PREFIX) :]
        if _utf8_compare_digest(offered_token, expected_token):
            return proto
    return None


def check_tap_bearer(authorization: str | None, expected_token: str) -> bool:
    """Validate an HTTP ``Authorization: Bearer <tap-token>`` header against the
    expected tap token, constant-time. The HTTP twin of
    ``pick_tap_subprotocol``: the WS handshake can't set arbitrary headers so it
    carries the token in a subprotocol, whereas ``fetch`` can, so HTTP control
    endpoints (``POST /api/tap/new-session``) use a bearer header. Returns False
    on a missing/malformed header or a token mismatch.

    Empty ``expected_token`` → False (mirrors ``pick_tap_subprotocol``); callers
    gate this behind ``config.AUTH_ENABLED`` exactly as the WS path does, so an
    auth-disabled recorder never reaches here.
    """
    if not expected_token:
        return False
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return _utf8_compare_digest(token.strip(), expected_token)


async def basic_auth_middleware(request: Request, call_next):
    """The HTTP auth gate: dispatch every request to exactly ONE of three
    schemes, so they can't drift apart (see CONTEXT.md "HTTP auth gate ·
    auth schemes" and ADR-0008). Constant-time comparisons throughout so
    response time can't be used to guess characters.

      * PUBLIC     — exact (method, path) in `config.AUTH_EXEMPT_ROUTES`
                     (/health, /healthz). No credential.
      * TAP-BEARER — any path under `config.TAP_PREFIX` (/api/tap): the
                     Bridge's HTTP control plane. The SAME branch that
                     routes these past Basic auth also REQUIRES the tap
                     bearer (`check_tap_bearer`), so a bearer-less
                     /api/tap/* route is impossible by construction and the
                     handlers carry no gate of their own.
      * BASIC      — everything else: dashboard HTTP Basic against
                     `recorder.auth.value`.

    The /tap WebSocket is a separate path: middlewares of this kind don't
    see WS upgrades, so it carries the token in `Sec-WebSocket-Protocol`,
    validated by `pick_tap_subprotocol` from the WS route handler.
    """
    if not config.AUTH_ENABLED:
        return await call_next(request)
    # CORS preflight: browsers never send credentials on OPTIONS. If auth
    # blocked preflight, the actual cross-origin POST from the bridge
    # (spatial.chat → recorder) would never fire. Let CORSMiddleware handle
    # these unconditionally.
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    # PUBLIC scheme — exact (method, path) match, no credential. Checked
    # before the recorder fetch so health probes answer during boot.
    if (request.method.upper(), request.url.path) in config.AUTH_EXEMPT_ROUTES:
        return await call_next(request)

    # The Recorder holds both secrets; refuse cleanly if it isn't attached
    # yet (transient boot state) rather than crashing the middleware. Both
    # the TAP-BEARER and BASIC schemes below read from it.
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        return JSONResponse({"detail": "Recorder not ready"}, status_code=503)

    # TAP-BEARER scheme — the Bridge's control plane. One predicate: routes
    # under TAP_PREFIX skip Basic auth AND must carry the tap bearer, so the
    # two halves of the invariant can never diverge. AUTH_ENABLED is already
    # true here (the early return above), so no need to re-check it.
    if request.url.path.startswith(_TAP_PREFIX_SLASH):
        if not check_tap_bearer(request.headers.get("authorization"), recorder.tap.value):
            return JSONResponse({"detail": "invalid tap token"}, status_code=401)
        return await call_next(request)

    # BASIC scheme — the dashboard / REST default.
    realm_header = {"WWW-Authenticate": 'Basic realm="TapScribe recorder"'}
    auth_header = request.headers.get("authorization") or ""
    if not auth_header.lower().startswith("basic "):
        return JSONResponse({"detail": "Authentication required"}, status_code=401, headers=realm_header)
    try:
        decoded = base64.b64decode(auth_header.split(" ", 1)[1].strip(), validate=False).decode("utf-8")
    except Exception:
        return JSONResponse(
            {"detail": "Malformed Authorization header"}, status_code=401, headers=realm_header
        )
    user, _, pw = decoded.partition(":")
    user_ok = _utf8_compare_digest(user, config.AUTH_USER)
    pass_ok = _utf8_compare_digest(pw, recorder.auth.value)
    if not (user_ok and pass_ok):
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401, headers=realm_header)
    return await call_next(request)
