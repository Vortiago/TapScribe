"""Browser-driven E2E: the Stages dashboard (served at /) renders what the
pipeline produces.

All tests drive real headless Chromium against the running uvicorn server.
The headline flows:

- `test_dashboard_shows_active_taps_live_feed_and_merged_transcript` is the
  fast plumbing check — synthetic WAVs through two bridges plus a
  `FakeTranscriber`, so it runs in CI without `faster-whisper` installed.
  Walks Capture (taps rail + live captions) → Sessions → Transcript
  (transcribe whole session, merged render, alias-applied copy).
- `test_dashboard_renders_strip_silence_region_sub_rows` exercises the
  stripped-clip sub-rows in the Recordings view — splits a multi-burst WAV,
  expects N indented `.wavrow.is-clip` rows under the original, transcribes
  one region, and expands its inline transcript.
- `test_dashboard_with_real_audio_and_whisper` is the full-fat check:
  streams the committed `armstrong-en.wav` fixture through the bridge,
  clicks Transcript's **▶ transcribe range** button, and waits for real
  `faster-whisper` output to render in the merged transcript panel. Gated
  by `@pytest.mark.real_audio` and skipped unless `faster-whisper` + the
  audio fixture are present.
- Plus poll-safety sweeps (focus clobbering, idle DOM churn) and the
  structural perf guards at the bottom of the file.

Skipped entirely when the `playwright` PACKAGE isn't importable — the
module-level guard below, and the ONLY honest precondition for skipping.
Install with `pip install playwright && python -m playwright install
chromium`.

A `chromium.launch()` FAILURE is deliberately NOT a skip. Every test used
to swallow it into `pytest.skip`, so a launch failure after a successful
`playwright install` step (the pip/on-disk revision drift documented in
CLAUDE.md, a missing shared lib, a blocked sandbox) made all 65 tests skip
and the only CI leg that runs any dashboard test exited 0 GREEN having
exercised nothing. Letting the exception propagate matches
`harness.launch_bridge_context` ("not swallowed as a skip, so a genuinely
broken Chromium fails red") and `test_setup_ui.py`, which runs in the same
CI job with no such swallows. Don't reintroduce the try/except.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest

from tapscribe import transcribers as _transcribers
from tapscribe.recorder import JobState
from tapscribe.session_paths import FILENAME_META_JSON

from .conftest import FakeAliveProc, RunningRecorder
from .fake_transcriber import FakeTranscriber
from .harness import (
    playwright_session,
    stream_wav_via_tap,
    streams_drained,
    synth_speech_like_wav,
    wait_until,
    word_tokens,
)

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

ALICE_TEXT = "The quick brown fox jumps over the lazy dog."
BOB_TEXT = "Hello operator, this is a transcription pipeline check."

# The recordings hero's committed-cut hook: the JSON cut spans the canvas is
# currently drawing, or null while the overlay isn't up yet. Shared by every
# recordings-waveform test so the `#viewRoot .wave-canvas` / `data-cut-spans`
# selector lives in one place.
OVERLAY_JS = """
() => {
  const c = document.querySelector('#viewRoot .wave-canvas');
  return (c && c.dataset.cutSpans) ? c.dataset.cutSpans : null;
}
"""

# The hero's LIVE strip-preview hook (#89) — the would-be cut spans while a knob
# is being dragged, or null when no preview is up. Sibling of OVERLAY_JS.
PREVIEW_JS = """
() => {
  const c = document.querySelector('#viewRoot .wave-canvas');
  return (c && c.dataset.previewSpans) ? c.dataset.previewSpans : null;
}
"""


def set_knob_js(key: str, value: int) -> str:
    """A JS thunk that sets a strip-knob range input to `value` and fires its
    `input` event, so the debounced strip-preview recomputes."""
    return f"""
    () => {{
      const k = document.querySelector('#viewRoot [data-strip-knob="{key}"]');
      if (!k) return false;
      k.value = "{value}";
      k.dispatchEvent(new Event("input", {{ bubbles: true }}));
      return true;
    }}
    """


# Screenshots committed to the repo so the README can embed them and a
# reviewer can eyeball what the test actually saw.
SHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "dashboard-shots"


async def _shot(page, name: str) -> None:
    """Refresh one committed README screenshot — ONLY when explicitly asked.

    The PNGs under docs/dashboard-shots are repo artifacts, and
    `page.screenshot` produces different bytes on every run (render timing,
    dynamic timestamps), so writing them unconditionally dirties the working
    tree on every e2e run — including the pre-push hook's, right after a
    commit. Opt in with TAPSCRIBE_REFRESH_SHOTS=1 when deliberately
    refreshing the docs (same opt-in shape as TAPSCRIBE_PERF_SOAK)."""
    if os.environ.get("TAPSCRIBE_REFRESH_SHOTS") != "1":
        return
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(SHOTS_DIR / name), full_page=True)


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "audio"


@pytest.fixture
def fake_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriber:
    """Same shape as the HTTP E2E test's fixture — keep them aligned
    so any future change to the FakeTranscriber wiring is caught here
    too."""
    fake = FakeTranscriber(text_by_speaker={"Alice": ALICE_TEXT, "Bob": BOB_TEXT})

    def _factory(model_name: str, **_kwargs) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    _transcribers.clear_cache()
    return fake


async def test_dashboard_shows_active_taps_live_feed_and_merged_transcript(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001 — keeps the patched factory
    tmp_path: Path,
):
    """End-to-end through real Chromium: stream → see in UI → click
    transcribe → see merged transcript in UI, across the Stages views
    (Capture → Sessions → Transcript).

    The dashboard polls `/api/state` every 500ms; every UI wait is on a
    DOM condition (text appearance, count change), never a fixed sleep,
    so the test scales with the poll cadence rather than racing it.
    """
    rec = running_recorder.recorder
    fake_wlk = running_recorder.fake_wlk
    ws_base = running_recorder.ws_base_url

    # Long enough that both WSes are still streaming while the rail rows +
    # caption pushes are asserted (the relays must be open for the pushes
    # to reach them), short enough not to drag the test out.
    wavs = {
        "alice": synth_speech_like_wav(tmp_path / "alice.wav", seconds=6.0, freq_hz=220.0),
        "bob": synth_speech_like_wav(tmp_path / "bob.wav", seconds=6.0, freq_hz=440.0),
    }

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = await context.new_page()
            # The dashboard polls /api/state forever so it's never
            # network-idle — wait on DOM ready instead.
            await page.goto(running_recorder.base_url, wait_until="domcontentloaded")

            # Idle render: Capture is the default view; the global taps rail
            # shows the active-taps empty state and the captions feed is empty.
            await page.wait_for_selector("#tapsRailBody .empty", timeout=5000)
            assert await page.locator("#tapsRailCount").inner_text() == "0"
            assert await page.locator('#viewRoot [data-slot="liveFeedCount"]').inner_text() == "0"
            await _shot(page, "01-idle.png")

            alice_task = asyncio.create_task(
                stream_wav_via_tap(
                    ws_base_url=ws_base,
                    identity="alice",
                    name="Alice",
                    wav_path=wavs["alice"],
                    utterance_id="utt-ui-alice",
                    frame_interval_s=0.02,
                )
            )
            bob_task = asyncio.create_task(
                stream_wav_via_tap(
                    ws_base_url=ws_base,
                    identity="bob",
                    name="Bob",
                    wav_path=wavs["bob"],
                    utterance_id="utt-ui-bob",
                    frame_interval_s=0.02,
                )
            )

            # The global taps rail must surface both speakers while their
            # WSes are open — on every view, but Capture is where we look.
            await page.wait_for_function(
                """
                () => {
                  const rows = Array.from(
                    document.querySelectorAll('#tapsRailBody .stream-row [data-slot="name"]'),
                  );
                  const names = rows.map((n) => n.textContent.trim());
                  return names.includes("Alice") && names.includes("Bob");
                }
                """,
                timeout=5000,
            )
            assert await page.locator("#tapsRailCount").inner_text() == "2"
            await _shot(page, "02-active-taps.png")

            # Settled lines from the fake WhisperLiveKit must surface in the
            # Capture view's captions feed, attributed to each speaker.
            # FakeWlk broadcasts to every connected relay → each push lands
            # twice (once tagged Alice, once Bob). The relay settles the tail
            # line only after a few stable empty-buffer snapshots
            # (live_relay.py _TAIL_STABLE_SNAPSHOTS), so each commit is
            # followed by confirming snapshots — mirroring WlK's rolling
            # re-broadcasts.
            fake_wlk.push_committed("first ui settled line")
            fake_wlk.push_committed("second ui settled line")
            for _ in range(4):
                fake_wlk.push_buffer("")

            # The feed coalesces consecutive same-speaker fragments into one
            # flowing line, and FakeWlk's broadcast order isn't deterministic
            # (the two pushes may land Alice/Alice/Bob/Bob and merge, or
            # interleave and stay separate). So assert on each speaker's
            # *combined* text rather than exact per-row matches.
            await page.wait_for_function(
                """
                () => {
                  const lines = Array.from(
                    document.querySelectorAll('#viewRoot [data-slot="liveFeedShell"] .feed-body .line'),
                  );
                  const byWho = {};
                  for (const l of lines) {
                    const who = l.querySelector(".who")?.textContent.trim();
                    const txt = l.querySelector(".txt")?.textContent.trim() || "";
                    if (!who) continue;
                    byWho[who] = (byWho[who] ? byWho[who] + " " : "") + txt;
                  }
                  const ok = (who) =>
                    !!byWho[who] &&
                    byWho[who].includes("first ui settled line") &&
                    byWho[who].includes("second ui settled line");
                  return ok("Alice") && ok("Bob");
                }
                """,
                timeout=10000,
            )
            # The header count tracks rendered (COALESCED) lines, not raw deque
            # entries (#80): consecutive same-speaker fragments are joined into
            # sentences, so >=1 line per speaker survives the merge (2 speakers
            # here). Stages mounts the feed count as a data-slot under #viewRoot.
            count = await page.locator('#viewRoot [data-slot="liveFeedCount"]').inner_text()
            assert int(count) >= 2
            await _shot(page, "03-live-transcripts.png")

            await asyncio.gather(alice_task, bob_task)
            assert await wait_until(lambda: streams_drained(rec), timeout=10.0)

            # The global Sessions view must list the current session with the
            # two WAVs the bridges wrote.
            await page.evaluate("() => window.gotoView('sessions')")
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector('.sessrow[data-sid="{rec.session_start}"]');
                  return row && row.querySelector('[data-slot="wavs"]')?.textContent.trim() === '2';
                }}
                """,
                timeout=5000,
            )
            await _shot(page, "04-sessions.png")

            # The headline flow: Transcript stage → ▶ transcribe range (blank
            # range = the whole session).
            await page.evaluate("() => window.gotoView('transcript')")
            tx_button = page.locator('#viewRoot [data-slot="txRangeBtn"]')
            await tx_button.wait_for(state="visible", timeout=5000)
            await tx_button.click()

            # The merged transcript must render with both speakers' scripted
            # text. We assert on `innerText` (the rendered, layout-aware
            # string) rather than `textContent`: the lines container is
            # `white-space: pre-wrap`, so any stray whitespace/newlines
            # between sibling spans would split a single segment across
            # multiple visual lines while still passing a textContent
            # `.includes()` check — innerText catches that.
            await page.wait_for_function(
                f"""
                () => {{
                  const region = document.querySelector(
                    '#viewRoot [data-slot="mergedHost"] [data-slot="lines"]');
                  if (!region) return false;
                  const visible = region.innerText;
                  // Each speaker's label and body must appear adjacent on
                  // the same visual line — i.e. "Alice: <text>".
                  return visible.includes('Alice: ' + {ALICE_TEXT!r})
                      && visible.includes('Bob: ' + {BOB_TEXT!r});
                }}
                """,
                timeout=15000,
            )

            # Both speakers are surfaced in the speaking-time bar legend.
            legend = page.locator(".spk-legend")
            await legend.wait_for(state="visible", timeout=5000)
            legend_text = await legend.inner_text()
            assert "Alice" in legend_text and "Bob" in legend_text
            await _shot(page, "05-merged-transcript.png")

            # The copy button must copy the speaker display names (aliases
            # applied) — what the user sees on screen — not the raw speaker
            # keys from the backend's `plain_text`. Set an alias and verify
            # the clipboard reflects it.
            await page.evaluate(
                """
                async (sess) => {
                  const r = await fetch('/api/session-meta/' + encodeURIComponent(sess), {
                    method: 'PUT',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({label: '', aliases: {Alice: 'Ms. Smith', Bob: 'Mr. Jones'}}),
                  });
                  if (!r.ok) throw new Error('PUT session-meta: ' + r.status);
                }
                """,
                rec.session_start,
            )
            # Wait for the new aliases to render in the merged transcript
            # (the merged pane's render signature includes the alias map).
            await page.wait_for_function(
                """
                () => {
                  const t = document.querySelector(
                    '#viewRoot [data-slot="mergedHost"] [data-slot="lines"]')?.innerText || '';
                  return t.includes('Ms. Smith: ') && t.includes('Mr. Jones: ');
                }
                """,
                timeout=5000,
            )
            # The copy button enables once the merged body is loaded.
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('#viewRoot [data-slot="txCopyBtn"]');
                  return b && !b.disabled;
                }""",
                timeout=5000,
            )
            await page.locator('#viewRoot [data-slot="txCopyBtn"]').click()
            # Verify the behaviour that matters: copy applies display-name
            # aliases (what the user sees), not the backend's raw speaker
            # keys. (The "✓ copied" flash is cosmetic and transient — the
            # clipboard content is the contract.)
            clipboard = await page.evaluate("() => navigator.clipboard.readText()")
            assert "Ms. Smith: " in clipboard and "Mr. Jones: " in clipboard, (
                f"copy merged didn't apply aliases: {clipboard!r}"
            )
            # The raw speaker keys ("Alice" / "Bob" in the FakeTranscriber
            # wiring) must not survive aliasing — otherwise the button is
            # copying backend `plain_text` and ignoring the user's aliases.
            assert "Alice: " not in clipboard and "Bob: " not in clipboard, (
                f"copy merged leaked raw speaker keys: {clipboard!r}"
            )
        finally:
            await browser.close()


async def test_dashboard_renders_strip_silence_region_sub_rows(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    tmp_path: Path,
):
    """The Recordings view's stripped source shows one indented
    `.wavrow.is-clip` per region the strip-silence splitter wrote to
    `<session>/stripped/`, each with its own download link + expandable
    inline transcript.

    This is the load-bearing UX guard for the sub-row UI: the splitter
    refactor (PR #49) silently dropped the old 1:1 stripped sibling UI,
    so a regression here would leave region WAVs invisible to the
    operator. The test streams a 3-burst WAV, splits it, asserts 3
    clip rows render under the parent, transcribes one region
    (server-side — transcription moved to the Transcript stage), and
    expands its inline transcript end-to-end.
    """
    # Imported here so the helper stays colocated with the strip-silence
    # pipeline test it was originally written for.
    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    # Scripted text the FakeTranscriber returns for any region WAV — the
    # speaker slug on every region is "Alice" (inherited from the
    # original's filename), so one entry covers all N regions.
    scripted = "stripped region segment from Alice"
    fake_transcriber.text_by_speaker["Alice"] = scripted

    src_wav = _build_speech_silence_wav(tmp_path / "alice-multi.wav")
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src_wav,
        utterance_id="utt-strip-ui",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
    recorded = sorted(rec.session_dir.glob("*.wav"))
    assert len(recorded) == 1, f"expected one recorded WAV, got {[w.name for w in recorded]}"
    original_name = recorded[0].name

    # Mint the per-region WAVs server-side before opening the dashboard so
    # the first poll cycle already includes them in `/api/state`.
    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            f"/api/sessions/{rec.session_start}/strip-silence",
            json={
                "min_silence_ms": 400,
                "pad_ms": 50,
                "speech_floor_db": -40.0,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["files_written"] == 1
    region_wavs = sorted((rec.session_dir / "stripped").glob("*.wav"))
    assert len(region_wavs) == 3, (
        f"strip-silence should have produced 3 region WAVs from the 3-burst source, "
        f"got {[w.name for w in region_wavs]}"
    )

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
            )
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            # Switch the WAV list to the stripped source — clip rows render
            # under their parent original only when that source is active.
            stripped_toggle = page.locator("#viewRoot").get_by_role("button", name="stripped", exact=True)
            await stripped_toggle.wait_for(state="visible", timeout=10000)
            await page.wait_for_function(
                """() => !document.querySelector('#viewRoot button[data-src="stripped"]')?.disabled""",
                timeout=10000,
            )
            await stripped_toggle.click()

            # The original WAV row must render, with exactly 3 indented
            # `.wavrow.is-clip` siblings under it (the three regions).
            await page.wait_for_function(
                """
                () => document.querySelectorAll('#viewRoot .wavlist .wavrow.is-clip').length === 3
                """,
                timeout=10000,
            )
            # Clip rows live inside the same `.wavlist` as the original —
            # i.e. not in some sibling list. Asserts the DOM topology so a
            # future refactor that re-parents them is flagged here.
            total_rows = await page.locator("#viewRoot .wavlist .wavrow").count()
            assert total_rows == 4, f"expected 1 original + 3 region rows, got {total_rows}"

            # Each clip row is identifiable by its region name + source and
            # carries a stripped-source download link.
            for r in region_wavs:
                row_sel = f'.wavrow.is-clip[data-wav="{r.name}"][data-src="stripped"]'
                await page.locator(row_sel).wait_for(state="visible", timeout=3000)
                href = await page.get_attribute(f'{row_sel} [data-slot="download"]', "href")
                assert href and "source=stripped" in href, f"clip download must target stripped: {href}"

            # Transcribe the first region server-side (the per-clip transcribe
            # button moved to the Transcript stage with the engine panel) and
            # wait for the clip's tx tag to flip on the next polls.
            first_region = region_wavs[0]
            async with httpx.AsyncClient(base_url=base, timeout=30.0) as client2:
                resp2 = await client2.post(
                    "/api/transcribe",
                    json={
                        "session": rec.session_start,
                        "name": first_region.name,
                        "source": "stripped",
                        "model": "tiny.en",
                    },
                )
                assert resp2.status_code == 200, resp2.text
            first_row_sel = f'.wavrow.is-clip[data-wav="{first_region.name}"][data-src="stripped"]'
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector('{first_row_sel}');
                  return row && row.querySelector('[data-slot="txTag"]')?.textContent.includes('✓');
                }}
                """,
                timeout=10000,
            )

            # Expanding the clip renders its inline transcript right below the
            # row. The row is a native <details>; clicking its <summary> toggles
            # it open (a clip has no select target, so the whole summary
            # toggles). Its text must be the scripted text the FakeTranscriber
            # returned.
            await page.locator(f"{first_row_sel} > summary").click()
            await page.wait_for_function(
                f"""
                () => {{
                  const tx = document.querySelectorAll('#viewRoot .wavlist .expand-tx');
                  return Array.from(tx).some((el) => el.innerText.includes({scripted!r}));
                }}
                """,
                timeout=5000,
            )

            await _shot(page, "07-stripped-region-sub-rows.png")

            # The original WAV's row is still present and identifiable by
            # its (unique) original filename — clip rows must not have
            # taken its place in the DOM.
            orig_row = page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{original_name}"]')
            await orig_row.wait_for(state="visible", timeout=2000)
        finally:
            await browser.close()


async def test_recordings_committed_cut_overlay_persists_across_reload(
    running_recorder: RunningRecorder,
    tmp_path: Path,
):
    """After ✂ strip, the selected original's waveform draws the committed
    cut — the EXACT {start_s, end_s} spans the strip response returned,
    surfaced on the canvas's data-cut-spans hook. Pins four paths: the
    cold-load resolve from persisted strip-meta, that the overlay PERSISTS
    across the source toggle (the hero always shows the original tap + cut,
    in both original AND stripped views), the LIVE swap when a re-strip lands
    while the page is open (the /api/state stripped_at stamp busts the
    client cache — no reload), and a final reload proving the LATEST cut
    survives with nothing in memory. The #90 acceptance guard."""
    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    src_wav = _build_speech_silence_wav(tmp_path / "alice-multi.wav")
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src_wav,
        utterance_id="utt-cut-overlay",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            f"/api/sessions/{rec.session_start}/strip-silence",
            json={"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0},
        )
        assert resp.status_code == 200, resp.text
        rows = [f for f in resp.json()["files"] if f.get("written")]
    assert len(rows) == 1
    expected_spans = rows[0]["region_spans"]
    assert len(expected_spans) == 3, f"the 3-burst source should commit 3 spans, got {expected_spans}"

    overlay_js = OVERLAY_JS

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            # The default source is "original", and the first WAV is the
            # auto-selected one — the overlay lands once the peaks and
            # strip-meta fetches settle.
            await page.wait_for_function(overlay_js, timeout=10000)
            first_attr = await page.evaluate(overlay_js)
            spans = json.loads(first_attr)
            assert spans == expected_spans, f"overlay spans {spans} != committed {expected_spans}"

            # The badge is the operator-visible "this is the committed cut"
            # cue, distinct from the future live knob preview (#89).
            await page.locator('#viewRoot [data-slot="cutBadge"]').wait_for(state="visible", timeout=3000)

            await _shot(page, "09-committed-cut-overlay.png")

            # The hero always shows the original tap + committed cut — the
            # "entire sound, kept vs stripped, in one line" view. The source
            # toggle only switches the LIST below, so toggling to "stripped"
            # must KEEP the overlay (and never 404 the waveform); toggling
            # back keeps it too. (The pre-#N behaviour dropped it here, which
            # left the stripped view trying to fetch the original name from
            # stripped/ → 404.)
            await page.locator("#viewRoot").get_by_role("button", name="stripped", exact=True).click()
            # The clip sub-rows confirm the stripped VIEW is active…
            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot .wavlist .wavrow.is-clip').length === 3""",
                timeout=5000,
            )
            # …while the hero keeps the original's committed overlay, no error.
            # Wait for the overlay hook before parsing so a transient null can't
            # turn a real mismatch into a json.loads(None) TypeError.
            await page.wait_for_function(overlay_js, timeout=5000)
            assert json.loads(await page.evaluate(overlay_js)) == expected_spans, (
                "toggling to stripped must keep the original tap's cut overlay"
            )
            assert await page.locator("#viewRoot .wave-msg").is_hidden(), (
                "the stripped view must not surface a waveform error (the 404 bug)"
            )
            await page.locator("#viewRoot").get_by_role("button", name="original", exact=True).click()
            await page.wait_for_function(overlay_js, timeout=5000)

            # Live path: a re-strip with a wider pad lands while the page is
            # open. The poll's new stripped_at stamp must bust the client
            # cache and swap the overlay to the NEW spans without a reload.
            async with httpx.AsyncClient(base_url=base, timeout=30.0) as client2:
                resp2 = await client2.post(
                    f"/api/sessions/{rec.session_start}/strip-silence",
                    json={"min_silence_ms": 400, "pad_ms": 150, "speech_floor_db": -40.0},
                )
                assert resp2.status_code == 200, resp2.text
                rows2 = [f for f in resp2.json()["files"] if f.get("written")]
            assert len(rows2) == 1
            new_spans = rows2[0]["region_spans"]
            assert new_spans != expected_spans, "a wider pad must move the committed spans"
            await page.wait_for_function(
                f"""() => {{
                  const c = document.querySelector('#viewRoot .wave-canvas');
                  return !!(c && c.dataset.cutSpans) && c.dataset.cutSpans !== {json.dumps(first_attr)};
                }}""",
                timeout=10000,
            )
            live_spans = json.loads(await page.evaluate(overlay_js))
            assert live_spans == new_spans, (
                f"live overlay spans {live_spans} != re-strip committed {new_spans}"
            )

            # Reload: the LATEST spans must come back EXACTLY from the
            # persisted strip-meta — no strip response left in memory.
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(overlay_js, timeout=10000)
            spans2 = json.loads(await page.evaluate(overlay_js))
            assert spans2 == new_spans, f"reload lost the committed cut: {spans2} != {new_spans}"
        finally:
            await browser.close()


async def test_recordings_stripped_toggle_hero_shows_original_overlay_not_404(
    running_recorder: RunningRecorder,
    tmp_path: Path,
):
    """Regression for the reported bug: clicking the "stripped" source toggle
    404'd the hero waveform. An original tap-WAV lives ONLY in <session>/;
    strip-silence writes region clips under stripped/ with NEW names, so
    /api/wav/{s}/{originalName}/peaks?source=stripped never resolves. The hero
    must ALWAYS render the original tap + committed cut overlay (peaks from the
    original source) in both toggle states — the toggle only switches the list
    below. Asserts: after toggling to stripped the canvas keeps the cut
    overlay, shows no error, and NO /api/wav call went out against the stripped
    source for an original name (nor did any /api/wav call 404)."""
    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    src_wav = _build_speech_silence_wav(tmp_path / "alice-multi.wav")
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src_wav,
        utterance_id="utt-hero-404",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            f"/api/sessions/{rec.session_start}/strip-silence",
            json={"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["files_written"] == 1

    overlay_js = OVERLAY_JS

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()

            # Capture every /api/wav response so a resurfaced stripped-source
            # peaks fetch (the bug) or any 404 shows up as a hard assertion.
            wav_responses: list[tuple[str, int]] = []
            page.on(
                "response",
                lambda r: wav_responses.append((r.url, r.status)) if "/api/wav/" in r.url else None,
            )

            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            # Original is auto-selected; its committed overlay lands once the
            # peaks + strip-meta fetches settle (original source — the only one
            # that exists for this name).
            await page.wait_for_function(overlay_js, timeout=10000)

            # Toggle to the stripped VIEW — clip rows appear below…
            await page.locator("#viewRoot").get_by_role("button", name="stripped", exact=True).click()
            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot .wavlist .wavrow.is-clip').length === 3""",
                timeout=10000,
            )

            # …and the hero KEEPS the original tap + overlay, with no error.
            assert await page.evaluate(overlay_js) is not None, (
                "the hero overlay must survive the stripped toggle (not 404)"
            )
            assert await page.locator("#viewRoot .wave-msg").is_hidden(), (
                "the stripped view must not show a waveform error message"
            )
            canvas_w = await page.locator("#viewRoot .wave-canvas").evaluate("c => c.width")
            assert canvas_w > 0, "the hero canvas must be painted, not blank"

            # Load-bearing regression assertion: no /api/wav call 404'd, and the
            # hero never asked for a stripped-source peaks fetch (an original
            # name doesn't exist under stripped/, which is what 404'd).
            bad = [
                (u, s) for (u, s) in wav_responses if s == 404 or ("source=stripped" in u and "/peaks" in u)
            ]
            assert not bad, f"stripped toggle triggered bad /api/wav requests: {bad}"
        finally:
            await browser.close()


async def test_recordings_stripped_view_original_row_targets_original_source(
    running_recorder: RunningRecorder,
    tmp_path: Path,
):
    """In the stripped VIEW the original tap-WAV row still targets the ORIGINAL
    source — its own download / delete / expand resolve under <session>/, never
    <session>/stripped/<originalName> (which never exists → 404). Only the
    indented region CLIP rows are the stripped source. Guards buildRowModels."""
    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    src_wav = _build_speech_silence_wav(tmp_path / "alice-multi.wav")
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src_wav,
        utterance_id="utt-orig-src",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
    original_name = sorted(rec.session_dir.glob("*.wav"))[0].name

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            f"/api/sessions/{rec.session_start}/strip-silence",
            json={"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["files_written"] == 1
    region_wavs = sorted((rec.session_dir / "stripped").glob("*.wav"))
    assert len(region_wavs) == 3

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            await page.locator("#viewRoot").get_by_role("button", name="stripped", exact=True).click()
            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot .wavlist .wavrow.is-clip').length === 3""",
                timeout=10000,
            )

            # The ORIGINAL row is source=original even in the stripped view…
            orig_row = f'#viewRoot .wavlist .wavrow[data-wav="{original_name}"]:not(.is-clip)'
            await page.locator(orig_row).wait_for(state="visible", timeout=3000)
            assert await page.get_attribute(orig_row, "data-src") == "original"
            orig_href = await page.get_attribute(f'{orig_row} [data-slot="download"]', "href")
            assert orig_href and "source=stripped" not in orig_href, (
                f"the original row must download the original, not stripped: {orig_href}"
            )
            assert "/api/wav/" in orig_href and original_name in orig_href

            # …while each region CLIP row is source=stripped.
            for r in region_wavs:
                clip_row = f'.wavrow.is-clip[data-wav="{r.name}"][data-src="stripped"]'
                await page.locator(clip_row).wait_for(state="visible", timeout=3000)
                clip_href = await page.get_attribute(f'{clip_row} [data-slot="download"]', "href")
                assert clip_href and "source=stripped" in clip_href, (
                    f"the clip row must download from stripped: {clip_href}"
                )
        finally:
            await browser.close()


