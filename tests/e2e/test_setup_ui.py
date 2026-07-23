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


async def test_backend_select_defaults_to_first_backend(running_recorder: RunningRecorder):
    """Regression: a family with >1 host-valid backend renders a backend <select>
    that DEFAULTS to (and so submits) the first backend, not a blank selection.
    A dropped field `value` once left the select blank while state held
    backends[0] — invisible on a single-backend (cpu-only) host like CI."""
    from tapscribe.runtime_probe import set_available_backends_for_testing

    # Two host-valid kinds → Whisper renders a real dropdown (cuda, cpu).
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    try:
        async with playwright_session() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await (await browser.new_context()).new_page()
                await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")
                select = page.get_by_label("Whisper backend", exact=True)
                await expect(select).to_be_visible(timeout=8000)
                # the visible selection must be a real backend (first = cuda), not blank
                await expect(select).to_have_value("cuda")
            finally:
                await browser.close()
    finally:
        set_available_backends_for_testing(None)


async def test_stale_selection_banner_names_the_skipped_family(
    running_recorder: RunningRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """A family the picker SKIPPED (its saved backend left the catalog) has to
    be visible on /setup, not just on stderr.

    In a Bundle the picker's stderr goes to a log file the operator never opens
    (ADR-0015), so an upgrade would silently stop installing their models with
    nothing anywhere to explain it. /setup is where they'd go to fix it, so
    that's where it has to be said — and it must name the family, since "some
    model didn't install" is not actionable.
    """
    import json

    from tapscribe import config as _config

    sidecar = tmp_path / ".tapscribe-install-warnings.json"
    sidecar.write_text(
        json.dumps(
            {"stale_backends": [{"family": "parakeet", "label": "Parakeet (NVIDIA)", "backend": "mlx"}]}
        )
    )
    monkeypatch.setattr(_config, "INSTALL_WARNINGS_FILE", sidecar)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")

            # tone="warn" renders role="status" (only tone="bad" is role="alert").
            banner = page.get_by_role("status").filter(has_text="Parakeet (NVIDIA)")
            await expect(banner).to_be_visible(timeout=8000)
            await expect(banner).to_contain_text("mlx")

            assert errors == [], f"console/page errors on /setup: {errors}"
        finally:
            await browser.close()


async def test_no_stale_banner_when_the_selection_is_clean(
    running_recorder: RunningRecorder, tmp_path, monkeypatch
):
    """The banner must not appear on the happy path — a permanently-visible
    warning is one nobody reads."""
    from tapscribe import config as _config

    monkeypatch.setattr(_config, "INSTALL_WARNINGS_FILE", tmp_path / "absent.json")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")
            await expect(
                page.get_by_role("heading", name=re.compile(r"Set up TapScribe|Manage models"))
            ).to_be_visible(timeout=8000)
            assert await page.locator("#stale").inner_html() == ""
        finally:
            await browser.close()


# ── WhisperLiveKit live-channel opt-out (#374) ──────────────────────────────
#
# `setup_install._STATE_FILE` is repointed at an unwritten tmp path for every
# test below — the DEFAULT param it feeds `read_picker_state()` is re-read
# fresh on each call (not bound at import time; see `read_picker_state`'s
# docstring), so this monkeypatch reaches the real `/api/setup/state` route
# exactly like `config.INSTALL_WARNINGS_FILE` does for the stale-selection
# banner above. Without it, the test would depend on whatever
# `.tapscribe-install.json` (if any) happens to sit at the real repo root.

_LIVE_LABEL = "Live captions (WhisperLiveKit — adds PyTorch)"


async def test_live_toggle_renders_and_defaults_checked(
    running_recorder: RunningRecorder, tmp_path, monkeypatch
):
    """No persisted choice (a fresh install, or a state file predating #374)
    → the toggle renders on the Whisper row and starts CHECKED — the live
    channel is opt-OUT, not opt-in."""
    from tapscribe import setup_install as _setup_install

    monkeypatch.setattr(_setup_install, "_STATE_FILE", tmp_path / "absent.json")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")

            toggle = page.get_by_label(_LIVE_LABEL)
            await expect(toggle).to_be_visible(timeout=8000)
            await expect(toggle).to_be_checked()
        finally:
            await browser.close()


async def test_live_toggle_appears_exactly_once_not_on_nb_whisper_too(
    running_recorder: RunningRecorder, tmp_path, monkeypatch
):
    """NB-Whisper is ALSO `f.live === true` in the catalog (it rides the same
    faster-whisper backend + whisperlivekit as Whisper), but both catalog
    families fold onto the picker's ONE `whisper` install family with ONE
    `live` bit (see `familyRow`'s comment). A second checkbox on NB-Whisper's
    row would look like an independent setting when it isn't — and
    `selectedLive()` only ever reads the first one, so a second checkbox
    would silently do nothing when toggled. Pin the count explicitly rather
    than relying on `get_by_label`'s strict-mode violation to catch a
    regression back to gating on `f.live`."""
    from tapscribe import setup_install as _setup_install

    monkeypatch.setattr(_setup_install, "_STATE_FILE", tmp_path / "absent.json")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")

            await expect(page.get_by_text("NB-Whisper", exact=False).first).to_be_visible(timeout=8000)
            await expect(page.locator(".fam__live-check")).to_have_count(1)
        finally:
            await browser.close()


async def test_live_toggle_starts_unchecked_when_persisted_off(
    running_recorder: RunningRecorder, tmp_path, monkeypatch
):
    """The toggle pre-checks to the OPERATOR'S actual last choice
    (`state.live_channel`), not always to checked."""
    import json

    from tapscribe import setup_install as _setup_install

    state_file = tmp_path / ".tapscribe-install.json"
    state_file.write_text(
        json.dumps({"version": 2, "choices": {"whisper": {"enabled": True, "backend": "cpu", "live": False}}})
    )
    monkeypatch.setattr(_setup_install, "_STATE_FILE", state_file)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")

            toggle = page.get_by_label(_LIVE_LABEL)
            await expect(toggle).to_be_visible(timeout=8000)
            await expect(toggle).not_to_be_checked()
        finally:
            await browser.close()


async def test_unchecking_live_toggle_sends_live_false_in_the_install_request(
    running_recorder: RunningRecorder, tmp_path, monkeypatch
):
    """Unchecking the toggle and installing must POST `{"live": false, ...}` —
    the actual request-contract change #374 makes. The install itself is
    intercepted (never runs real pip; same stance as this file's other tests)."""
    from tapscribe import setup_install as _setup_install

    monkeypatch.setattr(_setup_install, "_STATE_FILE", tmp_path / "absent.json")
    captured: dict[str, object] = {}

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()

            async def _install_route(route):
                captured.update(route.request.post_data_json or {})
                await route.fulfill(
                    status=200,
                    content_type="text/event-stream",
                    body='data: {"phase": "start"}\n\ndata: {"phase": "done", "ok": true, "returncode": 0}\n\n',
                )

            await page.route("**/api/setup/install", _install_route)

            await page.goto(running_recorder.base_url + "/setup", wait_until="domcontentloaded")

            toggle = page.get_by_label(_LIVE_LABEL)
            await expect(toggle).to_be_checked(timeout=8000)
            await toggle.uncheck()

            install = page.get_by_role("button", name=re.compile("Install", re.IGNORECASE))
            await expect(install).to_be_enabled()
            await install.click()

            await expect(page.get_by_text("Installed.")).to_be_visible(timeout=8000)
            assert captured.get("live") is False
            assert "whisper" in (captured.get("families") or {})
        finally:
            await browser.close()
