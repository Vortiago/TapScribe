"""HTTP Basic auth — password persistence + middleware.

The dashboard is an admin tool that exposes transcripts, delete buttons,
and live-channel controls; on LAN we don't want anyone else on the network
poking it. Username is fixed (`admin`); password is generated on first run
and persisted to `.auth-password` so the browser stays logged in across
recorder restarts.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config


def load_or_create_password(path: Path | None = None) -> str:
    """Read the password from the auth file, or generate-and-write one if
    missing/empty. Best-effort chmod 0600 on POSIX; Windows ignores file
    modes here, but the file is hidden by the leading dot and lives in the
    repo root which is gitignored."""
    p = path or config.AUTH_PASSWORD_FILE
    try:
        if p.is_file():
            existing = p.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    pw = secrets.token_urlsafe(12)
    try:
        p.write_text(pw, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except (OSError, NotImplementedError):
            pass  # Windows / restricted FS — best-effort
    except OSError as e:
        print(f"[tapscribe] WARNING: could not write {p}: {e}", flush=True)
        print("[tapscribe]          password will rotate on next restart.", flush=True)
    return pw


# Module-level cache so the middleware can reach it without dependency
# injection. Refreshed by load_or_create_password() at boot.
AUTH_PASS: str = load_or_create_password()


def set_password(new_pw: str) -> None:
    """Update the module-level AUTH_PASS. Used by __main__ after a
    --rotate-password flag wipes the on-disk file."""
    global AUTH_PASS
    AUTH_PASS = new_pw


async def basic_auth_middleware(request: Request, call_next):
    """HTTP Basic auth gate. Skips WebSocket upgrades (FastAPI middlewares
    of this kind don't see WS) and the /health path. Constant-time
    comparison so the response time can't be used to guess characters.

    Known gap: the /record WebSocket is NOT protected here because adding
    auth there requires the bridge extension to send the password during
    the WS handshake, which is a bigger plumbing change. In LAN mode the
    operator should still be cautious about what's running on the network.
    """
    if not config.AUTH_ENABLED:
        return await call_next(request)
    # CORS preflight: browsers never send Basic-auth credentials on OPTIONS.
    # If auth blocked preflight, the actual cross-origin POST from the
    # extension (spatial.chat → recorder) would never fire. Let
    # CORSMiddleware handle these unconditionally.
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    if (request.method.upper(), request.url.path) in config.AUTH_EXEMPT_ROUTES:
        return await call_next(request)

    realm_header = {"WWW-Authenticate": 'Basic realm="TapScribe recorder"'}
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("basic "):
        return JSONResponse({"detail": "Authentication required"}, status_code=401, headers=realm_header)
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1].strip(), validate=False).decode("utf-8")
    except Exception:
        return JSONResponse({"detail": "Malformed Authorization header"}, status_code=401, headers=realm_header)
    user, _, pw = decoded.partition(":")
    user_ok = hmac.compare_digest(user, config.AUTH_USER)
    pass_ok = hmac.compare_digest(pw, AUTH_PASS)
    if not (user_ok and pass_ok):
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401, headers=realm_header)
    return await call_next(request)