async def test_recordings_strip_preview_dropped_when_source_toggled(
    running_recorder: RunningRecorder,
    tmp_path: Path,
):
    """A live strip-preview is an ORIGINAL-view tuning artifact: toggling the
    source to "stripped" must DROP it, so the stale preview overlay never hides
    the stripped view's committed cut. The hero's waveKey is source-independent
    (it always shows the original tap), so without an explicit drop the preview
    would survive the toggle — this pins that drop."""
    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    src_wav = _build_speech_silence_wav(tmp_path / "alice-multi.wav")
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src_wav,
        utterance_id="utt-preview-toggle",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            f"/api/sessions/{rec.session_start}/strip-silence",
            json={"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["files_written"] == 1

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            # Original view: wait for the committed overlay, then drag a knob to
            # raise a live preview on top of it.
            await page.wait_for_function(OVERLAY_JS, timeout=10000)
            assert await page.evaluate(set_knob_js("pad_ms", 150))
            await page.wait_for_function(PREVIEW_JS, timeout=10000)

            # Toggle to the stripped view — the preview must be dropped…
            await page.locator("#viewRoot").get_by_role("button", name="stripped", exact=True).click()
            await page.wait_for_function(
                """() => !document.querySelector('#viewRoot .wave-canvas')?.dataset.previewSpans""",
                timeout=5000,
            )
            # …leaving the committed cut as what the hero actually shows.
            assert await page.evaluate(OVERLAY_JS) is not None, (
                "the committed cut must be visible once the stale preview is dropped"
            )
        finally:
            await browser.close()


async def test_recordings_strip_preview_tracks_knobs_and_matches_commit(
    running_recorder: RunningRecorder,
    tmp_path: Path,
):
    """Dragging a strip knob fires a debounced live preview (#89): the
    waveform redraws in place with the would-be cut (data-preview-spans),
    the stats row tracks the preview, and the preview spans EXACTLY match
    what a real ✂ strip with the same knobs then commits. A second drag
    re-computes the overlay."""
    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    src_wav = _build_speech_silence_wav(tmp_path / "alice-multi.wav")
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src_wav,
        utterance_id="utt-strip-preview",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    preview_js = PREVIEW_JS

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            # Wait for the real waveform (the message overlay empties once
            # peaks land) — the preview overlay needs a drawn canvas.
            await page.wait_for_function(
                """() => {
                  const m = document.querySelector('#viewRoot .wave-msg');
                  return m && m.hidden;
                }""",
                timeout=10000,
            )
            assert await page.evaluate(preview_js) is None, "no preview before any knob input"

            # Drag the pad knob: 200 -> 150. The debounced strip-preview
            # fires and the canvas redraws in place with the would-be cut.
            assert await page.evaluate(set_knob_js("pad_ms", 150))
            await page.wait_for_function(preview_js, timeout=10000)
            first_preview_raw = await page.evaluate(preview_js)
            first_preview = json.loads(first_preview_raw)
            assert len(first_preview) == 3, f"3-burst source should preview 3 spans, got {first_preview}"

            # The stats row tracks the preview…
            await page.wait_for_function(
                """() => document.querySelector('#viewRoot [data-slot="sClips"]')?.textContent === '3'""",
                timeout=5000,
            )
            # …and the legend explains the overlay.
            legend = page.locator('#viewRoot [data-slot="legend"]')
            await legend.wait_for(state="visible", timeout=3000)

            await _shot(page, "10-strip-preview-overlay.png")

            # The preview IS the cut: a real ✂ strip with the same knobs
            # (defaults except the dragged pad) commits exactly these spans.
            import httpx

            async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
                resp = await client.post(
                    f"/api/sessions/{rec.session_start}/strip-silence",
                    json={"min_silence_ms": 500, "pad_ms": 150, "speech_floor_db": -45.0},
                )
                assert resp.status_code == 200, resp.text
                rows = [f for f in resp.json()["files"] if f.get("written")]
            assert len(rows) == 1
            committed = [{"start_s": sp["start_s"], "end_s": sp["end_s"]} for sp in rows[0]["region_spans"]]
            assert committed == first_preview, f"committed cut {committed} != live preview {first_preview}"

            # The committed overlay lands on the same canvas (solid ticks vs
            # the preview's dashed — both data hooks present).
            await page.wait_for_function(
                """() => !!document.querySelector('#viewRoot .wave-canvas')?.dataset.cutSpans""",
                timeout=10000,
            )

            # A second drag re-computes the preview in place.
            assert await page.evaluate(set_knob_js("pad_ms", 50))
            await page.wait_for_function(
                f"""() => {{
                  const c = document.querySelector('#viewRoot .wave-canvas');
                  return !!(c && c.dataset.previewSpans) && c.dataset.previewSpans !== {json.dumps(first_preview_raw)};
                }}""",
                timeout=10000,
            )
            second_preview = json.loads(await page.evaluate(preview_js))
            assert len(second_preview) == 3
            assert second_preview != first_preview, "a narrower pad must move the preview spans"
        finally:
            await browser.close()


async def test_dashboard_delete_session_audio_keeps_transcript(
    running_recorder: RunningRecorder,
):
    """The Sessions view's per-row 'delete audio' action removes a session's
    WAVs while keeping its merged transcript. Drives the real `confirm()`
    dialog — without an accept handler, headless Chromium auto-dismisses
    the confirm and the delete no-ops.

    Operates on a SEEDED, non-current session because the delete endpoints
    refuse the current recording session.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url

    # Seed a previous (non-current) session on disk: one WAV + a merged
    # transcript. A 2020 timestamp sorts below rec.session_start and
    # renders with is_current=False, so the guard doesn't 409.
    prev_id = "2020-01-01T00-00-00Z"
    prev = rec.recordings_dir / prev_id
    prev.mkdir(parents=True)
    synth_speech_like_wav(
        prev / f"{prev_id}_alice_speaker_abcd1234.wav",
        seconds=1.0,
        freq_hz=220.0,
    )
    (prev / "session-transcript.json").write_text(
        '{"segments": [{"speaker": "Alice", "text": "kept after audio delete"}]}',
        encoding="utf-8",
    )

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            # Accept every confirm() so the destructive action proceeds.
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")

            # The seeded previous session renders as a row with 1 WAV and a
            # merged-transcript marker.
            row_sel = f'.sessrow[data-sid="{prev_id}"]'
            await page.locator(row_sel).wait_for(state="visible", timeout=10000)
            del_btn = page.locator(f'{row_sel} [data-slot="del"]')
            await del_btn.wait_for(state="visible", timeout=3000)
            await del_btn.click()

            # The row's WAV count drops to 0 once the delete + next poll land.
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector('{row_sel}');
                  // "—" is the zero glyph in the WAVs column.
                  return row && row.querySelector('[data-slot="wavs"]')?.textContent.trim() === '—';
                }}
                """,
                timeout=10000,
            )
            await _shot(page, "08-deleted-session-audio.png")
        finally:
            await browser.close()

    # On disk: the WAV is gone, the merged transcript survives.
    assert sorted(prev.glob("*.wav")) == [], "audio delete should remove every WAV"
    assert (prev / "session-transcript.json").is_file(), "merged transcript must be kept"


async def test_sessions_view_absorb_delete_and_prune(
    running_recorder: RunningRecorder,
):
    """The Sessions view's management actions, ported from the classic
    sidebar: absorb-merge one session into another (the source folder is
    deleted, its WAVs move into the target), whole-session delete, and the
    prune-empty toolbar action. Also pins the safety wiring: the CURRENT
    session must not offer delete/absorb (the server would 409 anyway).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url

    def seed(sid: str, *, wavs: int) -> Path:
        d = rec.recordings_dir / sid
        d.mkdir(parents=True)
        for i in range(wavs):
            synth_speech_like_wav(d / f"{sid}_seed{i}_speaker_{i:08d}.wav", seconds=0.3, freq_hz=220.0)
        return d

    target_id, source_id, empty_id = (
        "2024-01-01T10-00-00Z",
        "2024-01-02T10-00-00Z",
        "2024-01-03T10-00-00Z",
    )
    target_dir = seed(target_id, wavs=1)
    source_dir = seed(source_id, wavs=1)
    empty_dir = seed(empty_id, wavs=0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")

            # The current session's row must NOT offer the destructive
            # actions the server refuses for it.
            cur_sel = f'.sessrow[data-sid="{rec.session_start}"]'
            await page.locator(cur_sel).wait_for(state="visible", timeout=10000)
            assert await page.locator(f'{cur_sel} [data-slot="absorb"]').count() == 0, (
                "current session must not offer absorb-as-source"
            )
            cur_del = page.locator(f'{cur_sel} [data-slot="delSession"]')
            if await cur_del.count():
                assert await cur_del.is_disabled(), "current session delete must be disabled"

            # ABSORB: fold source into target via the row's picker. The source
            # row disappears (its folder is deleted) and the target's WAV
            # count doubles.
            await page.locator(f'.sessrow[data-sid="{source_id}"]').wait_for(state="visible", timeout=5000)
            await page.select_option(f'.sessrow[data-sid="{source_id}"] [data-slot="absorb"]', target_id)
            await page.wait_for_function(
                f"""
                () => {{
                  const src = document.querySelector('.sessrow[data-sid="{source_id}"]');
                  const tgt = document.querySelector('.sessrow[data-sid="{target_id}"]');
                  return !src && tgt
                    && tgt.querySelector('[data-slot="wavs"]')?.textContent.trim() === '2';
                }}
                """,
                timeout=10000,
            )
            assert not source_dir.exists(), "absorb must delete the source folder"
            assert len(sorted(target_dir.glob("*.wav"))) == 2

            # WHOLE-SESSION DELETE: the target row (audio + everything) goes.
            await page.locator(f'.sessrow[data-sid="{target_id}"] [data-slot="delSession"]').click()
            await page.wait_for_function(
                f"""() => !document.querySelector('.sessrow[data-sid="{target_id}"]')""",
                timeout=10000,
            )
            assert not target_dir.exists(), "session delete must remove the folder"

            # PRUNE EMPTY: the empty unlabeled session goes; the status text
            # surfaces the count; the current session survives.
            await page.locator('[data-slot="prune"]').click()
            await page.wait_for_function(
                f"""() => !document.querySelector('.sessrow[data-sid="{empty_id}"]')""",
                timeout=10000,
            )
            status = await page.locator('[data-slot="pruneStatus"]').inner_text()
            assert "1" in status, f"prune status should surface the count: {status!r}"
            assert not empty_dir.exists(), "prune must remove the empty session folder"
            assert await page.locator(cur_sel).count() == 1, "prune must keep the current session"
        finally:
            await browser.close()


@pytest.mark.real_audio
async def test_dashboard_with_real_audio_and_whisper(
    running_recorder: RunningRecorder,
):
    """Headline real-deal check: stream the committed Apollo 11 audio
    fixture through the bridge, click the Transcript stage's
    **▶ transcribe range** button (blank range = whole session), wait for
    real `faster-whisper` to produce a merged transcript, and assert the
    rendered DOM contains a recognisable word from the reference.

    The screenshot at the end (`06-real-audio-transcript.png`) is what
    the README embeds — it's the only one captured with real audio and
    a real model, so it's the load-bearing visual proof of the goal.

    Skipped when `faster-whisper` isn't importable or the fixture is
    missing. The picker's `tiny.en` model is used to keep first-run
    download time bounded; tiny.en is good enough to surface at least
    one anchor word from the Armstrong clip.
    """
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster_whisper not installed — install with `pip install -e .[whisper]`")
    fixture_wav = FIXTURES_DIR / "armstrong-en.wav"
    fixture_ref = FIXTURES_DIR / "armstrong-en.reference.txt"
    if not fixture_wav.is_file() or not fixture_ref.is_file():
        pytest.skip(
            f"missing fixture {fixture_wav.name} or its reference — see tests/fixtures/audio/README.md",
        )

    reference_words = word_tokens(fixture_ref.read_text(encoding="utf-8"))
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url

    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="fixture-armstrong",
        name="armstrong-en",
        wav_path=fixture_wav,
        utterance_id="utt-real-armstrong",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    assert (rec.session_dir / "armstrong-en").with_suffix(".wav").parent.exists()

    # The Transcript page declares languages, not a model (ADR-0011): the
    # generalist comes from batch-model.txt. Pin it to tiny.en (75 MB — bounded
    # first-run download) via the config API, since there's no engine picker.
    async with httpx.AsyncClient(base_url=running_recorder.base_url, timeout=30.0) as client:
        r = await client.put("/api/config/batch-model", json={"content": "tiny.en"})
        assert r.status_code == 200, r.text

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = await context.new_page()
            await page.goto(running_recorder.base_url + "/#transcript", wait_until="domcontentloaded")

            # Wait for the first /api/state poll to land — the range button
            # enables once the session has WAVs to transcribe.
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('#viewRoot [data-slot="txRangeBtn"]');
                  return b && !b.disabled;
                }""",
                timeout=10000,
            )

            # The model is pinned via batch-model.txt above (no engine picker on
            # the Transcript page any more, ADR-0011); the range button just runs.
            tx_button = page.locator('#viewRoot [data-slot="txRangeBtn"]')
            await tx_button.wait_for(state="visible", timeout=5000)
            await tx_button.click()

            # Real Whisper on CPU + a model-download-if-uncached step can
            # easily take 60+ s on first run. After the transcript
            # arrives the dashboard renders it on its next 1 s poll
            # tick — bound the wait at ~90 s by default (tiny.en on CPU
            # comfortably fits), overridable for slower hardware via
            # TAPSCRIBE_E2E_WHISPER_TIMEOUT_S. On timeout, raise with
            # the model and elapsed wait so a CI flake points at the
            # real culprit (model swap? cold-start download?) instead
            # of a bare Playwright TimeoutError. Two checks: at least
            # one reference anchor word appears (content), and every
            # segment `<div>` renders on a single visual line (layout —
            # `.transcript` uses `white-space: pre-wrap`, so stray
            # template whitespace would silently split each segment
            # across multiple lines).
            whisper_model = "tiny.en"
            timeout_s = float(os.environ.get("TAPSCRIBE_E2E_WHISPER_TIMEOUT_S", "90"))
            try:
                await page.wait_for_function(
                    f"""
                    () => {{
                      const region = document.querySelector(
                        '#viewRoot [data-slot="mergedHost"] [data-slot="lines"]');
                      if (!region) return false;
                      const text = region.textContent.toLowerCase();
                      if (text.length < 8) return false;
                      const refWords = {sorted(reference_words)!r};
                      if (!refWords.some((w) => text.includes(w))) return false;
                      const lines = Array.from(region.querySelectorAll(':scope > div'));
                      return lines.length > 0 && lines.every((l) => !l.innerText.includes('\\n'));
                    }}
                    """,
                    timeout=int(timeout_s * 1000),
                )
            except Exception as e:
                # Gather what actually rendered so a CI flake points at the
                # real culprit (transcribe never ran? marker never polled?
                # body fetched but reference word missing?).
                rendered = await page.evaluate(
                    """() => ({
                      txHint: document.querySelector('#viewRoot [data-slot="txHint"]')?.textContent,
                      lines: document.querySelectorAll(
                        '#viewRoot [data-slot="mergedHost"] [data-slot="lines"] > div').length,
                      sample: (document.querySelector(
                        '#viewRoot [data-slot="mergedHost"] [data-slot="lines"]')?.innerText || '').slice(0, 200),
                    })"""
                )
                tx_file = rec.session_dir / "session-transcript.json"
                raise AssertionError(
                    f"real-Whisper transcript never rendered: model={whisper_model!r}, "
                    f"waited {timeout_s:.0f}s "
                    f"(override via TAPSCRIBE_E2E_WHISPER_TIMEOUT_S). "
                    f"rendered={rendered!r} server_tx_exists={tx_file.is_file()} "
                    f"underlying error: {type(e).__name__}: {e}"
                ) from e

            await _shot(page, "06-real-audio-transcript.png")
        finally:
            await browser.close()


async def test_ui_only_click_updates_dom_without_a_fresh_poll(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    tmp_path: Path,
):
    """The dashboard's responsiveness guard: a click that only changes
    local UI state (here, expanding a WAV's inline transcript in the
    Recordings view) must apply from the client-side cache, NOT wait on a
    fresh /api/state fetch.

    We transcribe a WAV, let the dashboard cache it, then kill every
    further /api/state poll and click to expand. If the expand still
    happens, the re-render came from cache (refresh() paints from lastJson
    before polling). Before the fix the handler awaited /api/state (now
    dead), so the expand never rendered — exactly the "I click and nothing
    happens until I wait" symptom.
    """
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    scripted = "responsive expand check from Alice"
    fake_transcriber.text_by_speaker["Alice"] = scripted

    src = synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src,
        utterance_id="utt-resp-expand",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
    recorded = sorted(rec.session_dir.glob("*.wav"))
    assert len(recorded) == 1, f"expected one recorded WAV, got {[w.name for w in recorded]}"
    wav_name = recorded[0].name

    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            "/api/transcribe",
            json={"session": rec.session_start, "name": wav_name, "model": "tiny.en"},
        )
        assert resp.status_code == 200, resp.text

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            # Wait for the transcribed WAV row to render with its ✓ tx tag —
            # proves a poll has populated the client state with this WAV's
            # transcript marker.
            row_sel = f'.wavrow[data-wav="{wav_name}"]'
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector('{row_sel}');
                  return row && row.querySelector('[data-slot="txTag"]')?.textContent.includes('✓');
                }}
                """,
                timeout=10000,
            )
            # Pre-warm the per-WAV transcript body cache: open the row once
            # (the native <details> toggle fires a fetch of
            # /api/wav/.../transcript), wait for the text, then collapse. The
            # row is an original, so its name block SELECTS the waveform —
            # toggle via a neutral part of the summary (the duration) instead.
            await page.locator(f'{row_sel} [data-slot="dur"]').click()
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector('{row_sel}');
                  return row && row.open &&
                    Array.from(row.querySelectorAll('.expand-tx'))
                      .some((el) => el.textContent.includes({scripted!r}));
                }}
                """,
                timeout=5000,
            )
            await page.locator(f'{row_sel} [data-slot="dur"]').click()
            # A native <details> keeps its body in the DOM when collapsed (just
            # hidden) — assert the row is closed, not that the node is gone.
            await page.wait_for_function(
                f"""() => {{ const r = document.querySelector('{row_sel}'); return r && !r.open; }}""",
                timeout=5000,
            )

            # Kill every further /api/state poll. From here the dashboard
            # has no fresh server data; a UI-only click must apply from
            # the client-side cache or not at all.
            async def _kill_state(route):
                await route.fulfill(status=503, body="down")

            await page.route("**/api/state", _kill_state)

            await page.locator(f'{row_sel} [data-slot="dur"]').click()
            # The 1500ms bound is below what any rescuing poll could deliver
            # (polls are dead) — so a pass means the expand re-rendered from the
            # cached body on click (the toggle is pure DOM; fillExpand reads the
            # client cache), not from a network round trip.
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector('{row_sel}');
                  return row && row.open &&
                    Array.from(row.querySelectorAll('.expand-tx'))
                      .some((el) => el.textContent.includes({scripted!r}));
                }}
                """,
                timeout=1500,
            )
        finally:
            await browser.close()


async def test_lazy_transcript_fetch_is_cached_not_per_poll(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    tmp_path: Path,
):
    """The lazy transcript fetch must be CACHED by (session, transcribed_at):
    hit ONCE when the Transcript stage opens, then NOT re-fetched on every
    /api/state poll. Otherwise the optimisation just trades DOM churn for
    network churn.

    Records + transcribes a session so /api/state ships a transcript MARKER,
    opens the Transcript view (which renders the merged transcript via a lazy
    fetch of GET /api/sessions/{s}/transcript), counts both the poll ticks and
    the transcript-endpoint hits across ~6 poll cycles, and asserts the
    transcript endpoint fired exactly once while many polls elapsed.
    """
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    scripted = "cached fetch check from Alice"
    fake_transcriber.text_by_speaker["Alice"] = scripted

    src = synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src,
        utterance_id="utt-cache-count",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        # Transcribe the whole session so /api/state carries a session_transcript
        # marker and the dashboard's merged-transcript path fires a lazy fetch.
        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "tiny.en"},
        )
        assert resp.status_code == 200, resp.text

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()

            # Count poll ticks and lazy transcript-endpoint hits from the page.
            await page.add_init_script(
                """
                window.__statePolls = 0;
                window.__txFetches = 0;
                const _f = window.fetch;
                window.fetch = function (input, init) {
                  const u = typeof input === 'string' ? input : (input && input.url) || '';
                  if (u.includes('/api/state')) window.__statePolls++;
                  else if (/\\/api\\/sessions\\/[^/]+\\/transcript/.test(u)) window.__txFetches++;
                  return _f.apply(this, arguments);
                };
                """
            )
            await page.goto(base + "/#transcript", wait_until="domcontentloaded")

            # Wait until the merged transcript has rendered — proves the lazy
            # fetch fired and the body painted.
            await page.wait_for_function(
                f"""
                () => {{
                  const region = document.querySelector(
                    '#viewRoot [data-slot="mergedHost"] [data-slot="lines"]');
                  return region && region.innerText.includes({scripted!r});
                }}
                """,
                timeout=15000,
            )

            # Let several poll cycles elapse (poll cadence ~500ms) so a
            # per-poll re-fetch would clearly show up in the counter.
            await page.wait_for_function(
                "() => window.__statePolls >= 6",
                timeout=10000,
            )

            polls = await page.evaluate("() => window.__statePolls")
            fetches = await page.evaluate("() => window.__txFetches")
            assert polls >= 6, f"expected several polls, saw {polls}"
            # The transcript endpoint must have fired ONCE for this (session,
            # transcribed_at) — not once per poll. (1 is the contract; allow a
            # tiny slack only for an initial duplicate render, never per-poll.)
            assert fetches == 1, (
                f"lazy transcript fetch not cached: {fetches} hits across {polls} polls "
                f"(must be 1 per (session, transcribed_at), not per poll)"
            )
        finally:
            await browser.close()


# (The classic dashboard's model-select blur test was retired with the
# classic UI: the Stages engine select renders through renderRegion, whose
# focus guard the poll-safety sweep below already exercises for every view.)


# The Stages views, in the order window.gotoView accepts them. Drives the
# poll-safety sweep below across every stage so a new dropdown in any view
# can't silently regress the renderRegion guard. The global Sessions list is
# included because it holds a search box + per-row rename inputs that the
# 500ms poll must not clobber.
_NEXT_VIEWS = ("capture", "recordings", "transcript", "summary", "taps", "sessions", "people", "settings")

# > one poll period (500ms in next/main.js) so the sweep crosses at least
# one re-render boundary. The sweep also asserts a poll actually fired during
# this window, so a vacuous pass (polls somehow stalled) can't hide a regression.
_NEXT_POLL_CROSS_MS = 750


async def test_next_poll_render_does_not_clobber_open_controls(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    tmp_path: Path,
):
    """The Stages dashboard re-renders every per-tick region on each 500ms
    /api/state poll via host.replaceChildren(...). Replacing a node that holds
    a focused <select> / <input> / <textarea> would snap a dropdown shut or
    drop the caret mid-edit. The shared `renderRegion` primitive (templates.js)
    guards every per-tick region against that, and the spine adopts it as the
    reference.

    This sweep is the regression net: for each of the six views, it focuses
    every focusable control currently rendered in `#viewRoot` (plus the spine's
    session <select>), stamps a unique JS property on the live node, lets more
    than one poll elapse, and asserts the SAME node is still in the DOM with the
    stamp intact AND still document.activeElement. A clobbering re-render would
    have built a fresh node — dropping the JS property and the focus — so the
    assertion fails loudly.

    A control that can't actually take focus (e.g. a disabled engine <select>
    on the synthetic CI box with no transcriber backends installed) carries no
    live interaction state to protect, so the sweep skips it after confirming
    focus didn't land. Views with no focusable controls in this state are
    skipped too; the test asserts it exercised at least one control overall so
    it can't pass by finding nothing anywhere.
    """
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    # Seed a session with two recorded WAVs so Recordings/Transcript render
    # their WAV-list controls and People derives participants to name. Transcribe
    # one so the Transcript cache panel and merged view populate too.
    fake_transcriber.text_by_speaker["Alice"] = "Seeded line for the persistence sweep."
    src = synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src,
        utterance_id="utt-persist-1",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
    recorded = sorted(rec.session_dir.glob("*.wav"))
    assert len(recorded) == 1, f"expected one recorded WAV, got {[w.name for w in recorded]}"
    wav_name = recorded[0].name

    # Seed TWO archived sessions so the Sessions view renders its per-row
    # "absorb into…" <select> (an archived row needs a non-self archived
    # target) — otherwise that control is absent from the DOM and the sweep
    # can't verify its renderRegion focus-guard.
    for sweep_sid in ("2024-05-01T10-00-00Z", "2024-05-02T10-00-00Z"):
        d = rec.recordings_dir / sweep_sid
        d.mkdir(parents=True)
        synth_speech_like_wav(d / f"{sweep_sid}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            "/api/transcribe",
            json={"session": rec.session_start, "name": wav_name, "model": "tiny.en"},
        )
        assert resp.status_code == 200, resp.text

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            # Count /api/state hits so the sweep can prove a poll actually
            # crossed the focus window (otherwise a stalled poll would let a
            # would-be clobber pass unnoticed).
            await page.expose_function("__noteStatePoll", lambda: None)
            await page.add_init_script(
                """
                window.__statePolls = 0;
                const _fetch = window.fetch;
                window.fetch = (...args) => {
                  try {
                    const u = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
                    if (u.includes('/api/state')) window.__statePolls++;
                  } catch (_e) { /* never let bookkeeping break the real fetch */ }
                  return _fetch.apply(window, args);
                };
                """
            )
            await page.goto(base, wait_until="domcontentloaded")

            # Boot done once the spine has rendered its session <select> with the
            # seeded session as an option (proves /api/state + the model catalog
            # load have both landed).
            await page.wait_for_function(
                f"""
                () => {{
                  const sel = document.querySelector('[data-slot="sessionPick"]');
                  return sel && Array.from(sel.options).some(
                    (o) => o.value === {rec.session_start!r}
                  );
                }}
                """,
                timeout=10000,
            )

            # One sweep over a single view: focus + mark each focusable control,
            # cross a poll, assert each marked node survived intact & focused.
            # Returns the number of controls actually exercised in this view.
            async def sweep_view(view: str) -> int:
                await page.evaluate("(v) => window.gotoView(v)", view)
                # Let the view mount + its first per-tick update run.
                await page.wait_for_function(
                    "() => document.querySelector('#viewRoot')?.childElementCount > 0",
                    timeout=5000,
                )

                # Tag every candidate control with a stable index so each can
                # be reacquired by selector after a re-render. Tagging is inert
                # (no focus) — focus happens one control at a time below, so
                # focusing control N never steals focus from control N-1 and
                # muddies its result. The spine's session <select> rides along
                # (it's a per-tick region too, and lives outside #viewRoot).
                count = await page.evaluate(
                    """
                    () => {
                      const root = document.getElementById('viewRoot');
                      const spineSel = document.querySelector('[data-slot="sessionPick"]');
                      const sel =
                        'select, input[type="text"], input[type="search"], ' +
                        'input[type="number"], input:not([type]), textarea';
                      const controls = root ? [...root.querySelectorAll(sel)] : [];
                      if (spineSel) controls.push(spineSel);
                      controls.forEach((el, i) => el.setAttribute('data-sweep-idx', String(i)));
                      return controls.length;
                    }
                    """
                )

                exercised = 0
                for idx in range(count):
                    sweep_sel = f'[data-sweep-idx="{idx}"]'
                    # Focus + stamp this ONE control, then report whether it
                    # actually took focus. A control that can't (disabled, or a
                    # type that ignores .focus()) has no interaction state to
                    # protect — renderRegion is allowed to replace it — so skip.
                    mark = f"{view}:{idx}"
                    took_focus = await page.evaluate(
                        """
                        ([sel, mark]) => {
                          const el = document.querySelector(sel);
                          if (!el || el.disabled) return false;
                          el.focus();
                          if (document.activeElement !== el) return false;
                          el.__persistMark = mark;          // JS property a fresh node won't carry
                          el.setAttribute('data-persist-mark', mark);
                          return true;
                        }
                        """,
                        [sweep_sel, mark],
                    )
                    if not took_focus:
                        continue

                    polls_before = await page.evaluate("() => window.__statePolls || 0")
                    # Cross more than one poll period with this control focused.
                    # renderRegion must keep its node in place; a clobbering
                    # re-render would build a fresh node (no __persistMark) and
                    # drop focus.
                    await page.wait_for_timeout(_NEXT_POLL_CROSS_MS)
                    polls_after = await page.evaluate("() => window.__statePolls || 0")
                    assert polls_after > polls_before, (
                        f"view {view!r} control {mark}: no /api/state poll fired during the "
                        f"{_NEXT_POLL_CROSS_MS}ms window ({polls_before} → {polls_after}); "
                        "the sweep would pass vacuously"
                    )

                    # Same live node (JS property survives — a node minted by
                    # replaceChildren never would) AND still focused.
                    failure = await page.evaluate(
                        """
                        (mark) => {
                          const el = document.querySelector('[data-persist-mark="' + mark + '"]');
                          if (!el) return 'node gone (region was rebuilt)';
                          if (el.__persistMark !== mark) return 'JS mark lost (node was replaced)';
                          if (el !== document.activeElement) {
                            const a = document.activeElement;
                            return 'lost focus (active=' + (a ? a.tagName : 'none') + ')';
                          }
                          return '';
                        }
                        """,
                        mark,
                    )
                    assert not failure, (
                        f"view {view!r} control {mark}: clobbered by poll re-render — {failure}"
                    )
                    exercised += 1

                    # Blur before the next control so its focus result is clean.
                    await page.evaluate(
                        "() => { const a = document.activeElement; if (a && a.blur) a.blur(); }"
                    )
                return exercised

            total_controls = 0
            per_view: dict[str, int] = {}
            for view in _NEXT_VIEWS:
                n = await sweep_view(view)
                per_view[view] = n
                total_controls += n

            # The sweep must have actually exercised controls — otherwise a
            # render that quietly stopped emitting inputs would pass for free.
            assert total_controls > 0, (
                f"persistence sweep exercised no focusable controls across any view: {per_view}"
            )

            # The two seeded archived sessions must have produced the per-row
            # absorb <select> in the Sessions view — the newest renderRegion-
            # guarded dropdown this sweep exists to protect. If this fails,
            # the seeding above rotted and the sweep silently lost coverage.
            await page.evaluate("() => window.gotoView('sessions')")
            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot [data-slot="absorb"]').length >= 1""",
                timeout=5000,
            )
        finally:
            await browser.close()


async def test_transcript_languages_readout_names_the_specialist(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001 — fixture arms the fake backend so the view mounts
    tmp_path: Path,
):
    """ADR-0011: the Transcript page declares LANGUAGES, not a model, and a
    read-only readout names the exact models a transcribe will run — so the
    Norwegian nb-whisper (faster-whisper) specialist is visible BEFORE clicking,
    the direct fix for the surprise faster-whisper sidecar.

    Three assertions: (1) the inherited default {da,no,en} already names
    nb-whisper (the previously-silent specialist is now on screen, marked
    inherited); (2) an English-only pick collapses to "generalist only"; (3)
    picking Norwegian names nb-whisper-large again. No transcribe is run — this
    is a pure readout/derivation test against the real /api/languages catalog."""
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    src = synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.6, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=ws_base, identity="alice", name="Alice", wav_path=src, utterance_id="utt-lang-1"
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")
            # Boot done once the spine lists the seeded session (proves /api/state
            # + the catalogs, /api/languages among them, have landed).
            await page.wait_for_function(
                f"""() => {{
                  const sel = document.querySelector('[data-slot="sessionPick"]');
                  return sel && Array.from(sel.options).some((o) => o.value === {rec.session_start!r});
                }}""",
                timeout=10000,
            )
            await page.evaluate('() => window.gotoView("transcript")')
            # The meeting-languages <select multiple> mounts, populated from the
            # catalog (≥ da/no/en) — i.e. the view rebuilt with the loaded catalog.
            await page.wait_for_function(
                """() => {
                  const s = document.querySelector('[data-slot="txLanguages"]');
                  return s && s.options.length >= 3;
                }""",
                timeout=5000,
            )

            models = '[data-slot="txLangModels"]'
            effective = '[data-slot="txLangEffective"]'

            # (1) Inherited default {da,no,en} contains Norwegian → nb-whisper is
            # already named (the surprise, now visible), and the effective line
            # says it's inherited from the global default.
            await page.wait_for_function(
                f"""() => (document.querySelector({models!r})?.textContent || '').includes('nb-whisper-large')""",
                timeout=5000,
            )
            assert "inherited" in (await page.text_content(effective) or "")

            # (2) English-only pick → no specialist → "(generalist) only".
            await page.select_option('[data-slot="txLanguages"]', "en")
            await page.wait_for_function(
                f"""() => /\\(generalist\\) only\\s*$/.test(document.querySelector({models!r})?.textContent || '')""",
                timeout=5000,
            )

            # (3) Norwegian pick → nb-whisper-large (Norwegian) named again.
            await page.select_option('[data-slot="txLanguages"]', "no")
            await page.wait_for_function(
                f"""() => {{
                  const t = document.querySelector({models!r})?.textContent || '';
                  return t.includes('nb-whisper-large') && t.includes('Norwegian');
                }}""",
                timeout=5000,
            )
        finally:
            await browser.close()


async def test_transcript_transcribe_saves_languages_first_wysiwyg(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001 — arms the fake backend so the cover completes
    tmp_path: Path,
):
    """ADR-0011 save-on-transcribe (WYSIWYG): clicking a transcribe action writes
    the current language selection to session-meta BEFORE running, so an operator
    never transcribes with a stale set. Select Norwegian, click ▶ transcribe
    range, and assert the meeting's session-meta now pins ["no"] — the selection
    persisted as part of the action, not a separate step the operator can forget."""
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url
    sid = rec.session_start

    src = synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.6, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=ws_base, identity="alice", name="Alice", wav_path=src, utterance_id="utt-save-1"
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#transcript", wait_until="domcontentloaded")
            # Wait until the languages <select> is populated AND the range button
            # is enabled (session has a WAV to transcribe).
            await page.wait_for_function(
                """() => {
                  const s = document.querySelector('[data-slot="txLanguages"]');
                  const b = document.querySelector('#viewRoot [data-slot="txRangeBtn"]');
                  return s && s.options.length >= 3 && b && !b.disabled;
                }""",
                timeout=10000,
            )
            await page.select_option('[data-slot="txLanguages"]', "no")
            await page.locator('#viewRoot [data-slot="txRangeBtn"]').click()

            # The PUT to session-meta runs before the transcribe POST, so the
            # override lands regardless of the transcribe outcome. Poll it.
            async def _meta_pins_no() -> bool:
                async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
                    r = await client.get(f"/api/session-meta/{sid}")
                    return r.status_code == 200 and r.json().get("languages") == ["no"]

            assert await wait_until(_meta_pins_no, timeout=10.0), "transcribe did not save languages first"
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Performance guard: idle polling must not churn DOM nodes / event listeners.
# ---------------------------------------------------------------------------
# The dashboard is a polling SPA. A component that re-mounts or rebuilds a DOM
# region on EVERY /api/state tick (no signature gate) — e.g. re-`mount()`ing an
# empty-state fragment, or `renderRegion` with no `sig` — produces hundreds of
# collectable *detached* nodes + listeners per second. They are garbage, but an
# operator's always-open tab accumulates them between GCs and eventually OOMs
# (real report: Edge tab, climbing "DOM Nodes"/"JS event listeners", 47 s LCP).
#
# This guard measures the exact metrics the browser's Performance Monitor shows
# (Nodes incl. detached, JSEventListeners) via CDP, across ~10 s of TRUE idle
# (no taps, empty live feed). Listener growth must be ~0 (any growth = a
# component re-attaching listeners each tick); node growth gets a benign
# baseline but must not regress to the per-tick-churn range that the
# live-feed ascii / spine / active-taps produced before they were sig-gated.
#
# Tuning: these are deliberately loose enough to absorb CI poll-count variance
# and the remaining benign header re-render, but tight enough that reverting any
# of the sig gates (live-feed.js, active-taps.js, spine.js) trips it.
_IDLE_MAX_LISTENER_GROWTH = 40
_IDLE_MAX_NODE_GROWTH = 1400


