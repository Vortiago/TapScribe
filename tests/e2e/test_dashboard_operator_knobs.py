"""RED contract for #210 — the operator knobs are EDITABLE IN THE DASHBOARD.

`test_operator_knobs_config.py` pins the resolver and `test_operator_knobs_state.py`
pins the routes. Neither can see whether a Settings FIELD exists: the whole point of
#210 is that "restart the server with an env var" is the one config channel the
product philosophy rejects, so a knob that is file-tunable but has no dashboard
control has not closed the issue.

WHY THIS HALF IS AN E2E: the Settings view is client-side. `ruff` + `pytest` are
blind to it, `tsc --noEmit` cannot see a field wired to the wrong `/api/config/{key}`
URL, and this repo's node tests are pure-logic and cannot click. Driving the real
browser is the only tier that observes the control — and playwright + Chromium are
installed here, so it is a legitimate gate rather than a punt.

Pinned per knob: a control with an ACCESSIBLE NAME (not a bare `data-slot` — a
data-slot-only contract lets a field ship with a screen-reader-meaningless label),
and for one representative knob the full operator loop — type, save, and see the
value survive a reload, which is what proves the field is wired to the route rather
than holding the value in the page.
"""

from __future__ import annotations

import importlib.util

import pytest

from .conftest import RunningRecorder
from .harness import playwright_session

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)


# data-slot for the field, its accessible name, and the config key it saves through.
# EVERY operator knob is pinned — including idle-TTL, whose backend landed in #347
# but which has no dashboard field either, so #210 is not closed without it.
KNOB_FIELDS = [
    ("setIdleTtlS", "model idle TTL", "model-idle-ttl", "600"),
    ("setChunkS", "parakeet chunk seconds", "parakeet-chunk-s", "300"),
    ("setOverlapS", "parakeet overlap seconds", "parakeet-overlap-s", "20"),
    ("setSummarizeTimeoutS", "summarize timeout", "summarize-timeout-s", "600"),
    ("setGgufCtx", "summarize context window", "summarize-gguf-ctx", "16384"),
]
_IDS = [k[0] for k in KNOB_FIELDS]


async def _open_settings(page, base_url: str) -> None:
    await page.goto(base_url, wait_until="domcontentloaded")
    # Boot is done once the spine has rendered its session picker.
    await page.wait_for_function(
        """() => !!document.querySelector('[data-slot="sessionPick"]')""",
        timeout=15000,
        polling=50,
    )
    await page.evaluate('() => window.gotoView("settings")')
    await page.wait_for_function(
        """() => {
          const r = document.querySelector('#viewRoot');
          return r && r.childElementCount > 0;
        }""",
        timeout=15000,
        polling=50,
    )


async def test_settings_has_a_named_field_for_every_operator_knob(
    running_recorder: RunningRecorder,
):
    """Each knob gets a labelled control in Settings — the deliverable #210 asks
    for. Base has none of them."""
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await _open_settings(page, running_recorder.base_url)

            missing_slot: list[str] = []
            missing_name: list[str] = []
            for slot, name, _key, _val in KNOB_FIELDS:
                if await page.locator(f'#viewRoot [data-slot="{slot}"]').count() == 0:
                    missing_slot.append(slot)
                # The accessible name is what a screen reader (and the operator)
                # actually gets — a data-slot alone is a structural seam, not a label.
                if await page.locator("#viewRoot").get_by_label(name).count() == 0:
                    missing_name.append(name)

            assert not missing_slot, f"Settings is missing a control for: {missing_slot}"
            assert not missing_name, f"Settings controls without an accessible name: {missing_name}"
        finally:
            await browser.close()


async def test_saving_a_knob_in_settings_survives_a_reload(
    running_recorder: RunningRecorder,
):
    """The full operator loop for a representative knob: type it, save it, reload,
    and it is still there. A field that only holds the value in the page (never
    reaching PUT /api/config/{key}) passes a presence check and fails here."""
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await _open_settings(page, running_recorder.base_url)

            field = page.locator('#viewRoot [data-slot="setChunkS"]')
            await field.fill("300")
            await page.click('#viewRoot [data-slot="setChunkSSave"]')

            # The value must be durable, not page state: reload and re-open.
            await _open_settings(page, running_recorder.base_url)
            await page.wait_for_function(
                """() => {
                  const f = document.querySelector('#viewRoot [data-slot="setChunkS"]');
                  return f && f.value === "300";
                }""",
                timeout=15000,
                polling=50,
            )
        finally:
            await browser.close()


async def test_settings_shows_the_specialist_map_read_only(
    running_recorder: RunningRecorder,
):
    """#210 asks for the specialist language→model map to be VISIBLE, explicitly not
    editable yet ("visibility beats editability here"). So it renders as text and
    carries no save control — an editable field here would be scope creep that also
    turns a launch-time knob into a use-time one."""
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await _open_settings(page, running_recorder.base_url)

            line = page.locator('#viewRoot [data-slot="setSpecialists"]')
            assert await line.count() > 0, "Settings must show the specialist language→model map"
            text = (await line.inner_text()).strip()
            assert text, "the specialist map must render its content, not an empty node"
            assert await page.locator('#viewRoot [data-slot="setSpecialistsSave"]').count() == 0, (
                "the specialist map is read-only in #210 — it must have no save control"
            )
        finally:
            await browser.close()
