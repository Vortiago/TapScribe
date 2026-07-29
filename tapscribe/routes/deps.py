"""The one FastAPI dependency every router shares.

Lives here rather than in `app.py` so a router module never imports the app
module (which would be a cycle: `app.py` imports the routers). `app.py`
re-exports `get_recorder` for compatibility: `app.dependency_overrides` keys on
the callable's IDENTITY, so `from tapscribe.app import get_recorder` in a test
resolves the same object the routers depend on.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from ..recorder import Recorder


def get_recorder(request: Request) -> Recorder:
    """FastAPI dependency that returns the singleton Recorder attached to
    the app instance. Tests override this via `app.dependency_overrides[
    get_recorder] = lambda: my_recorder` for per-test isolation."""
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        raise HTTPException(503, "Recorder not attached to app.state")
    return recorder