async def _perf_metrics(client) -> tuple[float, float]:
    res = await client.send("Performance.getMetrics")
    m = {d["name"]: d["value"] for d in res["metrics"]}
    return m.get("Nodes", 0), m.get("JSEventListeners", 0)


async def test_dashboard_idle_polling_does_not_churn_dom(running_recorder: RunningRecorder):
    """At idle, the dashboard may not grow DOM nodes/listeners every poll.

    Catches the whole bug class of "render region rebuilt every tick without a
    change signature", which leaks collectable detached nodes/listeners fast
    enough to OOM a long-lived operator tab.
    """
    base = running_recorder.base_url
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            failures: list[str] = []
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            client = await page.context.new_cdp_session(page)
            await client.send("Performance.enable")
            # Count /api/state hits: per-tick churn can only be measured across
            # ticks that actually FIRED. With zero polls in the window the
            # deltas are trivially 0 and the guard passes vacuously.
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.goto(base, wait_until="domcontentloaded")
            # Settle: let first-paint + the first couple of polls land.
            await page.wait_for_timeout(2500)
            n0, l0 = await _perf_metrics(client)
            polls_0 = await page.evaluate("() => window.__statePolls || 0")
            # Idle for ~10 s of wall clock. That is NOT "~20 poll cycles": this
            # recorder is idle-and-unchanged, so ADR-0013's pacer backs off from
            # FAST_MS=500 to SLOW_MS=2000 after IDLE_STREAK=4 and the window
            # really contains ~5-8 ticks. The wall-clock window is deliberate
            # (the CDP metrics are a rate measurement), but the poll floor below
            # is what makes it non-vacuous.
            await page.wait_for_timeout(10000)
            n1, l1 = await _perf_metrics(client)
            polls_1 = await page.evaluate("() => window.__statePolls || 0")
            assert polls_1 >= polls_0 + 4, (
                f"only {polls_1 - polls_0} /api/state tick(s) crossed the idle window "
                f"({polls_0} → {polls_1}) — per-tick churn can't be measured across no ticks"
            )
            dn, dl = n1 - n0, l1 - l0
            print(f"[idle-churn] /: dNodes={dn:+.0f}  dListeners={dl:+.0f}  polls={polls_1 - polls_0}")
            if dl > _IDLE_MAX_LISTENER_GROWTH:
                failures.append(f"/: +{dl:.0f} listeners over ~10s idle (per-tick listener churn)")
            if dn > _IDLE_MAX_NODE_GROWTH:
                failures.append(f"/: +{dn:.0f} DOM nodes over ~10s idle (per-tick DOM churn)")
            await context.close()
            assert not failures, "idle DOM churn regression: " + "; ".join(failures)
        finally:
            await browser.close()


# JS init script shared by the two tests below: wraps window.fetch to count
# /api/state hits AND, separately, how many of those responses actually came
# back 304 — the discriminator that proves the server (not just the DOM) went
# genuinely quiet, so a flat renderAllCount across that window means the fix
# is skipping real no-op ticks rather than getting lucky with a JSON payload
# that happens to render identically.
_COUNT_STATE_304S_JS = """
window.__statePolls = 0;
window.__state304s = 0;
const _fetch = window.fetch;
window.fetch = (...args) => {
  const u = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
  const isState = u.includes('/api/state');
  if (isState) window.__statePolls++;
  const p = _fetch.apply(window, args);
  if (isState) p.then((r) => { if (r.status === 304) window.__state304s++; }).catch(() => {});
  return p;
};
"""


async def test_next_idle_304_ticks_skip_render_all(running_recorder: RunningRecorder):
    """api.js's fetchState() reuses the same cached state OBJECT on a 304
    (issue #245). Pre-fix, main.js's tick() called renderAll() on every poll
    regardless of that identity — recomputing the spine's O(sessions)
    signature (metaFor spreads + per-session alias-key joins), the Sessions
    view's listSig, and the People view's sig from scratch every ~500ms even
    when nothing on the server changed. An always-open, otherwise-idle
    operator tab paid that cost forever for no observable benefit.

    Guard: seed two archived, non-live sessions (no active taps — a live tap's
    changing level/lag would make the server's ETag legitimately differ every
    poll and the state would never 304, which would make this test vacuous).
    Confirm real 304s are landing (not just unchanged-looking JSON), snapshot
    window.__TAPSCRIBE_RENDER_ALL_COUNT, cross several more confirmed 304s,
    and assert the count did not move.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url

    for sid in ("2025-03-01T10-00-00Z", "2025-03-02T10-00-00Z"):
        d = rec.recordings_dir / sid
        d.mkdir(parents=True)
        synth_speech_like_wav(d / f"{sid}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")

            # Boot done once both seeded sessions render as rows.
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-sid]').length >= 2",
                timeout=10000,
            )

            # Let a few REAL 304s land before the baseline — this settles any
            # boot-time / model-catalog-load renders and proves the server has
            # actually gone quiet, not merely that nothing looks different.
            await page.wait_for_function("() => window.__state304s >= 3", timeout=10000)
            polls_baseline = await page.evaluate("() => window.__state304s")
            render_count_0 = await page.evaluate("() => window.__TAPSCRIBE_RENDER_ALL_COUNT")

            # Cross several more purely-idle, confirmed-304 polls.
            await page.wait_for_function(
                "(base) => window.__state304s >= base + 3",
                arg=polls_baseline,
                timeout=10000,
            )
            render_count_1 = await page.evaluate("() => window.__TAPSCRIBE_RENDER_ALL_COUNT")

            assert render_count_1 == render_count_0, (
                f"renderAll ran {render_count_1 - render_count_0} extra time(s) across "
                "purely-304 idle polls — tick() must skip renderAll entirely when "
                "fetchState() returns the identical cached object and no render is "
                "deferred (issue #245)"
            )
            await context.close()
        finally:
            await browser.close()


async def test_next_idle_focus_in_a_region_does_not_rerun_render_all(
    running_recorder: RunningRecorder,
):
    """A caret parked in a region with nothing changing must not re-run renderAll
    on every tick (ADR-0016, #245).

    renderRegion checks its `sig` BEFORE its interaction hold, precisely so a
    region with nothing to render marks no tick-retry. Get that order backwards
    — hold first, mark the retry, then check the sig — and the pass becomes
    self-sustaining: every 304 tick sees `wasDeferred`, re-runs renderAll, holds
    again on the same focus, and re-marks, for as long as the operator's cursor
    sits there. The DOM stays correct throughout, so no other test in this file
    notices; only the work is unbounded.

    The trigger has to be a state change that leaves the SPINE's sig alone,
    because a change the spine genuinely needs to render is owed a retry under
    either ordering (that is the hold working). Adding a WAV to a session the
    spine is not focused on is exactly that: /api/state changes (bytes,
    wav_count), while the spine's sig — view, backend, tap counts, session
    count, people count, per-session label/current/transcript flags, and the
    FOCUSED session's own counters — does not move.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url

    for sid in ("2025-03-01T10-00-00Z", "2025-03-02T10-00-00Z"):
        d = rec.recordings_dir / sid
        d.mkdir(parents=True)
        synth_speech_like_wav(d / f"{sid}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-sid]').length >= 2",
                timeout=10000,
            )

            # Park focus in the spine — a control INSIDE the renderRegion host.
            # `input[…]`, not `[…]`: the spine's nav items each carry a
            # data-slot="name" SPAN, and only the session-name field is focusable.
            name_input = '#spine input[data-slot="name"]'
            await page.click(name_input)
            assert await page.evaluate(
                "(sel) => document.activeElement === document.querySelector(sel)", name_input
            )

            # Move /api/state WITHOUT moving the spine's sig: a new WAV under a
            # session the spine is not focused on. This is what starts the loop
            # under a hold-first ordering — one real change, then quiet.
            focused_sid = await page.evaluate(
                "() => document.querySelector('#spine [data-slot=\"sessionPick\"]').value"
            )
            other = next(s for s in ("2025-03-01T10-00-00Z", "2025-03-02T10-00-00Z") if s != focused_sid)
            synth_speech_like_wav(
                rec.recordings_dir / other / f"{other}_seed_speaker_00000002.wav",
                seconds=0.3,
                freq_hz=330.0,
            )

            # Let that change land and the server go quiet again, then measure.
            await page.wait_for_function("() => window.__state304s >= 3", timeout=10000)
            polls_baseline = await page.evaluate("() => window.__state304s")
            render_count_0 = await page.evaluate("() => window.__TAPSCRIBE_RENDER_ALL_COUNT")

            await page.wait_for_function(
                "(base) => window.__state304s >= base + 3",
                arg=polls_baseline,
                timeout=10000,
            )
            render_count_1 = await page.evaluate("() => window.__TAPSCRIBE_RENDER_ALL_COUNT")

            assert render_count_1 == render_count_0, (
                f"renderAll ran {render_count_1 - render_count_0} extra time(s) across idle "
                "304 polls while a control in the spine held focus — an unchanged sig must "
                "short-circuit BEFORE the interaction hold, so an idle caret marks no "
                "tick-retry (ADR-0016, #245)"
            )
            # Focus is still where the operator left it.
            assert await page.evaluate(
                "(sel) => document.activeElement === document.querySelector(sel)", name_input
            )
            await context.close()
        finally:
            await browser.close()


async def test_next_region_hold_lands_after_blur_across_304_ticks(
    running_recorder: RunningRecorder,
):
    """A region swap held by focus lands on the first tick after blur, even when
    the poll has gone 304-quiet in the meantime (ADR-0016).

    The region analogue of the keyed-list guarantee: renderRegion no longer
    self-flushes on `focusout`, so the ONLY thing that lands the held swap is
    the tick-retry flag surviving main.js's 304 short-circuit. If a future
    change held a render without marking the flag, the spine would stay stale
    indefinitely once the server went quiet — the failure mode that made the
    flag exist. Deliberately blurs only AFTER 304s resume, so the retry is the
    only mechanism that can be under test.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    for sid in ("2025-03-01T10-00-00Z", "2025-03-02T10-00-00Z"):
        d = rec.recordings_dir / sid
        d.mkdir(parents=True)
        synth_speech_like_wav(d / f"{sid}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")
            picker = '#spine [data-slot="sessionPick"]'
            await page.wait_for_function(
                "(sel) => document.querySelectorAll(sel + ' option').length >= 2",
                arg=picker,
                timeout=10000,
            )

            # Baseline the option count rather than hardcoding it: the picker
            # lists the seeded sessions AND the recorder's own current session.
            baseline = await page.evaluate(
                "(sel) => document.querySelectorAll(sel + ' option').length", picker
            )

            # Focus a control inside the spine, then change what the spine
            # renders: another session moves its sig (sessions.length + the
            # per-session label term).
            await page.click('#spine input[data-slot="name"]')
            sid_c = "2025-03-05T10-00-00Z"
            d = rec.recordings_dir / sid_c
            d.mkdir(parents=True)
            synth_speech_like_wav(d / f"{sid_c}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

            # The spine is HELD: the new session must not appear while focus
            # sits inside it, however many polls carry the new state.
            await page.wait_for_function("() => window.__statePolls >= 4", timeout=10000)
            held_options = await page.evaluate(
                "(sel) => document.querySelectorAll(sel + ' option').length", picker
            )
            assert held_options == baseline, (
                f"the spine rebuilt to {held_options} options (from {baseline}) while a control "
                "inside it held focus — a region swap must defer to the interaction hold "
                "(ADR-0004)"
            )

            # Let the server go quiet again, so only the tick-retry can land it.
            polls_baseline = await page.evaluate("() => window.__state304s")
            await page.wait_for_function(
                "(base) => window.__state304s >= base + 2", arg=polls_baseline, timeout=10000
            )
            still_held = await page.evaluate(
                "(sel) => document.querySelectorAll(sel + ' option').length", picker
            )
            assert still_held == baseline, "the spine rebuilt under the focus once the poll went quiet"

            # Release focus — the held render lands on the next tick.
            await page.evaluate("() => document.activeElement.blur()")
            await page.wait_for_function(
                "([sel, want]) => document.querySelectorAll(sel + ' option').length >= want",
                arg=[picker, baseline + 1],
                timeout=10000,
            )
            await context.close()
        finally:
            await browser.close()


async def test_next_region_hold_covers_an_open_popover_inside_the_host(
    running_recorder: RunningRecorder,
):
    """A popover or <dialog> open INSIDE a region host holds the swap, and the
    held render lands once it closes (ADR-0016).

    This pins a hazard rather than reproducing a shipped bug: today every
    overlay in the dashboard is appended to document.body (live-channel.js's log
    dialog), so no renderRegion host contains one and the guard is never
    exercised by a real flow. The seam owns this term precisely so the first
    overlay someone puts inside a region is covered on arrival — before this
    consolidation the overlay branch was the one hold still delegated to the
    vendored copy, where a deferral could flush a build that went stale while
    the seam absorbed newer ticks. The overlay is injected here for the same
    reason the hazard is latent: there is no other way to reach the branch.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    for sid in ("2025-03-01T10-00-00Z", "2025-03-02T10-00-00Z"):
        d = rec.recordings_dir / sid
        d.mkdir(parents=True)
        synth_speech_like_wav(d / f"{sid}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")
            picker = '#spine [data-slot="sessionPick"]'
            await page.wait_for_function(
                "(sel) => document.querySelectorAll(sel + ' option').length >= 2",
                arg=picker,
                timeout=10000,
            )

            baseline = await page.evaluate(
                "(sel) => document.querySelectorAll(sel + ' option').length", picker
            )

            # Put an OPEN popover inside the spine (the region host).
            await page.evaluate(
                """() => {
                    const pop = document.createElement('div');
                    pop.id = 'probePopover';
                    pop.popover = 'manual';
                    pop.textContent = 'open';
                    document.getElementById('spine').appendChild(pop);
                    pop.showPopover();
                }"""
            )
            assert await page.evaluate("() => !!document.querySelector('#spine :popover-open')"), (
                "the probe popover did not open — the browser lacks Popover API support"
            )

            # Move the spine's sig while the popover is open.
            sid_c = "2025-03-05T10-00-00Z"
            d = rec.recordings_dir / sid_c
            d.mkdir(parents=True)
            synth_speech_like_wav(d / f"{sid_c}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

            await page.wait_for_function("() => window.__statePolls >= 4", timeout=10000)
            assert await page.evaluate("() => !!document.querySelector('#spine :popover-open')"), (
                "the region was swapped out from under an open popover inside it"
            )
            held_options = await page.evaluate(
                "(sel) => document.querySelectorAll(sel + ' option').length", picker
            )
            assert held_options == baseline, (
                f"the spine rebuilt to {held_options} options (from {baseline}) with a popover open inside it"
            )

            # Close it — the held render lands on the next tick.
            await page.evaluate("() => document.getElementById('probePopover').hidePopover()")
            await page.wait_for_function(
                "([sel, want]) => document.querySelectorAll(sel + ' option').length >= want",
                arg=[picker, baseline + 1],
                timeout=10000,
            )
            await context.close()
        finally:
            await browser.close()


async def test_next_deferred_render_lands_after_focus_clears_across_304_ticks(
    running_recorder: RunningRecorder,
):
    """The Sessions list renders keyed-and-in-place (#312, reconcileList): a
    poll that changes a SIBLING row while a rename <input> is focused updates
    that sibling's cells live, AROUND the focused input — no whole-region
    deferral, no clobber. This pins the three legs of that contract at once:

    - the sibling's transcript-status cell updates WHILE the rename input in
      another row holds focus (the old whole-region renderRegion mechanism
      deferred everything until blur);
    - the update is IN PLACE: the sibling row keeps its DOM node identity
      (identity-stamp trick — a rebuilt node can't carry the JS expando);
    - the focused input is untouched: focus AND its typed value survive the
      sibling's update crossing a poll.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid_a = "2025-03-03T10-00-00Z"
    sid_b = "2025-03-04T10-00-00Z"
    for sid in (sid_a, sid_b):
        d = rec.recordings_dir / sid
        d.mkdir(parents=True)
        synth_speech_like_wav(d / f"{sid}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")

            await page.wait_for_function(
                "() => document.querySelectorAll('[data-sid]').length >= 2",
                timeout=10000,
            )

            # Focus session A's inline rename input and type into it — fillRow
            # must leave the focused input alone while B updates around it.
            a_rename = f'[data-sid="{sid_a}"] [data-slot="rename"]'
            await page.click(a_rename)
            await page.type(a_rename, "standup notes")
            assert await page.evaluate(
                "(sel) => document.activeElement === document.querySelector(sel)", a_rename
            )

            # Identity-stamp B's row: a rebuilt node can't carry the expando,
            # so the stamp surviving proves the update was in place.
            b_row = f'[data-sid="{sid_b}"]'
            await page.evaluate("(sel) => { document.querySelector(sel).__stamp = 'b-row'; }", b_row)

            # Mutate session B on disk: land a merged transcript, flipping its
            # tx-status cell from "not run" to "merged · N seg" server-side.
            (rec.recordings_dir / sid_b / "session-transcript.json").write_text(
                json.dumps(
                    {
                        "transcribed_at": "2025-03-04T11:00:00+00:00",
                        "segments": [{"speaker": "Alice", "text": "hello", "abs_start": sid_b}],
                        "speakers": ["Alice"],
                        "speaking_seconds": {"Alice": 1.0},
                        "suppressed": [],
                        "suppressed_count": 0,
                        "wav_count": 1,
                        "transcribe_ms": 10,
                        "model": "tiny.en",
                        "backend": "fake",
                        "device": "cpu",
                    }
                ),
                encoding="utf-8",
            )

            b_tx = f'[data-sid="{sid_b}"] [data-slot="tx"]'
            # B's cell updates WHILE A's rename holds focus — the reconcile
            # path updates the sibling around the interaction instead of
            # deferring the whole region (#312).
            await page.wait_for_function(
                "(sel) => document.querySelector(sel)?.textContent?.startsWith('merged')",
                arg=b_tx,
                timeout=5000,
            )
            # …in place: B's row kept its node (the stamp survived)…
            assert await page.evaluate("(sel) => document.querySelector(sel).__stamp === 'b-row'", b_row), (
                "B's row was rebuilt (stamp lost) — the reconcile should mutate cells in place"
            )
            # …and the focused input was untouched: focus AND the typed value.
            assert await page.evaluate(
                "(sel) => document.activeElement === document.querySelector(sel)", a_rename
            ), "focus was clobbered by the sibling's in-place update"
            assert (
                await page.evaluate("(sel) => document.querySelector(sel).value", a_rename)
            ) == "standup notes", "the focused rename input's typed value was clobbered"

            # Cross a few plain-304 polls (nothing changing server-side) and
            # re-assert: the idle skip must not repaint over the held input.
            await page.wait_for_function("() => window.__state304s >= 3", timeout=10000)
            assert (
                await page.evaluate("(sel) => document.querySelector(sel).value", a_rename)
            ) == "standup notes"
            await context.close()
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Performance guard: state churn must not rebuild stable /next regions.
# ---------------------------------------------------------------------------
# The perf soak (tests/e2e/test_next_perf_soak.py, opt-in via
# TAPSCRIBE_PERF_SOAK=1) found the "/next locks up now and again" class: a
# region whose render signature includes per-tick values (job progress, the
# live-feed tail) gets rebuilt wholesale on every poll. For the merged
# transcript that's an O(segments) synchronous DOM build — 100-200 ms of
# blocked main thread PER TICK on a long session (30 long tasks / 2.4 s
# blocked over a 30 s soak at baseline). These guards pin the fixes
# STRUCTURALLY, with no timing thresholds (CI-runner safe): stamp a JS
# expando on a node inside the region, drive real churn across several
# polls, and fail if the node was rebuilt — a freshly minted node can't
# carry the stamp. Same trick as the focus-clobber sweep above.


def _seed_merged_session(rec, sid: str, *, segments: int) -> None:
    """A non-current on-disk session with one WAV + a merged transcript of
    `segments` segments, in the served wire shape."""
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    synth_speech_like_wav(d / f"{sid}_alice_speaker_0000aaaa.wav", seconds=0.5, freq_hz=220.0)
    (d / "session-transcript.json").write_text(
        json.dumps(
            {
                "transcribed_at": "2025-02-01T10:00:00+00:00",
                "segments": [
                    {
                        "speaker": "Alice",
                        "text": f"Segment {i}: the quick brown fox jumps over the lazy dog.",
                        "abs_start": f"2025-02-01T09:{i // 60:02d}:{i % 60:02d}+00:00",
                    }
                    for i in range(segments)
                ],
                "speakers": ["Alice"],
                "speaking_seconds": {"Alice": 480.0},
                "suppressed": [],
                "suppressed_count": 0,
                "wav_count": 1,
                "transcribe_ms": 1000,
                "model": "tiny.en",
                "backend": "fake",
                "device": "cpu",
            }
        ),
        encoding="utf-8",
    )


_MERGED_FIRST_LINE = '#viewRoot [data-slot="mergedHost"] [data-slot="lines"] > div'


async def test_live_captions_scoped_to_focused_session(running_recorder: RunningRecorder):
    """The 'Live captions' panel is the GLOBAL LiveTranscripts deque, but each
    line carries the session it was snapshotted to at /tap open (the
    detached-session isolation in CONTEXT.md). The dashboard must scope the
    panel to the FOCUSED session: an archived session never shows the live
    session's captions, an old session with no lines of its own shows a
    session-aware empty state, and Clear (which wipes the whole deque) is
    disabled off the current session. Pins the 'old session shows live text'
    bug — pre-fix every session's Capture rendered the whole deque."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    current = rec.session_start

    old_a = "2025-01-02T09-00-00Z"
    old_empty = "2025-01-01T09-00-00Z"
    _seed_merged_session(rec, old_a, segments=2)
    _seed_merged_session(rec, old_empty, segments=2)

    # Seed the global deque: one line for the LIVE (current) session, one for an
    # archived session. Pre-fix, BOTH rendered in every session's Capture view.
    rec.transcripts.append(
        {
            "ts": "2026-01-01T00:00:01+00:00",
            "identity": "live",
            "name": "Live",
            "text": "LIVECAPTION charlie",
            "session": current,
        }
    )
    rec.transcripts.append(
        {
            "ts": "2026-01-01T00:00:02+00:00",
            "identity": "olda",
            "name": "OldA",
            "text": "ARCHIVED alpha",
            "session": old_a,
        }
    )

    feed_txts = (
        "() => Array.from(document.querySelectorAll("
        "'#viewRoot [data-slot=\"liveFeedShell\"] .feed-body .line .txt'"
        ")).map((n) => n.textContent)"
    )
    count_sel = '#viewRoot [data-slot="liveFeedCount"]'

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")

            # Boot: the spine picker lists all three sessions.
            await page.wait_for_function(
                """(ids) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    if (!s) return false;
                    const have = new Set(Array.from(s.options).map((o) => o.value));
                    return ids.every((id) => have.has(id));
                }""",
                arg=[current, old_a, old_empty],
                timeout=10000,
            )

            async def focus_capture(sid: str) -> None:
                # Pick the session in the spine, then open its Capture stage
                # (an archived pick routes to Transcript; gotoView opens Capture).
                await page.evaluate(
                    """(sid) => {
                        const s = document.querySelector('[data-slot="sessionPick"]');
                        s.value = sid;
                        s.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    sid,
                )
                await page.evaluate("() => window.gotoView('capture')")

            async def clear_disabled() -> bool:
                return await page.evaluate(
                    """() => {
                        const b = document.querySelector('#viewRoot [data-slot="liveClear"]');
                        return !!b && b.disabled === true;
                    }"""
                )

            # --- LIVE (current) session: shows its own caption, not the other's,
            # and Clear is enabled (it owns the live deque). ---
            await focus_capture(current)
            await page.wait_for_function(
                f"{feed_txts}.some((t) => t.includes('LIVECAPTION charlie'))",
                timeout=8000,
            )
            texts = await page.evaluate(feed_txts)
            assert not any("ARCHIVED alpha" in t for t in texts), texts
            assert not await clear_disabled()

            # --- ARCHIVED session WITH its own line: shows ONLY it, never the
            # live session's caption (the reported bug), Clear disabled. ---
            await focus_capture(old_a)
            await page.wait_for_function(
                f"{feed_txts}.some((t) => t.includes('ARCHIVED alpha'))",
                timeout=8000,
            )
            texts = await page.evaluate(feed_txts)
            assert not any("LIVECAPTION charlie" in t for t in texts), texts
            await page.wait_for_function(
                f"""() => document.querySelector('{count_sel}')?.textContent === '1'""",
                timeout=5000,
            )
            assert await clear_disabled()

            # --- ARCHIVED EMPTY session: session-aware empty state, no foreign
            # lines, count 0, Clear disabled. ---
            await focus_capture(old_empty)
            await page.wait_for_function(
                """() => {
                    const shell = document.querySelector('#viewRoot [data-slot="liveFeedShell"]');
                    return !!shell && /isn.t recording/i.test(shell.textContent || "");
                }""",
                timeout=8000,
            )
            texts = await page.evaluate(feed_txts)
            assert texts == [], texts
            await page.wait_for_function(
                f"""() => document.querySelector('{count_sel}')?.textContent === '0'""",
                timeout=5000,
            )
            assert await clear_disabled()

            await context.close()
        finally:
            await browser.close()


async def test_dashboard_live_channel_start_stop(
    running_recorder: RunningRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """The operator starts and stops the live channel from the dashboard
    (#101's explicit `(e2e)` acceptance criterion; PRD #99 story 20).

    Nothing else in the suite drives this control: every fixture pre-marks the
    channel running and `AUTO_START_LIVE` is off. Here the channel begins
    STOPPED, the operator clicks **start** in the live panel, the panel flips
    to running and a tap's settled caption begins to flow, then the operator
    clicks **stop** and the channel goes down while `/tap` recording keeps
    working (graceful degradation, ADR-0002).

    The OS subprocess spawn is the ONE faked seam — `whisperlivekit-server`
    isn't installed in CI, and `build_live_cmd` + the relay have their own
    tests. Faking only `live.start` / `live.stop` (and leaving the channel's
    port aimed at the fake WlK) keeps the route, the `begin_transition` state
    machine, `/api/state`, and the panel's render all real — which is exactly
    what this criterion is about.
    """
    rr = running_recorder
    rec = rr.recorder
    fake_wlk = rr.fake_wlk

    # The fixture pre-marks the channel alive; begin from a clean STOPPED state.
    rec.live._proc = None
    rec.live.info["state"] = "stopped"

    def _fake_start(*, model=None, language=None):  # noqa: ARG001
        # Stand in for a fully-started child: alive proc (`FakeAliveProc` —
        # poll() is None) + running state, config.port still aimed at the fake
        # WlK so the relay connects.
        rec.live._proc = FakeAliveProc()
        rec.live.info["state"] = "running"
        rec.live.info["pid"] = "fake"
        return True, "started (faked spawn)"

    def _fake_stop(*, timeout=5.0):  # noqa: ARG001
        rec.live._proc = None
        rec.live.info["state"] = "stopped"
        rec.live.info["pid"] = ""
        return True, "stopped (faked)"

    monkeypatch.setattr(rec.live, "start", _fake_start)
    monkeypatch.setattr(rec.live, "stop", _fake_stop)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            async def _wait_live(state: str, btn: str) -> None:
                """Block until the live-panel badge reads `state` AND its
                state-specific action button (`btn`) is present."""
                await page.wait_for_function(
                    """([s, b]) =>
                         document.querySelector('[data-slot="liveStateBadge"]')?.textContent?.trim() === s
                         && !!document.querySelector(b)""",
                    arg=[state, btn],
                    timeout=10000,
                )

            await page.goto(rr.base_url + "/#capture", wait_until="domcontentloaded")

            # Stopped: the live panel offers Start.
            await _wait_live("stopped", "#liveStartBtn")

            # Click Start → POST /api/live/start → the channel reports running.
            await page.click("#liveStartBtn")
            await _wait_live("running", "#liveStopBtn")
            assert rec.live.running(), "channel must be running after dashboard Start"

            # Captions flow now that live is up: a tap opens a relay to the
            # fake WlK whose settled line lands in the live feed.
            stream_task = asyncio.create_task(
                stream_wav_via_tap(
                    ws_base_url=rr.ws_base_url,
                    identity="alice",
                    name="Alice",
                    wav_path=synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0),
                    utterance_id="utt-live-on",
                    frame_interval_s=0.025,
                )
            )
            async with httpx.AsyncClient(base_url=rr.base_url, timeout=10.0) as client:

                async def _alice_active() -> bool:
                    rows = (await client.get("/api/state")).json().get("active", [])
                    return any(r["identity"] == "alice" for r in rows)

                assert await wait_until(_alice_active, timeout=5.0), "tap never went active"

                # The tap going "active" only means its /tap WS is open on the recorder;
                # the relay's connection to the fake WlK is a separate async step that lags
                # it. push_committed broadcasts only to relays connected RIGHT NOW (it isn't
                # buffered/replayed — conftest FakeWlkThread.push_committed), so pushing
                # before the relay connects silently drops the line. Wait for the connection
                # first, or the caption never reaches the feed (the flake).
                assert await wait_until(lambda: len(fake_wlk.connections) >= 1, timeout=5.0), (
                    "the live relay never connected to the fake WlK"
                )
                fake_wlk.push_committed("live caption after start")
                await stream_task
                assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

                # The settled line reaches live_feed asynchronously (fake WlK -> relay ->
                # tail-flush on tap close), on a different path from the tap drain that
                # streams_drained tracks — so poll for it rather than reading the feed once.
                # Keep the last feed the poll saw so a timeout reports exactly that, with no
                # second /api/state fetch that might show a different snapshot.
                seen_feed: list = []

                async def _caption_in_feed() -> bool:
                    seen_feed[:] = (await client.get("/api/state")).json()["live_feed"]
                    return any(
                        e["identity"] == "alice" and e["text"] == "live caption after start"
                        for e in seen_feed
                    )

                assert await wait_until(_caption_in_feed, timeout=5.0), seen_feed

            # Click Stop → the channel goes down.
            await page.click("#liveStopBtn")
            await _wait_live("stopped", "#liveStartBtn")
            assert not rec.live.running(), "channel must be down after dashboard Stop"

            # Recording still works with the live channel down (ADR-0002).
            before = len(list(rec.session_dir.glob("*.wav")))
            await stream_wav_via_tap(
                ws_base_url=rr.ws_base_url,
                identity="bob",
                name="Bob",
                wav_path=synth_speech_like_wav(tmp_path / "bob.wav", seconds=0.5, freq_hz=330.0),
                utterance_id="utt-live-off",
            )
            assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
            assert await wait_until(
                lambda: len(list(rec.session_dir.glob("*.wav"))) == before + 1, timeout=5.0
            ), "recording must continue with the live channel stopped"
        finally:
            await browser.close()


async def test_next_job_ticks_do_not_rebuild_merged_transcript(running_recorder: RunningRecorder):
    """Job progress ticks (~1/s during a transcribe/strip) must update the job
    bar IN PLACE, not invalidate the merged transcript's render signature —
    each invalidation re-renders every segment row synchronously, which is the
    main-thread stall operators reported as the tab "locking up"."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-01T09-00-00Z"
    _seed_merged_session(rec, sid, segments=120)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#transcript", wait_until="domcontentloaded")

            # Pin the seeded session — the recorder's own current session also
            # lists, and the spine focuses it by default.
            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await page.evaluate(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    s.value = sid;
                    s.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                sid,
            )

            # The merged body arrives via the lazy per-(session, stamp) fetch.
            await page.wait_for_function(
                f"""() => document.querySelectorAll('{_MERGED_FIRST_LINE}').length >= 120""",
                timeout=15000,
            )
            await page.evaluate(f"""() => {{
                document.querySelector('{_MERGED_FIRST_LINE}').__guardMark = 1;
            }}""")

            # Five job ticks, each crossing at least one 500ms poll. Direct
            # dict write — the same field /api/state reads via jobs.snapshot();
            # going through the tracker's asyncio.Lock from this loop would
            # race the server loop's.
            for n in range(1, 6):
                rec.jobs._by_session[sid] = JobState(
                    session=sid,
                    kind="transcribe",
                    current=n,
                    total=9,
                    started_at=datetime.now(UTC),
                    current_file=f"f{n}.wav",
                    model="tiny.en",
                )
                await page.wait_for_timeout(600)

            # Non-vacuous: the job bar must have rendered the final tick…
            await page.wait_for_function(
                """() => document.querySelector('#viewRoot [data-slot="jobCount"]')?.textContent === '5 / 9'""",
                timeout=5000,
            )
            # …while the merged transcript's first line is still the SAME node.
            survived = await page.evaluate(f"""() => {{
                const el = document.querySelector('{_MERGED_FIRST_LINE}');
                return !!(el && el.__guardMark === 1);
            }}""")
            assert survived, (
                "merged transcript DOM was rebuilt on a job progress tick — that's an "
                "O(segments) synchronous stall per tick; keep it gated on its own "
                "signature (transcript.js lastTxSig), separate from job/WAV churn"
            )
            await context.close()
        finally:
            rec.jobs._by_session.pop(sid, None)
            await browser.close()


async def test_next_merged_transcript_rows_are_content_visibility_gated(
    running_recorder: RunningRecorder,
):
    """Each merged-transcript row must carry `content-visibility: auto` (with a
    `contain-intrinsic-size` placeholder) so the browser skips layout+paint of
    off-screen lines — the pure-CSS half of the huge-list virtualization already
    proven on `.wavrow`. Without it, the one-shot O(segments) rebuild in
    merged-transcript.js pays layout+paint for every off-screen row, which is
    the bulk of the documented 100-200 ms long task at 3000 segments (#212).

    Structural, no timing threshold (CI-runner safe): seed a merged session,
    open its Transcript view, and assert the computed style on a real rendered
    row. This also pins the SELECTOR — the merged rows are `.transcript > div`
    (no `.line` class); putting the rule on `.line` (the live feed) would leave
    these rows `content-visibility: visible` and fail here."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-01T09-00-00Z"
    _seed_merged_session(rec, sid, segments=120)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#transcript", wait_until="domcontentloaded")

            # Focus the seeded session (the recorder's own current session also
            # lists and is focused by default).
            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await page.evaluate(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    s.value = sid;
                    s.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                sid,
            )

            # The merged body arrives via the lazy per-(session, stamp) fetch.
            await page.wait_for_function(
                f"""() => document.querySelectorAll('{_MERGED_FIRST_LINE}').length >= 120""",
                timeout=15000,
            )

            styles = await page.evaluate(
                f"""() => {{
                    const el = document.querySelector('{_MERGED_FIRST_LINE}');
                    const cs = getComputedStyle(el);
                    return {{
                        contentVisibility: cs.contentVisibility,
                        containIntrinsicSize: cs.containIntrinsicSize,
                    }};
                }}"""
            )
            assert styles["contentVisibility"] == "auto", (
                "merged transcript rows must compute content-visibility:auto so "
                "off-screen rows skip layout+paint (the .wavrow pattern); got "
                f"{styles['contentVisibility']!r} — is the rule on `.transcript > div` "
                "or did it land on `.line` (the live feed) by mistake? (#212)"
            )
            # The `auto` keyword (remember-real-height) is the load-bearing part;
            # don't pin the exact px estimate so a future tune doesn't break this.
            assert "auto" in styles["containIntrinsicSize"], (
                "merged transcript rows need a contain-intrinsic-size:auto placeholder so "
                f"skipped rows keep the scroll height; got {styles['containIntrinsicSize']!r}"
            )
            await context.close()
        finally:
            await browser.close()


def _seed_multi_wav_session(rec, sid: str, *, n: int) -> tuple[Path, list[str]]:
    """A non-current on-disk session with `n` WAVs and NO transcripts yet — the
    multi-track page the blink report is about."""
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    names: list[str] = []
    for i in range(n):
        name = f"{sid}_spk{i}_id{i}_0000aa{i:02d}.wav"
        synth_speech_like_wav(d / name, seconds=0.4, freq_hz=200.0 + 20 * i)
        names.append(name)
    return d, names


def _land_wav_transcript(session_dir: Path, wav_name: str) -> None:
    """Write a primary per-WAV transcript sidecar for one WAV — exactly what a
    batch transcribe does as it finishes each track: it flips that WAV's
    transcribed_at, and so the session's files_sig on the next /api/state poll."""
    tdir = (session_dir / wav_name).with_suffix(".transcripts")
    tdir.mkdir(parents=True, exist_ok=True)
    # The lone sidecar resolves as the primary via wav_cache's newest-mtime
    # fallback, so no `_primary` pointer is needed to make read_primary_marker
    # surface transcribed_at (and so flip files_sig).
    (tdir / "fake__tiny.en.json").write_text(
        json.dumps(
            {
                "transcribed_at": "2025-02-01T10:00:00+00:00",
                "backend": "fake",
                "model": "tiny.en",
                "source": "original",
                "transcribe_ms": 1000,
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello world"}],
            }
        ),
        encoding="utf-8",
    )


async def test_next_files_sig_flip_does_not_blank_wav_list(running_recorder: RunningRecorder):
    """Regression: on a multi-WAV session, one track finishing transcription
    flips the session's files_sig. The lazy files listing then refetches under
    the new sig — but the Recordings view must keep showing the LAST-GOOD listing
    while that refetch is in flight, not blank the whole WAV list to a "loading…"
    placeholder. Pre-fix, loadSessionFiles returned null on every sig change, so
    each per-WAV completion during a batch transcribe wiped + rebuilt every row
    (and the header/waveform) — the "multi-track pages keep blinking while
    transcribing" report."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-01T09-00-00Z"
    session_dir, names = _seed_multi_wav_session(rec, sid, n=3)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/", wait_until="domcontentloaded")

            # Focus the seeded session and open its Recordings stage.
            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await _focus_session_view(page, sid, "recordings")

            # All three WAV rows render.
            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot .wavrow[data-wav]').length >= 3""",
                timeout=15000,
            )

            # Stamp the row for an UNRELATED track (names[0]) — it is NOT the one
            # about to be transcribed, so nothing it renders changes.
            stamped = await page.evaluate(
                """(name) => {
                    const row = document.querySelector(`#viewRoot .wavrow[data-wav="${name}"]`);
                    if (!row) return false;
                    row.__guardMark = 1;
                    return true;
                }""",
                names[0],
            )
            assert stamped, "could not find the unrelated WAV row to stamp"

            # A DIFFERENT track finishes transcription → its per-WAV sidecar lands
            # → the session's files_sig flips on the next poll.
            _land_wav_transcript(session_dir, names[2])

            # Wait until the flip is observed: the transcribed row shows its ✓ tx
            # marker (proves a full render cycle under the new files_sig ran).
            await page.wait_for_function(
                """(name) => {
                    const tag = document.querySelector(`#viewRoot .wavrow[data-wav="${name}"] [data-slot="txTag"]`);
                    return !!tag && tag.textContent.includes('✓');
                }""",
                arg=names[2],
                timeout=15000,
            )

            # The unrelated row must be the SAME node — never wiped to a loading
            # placeholder and rebuilt.
            survived = await page.evaluate(
                """(name) => {
                    const row = document.querySelector(`#viewRoot .wavrow[data-wav="${name}"]`);
                    return !!(row && row.__guardMark === 1);
                }""",
                names[0],
            )
            assert survived, (
                "the whole WAV list was blanked to a loading placeholder and "
                "rebuilt when one track's files_sig flipped — multi-track pages "
                "blink on every per-WAV completion during a batch transcribe. Hold "
                "the last-good listing while the refetch is in flight."
            )
            await context.close()
        finally:
            await browser.close()


async def test_next_files_sig_flip_does_not_blank_transcript_picker(running_recorder: RunningRecorder):
    """The Transcript stage's per-WAV picker shares the Recordings list's lazy
    files listing, so it has the same blink: a track finishing transcription
    flips files_sig and, pre-fix, blanked the whole picker to a "loading…"
    placeholder before rebuilding. Same stale-while-revalidate hold; pins the
    second multi-track surface."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-02T09-00-00Z"
    session_dir, names = _seed_multi_wav_session(rec, sid, n=3)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/", wait_until="domcontentloaded")

            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await _focus_session_view(page, sid, "transcript")

            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot .wavrow[data-wav]').length >= 3""",
                timeout=15000,
            )
            stamped = await page.evaluate(
                """(name) => {
                    const row = document.querySelector(`#viewRoot .wavrow[data-wav="${name}"]`);
                    if (!row) return false;
                    row.__guardMark = 1;
                    return true;
                }""",
                names[0],
            )
            assert stamped, "could not find the unrelated picker row to stamp"

            _land_wav_transcript(session_dir, names[2])

            await page.wait_for_function(
                """(name) => {
                    const tag = document.querySelector(`#viewRoot .wavrow[data-wav="${name}"] [data-slot="txTag"]`);
                    return !!tag && tag.textContent.includes('✓');
                }""",
                arg=names[2],
                timeout=15000,
            )
            survived = await page.evaluate(
                """(name) => {
                    const row = document.querySelector(`#viewRoot .wavrow[data-wav="${name}"]`);
                    return !!(row && row.__guardMark === 1);
                }""",
                names[0],
            )
            assert survived, (
                "the Transcript picker was blanked + rebuilt when one track's "
                "files_sig flipped — same blink as Recordings; hold the last-good "
                "listing while the refetch is in flight."
            )
            await context.close()
        finally:
            await browser.close()


# Watch the rows of a keyed list for ANY DOM churn. Pairs with
# _COUNT_STATE_304S_JS (installed alongside it), so a flat mutation count across a
# window of CONFIRMED 304s means the seam is genuinely skipping quiet ticks rather
# than getting lucky with an unchanged-looking DOM.
#
# `installRowWatch(rowSelector)` stamps every current row and observes the rows
# host for attribute/child mutations, recording which rows were touched by
# data-wav. This replaced a querySelectorAll counter that hooked the two
# hand-rolled selection walkers by their exact selector strings — renderList owns
# the selection repaint now, gated per row, so the question the guard asks is no
# longer "did the walker run" but the stronger "was any row touched at all".
_WATCH_ROW_MUTATIONS_JS = """
window.__rowMuts = 0;
window.__rowMutTargets = [];
window.installRowWatch = (rowSelector) => {
  const rows = Array.from(document.querySelectorAll(rowSelector));
  if (!rows.length) return 0;
  const host = rows[0].parentElement;
  rows.forEach((r, i) => { r.dataset.stamp = 'S' + i; });
  window.__rowMuts = 0;
  window.__rowMutTargets = [];
  const note = (node) => {
    const row = node && node.nodeType === 1 ? node.closest(rowSelector) : null;
    window.__rowMuts++;
    const name = row && row.dataset ? row.dataset.wav : null;
    if (name && !window.__rowMutTargets.includes(name)) window.__rowMutTargets.push(name);
  };
  new MutationObserver((records) => {
    for (const r of records) {
      // The stamps themselves are ours; never count them as churn.
      if (r.type === 'attributes' && r.attributeName === 'data-stamp') continue;
      note(r.target);
      for (const n of r.addedNodes) note(n);
      for (const n of r.removedNodes) note(n);
    }
  }).observe(host, { subtree: true, childList: true, attributes: true });
  return rows.length;
};
window.stampsIntact = (rowSelector) =>
  Array.from(document.querySelectorAll(rowSelector)).every((r) => !!r.dataset.stamp);
"""


async def test_next_wav_list_rows_are_untouched_on_quiet_ticks(running_recorder: RunningRecorder):
    """Issue #213, re-pinned against the keyed-list seam. The Recordings WAV list
    used to repaint every row's `.is-sel` class + aria-pressed on EVERY poll tick
    (an O(rows) cost paid at 2Hz forever on a quiet tab); the fix was a
    change-detection guard, and `renderList` now owns it as the list `sig` plus a
    per-row `itemSig`.

    Stronger guard than the walk counter it replaced: cross several REAL 304s and
    assert NO row was touched at all (no attribute write, no child churn, every
    identity stamp intact) — then select a DIFFERENT WAV and assert not only that
    the highlight moved, but that ONLY the two rows whose selected-state actually
    flipped were touched. That last part is what the per-row gate buys over the
    old whole-list walk."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-03T09-00-00Z"
    _session_dir, names = _seed_multi_wav_session(rec, sid, n=3)
    rows_sel = "#viewRoot .wavrow[data-wav]"

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.add_init_script(_WATCH_ROW_MUTATIONS_JS)
            await page.goto(base + "/", wait_until="domcontentloaded")

            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await _focus_session_view(page, sid, "recordings")

            await page.wait_for_function(
                f"() => document.querySelectorAll('{rows_sel}').length >= 3",
                timeout=15000,
            )
            # A selection exists (defaults to the first WAV) before the baseline.
            await page.wait_for_function(
                "() => !!document.querySelector('#viewRoot .wavrow.is-sel')",
                timeout=10000,
            )
            watched = await page.evaluate("(sel) => window.installRowWatch(sel)", rows_sel)
            assert watched >= 3, f"expected to watch >= 3 rows, watched {watched}"

            # Confirm real 304s are landing — proves the server has genuinely gone
            # quiet, not merely that nothing looks different in the payload.
            await page.wait_for_function("() => window.__state304s >= 3", timeout=10000)
            polls_baseline = await page.evaluate("() => window.__state304s")
            await page.wait_for_function(
                "(base) => window.__state304s >= base + 3",
                arg=polls_baseline,
                timeout=10000,
            )
            # …and force REAL render passes. Crossing 304s alone proves nothing:
            # main.js's tick() returns before renderAll on an unchanged 304, so an
            # idle window renders zero times and a zero-mutation assertion holds
            # even with the list sig deleted. gotoView re-renders synchronously
            # from the cached state, so each call is a full render whose only
            # correct outcome is "the sig gate skipped and no row was touched".
            renders = await _force_render_passes(page, "recordings")
            muts = await page.evaluate("() => window.__rowMuts")
            assert muts == 0, (
                f"the WAV list mutated its rows {muts} time(s) across idle polls and {renders} "
                "unchanged render passes — renderList's list sig must skip the reconcile "
                "entirely when nothing changed (issue #213)"
            )
            assert await page.evaluate("(sel) => window.stampsIntact(sel)", rows_sel), (
                "an idle 304 tick rebuilt WAV rows — the identity stamps are gone"
            )

            # Selecting a DIFFERENT WAV must still repaint, and touch ONLY the two
            # rows whose selected-state flipped.
            await page.evaluate("() => { window.__rowMuts = 0; window.__rowMutTargets = []; }")
            other = page.locator(f'#viewRoot .wavrow[data-wav="{names[1]}"]')
            await other.locator("[data-wav-select]").click()
            await page.wait_for_function(
                """(name) => {
                    const row = document.querySelector(`#viewRoot .wavrow[data-wav="${name}"]`);
                    return !!(row && row.classList.contains('is-sel'));
                }""",
                arg=names[1],
                timeout=10000,
            )
            assert await page.evaluate("() => window.__rowMuts") > 0, (
                "selecting a different WAV did not repaint the selection highlight"
            )
            touched = set(await page.evaluate("() => window.__rowMutTargets"))
            assert touched <= {names[0], names[1]}, (
                f"selecting a WAV touched rows beyond the two that flipped: {sorted(touched)} — "
                "the per-row itemSig gate should leave every other row alone"
            )
            assert await page.evaluate("(sel) => window.stampsIntact(sel)", rows_sel), (
                "selecting a WAV rebuilt rows instead of updating them in place"
            )
            await context.close()
        finally:
            await browser.close()


async def test_renderlist_holds_focused_row_and_lands_after_blur(running_recorder: RunningRecorder):
    """renderList rule 4 — the coarse per-row hold, and the ADR-0004 trap it
    exists for.

    sessions.js used to own this by hand and got it wrong: it stamped a row's
    signature ABOVE its focus guards, so a write skipped because the rename input
    was focused was stranded FOREVER — the next tick recomputed the identical sig
    and early-returned, the row kept a stale label, and a later keystroke
    persisted the stale value back over an external rename.

    Focus a row's rename input, change that session's label EXTERNALLY, and assert
    the row does not update while focused; then blur and assert it catches up on
    the next tick (the sig was not advanced, and markDeferredRender earned the
    retry even though the poll went quiet)."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-05T09-00-00Z"
    _seed_multi_wav_session(rec, sid, n=1)
    row_sel = f'#viewRoot [data-sid="{sid}"]'

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")

            await page.wait_for_selector(row_sel, timeout=15000)
            rename = page.locator(f'{row_sel} [data-slot="rename"]')
            await rename.focus()
            await page.wait_for_function(
                """(sel) => {
                    const i = document.querySelector(sel + ' [data-slot="rename"]');
                    return !!i && document.activeElement === i;
                }""",
                arg=row_sel,
                timeout=5000,
            )

            # An EXTERNAL rename: written straight to the session's meta sidecar
            # (via the layout constant — hand-typing the filename is how the first
            # cut of this test silently asserted nothing), so it arrives through the
            # poll rather than through this row's own saver.
            (rec.recordings_dir / sid / FILENAME_META_JSON).write_text(
                json.dumps({"label": "renamed elsewhere"}), encoding="utf-8"
            )

            # CONTROL: wait until the SERVER is actually serving the new label, so
            # the hold assertion below cannot pass vacuously because nothing changed.
            await page.wait_for_function(
                """async (sid) => {
                    const r = await fetch('/api/state', { cache: 'no-store' });
                    if (!r.ok) return false;
                    const j = await r.json();
                    const s = (j.sessions || []).find((x) => x.session === sid);
                    return !!s && (s.session_meta || {}).label === 'renamed elsewhere';
                }""",
                arg=sid,
                timeout=10000,
            )

            # Several polls must now cross with the row NOT updated — the label cell
            # is written by fillRow, which the seam holds for a focused row.
            await page.wait_for_timeout(2500)
            label_held = await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel + ' [data-slot="label"]');
                    return el ? el.textContent : null;
                }""",
                row_sel,
            )
            assert label_held is not None and "renamed elsewhere" not in label_held, (
                f"the row updated while its rename input was focused (label={label_held!r}) — "
                "renderList must hold a row whose control holds focus"
            )
            assert await page.evaluate(
                """(sel) => {
                    const i = document.querySelector(sel + ' [data-slot="rename"]');
                    return !!i && document.activeElement === i;
                }""",
                row_sel,
            ), "the focused rename input was torn out from under the operator"

            # Blur → the held write must land, without needing a server change to
            # wake the poll (the sig was never advanced).
            await page.evaluate("() => document.activeElement.blur()")
            await page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel + ' [data-slot="label"]');
                    return !!el && el.textContent.includes('renamed elsewhere');
                }""",
                arg=row_sel,
                timeout=10000,
            )
            await context.close()
        finally:
            await browser.close()


