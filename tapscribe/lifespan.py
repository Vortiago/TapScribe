"""Process-lifetime concerns: the access-log filter and the app lifespan.

Neither is a route, and both have to run at a specific moment rather than at
import time, which is why they are not inline in `app.py`'s assembly:

- the poll-spam filter and the query-redaction filter are installed INSIDE the
  lifespan because uvicorn replaces the access logger's handlers via dictConfig()
  during its own boot, so anything added before `uvicorn.run()` is dropped;
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


class _RedactQuerySecrets(logging.Filter):
    """Strip the query string off access-log lines for paths whose query carries a
    credential, keeping the line itself.

    `GET /login?k=<token>` is the case: uvicorn's access log renders
    `get_path_with_query_string(scope)`, so the single-use login token lands in
    the Recorder's stdout — which, in a Bundle, the tray pumps into the rotating
    `recorder.log` that its own "Show log" invites the operator to open and paste.
    Inside the store's grace window a re-spend of that token re-issues the live
    session cookie.

    The route already goes to some length to keep the token out of every durable
    place — it answers a 303 precisely so the address bar, history and the next
    `Referer` never hold it. The access log was the one that got missed.

    Redacting rather than SILENCING: dropping the line would also drop the only
    record that somebody signed in, and when they did. Redacting rather than
    matching on the parameter NAME: renaming `k` would silently reopen the leak,
    whereas a path whose whole query is dropped cannot grow a second secret.
    """

    #: Paths whose query string is a credential. Matched exactly — a prefix match
    #: would swallow the query of anything nested underneath.
    _SECRET_QUERY_PATHS = ("/login",)

    def filter(self, record):
        # args[2] is uvicorn's path-with-query-string; see its `access_logger.info`
        # call in protocols/http/*_impl.py. Guarded rather than assumed, because a
        # record from anywhere else on this logger has its own shape and must pass
        # through untouched rather than raise inside logging.
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3 or not isinstance(args[2], str):
            return True
        path, sep, _query = args[2].partition("?")
        if sep and path in self._SECRET_QUERY_PATHS:
            record.args = args[:2] + (f"{path}?<redacted>",) + args[3:]
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Install the poll-spam and query-redaction filters on the uvicorn access
    # logger. We do this in the lifespan (not module load) because uvicorn
    # replaces the access logger's handlers via dictConfig() during its own
    # boot — anything we add before uvicorn.run() would be dropped.
    access_log = logging.getLogger("uvicorn.access")
    access_log.addFilter(_SuppressPollAccess())
    access_log.addFilter(_RedactQuerySecrets())

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
