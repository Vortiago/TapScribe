"""#354 — the original/stripped source pick is ONE session-scoped store, shared
by the Recordings and Transcript views.

`effectiveSource(session, sourcePick)` was lifted to `next/shell.js` as a pure
function, but the state it reads stayed forked: `recordings.js` and
`transcript.js` each build their own `new Map()` (shell.js says so in the
helper's own docstring — "each keeps its own sourcePick map"). The computation
is shared while the source of truth is not, so the two views disagree about the
SAME session: pick "stripped" in Recordings, open Transcript, and it still
lists — and transcribes — the originals.

THE DECISION THIS CONTRACT MAKES: the pick belongs to the SESSION, not to the
view. The issue left it open (shared, or deliberately per-view + documented);
shared is the reading it calls "arguably the correct mental model" — it is
*the session's* source. One store in shell.js next to `effectiveSource`, both
views read and write it.

WHY AN E2E: the store is in-memory browser state read by two view closures.
`ruff` and `pytest` never load it, `tsc --noEmit` cannot see that two modules
kept separate Maps, and the dashboard's node tests cannot switch stages. Only a
real browser that picks in one view and then LOOKS at the other can tell shared
from forked. The pure-logic half of this contract — per-session scoping, the
no-stripped-folder fallback, the accessor arity — lives in
`tapscribe/web/js/next/source-pick.test.js`.

THE TRAP THIS IS BUILT AROUND — the stage switch below goes through
`window.gotoView(...)`, NEVER `page.goto`. A reload rebuilds every view and
resets the in-memory pick, which would make forked and shared indistinguishable
(both read "original" on a fresh page). And because main.js CACHES built views,
the other view is already alive holding its own idea of the source when the
operator arrives — so a fix that only seeds a shared value at build() time
still fails here. `src` is a term in both views' render signatures
(recordings' `chromeSig`, transcript's `ctlSig`), which is what must carry the
repaint; a fix that caches the resolved source per view instead of reading the
store each tick paints stale.

AND THE WRITE IS NOT THE WHOLE HANDLER — Recordings' own `onPick` also DROPS
the live strip-preview. Lifting the state moves the write off that handler, so
the last test pins that a pick made in Transcript still clears the preview:
every side effect the local writer performed has to keep firing when the write
originates elsewhere.

Every assertion is on what the operator sees (which toggle is lit, which rows
are listed, which stats and overlay the wave header shows) or on what the
transcribe request carries — never on the store's shape. DELIBERATELY NOT PINNED: whether the accessors take a session id or a
session object, how the store is keyed internally, how each view invalidates
its render signature.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

from .conftest import RunningRecorder
from .harness import playwright_session, stream_wav_via_tap, streams_drained, wait_until

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)


# The Recordings toggle renders into the view header's `actions`; the Transcript
# one into its own control-column host. Both come from `buildSourceToggle`, so
# both are a `.srcsw` marking the active button `.is-on`. Scoped to the toggle,
# never bare `[data-src]` — the WAV rows carry that attribute too.
TX_TOGGLE = '[data-slot="srcSwHost"] .srcsw'
REC_TOGGLE = "#viewRoot .srcsw"
# The hero canvas publishes its live strip-preview on `data-preview-spans` (the
# waveform component's declared e2e hook), so "is a preview still overlaid?" is
# observable without reading into the view's closure.
REC_PREVIEW = "#viewRoot .wave-canvas[data-preview-spans]"


async def _session_with_stripped_clips(rr: RunningRecorder, tmp_path: Path) -> int:
    """Record one WAV of alternating speech/silence and strip it, so the focused
    session has both originals and a stripped/ folder. Returns the clip count —
    without a stripped/ folder the "stripped" button stays disabled and there is
    no pick to share."""
    from .test_pipeline_strip_silence import _build_speech_silence_wav

    src = _build_speech_silence_wav(tmp_path / "src.wav")
    await stream_wav_via_tap(
        ws_base_url=rr.ws_base_url, identity="alice", name="Alice", wav_path=src, utterance_id="utt1"
    )
    assert await wait_until(lambda: streams_drained(rr.recorder), timeout=5.0)
    async with httpx.AsyncClient(base_url=rr.base_url, timeout=30.0) as c:
        r = await c.post(
            f"/api/sessions/{rr.recorder.session_start}/strip-silence",
            json={"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0},
        )
        assert r.status_code == 200 and r.json()["files_written"] == 1, r.text
    clips = sorted((rr.recorder.session_dir / "stripped").glob("*.wav"))
    assert len(clips) == 3, f"expected 3 region clips to pick between, got {[c.name for c in clips]}"
    return len(clips)


async def _lit_source(page, host: str) -> str:
    """Which source a toggle is showing as active. Waits for SOME button to be
    lit rather than for the expected one, so a wrong source fails with the value
    it found instead of an anonymous timeout."""
    await page.wait_for_selector(f"{host} [data-src].is-on", timeout=8000)
    return await page.get_attribute(f"{host} [data-src].is-on", "data-src")


async def _wave_stats(page) -> dict:
    """Recordings' wave-header stat quartet as the operator reads it. The live
    preview and the on-disk stripped summary paint the SAME four slots, so the
    values are what tells "uncommitted preview" from "committed cut" apart."""
    return await page.evaluate(
        """() => Object.fromEntries(["sClips", "sSpeech", "sIn", "sKept"].map((s) =>
             [s, document.querySelector(`#viewRoot [data-slot="${s}"]`)?.textContent ?? ""]))"""
    )


async def test_a_stripped_pick_in_recordings_is_the_same_pick_in_transcript(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The headline: the operator picks "stripped" in Recordings, walks to
    Transcript, and Transcript is on the stripped clips too — lit toggle AND a
    range transcribe that actually sends source=stripped. The transcribe POST is
    intercepted, so this needs no model."""
    rr = running_recorder
    n_clips = await _session_with_stripped_clips(rr, tmp_path)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            captured: dict = {}
            transcribe_posted = asyncio.Event()

            async def _route(route):
                captured.update(route.request.post_data_json or {})
                transcribe_posted.set()
                await route.fulfill(status=200, content_type="application/json", body='{"ok": true}')

            await page.route("**/api/transcribe-session", _route)
            await page.goto(rr.base_url + "/#recordings", wait_until="domcontentloaded")
            # The toggle only renders once the WAV listing has arrived.
            await page.wait_for_selector("#viewRoot .wavlist .wavrow", timeout=10000)

            await page.locator("#viewRoot").get_by_role("button", name="stripped", exact=True).click()
            await page.wait_for_function(
                """(n) => document.querySelectorAll('#viewRoot .wavlist .wavrow.is-clip').length === n""",
                arg=n_clips,
                timeout=10000,
            )

            # In-page stage switch — a reload would reset the pick and prove
            # nothing. Transcript is already BUILT and cached at this point.
            await page.evaluate('() => window.gotoView("transcript")')
            await page.wait_for_selector(f"{TX_TOGGLE} [data-src]", timeout=10000)

            lit = await _lit_source(page, TX_TOGGLE)
            assert lit == "stripped", (
                f"Transcript must show the session's pick made in Recordings, but it is on {lit!r} — "
                "the two views are still holding separate sourcePick maps"
            )

            await page.locator('[data-slot="txRangeBtn"]').click()
            await asyncio.wait_for(transcribe_posted.wait(), timeout=8.0)
            assert captured.get("source") == "stripped", (
                f"the range transcribe must run on the picked source, got {captured!r} — "
                "a lit toggle that still transcribes the originals is the operator harm"
            )
        finally:
            await browser.close()