async def test_renderlist_keeps_focused_row_that_left_the_list(running_recorder: RunningRecorder):
    """renderList rule 3 — the removal hold. Canon reconcileList removes every key
    absent from `items`, which would take a focused control with it, and
    CONTEXT.md defines the interaction hold as never destroying interaction state.

    Focus a row's rename input, then make that session vanish from the listing
    (its directory is removed, so the next gather_sessions drops it) and assert the
    row — and the focus inside it — survives. Blur, and it goes."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    keep, doomed = "2025-02-06T09-00-00Z", "2025-02-06T10-00-00Z"
    _seed_multi_wav_session(rec, keep, n=1)
    _seed_multi_wav_session(rec, doomed, n=1)
    row_sel = f'#viewRoot [data-sid="{doomed}"]'

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")

            await page.wait_for_selector(row_sel, timeout=15000)
            await page.locator(f'{row_sel} [data-slot="rename"]').focus()
            await page.wait_for_function(
                """(sel) => {
                    const i = document.querySelector(sel + ' [data-slot="rename"]');
                    return !!i && document.activeElement === i;
                }""",
                arg=row_sel,
                timeout=5000,
            )

            shutil.rmtree(rec.recordings_dir / doomed)
            # Wait until the SERVER has genuinely dropped it, so the held row is
            # demonstrably a client-side hold and not a slow poll.
            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && !Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=doomed,
                timeout=10000,
            )

            assert await page.evaluate("(sel) => !!document.querySelector(sel)", row_sel), (
                "the row was removed while its rename input held focus — renderList must "
                "defer the whole render when a focused row's key leaves the list"
            )
            assert await page.evaluate(
                """(sel) => {
                    const i = document.querySelector(sel + ' [data-slot="rename"]');
                    return !!i && document.activeElement === i;
                }""",
                row_sel,
            ), "focus was lost even though the row survived"

            await page.evaluate("() => document.activeElement.blur()")
            await page.wait_for_function("(sel) => !document.querySelector(sel)", arg=row_sel, timeout=10000)
            await context.close()
        finally:
            await browser.close()


async def test_renderlist_selection_hold_defers_reconcile_without_advancing_sig(
    running_recorder: RunningRecorder,
):
    """renderList rule 2 — a text selection inside the host defers the reconcile
    WITHOUT advancing the list sig, so the held render lands once the selection
    clears rather than being lost.

    Select text inside the Recordings WAV list, then add a WAV on disk (which
    flips files_sig, so the list genuinely wants to reconcile). The selection must
    survive and the new row must NOT appear; collapsing the selection must let the
    held reconcile land — on a tick, via markDeferredRender, since by then the
    poll is 304ing again."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-07T09-00-00Z"
    session_dir, names = _seed_multi_wav_session(rec, sid, n=2)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/", wait_until="domcontentloaded")
            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await _focus_session_view(page, sid, "recordings")
            await page.wait_for_function(
                "() => document.querySelectorAll('#viewRoot .wavrow[data-wav]').length >= 2",
                timeout=15000,
            )

            selected = await page.evaluate("""() => {
                const el = document.querySelector('#viewRoot .wavrow [data-slot="name"]');
                const r = document.createRange();
                r.selectNodeContents(el);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(r);
                return sel.toString();
            }""")
            assert selected, "failed to select text inside a WAV row"

            # A third WAV on disk → a new files_sig → the list wants to reconcile.
            extra = f"{sid}_spk9_id9_0000aa99.wav"
            synth_speech_like_wav(session_dir / extra, seconds=0.4, freq_hz=300.0)
            await page.wait_for_timeout(3000)  # crosses several polls

            still = await page.evaluate("() => window.getSelection().toString()")
            assert still == selected, (
                "reconciling the WAV list dissolved the operator's text selection — "
                "renderList must defer while a selection is inside the host"
            )
            assert await page.evaluate(
                "(n) => !document.querySelector(`#viewRoot .wavrow[data-wav='${n}']`)", extra
            ), "the list reconciled despite the selection hold"

            # Collapse it → the held reconcile must land on a following tick.
            await page.evaluate("() => window.getSelection().removeAllRanges()")
            await page.wait_for_function(
                "(n) => !!document.querySelector(`#viewRoot .wavrow[data-wav='${n}']`)",
                arg=extra,
                timeout=10000,
            )
            await context.close()
        finally:
            await browser.close()


async def test_next_transcript_picker_rows_are_untouched_on_quiet_ticks(running_recorder: RunningRecorder):
    """The Transcript stage's per-WAV picker is the seam's second adapter — same
    guard as test_next_wav_list_rows_are_untouched_on_quiet_ticks, pinned on that
    surface (its rows are <button class="wavrow">)."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-04T09-00-00Z"
    _session_dir, names = _seed_multi_wav_session(rec, sid, n=3)
    rows_sel = "#viewRoot button.wavrow[data-wav]"

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.add_init_script(_COUNT_STATE_304S_JS)
            await page.add_init_script(_WATCH_ROW_MUTATIONS_JS)
            await page.goto(base + "/", wait_until="domcontentloaded")

            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await _focus_session_view(page, sid, "transcript")

            await page.wait_for_function(
                f"() => document.querySelectorAll('{rows_sel}').length >= 3",
                timeout=15000,
            )
            await page.wait_for_function(
                "() => !!document.querySelector('#viewRoot button.wavrow.is-sel')",
                timeout=10000,
            )
            watched = await page.evaluate("(sel) => window.installRowWatch(sel)", rows_sel)
            assert watched >= 3, f"expected to watch >= 3 picker rows, watched {watched}"

            await page.wait_for_function("() => window.__state304s >= 3", timeout=10000)
            polls_baseline = await page.evaluate("() => window.__state304s")
            await page.wait_for_function(
                "(base) => window.__state304s >= base + 3",
                arg=polls_baseline,
                timeout=10000,
            )
            # Force REAL render passes — see the WAV-list twin for why crossing
            # 304s alone cannot fail.
            renders = await _force_render_passes(page, "transcript")
            muts = await page.evaluate("() => window.__rowMuts")
            assert muts == 0, (
                f"the picker mutated its rows {muts} time(s) across idle polls and {renders} "
                "unchanged render passes — renderList's list sig must skip the reconcile "
                "entirely when nothing changed (issue #213)"
            )
            assert await page.evaluate("(sel) => window.stampsIntact(sel)", rows_sel), (
                "an idle 304 tick rebuilt picker rows — the identity stamps are gone"
            )

            await page.evaluate("() => { window.__rowMuts = 0; window.__rowMutTargets = []; }")
            await page.locator(f'#viewRoot button.wavrow[data-wav="{names[1]}"]').click()
            await page.wait_for_function(
                """(name) => {
                    const row = document.querySelector(`#viewRoot button.wavrow[data-wav="${name}"]`);
                    return !!(row && row.classList.contains('is-sel'));
                }""",
                arg=names[1],
                timeout=10000,
            )
            assert await page.evaluate("() => window.__rowMuts") > 0, (
                "selecting a different WAV did not repaint the picker's selection highlight"
            )
            touched = set(await page.evaluate("() => window.__rowMutTargets"))
            assert touched <= {names[0], names[1]}, (
                f"selecting a WAV touched picker rows beyond the two that flipped: {sorted(touched)}"
            )
            await context.close()
        finally:
            await browser.close()


def _write_merged_transcript(
    session_dir: Path, *, segments: int, transcribed_at: str, text_prefix: str
) -> None:
    """(Over)write a session's merged transcript with `segments` lines stamped
    `transcribed_at` — a re-transcribe bumps the stamp, which the dashboard
    refetches. `text_prefix` distinguishes one merge's lines from another's."""
    (session_dir / "session-transcript.json").write_text(
        json.dumps(
            {
                "transcribed_at": transcribed_at,
                "segments": [
                    {
                        "speaker": "Alice",
                        "text": f"{text_prefix} {i}: the quick brown fox jumps over the lazy dog.",
                        "abs_start": f"2025-02-01T09:{i // 60:02d}:{i % 60:02d}+00:00",
                    }
                    for i in range(segments)
                ],
                "speakers": ["Alice"],
                "speaking_seconds": {"Alice": 480.0},
                "suppressed": [],
                "suppressed_count": 0,
                "wav_count": 1,
                "transcribe_ms": 1000,
                "model": "tiny.en",
                "backend": "fake",
                "device": "cpu",
            }
        ),
        encoding="utf-8",
    )


async def test_next_retranscribe_does_not_blank_merged_pane(running_recorder: RunningRecorder):
    """Re-transcribing a session bumps the merged transcript's transcribed_at,
    which refetches the merged body. The pane must hold the PREVIOUS merged
    transcript in place during the refetch (stale-while-revalidate), never
    blanking to a "loading transcript…" placeholder — wiping the transcript the
    operator is reading is the same blink as the WAV-list bug, on the same page."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-01T09-00-00Z"
    _seed_merged_session(rec, sid, segments=120)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/", wait_until="domcontentloaded")
            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await _focus_session_view(page, sid, "transcript")

            # The original merged body is on screen.
            await page.wait_for_function(
                f"""() => document.querySelectorAll('{_MERGED_FIRST_LINE}').length >= 120""",
                timeout=15000,
            )

            # Watch the pane for a "loading transcript…" blank across the refetch.
            await page.evaluate(
                """() => {
                    const host = document.querySelector('#viewRoot [data-slot="mergedHost"]');
                    window.__sawLoading = false;
                    const obs = new MutationObserver(() => {
                        if ((host.textContent || '').includes('loading transcript')) window.__sawLoading = true;
                    });
                    obs.observe(host, { childList: true, subtree: true, characterData: true });
                }"""
            )

            # Re-transcribe: a new transcribed_at + a distinct body.
            _write_merged_transcript(
                rec.recordings_dir / sid,
                segments=130,
                transcribed_at="2025-02-01T11:00:00+00:00",
                text_prefix="Bravo",
            )

            # The fresh body lands (a "Bravo" line appears)…
            await page.wait_for_function(
                f"""() => Array.from(document.querySelectorAll('{_MERGED_FIRST_LINE}'))
                    .some((d) => (d.textContent || '').includes('Bravo'))""",
                timeout=15000,
            )
            # …and the pane never blanked to the loading placeholder on the way.
            saw_loading = await page.evaluate("() => window.__sawLoading")
            assert not saw_loading, (
                "the merged transcript pane blanked to 'loading transcript…' during a "
                "re-transcribe refetch — hold the previous body (stale-while-revalidate) "
                "so the pane refreshes in place instead of wiping what's on screen."
            )
            await context.close()
        finally:
            await browser.close()


