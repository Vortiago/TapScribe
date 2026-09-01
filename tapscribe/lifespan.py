"""Process-lifetime concerns: the access-log filter and the app lifespan.

Neither is a route, and both have to run at a specific moment rather than at
import time, which is why they are not inline in `app.py`'s assembly:

- the poll-spam filter is installed INSIDE the lifespan because uvicorn replaces
  the access logger's handlers via dictConfig() during its own boot, so anything
  added before `uvicorn.run()` is dropped;
- the boot live-channel reconcile needs the Recorder that `__main__.py` attaches
  to `app.state` after import.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config
from .live_control import DesiredLiveState, LiveReconcileError, apply_live, plan_live
from .recorder import Recorder


class _SuppressPollAccess(logging.Filter):
    """Drop uvicorn access logs for the dashboard's per-second poll
    endpoints so the terminal isn't flooded. Real activity (POST
    /api/transcribe, DELETE /api/sessions/..., websocket records) still
    surfaces."""

    _SILENCED = ("/api/state", "/dashboard.css", "/next.css", "/web/", "/health", "/healthz")

    def filter(self, record):
        msg = record.getMessage()
        for needle in self._SILENCED:
            if needle in msg:
                return False
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Install poll-spam filter on the uvicorn access logger. We do this in
    # the lifespan (not module load) because uvicorn replaces the access
    # logger's handlers via dictConfig() during its own boot — anything we
    # add before uvicorn.run() would be dropped.
    logging.getLogger("uvicorn.access").addFilter(_SuppressPollAccess())

    # JSON logging is opt-in via --log-json (set on app.state in __main__).
    # Same dictConfig race as the poll filter — apply here, not at import.
    if getattr(app.state, "log_json", False):
        from .logging_setup import install_json_logging

        install_json_logging()

    # The dashboard's login-link store (ADR-0023). Per-app rather than a module
    # global, so tests never share one another's sessions; in memory, so a
    # Recorder restart logs the browser out, which is one tray click and no
    # third secret at rest.
    from .login_links import LoginLinks

    app.state.login_links = LoginLinks()

    recorder: Recorder | None = getattr(app.state, "recorder", None)
    if recorder is not None and config.AUTO_START_LIVE:
        # Reconcile the boot channel toward the operator's persisted default
        # live model (config/live-model.txt) — the SAME transition
        # /api/live/start runs. The Recorder always constructs a
        # WhisperLiveKitChannel at boot, so a persisted Moonshine default
        # needs a family swap even though config.model is unchanged (#259);
        # `plan_live` resolves that swap unconditionally. Auto-start stays
        # best-effort: a reconcile failure (e.g. a weights fetch) is logged
        # and skipped, never crashing startup.
        rec = recorder
        desired = DesiredLiveState(model=rec.live.config.model)
        try:
            plan = plan_live(rec.live, desired, use_mlx=rec.use_mlx)
            await asyncio.to_thread(apply_live, rec.live, plan, set_live=lambda ch: setattr(rec, "live", ch))
        except LiveReconcileError as exc:
            print(f"[tapscribe] live auto-start skipped: {exc}", flush=True)
    try:
        yield
    finally:
        if recorder is not None:
            recorder.live.stop(timeout=3.0)
