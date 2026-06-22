"""Browser smoke for the /setup page.

Drives real headless Chromium against the running uvicorn server: the page
boots, reads GET /api/setup/state, and renders the family picker + install
button with no console/page errors. It does NOT click Install — that would run
a real `pip install`. Robust to host install state (first-run vs. manage), so
it passes whether or not model backends are installed in the test env.

Skipped when Playwright isn't installed (same as test_dashboard_ui.py).
"""

from __future__ import annotations

import importlib.util
import re

import pytest

from .conftest import RunningRecorder
from .harness import playwright_session

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from playwright.async_api import expect  # noqa: E402 — after the skip so collection is clean


async def test_setup_page_renders_families_and_install_button(running_recorder: RunningRecorder):
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 900, "height": 800})
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")

            # Title resolves to one of the two modes once /api/setup/state loads.
            heading = page.get_by_role("heading", name=re.compile(r"Set up TapScribe|Manage models"))
            await expect(heading).to_be_visible(timeout=8000)

            # The Whisper family row renders (always in the catalog).
            await expect(page.get_by_text("Whisper", exact=False).first).to_be_visible(timeout=8000)

            # The install CTA renders and becomes enabled once state has loaded.
            install = page.get_by_role("button", name=re.compile("Install", re.IGNORECASE))
            await expect(install).to_be_enabled(timeout=8000)

            assert errors == [], f"console/page errors on /setup: {errors}"
        finally:
            await browser.close()