async def test_meeting_pipeline_job_renders_stage_labelled_bar(running_recorder: RunningRecorder):
    """A bridge-triggered end-of-meeting pipeline surfaces as a NORMAL session
    job (issue #102's dashboard acceptance criterion): the shared job bar must
    label a kind="pipeline" job with its current stage ("Pipeline ·
    Transcribing") and re-label when the chain advances to the next stage."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-01T09-00-00Z"
    _seed_merged_session(rec, sid, segments=5)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#transcript", wait_until="domcontentloaded")
            await page.wait_for_function(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === sid);
                }""",
                arg=sid,
                timeout=10000,
            )
            await page.evaluate(
                """(sid) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    s.value = sid;
                    s.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                sid,
            )

            # Mid-chain: the transcribe stage with per-WAV progress. Direct
            # dict write — same rationale as the job-ticks guard above.
            rec.jobs._by_session[sid] = JobState(
                session=sid,
                kind="pipeline",
                current=2,
                total=9,
                started_at=datetime.now(UTC),
                status="transcribing",
                current_file="f2.wav",
                model="tiny.en",
                stage="transcribe",
            )
            await page.wait_for_function(
                """() => {
                    const label = document.querySelector('#viewRoot [data-slot="jobLabel"]');
                    const count = document.querySelector('#viewRoot [data-slot="jobCount"]');
                    return label?.textContent === 'Pipeline · Transcribing'
                        && count?.textContent === '2 / 9';
                }""",
                timeout=10000,
            )

            # The chain advances — the SAME job re-labels to the next stage.
            rec.jobs._by_session[sid] = JobState(
                session=sid,
                kind="pipeline",
                current=0,
                total=1,
                started_at=datetime.now(UTC),
                status="summarizing",
                model="tiny.en",
                stage="summarize",
            )
            await page.wait_for_function(
                """() => document.querySelector('#viewRoot [data-slot="jobLabel"]')?.textContent
                    === 'Pipeline · Summarizing'""",
                timeout=10000,
            )
            await context.close()
        finally:
            rec.jobs._by_session.pop(sid, None)
            await browser.close()


async def test_next_caption_churn_appends_feed_lines_without_rebuilds(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """While a tap streams and captions settle, (a) the Capture header must
    not be rebuilt per tick (its sig is unchanged) and (b) an arriving caption
    that starts a NEW rendered line must APPEND it, leaving earlier lines
    untouched — rebuilding ≤200 rows per settled line was steady GC + layout
    pressure for whole meetings, and dropped any text selection the operator
    had in the feed.

    Captions end with a period so #80's coalescing splits them into two
    SENTENCES = two lines (same-speaker fragments without a terminator would
    coalesce into one growing line); the first sentence is stable, so it must
    survive as a marked node when the second appends."""
    rr = running_recorder
    # 30 s of audio at real-time pacing keeps the relay OPEN for the whole
    # test (normal duration 2-3 s) — if the WAV ran out mid-test the relay's
    # close-drain would stop feed updates and the waits below would flake.
    wav = synth_speech_like_wav(tmp_path / "caption-guard.wav", seconds=30.0, freq_hz=220.0)
    stream = asyncio.create_task(
        stream_wav_via_tap(
            ws_base_url=rr.ws_base_url,
            identity="guard1",
            name="Guard",
            wav_path=wav,
            frame_interval_s=0.02,
        )
    )
    try:
        # The relay must be connected before a caption can broadcast.
        assert await wait_until(lambda: len(rr.fake_wlk.connections) >= 1, timeout=10.0)

        async with playwright_session() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(viewport={"width": 1400, "height": 900})
                page = await context.new_page()
                await page.goto(rr.base_url + "/#capture", wait_until="domcontentloaded")

                feed_line = '#viewRoot [data-slot="liveFeedShell"] .feed-body .line'

                # The relay settles the TAIL line only after it holds across
                # _TAIL_STABLE_SNAPSHOTS consecutive empty-buffer snapshots
                # (live_relay.py), so each committed push is followed by a few
                # empty-buffer snapshots to confirm it — mirroring WlK's
                # rolling re-broadcasts.
                async def push_settled(text: str) -> None:
                    await asyncio.to_thread(rr.fake_wlk.push_committed, text)
                    for _ in range(4):
                        await asyncio.to_thread(rr.fake_wlk.push_buffer, "")

                await push_settled("First settled caption line.")
                await page.wait_for_function(
                    f"""() => document.querySelectorAll('{feed_line}').length >= 1""",
                    timeout=10000,
                )
                # The header-survival half of this guard assumes Capture's
                # header sub stays TICK-INVARIANT (recorder armed/paused +
                # session label, both constant here). If the sub ever grows a
                # per-tick value (live counts, lag, elapsed), header() will
                # legitimately rebuild and this guard must move its mark.
                await page.evaluate(f"""() => {{
                    document.querySelector('{feed_line}').__guardMark = 1;
                    document.querySelector('#viewRoot [data-slot="head"]').firstElementChild.__guardMark = 1;
                }}""")

                await push_settled("Second settled caption line.")
                await page.wait_for_function(
                    f"""() => document.querySelectorAll('{feed_line}').length >= 2""",
                    timeout=10000,
                )

                line_kept, head_kept = await page.evaluate(f"""() => {{
                    const line = document.querySelector('{feed_line}');
                    const head = document.querySelector('#viewRoot [data-slot="head"]').firstElementChild;
                    return [!!(line && line.__guardMark === 1), !!(head && head.__guardMark === 1)];
                }}""")
                assert line_kept, (
                    "live feed rebuilt existing lines when a caption arrived — new lines "
                    "must APPEND (see live-feed.js incremental render)"
                )
                assert head_kept, (
                    "Capture header was rebuilt on a poll tick with an unchanged sig — "
                    "header() must skip when eyebrow/title/sub are unchanged (shell.js)"
                )
                await context.close()
            finally:
                await browser.close()
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)
        # partial (not a lambda): a lambda's implicit return inside a finally
        # trips CodeQL's py/exit-from-finally; partial has no return node.
        await wait_until(partial(streams_drained, rr.recorder), timeout=10.0)


async def test_live_log_dialog_refresh_preserves_text_selection(
    running_recorder: RunningRecorder,
):
    """The 'view logs' dialog refetches /api/live/log once a second while
    open and rewrites its <pre>. Rewriting while the operator is
    select-copying log lines dissolved the selection on every tick
    (operator report: "it keeps flashing after I mark text") — the same
    interaction-clobbering class as the dropdown sweep, for selections.
    `renderLogInto` now holds the rewrite while a selection starts/ends
    inside the dialog (`selectionInside` in templates.js — the same guard
    renderRegion applies to every per-tick region).

    Guard: seed live log lines, open the dialog from Capture, select a span
    inside the <pre>, land a new log line server-side, cross >2 dialog
    refreshes — the selection must survive verbatim. Then clearing the
    selection must let the held-back refresh land (the new line appears),
    proving the guard defers updates rather than killing them.
    """
    rr = running_recorder
    for i in range(5):
        rr.recorder.live.log.append(f"INFO:whisperlivekit:seed line {i}")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#capture", wait_until="domcontentloaded")

            # The "view logs" button renders once live_log is non-empty.
            await page.wait_for_selector("#liveLogBtn", timeout=10000)
            await page.click("#liveLogBtn")
            await page.wait_for_function(
                """() => {
                    const pre = document.querySelector('#liveLogDialog [data-slot="pre"]');
                    return !!pre && pre.textContent.includes('seed line 4');
                }""",
                timeout=10000,
            )

            selected = await page.evaluate("""() => {
                const pre = document.querySelector('#liveLogDialog [data-slot="pre"]');
                const r = document.createRange();
                r.setStart(pre.firstChild, 0);
                r.setEnd(pre.firstChild, 25);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(r);
                return sel.toString();
            }""")
            assert len(selected) == 25

            # A fresh server-side line means the next 1s refresh would REWRITE
            # the <pre> — exactly what must not happen while text is selected.
            rr.recorder.live.log.append("INFO:whisperlivekit:line landed while selecting")
            await page.wait_for_timeout(2500)  # crosses >2 dialog refresh ticks

            still = await page.evaluate("() => window.getSelection().toString()")
            assert still == selected, (
                "log dialog refresh dissolved the operator's text selection — "
                "renderLogInto must skip while a selection is inside the dialog"
            )

            # Collapse the selection → the deferred refresh must now land.
            await page.evaluate("() => window.getSelection().removeAllRanges()")
            await page.wait_for_function(
                """() => {
                    const pre = document.querySelector('#liveLogDialog [data-slot="pre"]');
                    return !!pre && pre.textContent.includes('line landed while selecting');
                }""",
                timeout=10000,
            )
            await context.close()
        finally:
            await browser.close()


async def test_recordings_strip_controls_stay_visible_with_many_wavs(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The strip-silence knobs + button live at the BOTTOM of the .wavehero
    panel. The hero is a flex item in the scrollable work column, and `.panel`'s
    `overflow: hidden` makes its flex auto-min-height resolve to 0 — so a tall
    WAV list below would shrink the hero and clip the knobbar off the bottom
    (operator report: "strip settings vanish on sessions with many WAVs").
    `.wavehero { flex: none }` pins it. Guard: with many WAVs, the strip button
    must sit WITHIN the hero's painted box (a shrunk/clipped hero pushes the
    button's bottom below the hero's bottom).
    """
    rr = running_recorder
    n_wavs = 16

    async def _one(i: int):
        wav = synth_speech_like_wav(tmp_path / f"many{i}.wav", seconds=0.25, freq_hz=180.0 + i * 12.0)
        await stream_wav_via_tap(
            ws_base_url=rr.ws_base_url,
            identity=f"many{i}",
            name=f"Speaker {i}",
            wav_path=wav,
            utterance_id=f"many-utt{i}",
        )

    await asyncio.gather(*(_one(i) for i in range(n_wavs)))
    assert await wait_until(lambda: streams_drained(rr.recorder), timeout=15.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/", wait_until="domcontentloaded")
            await page.wait_for_selector("#spine .navitem", timeout=6000)
            await page.evaluate("window.gotoView && window.gotoView('recordings')")
            await page.wait_for_selector(".wavlist .wavrow", timeout=6000)
            assert await page.locator(".wavlist .wavrow").count() >= n_wavs

            hero = await page.locator(".wavehero").bounding_box()
            btn = await page.locator("[data-slot=stripBtn]").bounding_box()
            knobs = await page.locator("[data-strip-knob]").count()
            assert hero and btn, "wavehero + strip button must render"
            hero_bottom = hero["y"] + hero["height"]
            btn_bottom = btn["y"] + btn["height"]
            assert btn_bottom <= hero_bottom + 4, (
                f"strip button (bottom {btn_bottom:.0f}) is clipped below the wavehero "
                f"(bottom {hero_bottom:.0f}) — the hero shrank + clipped its knobbar. "
                f"hero={hero} btn={btn}"
            )
            assert knobs == 3, f"all 3 strip knobs must render, got {knobs}"
        finally:
            await browser.close()


async def test_recordings_list_virtualized_rows_survive_select_and_poll(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The WAV list is virtualized the native way: each .wavrow carries
    `content-visibility` (CSS skips off-screen layout/paint), and the list is
    keyed-reconciled — NOT replaceChildren'd — so a selection or an idle poll
    tick never rebuilds the rows. Structural guards (no timing): (1) rows carry a
    non-`visible` computed content-visibility; (2) stamping a row's DOM node and
    then selecting a DIFFERENT row + crossing a poll leaves the stamped node
    intact (it was reused, not rebuilt). This is what keeps a thousands-row
    session snappy — a rebuild-per-tick was the jank.
    """
    rr = running_recorder
    n_wavs = 6

    async def _one(i: int):
        wav = synth_speech_like_wav(tmp_path / f"virt{i}.wav", seconds=0.25, freq_hz=180.0 + i * 12.0)
        await stream_wav_via_tap(
            ws_base_url=rr.ws_base_url,
            identity=f"virt{i}",
            name=f"Speaker {i}",
            wav_path=wav,
            utterance_id=f"virt-utt{i}",
        )

    await asyncio.gather(*(_one(i) for i in range(n_wavs)))
    assert await wait_until(lambda: streams_drained(rr.recorder), timeout=15.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#recordings", wait_until="domcontentloaded")
            await page.wait_for_function(
                f"() => document.querySelectorAll('#viewRoot .wavlist .wavrow').length >= {n_wavs}",
                timeout=8000,
            )

            # (1) Rows opt into content-visibility virtualization.
            cv = await page.evaluate(
                """() => {
                  const row = document.querySelector('#viewRoot .wavlist .wavrow');
                  return row && getComputedStyle(row).contentVisibility;
                }"""
            )
            assert cv and cv != "visible", f"rows must carry content-visibility (got {cv!r})"

            # Stamp a NON-selected row's DOM node so we can detect a rebuild.
            sel0 = await page.locator("#viewRoot .wavlist .wavrow.is-sel").get_attribute("data-wav")
            other = page.locator("#viewRoot .wavlist .wavrow:not(.is-sel)").first
            target = await other.get_attribute("data-wav")
            assert target and target != sel0
            await page.evaluate(
                """(want) => {
                  const r = document.querySelector(`#viewRoot .wavlist .wavrow[data-wav="${want}"]`);
                  r.dataset.stamp = 'keep-me';
                }""",
                target,
            )

            # (2) Select that row by its name, then cross a poll tick. A
            # replaceChildren would drop the stamp; a keyed reconcile keeps it.
            await other.locator("[data-wav-select]").click()
            await page.wait_for_function(
                """(want) => {
                  const sel = document.querySelector('#viewRoot .wavlist .wavrow.is-sel');
                  return sel && sel.getAttribute('data-wav') === want;
                }""",
                arg=target,
                timeout=6000,
            )
            await asyncio.sleep(1.2)  # cross at least one 500ms /api/state poll
            stamped = await page.evaluate(
                """(want) => {
                  const r = document.querySelector(`#viewRoot .wavlist .wavrow[data-wav="${want}"]`);
                  return r && r.dataset.stamp;
                }""",
                target,
            )
            assert stamped == "keep-me", "selecting + polling rebuilt the row (lost the stamp)"
        finally:
            await browser.close()


async def test_recordings_waveform_renders_real_canvas_not_mock(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The Recordings hero shows a REAL waveform <canvas> drawn from
    server-computed peaks (GET /api/wav/.../peaks), with a mm:ss time axis —
    and the old "mock · not wired" stub is gone.

    Streams one synthetic WAV, opens Recordings (the first WAV auto-selects),
    and asserts: the canvas renders + paints a non-zero bitmap, the time-axis
    labels populate (which only happens after peaks land + draw), and no
    stub / mock-tag markup survives. The structural sibling of
    test_summary_stage_has_no_mock_not_wired_tags, for the waveform slice.
    """
    rr = running_recorder
    wav = synth_speech_like_wav(tmp_path / "wave.wav", seconds=1.0, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=rr.ws_base_url,
        identity="alice",
        name="Alice",
        wav_path=wav,
        utterance_id="utt-wave",
    )
    assert await wait_until(lambda: streams_drained(rr.recorder), timeout=10.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#recordings", wait_until="domcontentloaded")
            await page.wait_for_selector("#viewRoot .wavlist .wavrow", timeout=8000)

            # A real <canvas> renders where the stub used to be.
            canvas = page.locator("#viewRoot .wave-canvas")
            await canvas.wait_for(state="attached", timeout=8000)

            # The mock stub + its "mock · not wired" tag are gone.
            assert await page.locator("#viewRoot .wavestub").count() == 0
            body_text = (await page.locator("#viewRoot").inner_text()).lower()
            assert "not wired" not in body_text, "the mock·not-wired stub must be gone"

            # Peaks loaded → the component drew the bars AND populated the mm:ss
            # time axis (axis labels render only on a successful peaks draw).
            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot .wave-axis span').length >= 2""",
                timeout=8000,
            )
            # The canvas backing bitmap was sized by the paint pass — proof it
            # actually drew rather than sitting as inert 0×0 markup.
            width = await canvas.evaluate("c => c.width")
            assert width > 0, "the waveform canvas should have a non-zero backing bitmap"
        finally:
            await browser.close()


async def test_recordings_name_selects_waveform_rest_of_row_toggles_expand(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The row is a native <details>: the NAME block selects the WAV for the
    waveform (preventDefaulting the toggle), and clicking the REST of the row
    (e.g. its duration) toggles the inline transcript open instead. Guards both
    halves of that contract: (1) clicking a row's name moves the selection — the
    hero name follows, .is-sel moves, the "🌊 viewing" badge lands on it and
    only it; (2) clicking the duration of a row toggles its <details> WITHOUT
    moving the selection.
    """
    rr = running_recorder
    for i, who in enumerate(("alice", "bob")):
        wav = synth_speech_like_wav(tmp_path / f"{who}.wav", seconds=1.0, freq_hz=200.0 + i * 60)
        await stream_wav_via_tap(
            ws_base_url=rr.ws_base_url,
            identity=who,
            name=who.capitalize(),
            wav_path=wav,
            utterance_id=f"utt-{who}",
        )
    assert await wait_until(lambda: streams_drained(rr.recorder), timeout=12.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#recordings", wait_until="domcontentloaded")
            await page.wait_for_function(
                "() => document.querySelectorAll('#viewRoot .wavlist .wavrow').length >= 2",
                timeout=8000,
            )

            # The first WAV auto-selects; switch to the OTHER (non-selected) row.
            sel0 = await page.locator("#viewRoot .wavlist .wavrow.is-sel").get_attribute("data-wav")
            other = page.locator("#viewRoot .wavlist .wavrow:not(.is-sel)").first
            target = await other.get_attribute("data-wav")
            assert target and target != sel0
            wave_before = await page.locator("#viewRoot [data-slot=waveName]").inner_text()

            # (1) Click the NAME block → selection moves there.
            await other.locator("[data-wav-select]").click()
            await page.wait_for_function(
                """(want) => {
                  const sel = document.querySelector('#viewRoot .wavlist .wavrow.is-sel');
                  return sel && sel.getAttribute('data-wav') === want;
                }""",
                arg=target,
                timeout=6000,
            )
            assert await page.locator("#viewRoot .wavlist .wavrow.is-sel").count() == 1

            # The hero name actually changed (the canvas now reflects the pick).
            wave_after = await page.locator("#viewRoot [data-slot=waveName]").inner_text()
            assert wave_after != wave_before, "the waveform hero name must follow the selection"

            # The "🌊 viewing" badge is visible on exactly the selected row.
            target_badge = page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{target}"] .wavrow__viewing')
            other_badge = page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{sel0}"] .wavrow__viewing')
            assert await target_badge.is_visible(), "selected row must show the 🌊 viewing badge"
            assert not await other_badge.is_visible(), "non-selected row must hide the badge"

            # (2) Clicking the DURATION of the still-unselected first row toggles
            # its <details> open and does NOT steal the selection.
            first_row = page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{sel0}"]')
            await first_row.locator('[data-slot="dur"]').click()
            await page.wait_for_function(
                """(want) => {
                  const r = document.querySelector(`#viewRoot .wavlist .wavrow[data-wav="${want}"]`);
                  return r && r.open;
                }""",
                arg=sel0,
                timeout=4000,
            )
            still_sel = await page.locator("#viewRoot .wavlist .wavrow.is-sel").get_attribute("data-wav")
            assert still_sel == target, "toggling a row's expand must not move the selection"
        finally:
            await browser.close()


async def test_transcribe_page_source_toggle_picks_original_or_stripped(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The Transcribe page must let you transcribe the ORIGINAL WAVs or the
    silence-stripped clips (operator report: no way to pick). With a stripped/
    folder present, the source toggle's "stripped" option enables; switching to
    it lists the stripped region clips in the per-WAV picker and makes the
    session-range transcribe send source=stripped. The transcribe POST is
    intercepted (no real job), so this needs no model/transcriber."""
    import httpx

    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rr = running_recorder
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
    n_clips = len(sorted((rr.recorder.session_dir / "stripped").glob("*.wav")))
    assert n_clips >= 1

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            captured: dict = {}
            # Set INSIDE the route handler, so the assertion below waits on the
            # request actually arriving rather than on a fixed budget. A
            # `wait_for_timeout` here fails with `got {}` on a loaded runner —
            # a timing failure indistinguishable from a real regression.
            transcribe_posted = asyncio.Event()

            async def _route(route):
                captured.update(route.request.post_data_json or {})
                transcribe_posted.set()
                await route.fulfill(status=200, content_type="application/json", body='{"ok": true}')

            await page.route("**/api/transcribe-session", _route)
            await page.goto(rr.base_url + "/#transcript", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="srcSwHost"] [data-src="original"]', timeout=6000)

            stripped_btn = page.locator('[data-slot="srcSwHost"] [data-src="stripped"]')
            assert not await stripped_btn.is_disabled(), "stripped must enable once a stripped/ folder exists"

            await stripped_btn.click()
            # Wait on the DOM conditions themselves, not a fixed budget: the
            # toggle latching on, and the picker having re-listed the clips.
            await page.wait_for_selector('[data-slot="srcSwHost"] [data-src="stripped"].is-on', timeout=6000)
            await page.wait_for_function(
                "n => document.querySelectorAll('[data-slot=wavList] .wavrow').length >= n",
                arg=n_clips,
                timeout=6000,
            )
            assert await page.locator('[data-slot="srcSwHost"] [data-src="stripped"].is-on').count() == 1

            await page.locator('[data-slot="txRangeBtn"]').click()
            await asyncio.wait_for(transcribe_posted.wait(), timeout=6.0)
            assert captured.get("source") == "stripped", (
                f"the session-range transcribe must send source=stripped, got {captured!r}"
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_transcribe_cache_panel_unions_original_and_stripped_variants(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """The Transcript cache panel shows a recording's WHOLE history — its
    original-audio variants AND its silence-stripped clip variants — in one
    source-tagged list that does NOT change when you flip the Original/Stripped
    toggle (operator ask: the rows are already tagged). And 'Set' on a stripped
    row sends source=stripped, so the server resolves the clip under stripped/
    instead of 404ing (the reported 'Set primary failed: 404'). Sidecars are
    seeded via the wav cache; the set-primary PUT is intercepted to assert its
    payload."""
    import httpx

    from tapscribe.wav_cache import cached_transcribe

    from .test_pipeline_strip_silence import _build_speech_silence_wav

    rr = running_recorder
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
        assert r.status_code == 200 and r.json()["files_written"] >= 1, r.text

    original = sorted(rr.recorder.session_dir.glob("*.wav"))[0]
    region = sorted((rr.recorder.session_dir / "stripped").glob("*.wav"))[0]
    spk = {"alice": "scripted alice text"}
    # Original recording: one cached variant (source=original).
    cached_transcribe(
        original,
        FakeTranscriber(text_by_speaker=spk, model_name="orig-model"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
        source="original",
    )
    # Stripped clip: TWO variants (source=stripped) so one row is non-primary
    # and renders a clickable "set" button to exercise the 404 fix.
    for model in ("region-a", "region-b"):
        cached_transcribe(
            region,
            FakeTranscriber(text_by_speaker=spk, model_name=model),
            initial_prompt=None,
            hotwords=None,
            hallucination_rules=[],
            source="stripped",
        )

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            put_payloads: list[dict] = []
            # Same reason as the source-toggle test: gate the payload assertion
            # on the PUT actually landing, not on a 500 ms guess.
            primary_put = asyncio.Event()

            async def _route(route):
                if route.request.method == "PUT":
                    put_payloads.append(route.request.post_data_json or {})
                    primary_put.set()
                await route.fulfill(
                    status=200, content_type="application/json", body='{"ok": true, "primary": {}}'
                )

            await page.route("**/api/wav/**/primary", _route)
            await page.goto(rr.base_url + "/#transcript", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="cacheBody"] .cacherow', timeout=6000)

            # One unified list: the original variant (is-original) + both stripped
            # clip variants (is-stripped), regardless of the toggle's default.
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-slot=cacheBody] .cacherow').length >= 3",
                timeout=6000,
            )
            # Select on the slot the template binds through + the tag's own
            # TEXT (transcript.js writes `v.source` into it), not the
            # presentational `.cacherow__src` / `.is-*` classes.
            src_tags = page.locator('[data-slot="cacheBody"] [data-slot="src"]')
            orig_tags = await src_tags.filter(has_text=re.compile(r"^original$")).count()
            strip_tags = await src_tags.filter(has_text=re.compile(r"^stripped$")).count()
            assert orig_tags >= 1 and strip_tags >= 2, f"orig={orig_tags} stripped={strip_tags}"
            rows_before = await page.locator('[data-slot="cacheBody"] .cacherow').count()

            # Flipping to Stripped must NOT change the cache list. Wait on the
            # toggle latching AND on the WAV picker having re-rendered to the
            # stripped source — that re-render is the tick that COULD have
            # rebuilt the cache list, so a fixed sleep here risks asserting
            # "unchanged" before anything had a chance to change it.
            await page.locator('[data-slot="srcSwHost"] [data-src="stripped"]').click()
            await page.wait_for_selector('[data-slot="srcSwHost"] [data-src="stripped"].is-on', timeout=6000)
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-slot=wavList] .wavrow').length > 0",
                timeout=6000,
            )
            rows_after = await page.locator('[data-slot="cacheBody"] .cacherow').count()
            assert rows_after == rows_before, f"cache list changed on toggle: {rows_before} -> {rows_after}"
            assert await src_tags.filter(has_text=re.compile(r"^stripped$")).count() >= 2

            # 'Set' on the non-primary stripped row sends source=stripped (404 fix).
            set_btn = page.locator('[data-slot="cacheBody"] [data-slot="primary"]', has_text="set")
            assert await set_btn.count() == 1, "expected one non-primary 'set' row (a stripped variant)"
            await set_btn.first.click()
            await asyncio.wait_for(primary_put.wait(), timeout=6.0)
            assert put_payloads and put_payloads[-1].get("source") == "stripped", (
                f"set-primary on a stripped row must send source=stripped, got {put_payloads!r}"
            )
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Summary stage (Command source) — Generate → render, + error surfacing.
# ---------------------------------------------------------------------------


def _py_summarize_cmd(script: str) -> str:
    """A cross-platform Command-source template that runs `script` under the
    server's own interpreter (the uvicorn server shares this venv), so the
    summarize subprocess works on the Linux playwright CI without `cat`/`echo`
    on PATH."""
    import shlex
    import sys

    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


async def test_summary_stage_command_source_generates_and_renders(
    running_recorder: RunningRecorder,
):
    """The wired Summary stage: with a merged transcript present, the operator
    enters a Command template, clicks Generate, and the summary the command
    prints to stdout renders in the output panel along with the source/command
    that produced it. Then a failing command surfaces a visible error. Drives
    the REAL POST /summarize → batch_summarize → subprocess seam (no mock)."""
    rr = running_recorder

    # Seed the CURRENT session with a merged transcript so it's selectable in
    # the spine AND the Summary stage's Generate is enabled (needs a transcript).
    sd = rr.recorder.session_dir
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "session-transcript.json").write_text(
        json.dumps(
            {
                "session": rr.recorder.session_start,
                "model": "test",
                "transcribed_at": "2026-01-01T00:00:00+00:00",
                "speakers": ["Alice"],
                "segments": [],
                "plain_text": "Alice: we decided to ship the dashboard.",
            }
        ),
        encoding="utf-8",
    )

    marker = "MEETING_SUMMARY_RENDERED_OK"
    echo_cmd = _py_summarize_cmd(f"import sys; sys.stdout.write({marker!r})")
    fail_cmd = _py_summarize_cmd("import sys; sys.stderr.write('kaboom'); sys.exit(1)")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # Local is the default source now (#86); switch to Command to reveal
            # its CLI template field, then wait for Generate to enable once the
            # seeded transcript lands on a poll (proves the view sees the marker).
            await page.wait_for_selector('button[data-src="command"]', timeout=6000)
            await page.click('button[data-src="command"]')
            await page.wait_for_selector('[data-slot="sumCmd"]', state="visible", timeout=6000)
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('[data-slot="sumGenerate"]');
                  return b && !b.disabled;
                }""",
                timeout=8000,
            )

            # Generate with the echo command — the summary is the marker.
            await page.fill('[data-slot="sumCmd"]', echo_cmd)
            await page.click('[data-slot="sumGenerate"]')
            await page.wait_for_function(
                """(m) => (document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(m)""",
                arg=marker,
                timeout=10000,
            )

            # The source/command that produced it is shown.
            hint = await page.locator('[data-slot="sumOutHint"]').text_content()
            assert "command" in (hint or ""), f"output hint must name the source/command, got {hint!r}"

            # A failing command surfaces a visible error in the note line.
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('[data-slot="sumGenerate"]');
                  return b && !b.disabled;
                }""",
                timeout=8000,
            )
            await page.fill('[data-slot="sumCmd"]', fail_cmd)
            await page.click('[data-slot="sumGenerate"]')
            await page.wait_for_function(
                """() => (document.querySelector('[data-slot="sumNote"]')?.textContent || '')
                          .toLowerCase().includes('failed')""",
                timeout=10000,
            )
        finally:
            await browser.close()


async def test_summary_stage_has_no_mock_not_wired_tags(running_recorder: RunningRecorder):
    """The 'mock · not wired' tags are gone from the Summary stage now that it's
    real; Local + Command + API (#85) are all wired — no source stays disabled."""
    rr = running_recorder
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")
            # Anchor on the source selector (always visible) — the command field
            # is hidden under the Local default now.
            await page.wait_for_selector('button[data-src="local"]', timeout=6000)

            assert await page.locator("#viewRoot .mocktag").count() == 0, (
                "the mock·not-wired tag must be gone"
            )
            # Local + Command + API all enabled now (#85 wired the API source).
            seg = page.locator("#viewRoot button[data-src]")
            assert await seg.count() == 3
            disabled = await page.locator("#viewRoot button[data-src][disabled]").count()
            assert disabled == 0, f"no source must be disabled, got {disabled} disabled options"
            # Local is the bundled-offline default selected source (#86).
            assert await page.locator('#viewRoot button[data-src="local"].is-on').count() == 1
        finally:
            await browser.close()


async def test_summary_stage_local_is_default_and_toggles_command_field(running_recorder: RunningRecorder):
    """Local (bundled, offline) is the default source in the Summary view (#86):
    its pane shows no CLI field, and switching to Command reveals the command
    template (and back to Local hides it). This is the source-selector wiring
    the Local slice adds on top of the Command tracer bullet — no model download
    needed, so it runs offline on CI."""
    rr = running_recorder
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # Local selected by default; its command field is not shown.
            await page.wait_for_selector('[data-src="local"].is-on', timeout=6000)
            assert not await page.locator('[data-slot="sumCmd"]').is_visible(), (
                "the command field must be hidden under the Local source"
            )

            # Switch to Command → the CLI template field appears.
            await page.click('button[data-src="command"]')
            await page.locator('[data-slot="sumCmd"]').wait_for(state="visible", timeout=4000)

            # Switch back to Local → it hides again, and Local is is-on.
            await page.click('button[data-src="local"]')
            await page.locator('[data-slot="sumCmd"]').wait_for(state="hidden", timeout=4000)
            assert await page.locator('[data-src="local"].is-on').count() == 1
        finally:
            await browser.close()


async def test_summary_api_source_reveals_pane_with_write_only_key(running_recorder: RunningRecorder):
    """The API source (#85): switching to it reveals the base-URL / model / key
    pane, and the key field is WRITE-ONLY — a password input that is never
    pre-filled (the server exposes only key_set, never the key itself). Switching
    away hides the pane. All click-driven, so the poll never rebuilds it."""
    rr = running_recorder
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            await page.wait_for_selector('[data-src="api"]', timeout=6000)
            # API pane hidden under the Local default.
            assert not await page.locator('[data-slot="sumApiBase"]').is_visible()

            # Switch to API → base URL, model, and key fields appear.
            await page.click('[data-src="api"]')
            await page.locator('[data-slot="sumApiBase"]').wait_for(state="visible", timeout=4000)
            assert await page.locator('[data-slot="sumApiModel"]').is_visible()

            # The key field is write-only: a password input, never pre-filled.
            key = page.locator('[data-slot="sumApiKey"]')
            assert await key.get_attribute("type") == "password"
            assert await key.input_value() == "", "the API key field must never be pre-filled"

            # Switch back to Local → the API pane hides again.
            await page.click('button[data-src="local"]')
            await page.locator('[data-slot="sumApiBase"]').wait_for(state="hidden", timeout=4000)
            assert await page.locator('[data-src="local"].is-on').count() == 1
        finally:
            await browser.close()


async def test_summary_command_preset_seeds_template_and_preview(running_recorder: RunningRecorder):
    """The Command source's preset dropdown + 'will run' preview: presets load
    from GET /api/summarize/models and SEED the editable template (not an
    allowlist); the preview spells out template + prompt-as-trailing-arg +
    transcript-on-stdin; hand-editing the template flips the preset back to
    custom. All input-event-driven — the poll never rebuilds the pane."""
    rr = running_recorder
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            await page.wait_for_selector('button[data-src="command"]', timeout=6000)
            await page.click('button[data-src="command"]')
            await page.locator('[data-slot="sumCmdPreset"]').wait_for(state="visible", timeout=4000)

            # Presets populate from the catalog fetch: custom… + claude + opencode.
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumCmdPreset"]')?.options.length >= 3""",
                timeout=6000,
            )
            labels = await page.eval_on_selector(
                '[data-slot="sumCmdPreset"]',
                "el => Array.from(el.options).map(o => o.label)",
            )
            assert any("Claude" in label for label in labels), labels
            assert any("OpenCode" in label for label in labels), labels

            # Picking the Claude preset seeds the editable template field with
            # the hardened tools-disabled invocation.
            await page.select_option('[data-slot="sumCmdPreset"]', "claude")
            cmd = await page.input_value('[data-slot="sumCmd"]')
            assert cmd.startswith("claude ") and "--tools" in cmd, cmd

            # The preview spells out the invocation: the template, the prompt as
            # a quoted trailing argument, and the transcript-on-stdin note.
            preview = await page.locator('[data-slot="sumCmdPreview"]').text_content()
            assert cmd in (preview or ""), preview
            assert "stdin" in (preview or ""), preview
            assert '"' in (preview or ""), "the prompt must show as a quoted trailing arg"

            # Editing the prompt updates the preview (input-event-driven).
            await page.fill('[data-slot="sumPrompt"]', "Five bullet points")
            await page.wait_for_function(
                """() => (document.querySelector('[data-slot="sumCmdPreview"]')?.textContent || '')
                          .includes('Five bullet points')""",
                timeout=4000,
            )

            # Hand-editing the template flips the preset back to custom.
            await page.fill('[data-slot="sumCmd"]', "my-own-tool --flag")
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumCmdPreset"]')?.value === ''""",
                timeout=4000,
            )
            preview2 = await page.locator('[data-slot="sumCmdPreview"]').text_content()
            assert "my-own-tool --flag" in (preview2 or ""), preview2
        finally:
            await browser.close()


