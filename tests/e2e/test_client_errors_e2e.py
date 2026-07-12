"""Backend E2E for ``POST /api/client-errors`` — the dashboard's client-error
relay (``wireErrorBar`` in ``tapscribe/web/js/lib/chrome.js`` beacons unhandled
browser errors here; the server logs and drops them, storage-free).

Pure httpx, so this runs in the lightweight ``pytest tests`` CI matrix. The
contract mirrors the toolkit's ``serve.mjs`` endpoint:

- always 204 (best-effort telemetry — the page must never see an error from
  its own error reporter);
- tolerant of sendBeacon's ``text/plain`` bodies and of garbage bodies;
- untrusted fields are length-capped and newline-stripped before logging
  (log-injection guard);
- flood-guarded per minute so a crash-looping page can't flood the log.
"""

from __future__ import annotations

import json
import logging

import httpx

from tapscribe import app as app_module

from .conftest import RunningRecorder


async def test_client_errors_logs_sanitized_and_floods_silently(running_recorder: RunningRecorder, caplog):
    # The flood window is module-global state shared across tests in one
    # process — reset it so this test owns the budget it asserts on.
    app_module._client_err_times.clear()

    payload = {
        "msg": "TypeError: x is undefined\nFAKE LOG LINE injected=true",
        "src": "unhandledrejection",
        "url": "#transcript",
        "ua": "pytest",
    }
    async with httpx.AsyncClient(base_url=running_recorder.base_url) as client:
        with caplog.at_level(logging.WARNING, logger="tapscribe.client"):
            # sendBeacon posts text/plain — no JSON content-type.
            r = await client.post(
                "/api/client-errors",
                content=json.dumps(payload).encode(),
                headers={"content-type": "text/plain;charset=UTF-8"},
            )
            assert r.status_code == 204

            # Garbage body: still 204, nothing raised.
            r = await client.post("/api/client-errors", content=b"\xff\xfenot json")
            assert r.status_code == 204

        joined = "\n".join(rec.message for rec in caplog.records)
        assert "TypeError: x is undefined" in joined
        # The injected newline must not survive into the log record.
        assert "FAKE LOG LINE" in joined  # content kept…
        assert "\nFAKE LOG LINE" not in joined  # …but folded onto one line
        assert "#transcript" in joined

        # Flood guard: past the per-window cap the endpoint still answers 204
        # but stops logging.
        with caplog.at_level(logging.WARNING, logger="tapscribe.client"):
            before = len([r for r in caplog.records if r.name == "tapscribe.client"])
            for _ in range(app_module._CLIENT_ERR_MAX_PER_WINDOW + 10):
                r = await client.post("/api/client-errors", content=json.dumps(payload).encode())
                assert r.status_code == 204
            after = len([r for r in caplog.records if r.name == "tapscribe.client"])
        assert after - before <= app_module._CLIENT_ERR_MAX_PER_WINDOW