async def test_a_stripped_pick_in_transcript_is_the_same_pick_in_recordings(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The other direction, because a one-way wiring (Recordings writes,
    Transcript reads) satisfies the test above while leaving the fork half
    alive. Recordings' harm layer is its WAV list: on the stripped source it
    lists the region clips."""
    rr = running_recorder
    n_clips = await _session_with_stripped_clips(rr, tmp_path)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#transcript", wait_until="domcontentloaded")
            await page.wait_for_selector(f"{TX_TOGGLE} [data-src]", timeout=10000)

            stripped_btn = page.locator(f'{TX_TOGGLE} [data-src="stripped"]')
            assert not await stripped_btn.is_disabled(), "stripped must enable once a stripped/ folder exists"
            await stripped_btn.click()
            await page.wait_for_selector(f'{TX_TOGGLE} [data-src="stripped"].is-on', timeout=8000)

            await page.evaluate('() => window.gotoView("recordings")')
            await page.wait_for_selector("#viewRoot .wavlist .wavrow", timeout=10000)

            lit = await _lit_source(page, REC_TOGGLE)
            assert lit == "stripped", (
                f"Recordings must show the session's pick made in Transcript, but it is on {lit!r}"
            )
            clips = await page.locator("#viewRoot .wavlist .wavrow.is-clip").count()
            assert clips == n_clips, (
                f"Recordings must list the {n_clips} stripped clips for the picked source, got {clips} — "
                "a lit toggle over an unchanged list is a half-applied pick"
            )
        finally:
            await browser.close()


async def test_a_pick_made_in_transcript_drops_recordings_live_strip_preview(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """Sharing the store moves the WRITE off Recordings' own `onPick` — and that
    handler did more than write: it DROPS the live strip-preview, because the
    preview is an original-view knob-tuning artifact that would otherwise stay
    overlaid on the stripped view's committed cut (the hero's waveKey is
    source-independent, so nothing downstream can notice the switch). So the
    same pick made in Transcript must still clear it.

    Asserts the state-DEPENDENT chrome — the canvas overlay and the wave-header
    stat quartet — not the lit toggle plus a row count, neither of which can see
    a stranded preview.
    """
    rr = running_recorder
    n_clips = await _session_with_stripped_clips(rr, tmp_path)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#recordings", wait_until="domcontentloaded")
            await page.wait_for_selector("#viewRoot .wavlist .wavrow", timeout=10000)

            # Tune a strip knob — the `input` a drag emits — and wait for the
            # preview to actually LAND. Without that precondition there is
            # nothing to strand and every assertion below passes vacuously.
            from .test_dashboard_ui import set_knob_js

            assert await page.evaluate(set_knob_js("min_silence_ms", 800))
            await page.wait_for_selector(REC_PREVIEW, timeout=20000)
            live = await _wave_stats(page)
            assert live["sKept"].endswith("%"), (
                f"the live preview must own the wave-header stats before the pick, got {live!r}"
            )

            # The pick is made in the OTHER view, so Recordings' onPick — the
            # only thing that used to drop the preview — never runs.
            await page.evaluate('() => window.gotoView("transcript")')
            await page.wait_for_selector(f"{TX_TOGGLE} [data-src]", timeout=10000)
            await page.locator(f'{TX_TOGGLE} [data-src="stripped"]').click()
            await page.wait_for_selector(f'{TX_TOGGLE} [data-src="stripped"].is-on', timeout=8000)

            await page.evaluate('() => window.gotoView("recordings")')
            await page.wait_for_selector("#viewRoot .wavlist .wavrow", timeout=10000)
            assert await _lit_source(page, REC_TOGGLE) == "stripped"

            await page.wait_for_function(
                """(sel) => !document.querySelector(sel)""", arg=REC_PREVIEW, timeout=8000
            )
            stats = await _wave_stats(page)
            assert stats["sClips"] == str(n_clips) and stats["sIn"] == "—" and stats["sKept"] == "—", (
                f"after the pick the wave header must show the on-disk stripped summary, got {stats!r} — "
                "the original's uncommitted preview stats are stranded over the stripped view"
            )
        finally:
            await browser.close()


async def test_the_pick_is_in_memory_and_a_reload_returns_to_the_original(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """Scope guard, green before and after: sharing the pick between two views
    is NOT a request to persist it. The pick stays session-scoped in-memory
    state, so a reload comes back on the original — no localStorage, no new
    field on the session's meta."""
    rr = running_recorder
    await _session_with_stripped_clips(rr, tmp_path)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#recordings", wait_until="domcontentloaded")
            await page.wait_for_selector("#viewRoot .wavlist .wavrow", timeout=10000)

            await page.locator("#viewRoot").get_by_role("button", name="stripped", exact=True).click()
            assert await _lit_source(page, REC_TOGGLE) == "stripped"

            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_selector("#viewRoot .wavlist .wavrow", timeout=10000)
            lit = await _lit_source(page, REC_TOGGLE)
            assert lit == "original", f"a reload must come back on the original source, got {lit!r}"
        finally:
            await browser.close()