async def test_summary_output_renders_markdown_safely(running_recorder: RunningRecorder):
    """The summary output pane renders the model's markdown (heading, bullets,
    bold) as real elements — Claude and friends answer in markdown — via
    templates.js renderMarkdown, which builds DOM through createElement/
    textContent only. The flip side is the security property this test pins:
    the summary is UNTRUSTED model output (the Command source pipes an
    untrusted transcript through an LLM), so injected HTML must stay literal
    text — an `<img onerror=…>` in the summary must never become an element."""
    rr = running_recorder

    sd = rr.recorder.session_dir
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "session-transcript.json").write_text(
        json.dumps(
            {
                "session": rr.recorder.session_start,
                "model": "test",
                "transcribed_at": "2026-01-01T00:00:00+00:00",
                "speakers": ["Alice"],
                "segments": [],
                "plain_text": "Alice: we decided to ship the dashboard.",
            }
        ),
        encoding="utf-8",
    )

    md = "# Decisions\n- ship the MD_MARKER dashboard\n**bold** <img src=x onerror=alert(1)>"
    md_cmd = _py_summarize_cmd(f"import sys; sys.stdin.read(); sys.stdout.write({md!r})")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            await page.wait_for_selector('button[data-src="command"]', timeout=6000)
            await page.click('button[data-src="command"]')
            await page.wait_for_selector('[data-slot="sumCmd"]', state="visible", timeout=6000)
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('[data-slot="sumGenerate"]');
                  return b && !b.disabled;
                }""",
                timeout=8000,
            )
            await page.fill('[data-slot="sumCmd"]', md_cmd)
            await page.click('[data-slot="sumGenerate"]')
            await page.wait_for_function(
                """() => (document.querySelector('[data-slot="sumOut"]')?.textContent || '')
                          .includes('MD_MARKER')""",
                timeout=10000,
            )

            shape = await page.evaluate(
                """() => {
                  const out = document.querySelector('[data-slot="sumOut"]');
                  return {
                    h1: out.querySelectorAll('.sumtext h1').length,
                    li: out.querySelectorAll('.sumtext li').length,
                    strong: out.querySelectorAll('.sumtext strong').length,
                    img: out.querySelectorAll('img').length,
                    text: out.textContent || '',
                  };
                }"""
            )
            # The markdown became real elements inside the output pane…
            assert shape["h1"] == 1, shape
            assert shape["li"] == 1, shape
            assert shape["strong"] == 1, shape
            # …but the injected tag did NOT — it stays the literal characters.
            assert shape["img"] == 0, "injected <img onerror> must not become an element"
            assert "<img" in shape["text"], shape["text"]
        finally:
            await browser.close()


async def test_summary_stage_local_sends_picked_model_and_max_tokens(running_recorder: RunningRecorder):
    """The Local source's model dropdown + max-output-tokens knob (PR #96) reach
    the backend. The <select> populates from GET /api/summarize/models; picking a
    model and editing max-tokens, then Generate, POSTs
    `{source:'local', model, max_tokens}`.

    We mock the catalog endpoint (so a *second* selectable model exists regardless
    of which backend this box routes to — the Linux gguf catalog ships only one)
    and intercept the summarize POST to capture its body + return a fake summary,
    so the test exercises the real summary.js populate→select→send wiring without
    loading a multi-GB model on CI. The REAL catalog endpoint's shape is pinned
    separately by test_api_summarize_models_lists_catalog."""
    rr = running_recorder

    # Seed the CURRENT session with a merged transcript so Generate enables.
    sd = rr.recorder.session_dir
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "session-transcript.json").write_text(
        json.dumps(
            {
                "session": rr.recorder.session_start,
                "model": "test",
                "transcribed_at": "2026-01-01T00:00:00+00:00",
                "speakers": ["Alice"],
                "segments": [],
                "plain_text": "Alice: we decided to ship the dashboard.",
            }
        ),
        encoding="utf-8",
    )

    fake_catalog = {
        "backend": "mlx",
        "default": "vendor/model-A-4bit",
        "models": [
            {
                "repo_id": "vendor/model-A-4bit",
                "label": "Model A (4-bit)",
                "approx_gb": 8.0,
                "context_tokens": 32768,
                "note": "the default",
                "is_default": True,
            },
            {
                "repo_id": "vendor/model-B-4bit",
                "label": "Model B (4-bit)",
                "approx_gb": 13.0,
                "context_tokens": 32000,
                "note": "the other one",
                "is_default": False,
            },
        ],
        "max_tokens_default": 2048,
        "max_tokens_min": 16,
        "max_tokens_max": 8192,
    }
    captured: dict[str, object] = {}

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            # Routes installed BEFORE goto so they're live when summary.js mounts
            # (the catalog fetch fires once at build time).
            async def _catalog_route(route):
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(fake_catalog)
                )

            async def _summarize_route(route):
                captured.update(route.request.post_data_json or {})
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "session": rr.recorder.session_start,
                            "summary": "FAKE_LOCAL_SUMMARY_OK",
                            "source": "local",
                            "prompt": "",
                            "model": (captured.get("model") or ""),
                            "command": "",
                            "took_ms": 1,
                            "created_at": "2026-01-01T00:00:00+00:00",
                        }
                    ),
                )

            await page.route("**/api/summarize/models", _catalog_route)
            await page.route("**/api/sessions/*/summarize", _summarize_route)

            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # The model <select> populates from the (mocked) catalog: two options.
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumModel"]')?.options.length === 2""",
                timeout=8000,
            )
            # Pick the non-default model and set a sentinel output cap.
            await page.select_option('[data-slot="sumModel"]', "vendor/model-B-4bit")
            await page.fill('[data-slot="sumMaxTokens"]', "4096")

            # Generate enables once the seeded transcript lands on a poll.
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('[data-slot="sumGenerate"]');
                  return b && !b.disabled;
                }""",
                timeout=8000,
            )
            await page.click('[data-slot="sumGenerate"]')

            # The fake summary renders.
            await page.wait_for_function(
                """() => (document.querySelector('[data-slot="sumOut"]')?.textContent || '')
                          .includes('FAKE_LOCAL_SUMMARY_OK')""",
                timeout=10000,
            )

            # The POST body carried the picked model + max_tokens under the local source.
            assert captured.get("source") == "local", captured
            assert captured.get("model") == "vendor/model-B-4bit", captured
            assert captured.get("max_tokens") == 4096, captured
        finally:
            await browser.close()


async def test_summary_stage_local_seeds_max_tokens_and_surfaces_error(running_recorder: RunningRecorder):
    """The max-output-tokens input SEEDS from the server (value + bounds), and a
    rejected Generate (e.g. an off-catalog / unloadable model the backend 400s)
    surfaces a visible error in the note.

    We mock the catalog with a distinctive default (1536, not the HTML
    placeholder 2048) + custom bounds to prove the input is seeded from
    `GET /api/summarize/models`, and fail the summarize POST with the real
    off-catalog 400 shape to prove the Local source renders the error."""
    rr = running_recorder

    sd = rr.recorder.session_dir
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "session-transcript.json").write_text(
        json.dumps(
            {
                "session": rr.recorder.session_start,
                "model": "test",
                "transcribed_at": "2026-01-01T00:00:00+00:00",
                "speakers": ["Alice"],
                "segments": [],
                "plain_text": "Alice: we decided to ship the dashboard.",
            }
        ),
        encoding="utf-8",
    )

    fake_catalog = {
        "backend": "gguf",
        "default": "vendor/only-model",
        "models": [
            {
                "repo_id": "vendor/only-model",
                "label": "Only Model",
                "approx_gb": 5.0,
                "context_tokens": 128000,
                "note": "",
                "is_default": True,
            }
        ],
        # Distinctive default + bounds — none equal the HTML placeholder "2048",
        # so seeing them in the input proves it was seeded from the server.
        "max_tokens_default": 1536,
        "max_tokens_min": 128,
        "max_tokens_max": 4096,
    }

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            async def _catalog_route(route):
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(fake_catalog)
                )

            async def _summarize_route(route):
                # The backend's real off-catalog / unavailable rejection shape.
                await route.fulfill(
                    status=400,
                    content_type="application/json",
                    body=json.dumps({"detail": "the local summarizer model 'x' isn't a known gguf model"}),
                )

            await page.route("**/api/summarize/models", _catalog_route)
            await page.route("**/api/sessions/*/summarize", _summarize_route)

            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # The input is seeded from the catalog (value 1536, not the HTML 2048).
            await page.wait_for_function(
                """() => {
                  const sel = document.querySelector('[data-slot="sumModel"]');
                  const mt = document.querySelector('[data-slot="sumMaxTokens"]');
                  return sel && sel.options.length === 1 && mt && mt.value === '1536';
                }""",
                timeout=8000,
            )
            mt = page.locator('[data-slot="sumMaxTokens"]')
            assert await mt.get_attribute("min") == "128", "min bound must seed from the catalog"
            assert await mt.get_attribute("max") == "4096", "max bound must seed from the catalog"

            # Generate enables once the seeded transcript lands; the rejected POST
            # surfaces a visible error in the note.
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('[data-slot="sumGenerate"]');
                  return b && !b.disabled;
                }""",
                timeout=8000,
            )
            await page.click('[data-slot="sumGenerate"]')
            await page.wait_for_function(
                """() => {
                  const n = document.querySelector('[data-slot="sumNote"]');
                  return n && n.classList.contains('is-err') &&
                         (n.textContent || '').toLowerCase().includes('failed');
                }""",
                timeout=10000,
            )
        finally:
            await browser.close()


async def test_summary_persists_across_reload(running_recorder: RunningRecorder):
    """#83: a generated summary survives a dashboard reload. Generate via the
    Command source, reload the page, and the stored summary renders again
    WITHOUT clicking Generate — served from session-summary.json through the
    slim marker + lazy GET /api/sessions/{session}/summary, not from view
    memory (the reload wiped that)."""
    rr = running_recorder

    sd = rr.recorder.session_dir
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "session-transcript.json").write_text(
        json.dumps(
            {
                "session": rr.recorder.session_start,
                "model": "test",
                "transcribed_at": "2026-01-01T00:00:00+00:00",
                "speakers": ["Alice"],
                "segments": [],
                "plain_text": "Alice: we decided to ship the dashboard.",
            }
        ),
        encoding="utf-8",
    )

    marker = "PERSISTED_SUMMARY_SURVIVES_RELOAD"
    echo_cmd = _py_summarize_cmd(f"import sys; sys.stdout.write({marker!r})")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # Generate once via the Command source (the #82 flow).
            await page.wait_for_selector('button[data-src="command"]', timeout=6000)
            await page.click('button[data-src="command"]')
            await page.wait_for_selector('[data-slot="sumCmd"]', state="visible", timeout=6000)
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('[data-slot="sumGenerate"]');
                  return b && !b.disabled;
                }""",
                timeout=8000,
            )
            await page.fill('[data-slot="sumCmd"]', echo_cmd)
            await page.click('[data-slot="sumGenerate"]')
            await page.wait_for_function(
                """(m) => (document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(m)""",
                arg=marker,
                timeout=10000,
            )

            # Reload — view-local state is gone; the stored summary must come
            # back on its own (marker → lazy GET), with NO Generate click.
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(
                """(m) => (document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(m)""",
                arg=marker,
                timeout=10000,
            )

            # The hint still names what produced it (persisted metadata, not
            # view memory).
            hint = await page.locator('[data-slot="sumOutHint"]').text_content()
            assert "command" in (hint or ""), f"hint must name the persisted source, got {hint!r}"
        finally:
            await browser.close()


async def test_dashboard_renders_real_end_of_meeting_pipeline_summary(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001 — fake ASR keeps it CI-runnable; the chain is otherwise real
    tmp_path: Path,
):
    """The dashboard surfaces a REAL end-of-meeting pipeline run.

    The existing job-bar coverage injects a synthetic JobState
    (`test_meeting_pipeline_job_renders_stage_labelled_bar`), and the
    summary-stage tests drive the single Generate button on a hand-seeded
    transcript — neither runs the unified pipeline orchestrator. Here a
    captured session is put through the real strip → transcribe → summarize
    chain as ONE Session job (triggered through the tap pipeline endpoint the
    Bridges use), and the dashboard's Summary view must render the summary
    that real chain produced — and keep it across a reload, served from the
    persisted session-summary.json the pipeline wrote, never seeded by the
    test. The transcriber is faked (so this runs in the dashboard CI job
    without faster-whisper); every other stage — strip, merge, the Command
    summarizer subprocess, persistence — is real.
    """
    rr = running_recorder
    rec = rr.recorder

    # Operator-default summarizer = a Command source that proves BOTH that the
    # summarize stage ran AND that the real merged transcript reached it
    # (fake_transcriber emits "…quick brown fox…" for Alice, so 'quick' only
    # appears if the chain actually transcribed + merged before summarizing).
    marker = "REAL_PIPELINE_SUMMARY_OK"
    summary_cmd = _py_summarize_cmd(
        "import sys; t = sys.stdin.read();"
        f" sys.stdout.write({marker!r} + (' quick' if 'quick' in t else ' NO_TRANSCRIPT'))"
    )
    rec.config_dir.joinpath("summarizer.json").write_text(
        json.dumps({"source": "command", "command": summary_cmd}), encoding="utf-8"
    )

    # Capture: one real /tap recording into the current session.
    await stream_wav_via_tap(
        ws_base_url=rr.ws_base_url,
        identity="alice",
        name="Alice",
        wav_path=synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0),
        utterance_id="utt-pipeline",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
    sid = rec.session_start

    # Trigger the unified pipeline through the tap endpoint and wait for the
    # whole chain to finish (a failed stage fails the test with its reason).
    async with httpx.AsyncClient(base_url=rr.base_url, timeout=30.0) as client:
        r = await client.post(f"/api/tap/sessions/{sid}/pipeline")
        assert r.status_code == 202, r.text

        async def _pipeline_done() -> bool:
            body = (await client.get(f"/api/tap/sessions/{sid}/pipeline")).json()
            assert body.get("state") != "failed", f"pipeline failed: {body}"
            return body.get("state") == "done"

        assert await wait_until(_pipeline_done, timeout=60.0, interval=0.25), "pipeline did not finish"

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            # Current session is the default Summary-view selection; its
            # persisted summary lazy-loads with no Generate click (the same
            # marker → GET /api/sessions/{s}/summary path the reload test pins).
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")
            await page.wait_for_function(
                """(m) => (document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(m)""",
                arg=marker,
                timeout=15000,
            )
            out = await page.locator('[data-slot="sumOut"]').text_content()
            assert "quick" in (out or ""), f"summary must reflect the real merged transcript, got {out!r}"

            # Survives reload — served from the pipeline's persisted summary,
            # not view memory (the reload wiped that).
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(
                """(m) => (document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(m)""",
                arg=marker,
                timeout=15000,
            )
            hint = await page.locator('[data-slot="sumOutHint"]').text_content()
            assert "command" in (hint or ""), f"hint must name the persisted source, got {hint!r}"
        finally:
            await browser.close()


async def test_dashboard_flags_stale_summary_after_retranscribe_and_clears_on_regenerate(
    running_recorder: RunningRecorder,
):
    """#94: a summary built before a re-transcribe is flagged stale, and the cue
    clears when the operator regenerates.

    The persisted summary carries the `transcribed_at` of the transcript it was
    built from (batch_summarize), projected onto the session's slim
    `session_summary` marker in /api/state. The Summary view compares that stamp
    against the live `session_transcript.transcribed_at` and, when the transcript
    is newer (the session was re-transcribed since), renders a 'predates the
    current transcript' cue — in place on the poll tick, with NO reload.
    Regenerating rebuilds from the current transcript, so the summary's stamp
    catches up and the cue clears.

    Full-stack proof the Generate route persists the stamp, /api/state projects
    it, and the view's staleness compare + sibling cue render all line up — which
    the unit/route tests (persisted field + marker shape in isolation) can't
    exercise together.
    """
    rr = running_recorder

    sd = rr.recorder.session_dir
    sd.mkdir(parents=True, exist_ok=True)

    def _write_transcript(*, stamp: str, text: str) -> None:
        # A re-transcribe rewrites session-transcript.json with new text + stamp.
        # The /api/state JSON cache is keyed on (mtime_ns, size); the distinct
        # `text` between the two writes changes the size, so the next poll re-reads
        # the bumped stamp without the test relying on mtime granularity.
        (sd / "session-transcript.json").write_text(
            json.dumps(
                {
                    "session": rr.recorder.session_start,
                    "model": "test",
                    "transcribed_at": stamp,
                    "speakers": ["Alice"],
                    "segments": [],
                    "plain_text": text,
                }
            ),
            encoding="utf-8",
        )

    # Transcript v1 (older stamp).
    _write_transcript(stamp="2026-01-01T00:00:00+00:00", text="Alice: we decided to ship the dashboard.")

    marker = "STALE_CUE_SUMMARY_OK"
    echo_cmd = _py_summarize_cmd(f"import sys; sys.stdout.write({marker!r})")
    stale_text = "predates the current transcript"

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # Generate once via the Command source — the summary records the v1
            # transcript stamp.
            await page.wait_for_selector('button[data-src="command"]', timeout=6000)
            await page.click('button[data-src="command"]')
            await page.wait_for_selector('[data-slot="sumCmd"]', state="visible", timeout=6000)
            await page.wait_for_function(
                """() => {
                  const b = document.querySelector('[data-slot="sumGenerate"]');
                  return b && !b.disabled;
                }""",
                timeout=8000,
            )
            await page.fill('[data-slot="sumCmd"]', echo_cmd)
            await page.click('[data-slot="sumGenerate"]')
            await page.wait_for_function(
                """(m) => (document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(m)""",
                arg=marker,
                timeout=10000,
            )
            # Fresh summary — no stale cue yet (its stamp matches the transcript's).
            out = await page.locator('[data-slot="sumOut"]').text_content()
            assert stale_text not in (out or ""), f"summary must not be stale yet, got {out!r}"

            # Re-transcribe: bump the merged transcript's stamp on disk. The
            # /api/state poll picks it up and the view flags the summary stale IN
            # PLACE (no reload) — its recorded stamp is now older than the live
            # transcript's.
            _write_transcript(
                stamp="2026-02-01T00:00:00+00:00",
                text="Alice: we decided to ship the dashboard, and to cut a release this week.",
            )
            await page.wait_for_function(
                """(s) => (document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(s)""",
                arg=stale_text,
                timeout=10000,
            )

            # Regenerate against the current transcript: the new summary records
            # the newer stamp, so the cue clears (and the summary still renders).
            await page.click('[data-slot="sumGenerate"]')
            await page.wait_for_function(
                """(s) => !(document.querySelector('[data-slot="sumOut"]')?.textContent || '').includes(s)""",
                arg=stale_text,
                timeout=10000,
            )
            out2 = await page.locator('[data-slot="sumOut"]').text_content()
            assert marker in (out2 or ""), f"regenerated summary must still render, got {out2!r}"
        finally:
            await browser.close()


async def test_settings_summarizer_default_card_saves_and_prefills(running_recorder: RunningRecorder):
    """#84: the Settings stage's Summarizer card edits the structured global
    default. Pick the Command source, type a template + prompt, Save — then
    reload: the card pre-fills from the persisted config (state poll), not
    from view memory."""
    rr = running_recorder

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#settings", wait_until="domcontentloaded")

            # The card builds once; the source segctl scopes its own buttons.
            await page.wait_for_selector('[data-slot="sdSource"] [data-sd-src="command"]', timeout=6000)
            await page.click('[data-slot="sdSource"] [data-sd-src="command"]')
            await page.wait_for_selector('[data-slot="sdCmd"]', state="visible", timeout=6000)
            await page.fill('[data-slot="sdCmd"]', "claude -p --bare")
            await page.fill('[data-slot="sdPrompt"]', "GLOBAL DEFAULT PROMPT")
            await page.click('[data-slot="sdSave"]')
            await page.wait_for_function(
                """() => (document.querySelector('[data-slot="sdStatus"]')?.textContent || '') === 'saved'""",
                timeout=8000,
            )

            # Reload: the card must pre-fill from the persisted global default.
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="sdPrompt"]', timeout=6000)
            await page.wait_for_function(
                """() => {
                  const ta = document.querySelector('[data-slot="sdPrompt"]');
                  return ta && ta.value === 'GLOBAL DEFAULT PROMPT';
                }""",
                timeout=8000,
            )
            on = await page.get_attribute('[data-slot="sdSource"] [data-sd-src="command"]', "class")
            assert "is-on" in (on or ""), f"saved source must pre-select, got class {on!r}"
            cmd_visible = await page.is_visible('[data-slot="sdCmd"]')
            assert cmd_visible, "command detail pane must show for the saved Command source"
            # The command value applies once the catalog fetch settles (the
            # shared controls sequence saved values on it) — wait, don't race it.
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sdCmd"]')?.value === 'claude -p --bare'""",
                timeout=8000,
            )

            # The backend agrees (the card saved through PUT /api/summarize/config).
            cfg = json.loads(await (await context.request.get(rr.base_url + "/api/summarize/config")).text())
            assert cfg["source"] == "command"
            assert cfg["prompt"] == "GLOBAL DEFAULT PROMPT"
        finally:
            await browser.close()


async def test_settings_models_card_links_to_setup(running_recorder: RunningRecorder):
    """The Settings stage is the dashboard's entry point to the browser
    model-setup page (/setup, a separate full-page app): a 'Manage models'
    link plus a one-shot installed-summary line filled from GET
    /api/setup/state. Without this card, /setup is reachable only by typing
    the URL."""
    rr = running_recorder

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#settings", wait_until="domcontentloaded")

            # A plain navigation link to the separate /setup page (not an
            # in-dashboard view), so it carries an href rather than a click
            # handler.
            link = page.get_by_role("link", name="Manage models")
            await link.wait_for(timeout=6000)
            assert (await link.get_attribute("href")) == "/setup"

            # The installed-summary line fills once from /api/setup/state — it
            # must replace the "…" placeholder with either an "Installed: …"
            # list or the nothing-installed hint, depending on the host.
            await page.wait_for_function(
                """() => {
                  const el = document.querySelector('[data-slot="modelsInstalled"]');
                  return el && el.textContent && el.textContent !== '…';
                }""",
                timeout=8000,
            )
            summary = await page.text_content('[data-slot="modelsInstalled"]')
            assert "Installed:" in (summary or "") or "No models installed" in (summary or ""), (
                f"unexpected installed-summary text: {summary!r}"
            )
        finally:
            await browser.close()


async def test_settings_connect_a_bridge_card_reveals_and_hides_tap_token(running_recorder: RunningRecorder):
    """#190: the Settings stage's "Connect a bridge" card is the dashboard's
    only surface for the /tap bearer token — before this, an operator had to
    scrape terminal output or open .tap-token by hand.

    The token must NOT be in the DOM until an explicit reveal click (never
    rendered by default, never fetched on page load), the reveal fetches
    GET /api/tap-token exactly once (a second reveal reuses the cached
    value), and hiding re-masks the DOM rather than leaving the plaintext
    sitting behind CSS. The expected token comes from the running Recorder
    the test harness minted for THIS run (`rr.recorder.tap.value`) — never a
    hardcoded fixture token."""
    rr = running_recorder
    real_token = rr.recorder.tap.value

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            tap_token_requests = []
            page.on(
                "request", lambda req: tap_token_requests.append(req) if "/api/tap-token" in req.url else None
            )

            await page.goto(rr.base_url + "/#settings", wait_until="domcontentloaded")

            token_el = page.locator('[data-slot="bridgeToken"]')
            reveal_btn = page.get_by_role("button", name=re.compile(r"reveal"))
            await reveal_btn.wait_for(timeout=6000)

            # Host/port render immediately (not sensitive, no reveal needed);
            # the token stays masked and unfetched until the operator asks.
            assert real_token not in (await page.content()), "tap token must not be in the DOM before reveal"
            assert len(tap_token_requests) == 0, (
                "GET /api/tap-token must not fire before an explicit reveal click"
            )
            masked_before = await token_el.text_content()
            assert masked_before and real_token not in masked_before

            await reveal_btn.click()
            await page.wait_for_function(
                """(expected) => {
                  const el = document.querySelector('[data-slot="bridgeToken"]');
                  return el && el.textContent === expected;
                }""",
                arg=real_token,
                timeout=6000,
            )
            assert len(tap_token_requests) == 1

            # A second reveal-toggle round trip (hide, then reveal again)
            # must reuse the cached value rather than re-fetching.
            hide_btn = page.get_by_role("button", name=re.compile(r"hide"))
            await hide_btn.click()
            masked_after_hide = await token_el.text_content()
            assert masked_after_hide and real_token not in masked_after_hide, (
                "hiding must re-mask the DOM, not just leave the plaintext behind CSS"
            )

            reveal_again = page.get_by_role("button", name=re.compile(r"reveal"))
            await reveal_again.click()
            await page.wait_for_function(
                """(expected) => {
                  const el = document.querySelector('[data-slot="bridgeToken"]');
                  return el && el.textContent === expected;
                }""",
                arg=real_token,
                timeout=6000,
            )
            assert len(tap_token_requests) == 1, "a second reveal must reuse the cached token, not re-fetch"
        finally:
            await browser.close()


async def test_settings_get_a_bridge_card_links_to_release_assets(running_recorder: RunningRecorder):
    """PR-C: the Settings stage's "Get a bridge" card gives an operator a
    one-click path to the SpatialChat extension zip + Windows tray exe instead
    of a repo clone + manual build.

    The two download anchors' hrefs are filled once from a single best-effort
    GET /api/bridges on render (no per-tick work), and point at the permanent
    `releases/latest/download/<asset>` URLs (ADR-0012) — the browser downloads
    straight from GitHub, so these are plain cross-origin hrefs, not
    same-origin download triggers. The card is static: exactly ONE /api/bridges
    fetch fires on render, and none on a subsequent poll tick."""
    rr = running_recorder

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            bridges_requests = []
            page.on(
                "request", lambda req: bridges_requests.append(req) if "/api/bridges" in req.url else None
            )
            # Count /api/state hits so "cross a poll tick" below is a PROVEN
            # crossing. Under ADR-0013's idle backoff (FAST_MS=500,
            # IDLE_STREAK=4, SLOW_MS=2000) this recorder is idle-and-unchanged,
            # so the pacer flips to 2 s and a fixed 1200 ms window can contain
            # ZERO ticks — making the invariant below re-assert its own
            # pre-existing value and pass for a card that DID re-fetch per poll.
            await page.add_init_script(_COUNT_STATE_304S_JS)

            await page.goto(rr.base_url + "/#settings", wait_until="domcontentloaded")

            spatial = page.locator('[data-slot="bridgeDlSpatial"]')
            tray = page.locator('[data-slot="bridgeDlTray"]')
            await spatial.wait_for(timeout=6000)

            # The hrefs are filled asynchronously from GET /api/bridges; wait for
            # both to carry the composed release URL.
            await page.wait_for_function(
                """() => {
                  const s = document.querySelector('[data-slot="bridgeDlSpatial"]');
                  const t = document.querySelector('[data-slot="bridgeDlTray"]');
                  return s && t && s.href && t.href
                    && s.href.includes('releases/latest/download/')
                    && t.href.includes('releases/latest/download/');
                }""",
                timeout=8000,
            )

            spatial_href = await spatial.get_attribute("href")
            tray_href = await tray.get_attribute("href")
            assert "releases/latest/download/tapscribe-spacialchat-bridge.zip" in (spatial_href or "")
            assert "releases/latest/download/TapScribe.TrayBridge-win-x64.zip" in (tray_href or "")

            # Static card: exactly one /api/bridges fetch on render.
            assert len(bridges_requests) == 1, (
                f"GET /api/bridges must fire exactly once on render, saw {len(bridges_requests)}"
            )

            # Cross REAL poll ticks (wait on the counter advancing, not on a
            # wall-clock budget) and confirm the card does no further fetching.
            polls_before = await page.evaluate("() => window.__statePolls || 0")
            await page.wait_for_function(
                "(base) => (window.__statePolls || 0) >= base + 2",
                arg=polls_before,
                timeout=15000,
            )
            polls_after = await page.evaluate("() => window.__statePolls || 0")
            assert polls_after >= polls_before + 2, (
                f"no poll crossed the window ({polls_before} → {polls_after}) — the invariant below "
                "would only re-assert its own pre-existing value"
            )
            assert len(bridges_requests) == 1, "the Get-a-bridge card must not re-fetch on a poll tick"
        finally:
            await browser.close()


# Shared layout-reachability probe used by the Settings stack guard AND the
# cross-view sweep below. Returns the granular per-`.work__inner .panel` signals
# both reason over — is the card clipped (an overflow:hidden box shorter than its
# content), is it past the fold, does its OWN body scroll, does an ANCESTOR up to
# the view boundary scroll — and lets each test combine them for its question.
# Structure-independent: it never names the fix classes (`scroll-stack`/`noshrink`).
_PANEL_LAYOUT_JS = """() => {
  const titleOf = p => (p.querySelector('.panel__title')?.textContent || '').trim() || '(untitled panel)';
  const ancestorScrolls = el => {
    for (let n = el.parentElement; n; n = n.parentElement) {
      const oy = getComputedStyle(n).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && n.scrollHeight > n.clientHeight + 1) return true;
      if (n.classList.contains('work__inner')) break;  // view boundary
    }
    return false;
  };
  return [...document.querySelectorAll('.work__inner .panel')].map(p => {
    const body = p.querySelector(':scope > .panel__body');
    return {
      title: titleOf(p),
      clipped: p.scrollHeight > p.clientHeight + 1,
      belowFold: p.getBoundingClientRect().bottom > window.innerHeight + 1,
      bodyScrolls: !!(body && body.scrollHeight > body.clientHeight + 1),
      ancestorScrolls: ancestorScrolls(p),
    };
  });
}"""


async def test_settings_stack_scrolls_without_clipping_cards(running_recorder: RunningRecorder):
    """Operator report: on the Settings page "things were not taking up the
    space they need and no scrollbars" — cards squished, Save buttons gone.

    The editor cards (Models, Live, Summarizer, Batch) are taller than a normal
    laptop viewport. They used to be direct flex children of `.work__inner`, so
    a short viewport pushed free space negative and every card shrank below its
    content; `.panel`'s overflow:hidden then clipped the Save buttons while the
    view never overflowed, so NO scrollbar appeared. The fix wraps them in a
    `.scroll-stack` block scroller: cards keep natural height and the stack
    scrolls. The assertions are deliberately structure-independent (they don't
    name `.scroll-stack`) so they re-fail on the SYMPTOM if a later refactor
    reintroduces the clip — (1) no card is clipped, and (2) when the cards
    exceed the viewport some ancestor scroller actually scrolls, so the last
    card's Save button stays reachable. One short height is enough: the cards
    overflow any normal viewport, so the structural assertions don't vary with
    the exact pixel height (no threshold to sweep)."""
    rr = running_recorder
    height = 650
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": height})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#settings", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="sdSave"]', timeout=6000)

            # Measure once the live + batch cards have rendered (one poll tick).
            rows = await page.evaluate(_PANEL_LAYOUT_JS)
            assert len(rows) >= 4, (
                f"expected the 4 settings cards (Models, Live, Summarizer, Batch), saw {len(rows)}"
            )
            # (1) No card clipped — every control (incl. the Save buttons) renders.
            # This is the primary bug symptom: without the fix the cards squish to
            # fit and overflow:hidden cuts them off, so this fails first and loudly.
            clipped = [r["title"] for r in rows if r["clipped"]]
            assert not clipped, f"at 1440x{height} these cards are clipped (content cut off): {clipped}"
            # Precondition: with the cards at natural height they must overflow this
            # viewport — otherwise (2) passes vacuously without exercising the scroll
            # path. (In the bug state the cards squish to fit, so this stays empty and
            # the clip check above is what fires.)
            below_fold = [r for r in rows if r["belowFold"]]
            assert below_fold, (
                f"at 1440x{height} no card extends past the fold — the test isn't "
                "exercising overflow; lower the viewport height"
            )
            # (2) Any card extending past the viewport must be reachable by a page
            # scroll (some ancestor scrolls).
            unreachable = [r["title"] for r in below_fold if not r["ancestorScrolls"]]
            assert not unreachable, (
                f"at 1440x{height} these cards extend below the fold with NO scrollbar — "
                f"unreachable (the reported bug): {unreachable}"
            )

            # The concrete element that vanished: the Summarizer "Save default"
            # button must be reachable — scrolling it into view lands it FULLY on
            # screen. bounding_box() is None for a non-visible element, so this also
            # subsumes an is_visible() assertion.
            save = page.get_by_test_id("sdSave")
            await save.scroll_into_view_if_needed()
            box = await save.bounding_box()
            assert box and box["y"] >= 0 and box["y"] + box["height"] <= height + 1, (
                f"Save button not fully on-screen after scroll (box={box}, viewport h={height})"
            )
        finally:
            await browser.close()


async def test_no_view_clips_content_without_scroll_path(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    tmp_path: Path,
):
    """Cross-view layout-reachability guard for the Settings/Taps/Capture clip
    class. A view that stacks natural-height `.panel`s in `.work__inner` (a flex
    column; every `.panel` is overflow:hidden) shrinks them under negative free
    space on a short viewport and clips their content with NO scrollbar — the
    boxes shrink to fit, so the column never overflows and nothing scrolls. This
    sweeps EVERY /next view at a short height and fails if any `.panel` is
    clipped (scrollHeight > clientHeight) with no scroll path (neither its own
    body nor any ancestor scrolls).

    Structure-independent on purpose — it never names `.scroll-stack`/`.noshrink`,
    so it re-fails on the SYMPTOM if a refactor reintroduces the clip in ANY view
    (this is what caught Taps + Capture once Settings was fixed). A new stacked
    view that forgets a scroll owner trips it."""
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    # Seed a session WAV + transcript + two archived sessions so the data-driven
    # views render real content; the at-risk stacked views (Settings / Taps /
    # Capture) render their panels regardless.
    fake_transcriber.text_by_speaker["Alice"] = "Seeded reachability-sweep line. " * 8
    src = synth_speech_like_wav(tmp_path / "sweep.wav", seconds=0.8, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=ws_base, identity="alice", name="Alice", wav_path=src, utterance_id="utt-reach-1"
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)
    for sid in ("2024-05-01T10-00-00Z", "2024-05-02T10-00-00Z"):
        d = rec.recordings_dir / sid
        d.mkdir(parents=True)
        synth_speech_like_wav(d / f"{sid}_seed_speaker_00000001.wav", seconds=0.3, freq_hz=220.0)
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        recorded = sorted(rec.session_dir.glob("*.wav"))
        resp = await client.post(
            "/api/transcribe",
            json={"session": rec.session_start, "name": recorded[0].name, "model": "tiny.en"},
        )
        assert resp.status_code == 200, resp.text

    views = ("capture", "recordings", "transcript", "summary", "taps", "sessions", "people", "settings")
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # A short viewport so the stacked at-risk views overflow and would
            # clip if a scroll owner were missing.
            context = await browser.new_context(viewport={"width": 1400, "height": 600})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")
            # Boot done once the spine has rendered the seeded session (proves
            # /api/state + the model catalogs the live/batch cards read have landed).
            await page.wait_for_function(
                f"""() => {{
                  const sel = document.querySelector('[data-slot="sessionPick"]');
                  return sel && Array.from(sel.options).some(o => o.value === {rec.session_start!r});
                }}""",
                timeout=10000,
            )
            offenders = {}
            for v in views:
                await page.evaluate("(x) => window.gotoView(x)", v)
                # Deterministic per-view settle: wait for the view's panels to
                # render (every view has ≥1), not a fixed sleep — the catalogs are
                # already loaded (boot wait above), so the cards lay out at once.
                await page.wait_for_function(
                    "() => document.querySelectorAll('.work__inner .panel').length > 0", timeout=8000
                )
                # A clipped panel whose content is reachable neither by its own body
                # scrolling nor by a page (ancestor) scroll = the bug class.
                rows = await page.evaluate(_PANEL_LAYOUT_JS)
                bad = [
                    r["title"]
                    for r in rows
                    if r["clipped"] and not r["bodyScrolls"] and not r["ancestorScrolls"]
                ]
                if bad:
                    offenders[v] = bad
            assert not offenders, (
                f"at 1400x600 these views clip panel content with NO scroll path "
                f"(the Settings/Taps/Capture bug class): {offenders}"
            )
        finally:
            await browser.close()


async def test_summary_prefills_effective_config_and_saves_session_override(
    running_recorder: RunningRecorder,
):
    """#84: the Summary view's source/prompt read the EFFECTIVE config and
    write the per-session override. (a) With a global default saved, the view
    pre-fills from it. (b) 'save for this session' persists an override that
    survives reload — while Settings still shows the global. (c) 'use global
    default' clears the override and the controls re-seed from the global."""
    rr = running_recorder
    sid = rr.recorder.session_start

    # Global default: Command source + its template + a global prompt.
    import httpx

    async with httpx.AsyncClient(base_url=rr.base_url, timeout=30.0) as client:
        r = await client.put(
            "/api/summarize/config",
            json={"source": "command", "command": "claude -p", "prompt": "GLOBAL PROMPT"},
        )
        assert r.status_code == 200, r.text

    # A transcript so the current session is a normal, selectable session.
    sd = rr.recorder.session_dir
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "session-transcript.json").write_text(
        json.dumps(
            {
                "session": sid,
                "model": "test",
                "transcribed_at": "2026-01-01T00:00:00+00:00",
                "speakers": ["Alice"],
                "segments": [],
                "plain_text": "Alice: we decided to ship.",
            }
        ),
        encoding="utf-8",
    )

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # (a) Pre-fill from the global default (no override saved yet).
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumPrompt"]')?.value === 'GLOBAL PROMPT'""",
                timeout=8000,
            )
            on = await page.get_attribute('button[data-src="command"]', "class")
            assert "is-on" in (on or ""), f"global default source must pre-select, got {on!r}"
            # The command value applies once the catalog fetch settles (the
            # shared controls sequence saved values on it) — wait, don't race it.
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumCmd"]')?.value === 'claude -p'""",
                timeout=8000,
            )

            # (b) Save a per-session override: Local source + a session prompt.
            await page.click('button[data-src="local"]')
            await page.fill('[data-slot="sumPrompt"]', "SESSION PROMPT")
            await page.click('[data-slot="sumSaveSession"]')
            await page.wait_for_function(
                """() => (document.querySelector('[data-slot="sumSaveStatus"]')?.textContent || '') === 'saved'""",
                timeout=8000,
            )

            # Survives reload (pre-fills from the override, not the global).
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumPrompt"]')?.value === 'SESSION PROMPT'""",
                timeout=8000,
            )
            on = await page.get_attribute('button[data-src="local"]', "class")
            assert "is-on" in (on or ""), f"override source must pre-select after reload, got {on!r}"
            note = await page.locator('[data-slot="sumOverrideNote"]').text_content()
            assert "override" in (note or ""), f"override indicator must show, got {note!r}"

            # …while Settings still shows the GLOBAL default (the card edits
            # the global, not the session). The hash is only read at boot, so
            # cross-view navigation needs a reload.
            await page.goto(rr.base_url + "/#settings", wait_until="domcontentloaded")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sdPrompt"]')?.value === 'GLOBAL PROMPT'""",
                timeout=8000,
            )

            # (c) Clear the override — the controls re-seed from the global.
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumPrompt"]')?.value === 'SESSION PROMPT'""",
                timeout=8000,
            )
            await page.click('[data-slot="sumUseDefault"]')
            await page.wait_for_function(
                """() => document.querySelector('[data-slot="sumPrompt"]')?.value === 'GLOBAL PROMPT'""",
                timeout=8000,
            )
            meta = json.loads(
                await (await context.request.get(rr.base_url + f"/api/session-meta/{sid}")).text()
            )
            assert meta.get("summary_source", "") == ""
            assert meta.get("summary_prompt", "") == ""
        finally:
            await browser.close()


async def test_renderregion_sig_audit_finds_no_drift(running_recorder: RunningRecorder):
    """Every renderRegion call carries a `sig` that gates whether the build
    closure re-runs. If the sig is missing a value the build actually reads,
    the region silently goes stale (#94). The guard in templates.js lets the
    audit run opt-in via globalThis.__TAPSCRIBE_SIG_AUDIT — when set, every
    skipped rebuild re-invokes build() into a detached probe and pushes any
    mismatch to globalThis.__TAPSCRIBE_SIG_DRIFT.

    `renderList` (keyed lists) records into the same array from two probes of its
    own — the list-level one (do the rows on screen still match what `items`
    would produce?) and the per-row one (does `update` write anything `itemSig`
    doesn't name?).

    This test turns the audit on, exercises real views across several poll
    cycles, and asserts zero drift was recorded: any future sig-drift will
    trip it as soon as a region's build output changes without its sig changing.

    It ALSO asserts the probes actually FIRED. Zero drift over empty views is
    vacuous, and that is exactly what this test used to be for the keyed lists:
    it seeded no sessions, so the Sessions rows and the Transcript picker had no
    rows to probe and the row audit never ran once. The seeding below and the
    per-kind probe assertions are what make "no drift" mean
    something."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    # Two sessions with WAVs: gives the Sessions view real rows (and a non-empty
    # absorb-target set) and the Recordings/Transcript views a real WAV list.
    for sid in ("2025-03-01T09-00-00Z", "2025-03-02T09-00-00Z"):
        _seed_multi_wav_session(rec, sid, n=2)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")

            # Wait for first paint + spine rendering.
            await page.wait_for_function(
                "() => document.querySelector('[data-slot=\"sessionPick\"]')",
                timeout=10000,
            )
            # Turn audit on AFTER the initial render so the first paint (no
            # prior sig remembered) doesn't produce false positives.
            await page.evaluate(
                "() => { window.__TAPSCRIBE_SIG_AUDIT = true; window.__TAPSCRIBE_SIG_DRIFT = []; "
                "window.__TAPSCRIBE_SIG_PROBES = { region: 0, list: 0, row: 0 }; }"
            )

            # Drive through each view TWICE, because the audit only probes a
            # render that was SKIPPED, and an idle tab produces none: main.js's
            # tick() returns before renderAll when /api/state 304s unchanged, so
            # waiting out poll periods on a quiet tab re-renders nothing and the
            # probes never run (which is how this test passed vacuously). gotoView
            # calls renderAll synchronously from the cached state, so a second
            # visit re-renders with IDENTICAL state — every sig-gated region and
            # keyed list skips, and each skip is what gets probed. A poll period
            # is still crossed for realism.
            for view in _NEXT_VIEWS:
                await page.evaluate("(v) => window.gotoView(v)", view)
                await page.wait_for_function(
                    "() => document.querySelector('#viewRoot')?.childElementCount > 0",
                    timeout=5000,
                )
                # A second pass over identical state is what makes every sig-gated
                # region and keyed list SKIP, and a skip is what gets probed.
                await _force_render_passes(page, view, 1)

            # Each probe KIND must have run. A single total is not enough: the
            # keyed-list probes alone satisfied `probes > 0` while the renderRegion
            # half went entirely unexercised, and the LIST probe satisfied it while
            # no row was ever probed — so a real drift in either unprobed half
            # would have shipped green under a passing anti-vacuity assertion.
            kinds = await page.evaluate(
                "() => window.__TAPSCRIBE_SIG_PROBES || { region: 0, list: 0, row: 0 }"
            )
            for kind, n in kinds.items():
                assert n > 0, (
                    f"the sig audit never ran a {kind} probe ({kinds}) — 'no drift' says nothing "
                    f"about {kind} signatures. Seed state those regions/rows actually render, and "
                    "make sure a SKIPPED render happens (an unchanged 304 renders nothing at all)."
                )

            # Assert no drift was recorded across any view.
            drift = await page.evaluate("() => (window.__TAPSCRIBE_SIG_DRIFT || []).length")
            assert drift == 0, (
                f"renderRegion sig audit found {drift} region(s) where build output changed "
                f"but the sig did not — those regions will go stale. Inspect "
                "window.__TAPSCRIBE_SIG_DRIFT in the browser for details."
            )
        finally:
            await browser.close()


def _seed_named_session(rec, sid: str, *, speaker: str) -> Path:
    """A non-current on-disk session with one WAV + a merged transcript whose
    sole speaker is `speaker` — so the People view derives exactly ONE
    participant keyed by that speaker slug (the WAV's filename slug is made to
    match the transcript speaker, so files[].speaker_name and the transcript's
    speakers[] collapse to a single row rather than two)."""
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    # Recorder filename: <iso>_<speakerSlug>_<ident>_<uuid8>.wav. parse_wav_speaker_slug
    # = parts[1:-2], so a single-token slug round-trips to exactly `speaker`.
    synth_speech_like_wav(d / f"{sid}_{speaker}_{speaker.lower()}_0000aaaa.wav", seconds=0.4, freq_hz=200.0)
    (d / "session-transcript.json").write_text(
        json.dumps(
            {
                "transcribed_at": "2025-03-01T10:00:00+00:00",
                "segments": [
                    {
                        "speaker": speaker,
                        "text": f"{speaker} said the quick brown fox.",
                        "abs_start": "2025-03-01T09:00:00+00:00",
                    },
                ],
                "speakers": [speaker],
                "speaking_seconds": {speaker: 12.0},
                "suppressed": [],
                "suppressed_count": 0,
                "wav_count": 1,
                "transcribe_ms": 1000,
                "model": "tiny.en",
                "backend": "fake",
                "device": "cpu",
            }
        ),
        encoding="utf-8",
    )
    return d


async def _force_render_passes(page, view: str, n: int = 3) -> int:
    """Drive `n` REAL render passes of `view` and return how many actually ran.

    Crossing poll ticks renders nothing: main.js's tick() returns before renderAll
    when /api/state 304s unchanged, so a test that waits out idle polls and then
    asserts "nothing was touched" cannot fail. gotoView re-renders synchronously
    from the cached state, so each call is a full pass whose only correct outcome
    is that the sig gates skipped. Asserts the counter moved, so the caller's
    assertion can never be vacuous.
    """
    before = await page.evaluate("() => window.__TAPSCRIBE_RENDER_ALL_COUNT || 0")
    await page.evaluate("([v, n]) => { for (let i = 0; i < n; i++) window.gotoView(v); }", [view, n])
    ran = await page.evaluate("() => window.__TAPSCRIBE_RENDER_ALL_COUNT || 0") - before
    assert ran >= n, (
        f"expected >= {n} forced render passes of {view!r}, got {ran} — the assertion "
        "that follows would be vacuous"
    )
    return ran


async def _pick_session(page, sid: str) -> None:
    """Select `sid` in the spine picker, waiting for its option to land first.

    The ONE spelling of this — the raw select-and-dispatch was drifting into
    several near-copies, and only some of them waited for the option, so the
    flake fix covered half the call sites.
    """
    await page.wait_for_function(
        """(sid) => {
            const s = document.querySelector('[data-slot="sessionPick"]');
            return !!s && Array.from(s.options).some((o) => o.value === sid);
        }""",
        arg=sid,
        timeout=10000,
    )
    await page.evaluate(
        """(sid) => {
            const s = document.querySelector('[data-slot="sessionPick"]');
            s.value = sid;
            s.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        sid,
    )


async def _focus_session_view(page, sid: str, view: str) -> None:
    """Pick `sid` in the spine, then open one of the Stages `view`s for it —
    the spine-select + gotoView pattern several tests share."""
    await _pick_session(page, sid)
    await page.evaluate("(v) => window.gotoView(v)", view)


async def test_people_view_registry_auto_binds_renames_merges_detaches(
    running_recorder: RunningRecorder,
):
    """The People view as the canonical cross-session registry (ADR-0009), end
    to end:

    1. Auto-bind — every recorded speaker appears in the GLOBAL registry with NO
       naming step (the "nothing until I save it" complaint is gone): two seeded
       sessions surface two People keyed on their device identity tokens.
    2. Rename — typing a name PUTs /api/people/{id}; it persists to people.json
       (GET /api/people confirms — not just optimistic UI) AND propagates to the
       session's server-resolved speaker map (`/api/state` session.names).
    3. Merge — folding one Person into another (the "map them to things" hatch)
       leaves ONE row owning both identity tokens.
    4. Detach — pulls an identity back out into its own Person (the undo).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url

    s_alice = "2025-03-01T10-00-00Z"
    s_bob = "2025-03-02T10-00-00Z"
    _seed_named_session(rec, s_alice, speaker="Alice")
    _seed_named_session(rec, s_bob, speaker="Bob")

    row = '#viewRoot [data-slot="people"] .pregrow'
    # JS expression → [{name, ids[]}] for every registry row. `name` reads the
    # input value, falling back to its placeholder (an unnamed Person shows its
    # default name only as the placeholder).
    rows_js = (
        "Array.from(document.querySelectorAll('" + row + "')).map((r) => ({"
        "  name: r.querySelector('[data-slot=\"name\"]').value || r.querySelector('[data-slot=\"name\"]').placeholder,"
        "  ids: Array.from(r.querySelectorAll('[data-slot=\"tok\"]')).map((t) => t.textContent),"
        "}))"
    )

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")
            await page.wait_for_function(
                """(ids) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    if (!s) return false;
                    const have = new Set(Array.from(s.options).map((o) => o.value));
                    return ids.every((id) => have.has(id));
                }""",
                arg=[s_alice, s_bob],
                timeout=10000,
            )
            await page.evaluate("() => window.gotoView('people')")

            # 1. Auto-bind: BOTH recorded speakers in the GLOBAL list, no naming.
            await page.wait_for_function(
                f"() => {{ const rows = {rows_js}; return rows.length === 2 "
                "&& rows.some((r) => r.ids.includes('Alice')) "
                "&& rows.some((r) => r.ids.includes('Bob')); }",
                timeout=8000,
            )

            # 2. Rename Alice's row → persists to people.json + propagates.
            alice_row = page.locator(row, has=page.locator('[data-slot="tok"]', has_text="Alice"))
            await alice_row.locator('[data-slot="name"]').fill("Alice Anderson")
            await alice_row.locator('[data-slot="name"]').blur()
            async with httpx.AsyncClient(base_url=base) as client:
                people = []
                for _ in range(40):
                    people = (await client.get("/api/people")).json()["people"]
                    if any(
                        p["name"] == "Alice Anderson" and p["named"] and p["identities"] == ["Alice"]
                        for p in people
                    ):
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise AssertionError(f"rename did not persist to people.json: {people}")
                state = (await client.get("/api/state")).json()
            sess = next(s for s in state["sessions"] if s["session"] == s_alice)
            assert sess["names"]["Alice"] == "Alice Anderson", sess.get("names")

            # 3. Merge Bob INTO Alice Anderson → one Person owns both identities.
            # Wait until the browser has re-polled and rebuilt the merge pickers
            # with Alice's NEW name before selecting it (the option text tracks
            # the live registry, not the value typed a moment ago).
            await page.wait_for_function(
                """() => Array.from(document.querySelectorAll('#viewRoot [data-slot="people"] option'))
                    .some((o) => o.textContent === 'Alice Anderson')""",
                timeout=8000,
            )
            bob_row = page.locator(row, has=page.locator('[data-slot="tok"]', has_text="Bob"))
            await bob_row.locator('[data-slot="merge"]').select_option(label="Alice Anderson")
            await page.wait_for_function(
                f"() => {{ const rows = {rows_js}; return rows.length === 1 "
                "&& rows[0].ids.includes('Alice') && rows[0].ids.includes('Bob'); }",
                timeout=8000,
            )

            # 4. Detach Bob back out → two People again.
            await (
                page.locator(".idchip", has=page.locator('[data-slot="tok"]', has_text="Bob"))
                .locator('[data-slot="detach"]')
                .click()
            )
            await page.wait_for_function(
                f"() => {{ const rows = {rows_js}; return rows.length === 2; }}",
                timeout=8000,
            )
        finally:
            await browser.close()


async def test_capture_per_session_prompt_and_hotwords_overrides_save_independently(
    running_recorder: RunningRecorder,
):
    """The Capture view's per-session prompt + hotwords override editors, each
    saving via a partial PUT /api/session-meta/{s} ({prompt} OR {hotwords}),
    untested until now. Saving the prompt then the hotwords must leave BOTH
    stored — the partial merge must not let the second save clobber the first
    (the bug a regressed overwrite-merge would introduce) — and a reload
    re-seeds both fields from the saved overrides."""
    rec = running_recorder.recorder
    base = running_recorder.base_url

    # An on-disk session to attach overrides to (the live current session has no
    # folder until it records, so session-meta PUTs to it would 404).
    sid = "2025-06-01T10-00-00Z"
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    synth_speech_like_wav(d / f"{sid}_Erin_erin_0000eeee.wav", seconds=0.4, freq_hz=205.0)

    async def save_and_wait(field: str, save: str, status: str, value: str) -> None:
        await page.locator(f'#viewRoot [data-slot="{field}"]').fill(value)
        await page.locator(f'#viewRoot [data-slot="{save}"]').click()
        await page.wait_for_function(
            f"""() => document.querySelector('#viewRoot [data-slot="{status}"]')?.textContent === 'saved'""",
            timeout=8000,
        )

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="sessionPick"]', timeout=10000)
            await _focus_session_view(page, sid, "capture")

            # The override editors enable once a session is focused.
            await page.locator('#viewRoot [data-slot="capPrompt"]').wait_for(state="visible", timeout=8000)
            await page.wait_for_function(
                """() => {
                    const t = document.querySelector('#viewRoot [data-slot="capPrompt"]');
                    return t && !t.disabled;
                }""",
                timeout=8000,
            )

            await save_and_wait("capPrompt", "capPromptSave", "capPromptStatus", "Discuss the Q3 roadmap.")
            await save_and_wait(
                "capHotwords", "capHotwordsSave", "capHotwordsStatus", "Acme Inc., Patricia Lin"
            )

            # Both overrides persisted — the second (hotwords) save did NOT wipe
            # the first (prompt). Partial-meta merge invariant.
            async with httpx.AsyncClient(base_url=base) as client:
                meta = (await client.get(f"/api/session-meta/{sid}")).json()
            assert meta.get("prompt") == "Discuss the Q3 roadmap.", meta
            assert meta.get("hotwords") == "Acme Inc., Patricia Lin", meta

            # A fresh load re-seeds both editors from the saved overrides.
            await page.goto(base, wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="sessionPick"]', timeout=10000)
            await _focus_session_view(page, sid, "capture")
            await page.wait_for_function(
                """() => {
                    const p = document.querySelector('#viewRoot [data-slot="capPrompt"]');
                    const h = document.querySelector('#viewRoot [data-slot="capHotwords"]');
                    return p && h && p.value === 'Discuss the Q3 roadmap.'
                        && h.value === 'Acme Inc., Patricia Lin';
                }""",
                timeout=8000,
            )
        finally:
            await browser.close()


async def test_capture_per_session_languages_override_saves_and_reseeds(
    running_recorder: RunningRecorder,
):
    """The Capture view's per-meeting candidate-language picker (ADR-0010): a
    multi-select over the catalog languages saving via PUT /api/session-meta/{s}
    {languages}. Selecting da+no and saving persists the override (and preserves
    a pre-existing prompt — partial-merge invariant), and a reload re-seeds the
    selection from the saved override."""
    rec = running_recorder.recorder
    base = running_recorder.base_url

    sid = "2025-06-02T10-00-00Z"
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    (d / "session-meta.json").write_text(json.dumps({"prompt": "keep me"}), encoding="utf-8")
    synth_speech_like_wav(d / f"{sid}_Finn_finn_0000ffff.wav", seconds=0.4, freq_hz=215.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="sessionPick"]', timeout=10000)
            await _focus_session_view(page, sid, "capture")

            # The picker enables + fills its options once a session is focused.
            await page.wait_for_function(
                """() => {
                    const s = document.querySelector('#viewRoot [data-slot="capLanguages"]');
                    return s && !s.disabled && s.options.length > 0;
                }""",
                timeout=8000,
            )
            await page.select_option('#viewRoot [data-slot="capLanguages"]', ["da", "no"])
            await page.locator('#viewRoot [data-slot="capLanguagesSave"]').click()
            await page.wait_for_function(
                """() => document.querySelector('#viewRoot [data-slot="capLanguagesStatus"]')?.textContent === 'saved'""",
                timeout=8000,
            )

            # Persisted as a list, and the pre-existing prompt survived the
            # partial PUT.
            async with httpx.AsyncClient(base_url=base) as client:
                meta = (await client.get(f"/api/session-meta/{sid}")).json()
            assert meta.get("languages") == ["da", "no"], meta
            assert meta.get("prompt") == "keep me", meta

            # A fresh load re-seeds exactly the saved override.
            await page.goto(base, wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="sessionPick"]', timeout=10000)
            await _focus_session_view(page, sid, "capture")
            await page.wait_for_function(
                """() => {
                    const s = document.querySelector('#viewRoot [data-slot="capLanguages"]');
                    if (!s || !s.options.length) return false;
                    const sel = Array.from(s.selectedOptions).map(o => o.value).sort().join(',');
                    return sel === 'da,no';
                }""",
                timeout=8000,
            )
        finally:
            await browser.close()


async def test_settings_default_languages_picker_saves_global_default(
    running_recorder: RunningRecorder,
):
    """The Settings → Batch engine card's default-languages picker (ADR-0010):
    a multi-select that persists the global candidate-language default via PUT
    /api/config/languages and is reflected back in /api/state."""
    base = running_recorder.base_url

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#settings", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="sessionPick"]', timeout=10000)

            await page.wait_for_function(
                """() => {
                    const s = document.querySelector('#viewRoot [data-slot="langSel"]');
                    return s && s.options.length > 0;
                }""",
                timeout=8000,
            )
            await page.select_option('#viewRoot [data-slot="langSel"]', ["da", "en"])
            await page.locator('#viewRoot [data-slot="langSave"]').click()
            await page.wait_for_function(
                """() => document.querySelector('#viewRoot [data-slot="langStatus"]')?.textContent === 'saved'""",
                timeout=8000,
            )

            async with httpx.AsyncClient(base_url=base) as client:
                state = (await client.get("/api/state")).json()
            assert state["languages"]["default"] == ["da", "en"], state["languages"]
        finally:
            await browser.close()


async def test_sessions_view_inline_label_rename_persists(
    running_recorder: RunningRecorder,
):
    """The Sessions view's inline rename (debounced optimistic PUT
    /api/session-meta/{s} { label }), untested until now. Typing a label into a
    row's rename field flips its status to "saved", the row's display label
    follows immediately, the server stores it (GET /api/session-meta), and a
    fresh load re-seeds the field from the saved label (not just optimistic
    state). The partial { label } PUT must not disturb the session's aliases."""
    rec = running_recorder.recorder
    base = running_recorder.base_url

    sid = "2025-05-01T10-00-00Z"
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    synth_speech_like_wav(d / f"{sid}_Dana_dana_0000dddd.wav", seconds=0.4, freq_hz=210.0)
    # Pre-seed an alias so we can prove the partial { label } PUT preserves it.
    (d / "session-meta.json").write_text(json.dumps({"aliases": {"Dana": "Dana Scully"}}), encoding="utf-8")

    row = f'#viewRoot .sessrow[data-sid="{sid}"]'

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")
            await page.locator(row).wait_for(state="visible", timeout=10000)

            await page.locator(f'{row} [data-slot="rename"]').fill("Quarterly Planning")
            await page.wait_for_function(
                f"""() => {{
                    const el = document.querySelector('{row} [data-slot="renameStatus"]');
                    return !!el && el.textContent === 'saved';
                }}""",
                timeout=8000,
            )
            # The row's display label follows the typed value.
            assert (
                await page.locator(f'{row} [data-slot="label"]').inner_text()
            ).strip() == "Quarterly Planning"

            # Server stored the label AND preserved the pre-existing alias (partial merge).
            async with httpx.AsyncClient(base_url=base) as client:
                meta = (await client.get(f"/api/session-meta/{sid}")).json()
            assert meta.get("label") == "Quarterly Planning", meta
            assert meta.get("aliases", {}).get("Dana") == "Dana Scully", (
                "a partial { label } PUT must not drop existing aliases"
            )

            # Persistence: a fresh load re-seeds the rename field from the saved label.
            await page.goto(base + "/#sessions", wait_until="domcontentloaded")
            await page.wait_for_function(
                f"""() => {{
                    const i = document.querySelector('{row} [data-slot="rename"]');
                    return !!i && i.value === 'Quarterly Planning';
                }}""",
                timeout=10000,
            )
        finally:
            await browser.close()


async def test_capture_recording_toggle_and_clear_captions(
    running_recorder: RunningRecorder,
):
    """The Capture view's two action controls, untested at the UI level:

    - the recording pill (● recording / ⏸ paused) → POST /api/recording/toggle.
      Clicking it flips recorder.recording_enabled (verified through
      /api/state), and the pill's label + state class follow on the next poll.
    - the "clear" captions button → DELETE /api/live-transcript. With a live
      caption seeded into the global deque, the feed shows it (count 1); after
      Clear the deque is wiped and the feed repaints empty (count 0).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    current = rec.session_start

    rec.transcripts.append(
        {
            "ts": "2026-01-01T00:00:01+00:00",
            "identity": "live",
            "name": "Live",
            "text": "CLEARME caption text",
            "session": current,
        }
    )

    rec_pill = '#viewRoot [data-slot="recPill"]'
    feed_count = '#viewRoot [data-slot="liveFeedCount"]'

    async def state_recording() -> bool:
        async with httpx.AsyncClient(base_url=base) as client:
            return (await client.get("/api/state")).json().get("recording_enabled")

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#capture", wait_until="domcontentloaded")

            # --- Recording pill: starts armed, toggles to paused and back. ---
            await page.locator(rec_pill).wait_for(state="visible", timeout=8000)
            await page.wait_for_function(
                f"""() => {{
                    const b = document.querySelector('{rec_pill}');
                    return b && b.classList.contains('is-on') && /recording/.test(b.textContent);
                }}""",
                timeout=8000,
            )
            assert await state_recording() is True

            await page.locator(rec_pill).click()
            await page.wait_for_function(
                f"""() => {{
                    const b = document.querySelector('{rec_pill}');
                    return b && b.classList.contains('is-paused') && /paused/.test(b.textContent);
                }}""",
                timeout=8000,
            )
            assert await state_recording() is False, "toggle must flip recording_enabled off"

            await page.locator(rec_pill).click()
            await page.wait_for_function(
                f"""() => document.querySelector('{rec_pill}')?.classList.contains('is-on')""",
                timeout=8000,
            )
            assert await state_recording() is True, "a second toggle must re-arm recording"

            # --- Clear captions: the seeded line shows, then Clear wipes it. ---
            await page.wait_for_function(
                f"""() => document.querySelector('{feed_count}')?.textContent === '1'""",
                timeout=8000,
            )
            await page.locator('#viewRoot [data-slot="liveClear"]').click()
            await page.wait_for_function(
                f"""() => document.querySelector('{feed_count}')?.textContent === '0'""",
                timeout=8000,
            )
            assert rec.transcripts.snapshot() == [], "clear must wipe the live-caption deque server-side"
        finally:
            await browser.close()


async def test_taps_toggle_and_recording_pill_wired_in_both_hosts(
    running_recorder: RunningRecorder,
    tmp_path: Path,
):
    """Issue #253: the `.tap-toggle` click delegation used to be copy-pasted
    onto TWO separate DOM hosts (the global rail in main.js + the Taps view's
    own row list in taps.js), and the recording pill's click handler was
    copy-pasted across Capture + Taps. Neither was ever driven by a UI-level
    test — this pins the consolidated `activeTaps.wireToggles` /
    `wireRecPill` helpers actually work on every host they're bound to.

    A single streamed tap (identity "alice") stays open long enough to
    interact with. `/api/state`'s `active[].record`/`.live` mirror the LIVE
    per-identity preference on every poll (app.py `api_state`), not a
    connection-open-time snapshot, so a toggle click's effect is visible via
    a fresh GET even while the WS is still streaming.
    """
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url
    # Long enough that the tap is still open for every click below (5 click +
    # backend-confirm round trips plus a view switch), short enough not to
    # drag the test out — same sizing rationale as the two-tap screenshot test
    # above, which does more (6.0s for two concurrent streams + a caption
    # exchange) and finds 6.0s comfortable margin.
    wav = synth_speech_like_wav(tmp_path / "alice.wav", seconds=6.0, freq_hz=220.0)

    async def active_alice() -> dict | None:
        async with httpx.AsyncClient(base_url=base) as client:
            state = (await client.get("/api/state")).json()
        return next((a for a in state.get("active", []) if a.get("identity") == "alice"), None)

    async def alice_field_is(field: str, value: bool) -> bool:
        a = await active_alice()
        return bool(a) and a[field] is value

    async def recording_enabled() -> bool:
        async with httpx.AsyncClient(base_url=base) as client:
            return (await client.get("/api/state")).json().get("recording_enabled")

    async def recording_is(value: bool) -> bool:
        return await recording_enabled() is value

    rail_toggle = '#tapsRailBody .tap-toggle[data-toggle="record"]'
    taps_body_toggle = '#viewRoot [data-slot="activeTapsBody"] .tap-toggle[data-toggle="live"]'
    taps_rec_pill = '#viewRoot [data-slot="recPill"]'

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#capture", wait_until="domcontentloaded")

            alice_task = asyncio.create_task(
                stream_wav_via_tap(
                    ws_base_url=ws_base,
                    identity="alice",
                    name="Alice",
                    wav_path=wav,
                    utterance_id="utt-toggle-ui",
                    frame_interval_s=0.02,
                )
            )
            try:
                # --- Rail host (main.js): record toggle, on the default view. ---
                await page.wait_for_selector(rail_toggle, timeout=8000)
                assert (await active_alice())["record"] is True, "record defaults on"

                await page.locator(rail_toggle).click()
                await page.wait_for_function(
                    f"""() => !document.querySelector('{rail_toggle}')?.classList.contains('on')""",
                    timeout=8000,
                )
                assert await wait_until(lambda: alice_field_is("record", False), timeout=5.0), (
                    "rail toggle click must PUT /api/tap-settings and flip record off"
                )

                await page.locator(rail_toggle).click()
                await page.wait_for_function(
                    f"""() => document.querySelector('{rail_toggle}')?.classList.contains('on')""",
                    timeout=8000,
                )
                assert await wait_until(lambda: alice_field_is("record", True), timeout=5.0), (
                    "a second rail toggle click must re-arm record"
                )

                # --- Taps view's own host (taps.js): a DIFFERENT DOM host than
                # the rail, per active-taps.js's per-host WeakMap render state.
                # window.gotoView is the app's own client-side router (exposed
                # for automation) — a plain hash-only page.goto would NOT switch
                # the mounted view, since main.js resolves the hash once at load
                # and never wires a hashchange listener.
                await page.evaluate("() => window.gotoView('taps')")
                await page.wait_for_selector(taps_body_toggle, timeout=8000)

                await page.locator(taps_body_toggle).click()
                await page.wait_for_function(
                    f"""() => !document.querySelector('{taps_body_toggle}')?.classList.contains('on')""",
                    timeout=8000,
                )
                assert await wait_until(lambda: alice_field_is("live", False), timeout=5.0), (
                    "the Taps view's OWN toggle host must also PUT /api/tap-settings"
                )

                # --- Taps view's recording pill (shell.js wireRecPill), the
                # other duplicated-then-shared handler from the same issue.
                assert await recording_enabled() is True
                await page.locator(taps_rec_pill).click()
                await page.wait_for_function(
                    f"""() => document.querySelector('{taps_rec_pill}')?.classList.contains('is-paused')""",
                    timeout=8000,
                )
                assert await wait_until(lambda: recording_is(False), timeout=5.0), (
                    "Taps recPill click must pause recording"
                )

                await page.locator(taps_rec_pill).click()
                await page.wait_for_function(
                    f"""() => document.querySelector('{taps_rec_pill}')?.classList.contains('is-on')""",
                    timeout=8000,
                )
                assert await wait_until(lambda: recording_is(True), timeout=5.0), (
                    "a second Taps recPill click must re-arm recording"
                )
            finally:
                await alice_task
        finally:
            await browser.close()


async def test_recordings_delete_single_wav_removes_row_and_file(
    running_recorder: RunningRecorder,
):
    """The Recordings view's per-WAV delete (🗑 → DELETE /api/wav/{s}/{name}),
    untested until now. On a NON-current session the row exposes a delete
    button; clicking it (confirm auto-accepted) removes the row, deletes the
    file from disk, and leaves the session's OTHER WAV untouched. Also pins the
    safety wiring: the CURRENT session's rows must NOT offer delete (the server
    refuses it with 409, so the button is removed there)."""
    rec = running_recorder.recorder
    base = running_recorder.base_url

    sid = "2025-04-01T10-00-00Z"
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    keep = f"{sid}_Alice_alice_0000aaaa.wav"
    drop = f"{sid}_Bob_bob_0000bbbb.wav"
    synth_speech_like_wav(d / keep, seconds=0.4, freq_hz=200.0)
    synth_speech_like_wav(d / drop, seconds=0.4, freq_hz=260.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            page.on("dialog", lambda dlg: asyncio.create_task(dlg.accept()))
            await page.goto(base, wait_until="domcontentloaded")

            await page.wait_for_function(
                """(id) => {
                    const s = document.querySelector('[data-slot="sessionPick"]');
                    return !!s && Array.from(s.options).some((o) => o.value === id);
                }""",
                arg=sid,
                timeout=10000,
            )

            # Focus the archived session; both WAV rows render with a delete button.
            await _focus_session_view(page, sid, "recordings")
            for name in (keep, drop):
                await page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{name}"]').wait_for(
                    state="visible", timeout=8000
                )
            assert (
                await page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{drop}"] [data-wav-delete]').count()
                == 1
            ), "an archived session's WAV row must offer delete"

            # Delete the Bob WAV; its row disappears and the file leaves disk.
            await page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{drop}"] [data-wav-delete]').click()
            await page.wait_for_function(
                """(name) => !document.querySelector(
                    `#viewRoot .wavlist .wavrow[data-wav="${name}"]`)""",
                arg=drop,
                timeout=8000,
            )
            assert not (d / drop).exists(), "delete must remove the WAV from disk"
            assert (d / keep).exists(), "deleting one WAV must not touch its sibling"
            assert await page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{keep}"]').count() == 1, (
                "the sibling row must remain"
            )

            # Safety: the CURRENT (live) session's rows must not offer delete. Seed
            # a WAV into the current session and focus it.
            cur = rec.session_start
            cdir = rec.recordings_dir / cur
            cdir.mkdir(parents=True, exist_ok=True)
            cur_wav = f"{cur}_Carol_carol_0000cccc.wav"
            synth_speech_like_wav(cdir / cur_wav, seconds=0.4, freq_hz=300.0)
            await _focus_session_view(page, cur, "recordings")
            await page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{cur_wav}"]').wait_for(
                state="visible", timeout=8000
            )
            assert (
                await page.locator(
                    f'#viewRoot .wavlist .wavrow[data-wav="{cur_wav}"] [data-wav-delete]'
                ).count()
                == 0
            ), "the current session's WAV row must not offer delete"
        finally:
            await browser.close()


async def test_transcript_single_wav_transcribe_marks_row_done(
    running_recorder: RunningRecorder, fake_transcriber: FakeTranscriber, tmp_path: Path
):
    """The Transcript view's per-WAV "transcribe" button (POST /api/transcribe
    via txOneBtn), untested until now (existing tests cover session-range and
    the source toggle, never the single-WAV path). Streaming one WAV, the
    picker shows it with "no tx"; clicking transcribe runs the FakeTranscriber,
    the row's tag flips to "✓ tx", the button relabels to "re-transcribe", and
    the per-WAV cached transcript holds the scripted text."""
    rr = running_recorder
    wav = synth_speech_like_wav(tmp_path / "alice.wav", seconds=1.0, freq_hz=200.0)
    await stream_wav_via_tap(
        ws_base_url=rr.ws_base_url, identity="alice", name="Alice", wav_path=wav, utterance_id="utt-a"
    )
    assert await wait_until(lambda: streams_drained(rr.recorder), timeout=12.0)
    wav_name = sorted((rr.recorder.recordings_dir / rr.recorder.session_start).glob("*.wav"))[0].name

    tx_tag = '#viewRoot [data-slot="wavList"] .wavrow [data-slot="txTag"]'
    tx_one = '#viewRoot [data-slot="txOneBtn"]'

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#transcript", wait_until="domcontentloaded")

            await page.locator(tx_tag).first.wait_for(state="visible", timeout=8000)
            assert (await page.locator(tx_tag).first.inner_text()).strip() == "no tx"
            # The default selection (files[0]) enables the single-transcribe button.
            await page.wait_for_function(
                f"""() => {{ const b = document.querySelector('{tx_one}');
                    return b && !b.disabled && /transcribe/.test(b.textContent); }}""",
                timeout=8000,
            )

            await page.locator(tx_one).click()
            # The row's tag flips to done and the button relabels to re-transcribe.
            await page.wait_for_function(
                f"""() => document.querySelector('{tx_tag}')?.textContent.includes('✓')""",
                timeout=10000,
            )
            await page.wait_for_function(
                f"""() => /re-transcribe/.test(document.querySelector('{tx_one}')?.textContent || '')""",
                timeout=8000,
            )
            # The transcriber ran against the recorder's own WAV copy (`wav_name`),
            # not the source we streamed in — assert on that name.
            assert any(p.name == wav_name for p in fake_transcriber.calls), (
                f"the single-WAV transcribe must call the transcriber for {wav_name}: {fake_transcriber.calls}"
            )

            # Server-side: the per-WAV cache holds the scripted text.
            async with httpx.AsyncClient(base_url=rr.base_url) as client:
                tx = (await client.get(f"/api/wav/{rr.recorder.session_start}/{wav_name}/transcript")).json()
            assert ALICE_TEXT in json.dumps(tx), tx
        finally:
            await browser.close()


async def test_get_by_test_id_is_wired_to_data_slot() -> None:
    """Pin the repo's e2e selector convention.

    `playwright_session()` points Playwright's test-id attribute at `data-slot`
    — the native `data-*` marker the dashboard templates already bind through
    (`slot()`/`pick()` in `web/js/templates.js`) — so `page.get_by_test_id("x")`
    resolves `[data-slot="x"]`. This test is hermetic (no server): it sets its
    own DOM, so it asserts the *wiring*, not any view. A Playwright/Chromium
    bump or a stray `set_test_id_attribute` that reverted the convention turns
    this red instead of silently breaking every `get_by_test_id` in the suite.
    """
    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content('<button data-slot="startMeeting">Start</button>')

            by_test_id = page.get_by_test_id("startMeeting")
            assert await by_test_id.count() == 1, "get_by_test_id must resolve [data-slot=…]"
            assert (await by_test_id.text_content()) == "Start"
            # Resolves the same node the suite's existing CSS-attribute locators target.
            assert await page.locator('[data-slot="startMeeting"]').count() == 1
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Playback (#191) — the Player is shell-owned and outside the tick.
# ---------------------------------------------------------------------------


def _seed_wav_session(rec, sid: str, *, names: list[str], seconds: float = 1.0) -> Path:
    """A non-current on-disk session holding real, decodable WAVs.

    `seconds` matters when a test seeks mid-file: the browser clamps a seek to
    the media duration, so a 1 s WAV can't prove a 3 s offset landed.
    """
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    for n in names:
        synth_speech_like_wav(d / n, seconds=seconds, freq_hz=220.0)
    return d


async def _focus_session(page, sid: str, *, stage: str | None = None) -> None:
    """Pin the spine to a seeded session (the recorder's own current session
    also lists, and the spine focuses that by default).

    Selecting a session reuses the spine's session-switch, which ROUTES into
    Capture/Transcript — so a caller that needs a specific stage passes it and
    navigates there afterwards.
    """
    await _pick_session(page, sid)
    if stage:
        await _goto_stage(page, stage)


async def _goto_stage(page, stage: str) -> None:
    """Switch stages the way an operator does: click the spine's nav item.

    Writing `location.hash` does NOT work — `viewFromHash()` runs once at boot
    and there is no hashchange listener, so the hash is an output of navigation
    (`syncHash`), not an input to it. A test that sets the hash asserts nothing.
    """
    # The nav item's accessible name carries its step number and status chip
    # ("2 Recordings · 3 WAVs"), so match on the label within the spine.
    await page.locator("#spine").get_by_role("button", name=re.compile(stage, re.I)).first.click()
    await page.wait_for_function("(s) => location.hash.slice(1) === s", arg=stage.lower(), timeout=5000)


async def test_next_player_is_shell_owned_and_survives_a_stage_switch(
    running_recorder: RunningRecorder,
):
    """The Player is ONE element, owned by the shell, outside `#viewRoot`.

    This is the invariant every other playback behaviour rests on. A per-view
    `<audio>` is detached on stage navigation (`mount(root, built.host)`), and
    removing a media element from a Document PAUSES it — so a player mounted
    inside a view silently stops the moment the operator walks from Recordings
    to Transcript, which is the walk the feature exists to support. An identity
    stamp catches that structurally, with no timing threshold (ADR-0017).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-01T09-00-00Z"
    _seed_wav_session(rec, sid, names=[f"{sid}_alice_speaker_0000aaaa.wav"])

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            player = page.get_by_test_id("player")
            await player.wait_for(state="attached", timeout=10000)

            # LOAD something first, so "survives" means the playback survives —
            # not merely that an empty element wasn't rebuilt.
            play = page.get_by_role("button", name=re.compile("^play ", re.I)).first
            await play.wait_for(state="visible", timeout=10000)
            await play.click()
            await page.wait_for_function(
                """() => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && a.getAttribute('src') && a.readyState >= 1;
                }""",
                timeout=15000,
            )
            src_before = await page.evaluate(
                """() => document.querySelector('[data-slot="player"]').getAttribute('src')"""
            )

            # Owned by the shell, not by the view that shows the affordances.
            assert await page.evaluate(
                """() => {
                    const el = document.querySelector('[data-slot="player"]');
                    const root = document.getElementById('viewRoot');
                    return !!el && !!root && !root.contains(el);
                }"""
            ), "the Player must live outside #viewRoot"
            # Exactly one, so there can be no two-players-at-once state.
            assert await page.locator('[data-slot="player"]').count() == 1

            await page.evaluate(
                """() => { document.querySelector('[data-slot="player"]').__guardMark = 1; }"""
            )

            # Walk Recordings -> Transcript -> Recordings, crossing polls. Real
            # spine clicks, so the view host is genuinely detached and
            # re-mounted — which is what would pause a per-view player.
            for stage in ("transcript", "recordings"):
                await _goto_stage(page, stage)
                await asyncio.sleep(1.2)

            assert await page.evaluate(
                """() => document.querySelector('[data-slot="player"]')?.__guardMark === 1"""
            ), "the Player was rebuilt across a stage switch"
            # Same node AND same source: a rebuilt-but-identical element would
            # have lost the src, and a detached one would have been paused.
            src_after = await page.evaluate(
                """() => document.querySelector('[data-slot="player"]').getAttribute('src')"""
            )
            assert src_after == src_before, f"{src_before!r} -> {src_after!r}"
        finally:
            await browser.close()


async def test_next_row_play_loads_the_wav_and_decodes_it(running_recorder: RunningRecorder):
    """▶ on a WAV row loads that exact file and the browser decodes it.

    Event-driven, not timed: `loadedmetadata` + `duration > 0` proves the route,
    the HTTP Basic path a media element can't help with, and the decode, end to
    end. Deliberately NOT asserted: that `currentTime` advances — headless
    Chromium has no audio device, so that would be a wall-clock assertion.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-02T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    _seed_wav_session(rec, sid, names=[wav])

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            play = page.get_by_role("button", name=re.compile("^play ", re.I)).first
            await play.wait_for(state="visible", timeout=10000)
            await play.click()

            # The bar reveals itself only once something is loaded.
            await page.wait_for_function(
                """() => !document.getElementById('playerBar').hidden""",
                timeout=5000,
            )
            src = await page.evaluate(
                """() => document.querySelector('[data-slot="player"]').getAttribute('src')"""
            )
            assert src is not None
            assert f"/api/wav/{sid}/{wav}" in src, src
            assert "source=original" in src, src

            await page.wait_for_function(
                """() => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && a.readyState >= 1 && a.duration > 0;
                }""",
                timeout=15000,
            )
        finally:
            await browser.close()


def _seed_seekable_session(rec, sid: str, wav: str) -> None:
    """A session whose merged transcript names its source WAV, with a WAV long
    enough that a mid-file seek isn't clamped to the end by the browser."""
    d = _seed_wav_session(rec, sid, names=[wav], seconds=6.0)
    (d / "session-transcript.json").write_text(
        json.dumps(
            {
                "transcribed_at": "2025-03-03T10:00:00+00:00",
                "segments": [
                    {
                        "speaker": "Alice",
                        "text": "First line, right at the top of the recording.",
                        "abs_start": "2025-03-03T09:00:00+00:00",
                        "source_wav": wav,
                    },
                    {
                        "speaker": "Alice",
                        "text": "Suspect line, three seconds in.",
                        "abs_start": "2025-03-03T09:00:03+00:00",
                        "source_wav": wav,
                    },
                ],
                "speakers": ["Alice"],
                "speaking_seconds": {"Alice": 6.0},
                "suppressed": [],
                "suppressed_count": 0,
                "wav_count": 1,
                "transcribe_ms": 1000,
                "model": "tiny.en",
                "backend": "fake",
                "device": "cpu",
            }
        ),
        encoding="utf-8",
    )


async def test_next_transcript_timestamp_seeks_its_source_wav(running_recorder: RunningRecorder):
    """Clicking a merged line's timestamp plays THAT line's audio, at its offset.

    The whole point of the feature: "this line looks wrong — did she really say
    that?". The seek target names the file the words came from (`source_wav`) and
    the offset is `abs_start` minus that file's `wav_start`, so a stripped-source
    transcript lands on the right syllable too (ADR-0017).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-03T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    _seed_seekable_session(rec, sid, wav)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#transcript", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="transcript")

            # The merged body arrives via the lazy per-(session, stamp) fetch.
            await page.wait_for_function(
                f"""() => document.querySelectorAll('{_MERGED_FIRST_LINE}').length >= 2""",
                timeout=15000,
            )

            # The second line's timestamp — a real button, so it's clickable and
            # keyboard-reachable, and it reuses the node the line already had.
            ts = page.locator(f'{_MERGED_FIRST_LINE} [data-slot="ts"]').nth(1)
            await ts.click()

            await page.wait_for_function(
                """() => !document.getElementById('playerBar').hidden""",
                timeout=5000,
            )
            src = await page.evaluate(
                """() => document.querySelector('[data-slot="player"]').getAttribute('src')"""
            )
            assert f"/api/wav/{sid}/{wav}" in src, src
            assert "source=original" in src, src

            # The seek must survive a cold load: assigning currentTime before
            # metadata exists is unreliable, so the Player queues it.
            await page.wait_for_function(
                """() => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && a.readyState >= 1 && Math.abs(a.currentTime - 3) < 0.35;
                }""",
                timeout=15000,
            )
        finally:
            await browser.close()


async def test_next_deleting_the_playing_wav_stops_the_player(running_recorder: RunningRecorder):
    """Deleting the WAV you're listening to must stop the audio.

    The browser has the bytes BUFFERED — a local WAV is usually fetched whole —
    so no media `error` fires and playback of a deleted recording otherwise runs
    to the end. "I deleted it and it kept talking." Hence explicit eviction from
    the mutating verb, not just an error listener (ADR-0017).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-04T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    _seed_wav_session(rec, sid, names=[wav])

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            play = page.get_by_role("button", name=re.compile("^play ", re.I)).first
            await play.wait_for(state="visible", timeout=10000)
            await play.click()
            await page.wait_for_function(
                """() => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && a.getAttribute('src') && a.readyState >= 1;
                }""",
                timeout=15000,
            )

            await page.locator(".wavrow [data-wav-delete]").first.click()

            await page.wait_for_function(
                """() => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && !a.getAttribute('src') && a.paused;
                }""",
                timeout=10000,
            )
            # And it says why, rather than silently going quiet.
            msg = await page.locator('[data-slot="playerMsg"]').text_content()
            assert msg and "delet" in msg.lower(), msg
        finally:
            await browser.close()


async def test_next_waveform_click_seeks_and_draws_a_playhead(running_recorder: RunningRecorder):
    """Clicking the waveform plays the displayed WAV from that point, and the
    playhead appears on it.

    The waveform is a control surface as well as a display one (#191 decision 8).
    The playhead is a transform-driven overlay element, never a canvas repaint —
    the peaks draw stays behind `lastWaveSig`, so a position moving at frame rate
    cannot drag an O(bins) rebuild along with it (ADR-0017).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-05T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    _seed_wav_session(rec, sid, names=[wav])

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            # The hero canvas draws once the lazy peaks land.
            canvas = page.locator('[data-slot="canvas"]')
            await canvas.wait_for(state="visible", timeout=10000)
            box = await canvas.bounding_box()
            assert box and box["width"] > 40

            # Click at ~40% across: a 6s… (1s) WAV, so assert the FRACTION, not
            # an absolute second — the seeded WAV's duration is the source of
            # truth and the browser clamps to it.
            await page.mouse.click(box["x"] + box["width"] * 0.4, box["y"] + box["height"] / 2)

            await page.wait_for_function(
                """() => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && a.getAttribute('src') && a.readyState >= 1 && a.duration > 0;
                }""",
                timeout=15000,
            )
            frac = await page.evaluate(
                """() => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a.currentTime / a.duration;
                }"""
            )
            assert 0.3 < frac < 0.5, f"clicked 40% across, landed at {frac:.2f}"

            # The playhead is drawn, and it is an ELEMENT (not canvas pixels).
            head = page.locator('[data-slot="playhead"]')
            await head.wait_for(state="visible", timeout=5000)
            # Positioned by TRANSFORM, not `left`: the move must stay
            # compositor-only so a frame-rate playhead can't drag layout (and
            # certainly not an O(bins) canvas repaint) along with it.
            transform = await page.evaluate(
                """() => document.querySelector('[data-slot="playhead"]').style.transform"""
            )
            assert "translateX(" in transform, f"expected a translateX, got {transform!r}"
        finally:
            await browser.close()


async def test_next_playhead_is_absent_while_playing_another_wav(
    running_recorder: RunningRecorder,
):
    """Strict identity: the playhead appears ONLY on the file being played.

    The hero always shows the selected ORIGINAL, but the Player may hold a
    different WAV (or a stripped clip, or another session's file). Drawing a
    position on a timeline that isn't playing would be a confident lie, so it
    draws nothing (#191 decision 9).
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-06T09-00-00Z"
    first = f"{sid}_alice_speaker_0000aaaa.wav"
    second = "2025-03-06T09-05-00Z_bob_speaker_0000bbbb.wav"
    _seed_wav_session(rec, sid, names=[first, second])

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            # Hero shows `first` (the default selection); play `second` instead.
            row_two = page.locator(f'.wavrow[data-wav="{second}"]')
            await row_two.wait_for(state="attached", timeout=10000)
            await row_two.locator('[data-slot="play"]').click()

            await page.wait_for_function(
                """(name) => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && (a.getAttribute('src') || '').includes(name) && a.readyState >= 1;
                }""",
                arg=second,
                timeout=15000,
            )

            # The hero is still showing `first`, so there must be no playhead.
            assert await page.evaluate(
                """() => {
                    const h = document.querySelector('[data-slot="playhead"]');
                    return !h || h.hidden;
                }"""
            ), "a playhead was drawn on a WAV that isn't the one playing"
        finally:
            await browser.close()


async def test_next_playhead_clears_when_the_selection_moves_while_paused(
    running_recorder: RunningRecorder,
):
    """A PAUSED playhead must not survive the canvas being redrawn for another WAV.

    Strict identity (#191 decision 9) is enforced from the Player's position
    ticks, which only fire while audio is moving. Pause, then select a different
    WAV: the canvas redraws for the new file but no media event fires, so nothing
    re-runs the identity check and the old position would sit there pointing at a
    file the Player isn't holding — the "confident lie" the rule forbids.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-07T09-00-00Z"
    first = f"{sid}_alice_speaker_0000aaaa.wav"
    second = "2025-03-07T09-05-00Z_bob_speaker_0000bbbb.wav"
    _seed_wav_session(rec, sid, names=[first, second])

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            # Play the WAV the hero is showing, so the playhead is legitimately up.
            row_one = page.locator(f'.wavrow[data-wav="{first}"]')
            await row_one.wait_for(state="attached", timeout=10000)
            await row_one.locator('[data-slot="play"]').click()
            await page.wait_for_selector('[data-slot="playhead"]:not([hidden])', timeout=15000)

            # Pause, then select the OTHER WAV — the hero redraws for `second`.
            await page.evaluate("""() => { document.querySelector('[data-slot="player"]').pause(); }""")
            await page.locator(f'.wavrow[data-wav="{second}"] [data-wav-select]').click()

            await page.wait_for_function(
                """() => {
                    const h = document.querySelector('[data-slot="playhead"]');
                    return !h || h.hidden;
                }""",
                timeout=5000,
            )
        finally:
            await browser.close()


def _seed_suppressed_session(rec, sid: str, wav: str) -> None:
    """A session whose merged transcript has a hallucination-filtered segment."""
    d = _seed_wav_session(rec, sid, names=[wav], seconds=6.0)
    (d / "session-transcript.json").write_text(
        json.dumps(
            {
                "transcribed_at": "2025-03-08T10:00:00+00:00",
                "segments": [
                    {
                        "speaker": "Alice",
                        "text": "A kept line.",
                        "abs_start": "2025-03-08T09:00:00+00:00",
                        "source_wav": wav,
                    }
                ],
                "speakers": ["Alice"],
                "speaking_seconds": {"Alice": 6.0},
                "suppressed": [
                    {
                        "speaker": "Alice",
                        "text": "Thanks for watching!",
                        "abs_start": "2025-03-08T09:00:02+00:00",
                        "matched_rule": "outro",
                        "source_wav": wav,
                    }
                ],
                "suppressed_count": 1,
                "wav_count": 1,
                "transcribe_ms": 1000,
                "model": "tiny.en",
                "backend": "fake",
                "device": "cpu",
            }
        ),
        encoding="utf-8",
    )


async def test_next_audit_table_time_cell_seeks_the_dropped_lines_audio(
    running_recorder: RunningRecorder,
):
    """The hallucination audit's time cell seeks too (#191 decision 10).

    "The filter dropped this line — was there speech there at all?" is the
    sharpest version of the loop, and a suppressed segment carries `source_wav`
    just like a kept one. The table lives inside `mergedHost`, so the view's ONE
    delegated listener serves it: this test is here because that wiring is the
    part that silently doesn't happen.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-08T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    _seed_suppressed_session(rec, sid, wav)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#transcript", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="transcript")

            cell = page.locator('.audit-tbl tbody [data-slot="time"]')
            await cell.wait_for(state="visible", timeout=15000)
            await cell.click()

            await page.wait_for_function(
                """(name) => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a
                        && (a.getAttribute('src') || '').includes(name)
                        && a.readyState >= 1
                        && Math.abs(a.currentTime - 2) < 0.35;
                }""",
                arg=wav,
                timeout=15000,
            )
        finally:
            await browser.close()


async def test_next_player_bar_does_not_cover_the_shell(running_recorder: RunningRecorder):
    """The docked Player must SHORTEN the shell, not sit on top of it.

    The bar is `position: fixed` (a grid row would rework the 100vh height math
    every panel's internal scrolling depends on), so without the shell giving up
    the same height the bar occludes the bottom of the spine, the workspace and
    the rail — the session-info card at the bottom of the spine goes unreadable.
    Geometric assertion: once the bar is visible, no shell column may extend
    past its top edge.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-10T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    _seed_wav_session(rec, sid, names=[wav])

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            # Baseline: with nothing loaded the shell owns the whole viewport.
            assert await page.evaluate(
                """() => Math.round(document.getElementById('next-app').getBoundingClientRect().bottom)
                        >= window.innerHeight - 1"""
            ), "with no audio loaded the shell should still fill the viewport"

            play = page.get_by_role("button", name=re.compile("^play ", re.I)).first
            await play.wait_for(state="visible", timeout=10000)
            await play.click()
            await page.wait_for_function(
                """() => !document.getElementById('playerBar').hidden""", timeout=5000
            )

            overlap = await page.evaluate(
                """() => {
                    const bar = document.getElementById('playerBar').getBoundingClientRect();
                    const worst = [];
                    for (const sel of ['#next-app', '#spine', '#work', '#tapsRail']) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        const r = el.getBoundingClientRect();
                        if (r.bottom > bar.top + 1) {
                            worst.push({sel, bottom: Math.round(r.bottom)});
                        }
                    }
                    return {barTop: Math.round(bar.top), worst};
                }"""
            )
            assert not overlap["worst"], (
                f"shell columns extend under the player bar (top={overlap['barTop']}): {overlap['worst']}"
            )
        finally:
            await browser.close()


async def test_next_play_kept_starts_at_the_first_kept_span(running_recorder: RunningRecorder):
    """▶ kept plays what ✂ would LEAVE, starting at the first kept region.

    The strip knobs ask "did that cut real speech?", which is a listening
    question — so the preview gets a listening answer. The gap-hopping itself is
    unit-tested (`spanPlaybackStep`); what this pins is the wiring: the button
    is inert until a cut is on screen, and once one is it loads the ORIGINAL and
    lands at the first kept span rather than at 0:00.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-11T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    # REAL speech that begins with silence. A synthesised tone is continuous, so
    # its only kept span starts at 0.00 and the "skips the lead-in" claim would
    # pass vacuously — and Silero scores pure tones unreliably anyway.
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "solen-da.wav", d / wav)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            kept = page.get_by_test_id("playKeptBtn")
            await kept.wait_for(state="visible", timeout=10000)
            # Nothing cut yet: the affordance must not pretend it can play.
            assert await kept.is_disabled(), "▶ kept should be inert with no cut on screen"

            # Drag a knob to produce a live preview (the debounced strip-preview
            # fetch is what puts spans on the canvas).
            await page.evaluate(
                """() => {
                    const r = document.querySelector('[data-strip-knob="min_silence_ms"]');
                    r.value = '300';
                    r.dispatchEvent(new Event('input', { bubbles: true }));
                    r.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            await page.wait_for_function(
                """() => !document.querySelector('[data-slot="playKeptBtn"]').disabled""",
                timeout=15000,
            )

            await kept.click()
            await page.wait_for_function(
                """(name) => {
                    const a = document.querySelector('[data-slot="player"]');
                    return a && (a.getAttribute('src') || '').includes(name)
                        && a.getAttribute('src').includes('source=original')
                        && a.readyState >= 1;
                }""",
                arg=wav,
                timeout=15000,
            )
            started_at = await page.evaluate(
                """() => document.querySelector('[data-slot="player"]').currentTime"""
            )
            assert started_at > 0.05, f"kept playback must skip the lead-in silence, started at {started_at}"
        finally:
            await browser.close()


async def test_next_playhead_still_works_after_a_stage_walk(running_recorder: RunningRecorder):
    """The playhead must survive leaving Recordings and coming back.

    Regression pin for the exact failure the rest of the playback suite missed:
    the ticker retired itself the first frame the (CACHED, later re-mounted) view
    host was detached, so after one walk to Transcript and back the waveform
    never drew a playhead again for the life of the page — on the very
    navigation ADR-0017's shell-owned Player exists to support.
    """
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-03-12T09-00-00Z"
    wav = f"{sid}_alice_speaker_0000aaaa.wav"
    _seed_wav_session(rec, sid, names=[wav], seconds=6.0)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")
            await _focus_session(page, sid, stage="recordings")

            row = page.locator(f'.wavrow[data-wav="{wav}"]')
            await row.wait_for(state="attached", timeout=10000)
            await row.locator('[data-slot="play"]').click()
            await page.wait_for_selector('[data-slot="playhead"]:not([hidden])', timeout=15000)

            # Walk away and back while it keeps playing.
            await _goto_stage(page, "transcript")
            await asyncio.sleep(1.0)
            await _goto_stage(page, "recordings")

            # The playhead must be LIVE, not merely left visible with a stale
            # transform from before the walk — a dead ticker leaves exactly that,
            # so "is it visible" passes vacuously. Assert it MOVES.
            await page.wait_for_selector('[data-slot="playhead"]:not([hidden])', timeout=10000)
            before = await page.evaluate(
                """() => document.querySelector('[data-slot="playhead"]').style.transform"""
            )
            moved = await page.wait_for_function(
                """(prev) => {
                    const h = document.querySelector('[data-slot="playhead"]');
                    return !h.hidden && h.style.transform !== prev ? h.style.transform : false;
                }""",
                arg=before,
                timeout=8000,
            )
            assert moved, "the playhead never advanced after the stage walk"
        finally:
            await browser.close()
