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

Skipped entirely when Playwright's Chromium isn't installed. Install
with `pip install playwright && python -m playwright install chromium`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from tapscribe import transcribers as _transcribers
from tapscribe.recorder import JobState

from .conftest import RunningRecorder
from .fake_transcriber import FakeTranscriber
from .harness import stream_wav_via_tap, streams_drained, synth_speech_like_wav, wait_until

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from playwright.async_api import async_playwright  # noqa: E402 — must follow the skip

ALICE_TEXT = "The quick brown fox jumps over the lazy dog."
BOB_TEXT = "Hello operator, this is a transcription pipeline check."

# Screenshots committed to the repo so the README can embed them and a
# reviewer can eyeball what the test actually saw.
SHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "dashboard-shots"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "audio"

_WORD_RE = re.compile(r"[^\W\d_]+", flags=re.UNICODE)


def _word_tokens(text: str, *, min_len: int = 4) -> set[str]:
    """Lowercased alphabetic tokens of at least `min_len` chars."""
    return {m.group(0).lower() for m in _WORD_RE.finditer(text) if len(m.group(0)) >= min_len}


@pytest.fixture
def fake_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriber:
    """Same shape as the HTTP E2E test's fixture — keep them aligned
    so any future change to the FakeTranscriber wiring is caught here
    too."""
    fake = FakeTranscriber(text_by_speaker={"Alice": ALICE_TEXT, "Bob": BOB_TEXT})

    def _factory(model_name: str, **_kwargs) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.batch_transcribe as _bt

    monkeypatch.setattr(_bt, "load_transcriber", _factory)
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

    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Long enough that both WSes are still streaming while the rail rows +
    # caption pushes are asserted (the relays must be open for the pushes
    # to reach them), short enough not to drag the test out.
    wavs = {
        "alice": synth_speech_like_wav(tmp_path / "alice.wav", seconds=6.0, freq_hz=220.0),
        "bob": synth_speech_like_wav(tmp_path / "bob.wav", seconds=6.0, freq_hz=440.0),
    }

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers (CodeQL py/uninitialized-local-variable)
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
            await page.screenshot(path=str(SHOTS_DIR / "01-idle.png"), full_page=True)

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
            await page.screenshot(path=str(SHOTS_DIR / "02-active-taps.png"), full_page=True)

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
            await page.screenshot(path=str(SHOTS_DIR / "03-live-transcripts.png"), full_page=True)

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
            await page.screenshot(path=str(SHOTS_DIR / "04-sessions.png"), full_page=True)

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
            await page.screenshot(path=str(SHOTS_DIR / "05-merged-transcript.png"), full_page=True)

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

    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
        try:
            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
            )
            page = await context.new_page()
            await page.goto(base + "/#recordings", wait_until="domcontentloaded")

            # Switch the WAV list to the stripped source — clip rows render
            # under their parent original only when that source is active.
            stripped_toggle = page.locator('#viewRoot .srcsw__opt[data-src="stripped"]')
            await stripped_toggle.wait_for(state="visible", timeout=10000)
            await page.wait_for_function(
                """() => !document.querySelector('#viewRoot .srcsw__opt[data-src="stripped"]')?.disabled""",
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
            # row. Its text must be the scripted text the FakeTranscriber
            # returned.
            await page.locator(f"{first_row_sel} [data-wav-expand]").click()
            await page.wait_for_function(
                f"""
                () => {{
                  const tx = document.querySelectorAll('#viewRoot .wavlist .expand-tx');
                  return Array.from(tx).some((el) => el.innerText.includes({scripted!r}));
                }}
                """,
                timeout=5000,
            )

            await page.screenshot(
                path=str(SHOTS_DIR / "07-stripped-region-sub-rows.png"),
                full_page=True,
            )

            # The original WAV's row is still present and identifiable by
            # its (unique) original filename — clip rows must not have
            # taken its place in the DOM.
            orig_row = page.locator(f'#viewRoot .wavlist .wavrow[data-wav="{original_name}"]')
            await orig_row.wait_for(state="visible", timeout=2000)
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
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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
            await page.screenshot(
                path=str(SHOTS_DIR / "08-deleted-session-audio.png"),
                full_page=True,
            )
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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

    reference_words = _word_tokens(fixture_ref.read_text(encoding="utf-8"))
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="fixture-armstrong",
        name="armstrong-en",
        wav_path=fixture_wav,
        utterance_id="utt-real-armstrong",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    assert (rec.session_dir / "armstrong-en").with_suffix(".wav").parent.exists()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers (CodeQL py/uninitialized-local-variable)
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

            # The engine panel's model select defaults to the catalog's first
            # model. tiny.en is only 75 MB — better fit for a test that might
            # run on a fresh machine.
            await page.select_option("#viewRoot .sel--model", "tiny.en")

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

            await page.screenshot(
                path=str(SHOTS_DIR / "06-real-audio-transcript.png"),
                full_page=True,
            )
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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
            # Pre-warm the per-WAV transcript body cache: expand once (this
            # may fetch /api/wav/.../transcript), wait for the text, collapse.
            await page.locator(f"{row_sel} [data-wav-expand]").click()
            await page.wait_for_function(
                f"""
                () => Array.from(document.querySelectorAll('#viewRoot .wavlist .expand-tx'))
                  .some((el) => el.innerText.includes({scripted!r}))
                """,
                timeout=5000,
            )
            await page.locator(f"{row_sel} [data-wav-expand]").click()
            await page.wait_for_function(
                """() => document.querySelectorAll('#viewRoot .wavlist .expand-tx').length === 0""",
                timeout=5000,
            )

            # Kill every further /api/state poll. From here the dashboard
            # has no fresh server data; a UI-only click must apply from
            # the client-side cache or not at all.
            async def _kill_state(route):
                await route.fulfill(status=503, body="down")

            await page.route("**/api/state", _kill_state)

            await page.locator(f"{row_sel} [data-wav-expand]").click()
            # The 1500ms bound is below what any rescuing poll could deliver
            # (polls are dead) — so a pass means the expand rendered from
            # cache on click, not from a network round trip.
            await page.wait_for_function(
                f"""
                () => Array.from(document.querySelectorAll('#viewRoot .wavlist .expand-tx'))
                  .some((el) => el.innerText.includes({scripted!r}))
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
        try:
            failures: list[str] = []
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            client = await page.context.new_cdp_session(page)
            await client.send("Performance.enable")
            await page.goto(base, wait_until="domcontentloaded")
            # Settle: let first-paint + the first couple of polls land.
            await page.wait_for_timeout(2500)
            n0, l0 = await _perf_metrics(client)
            # Idle through ~20 poll cycles — no input.
            await page.wait_for_timeout(10000)
            n1, l1 = await _perf_metrics(client)
            dn, dl = n1 - n0, l1 - l0
            print(f"[idle-churn] /: dNodes={dn:+.0f}  dListeners={dl:+.0f}")
            if dl > _IDLE_MAX_LISTENER_GROWTH:
                failures.append(f"/: +{dl:.0f} listeners over ~10s idle (per-tick listener churn)")
            if dn > _IDLE_MAX_NODE_GROWTH:
                failures.append(f"/: +{dn:.0f} DOM nodes over ~10s idle (per-tick DOM churn)")
            await context.close()
            assert not failures, "idle DOM churn regression: " + "; ".join(failures)
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


async def test_next_job_ticks_do_not_rebuild_merged_transcript(running_recorder: RunningRecorder):
    """Job progress ticks (~1/s during a transcribe/strip) must update the job
    bar IN PLACE, not invalidate the merged transcript's render signature —
    each invalidation re-renders every segment row synchronously, which is the
    main-thread stall operators reported as the tab "locking up"."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    sid = "2025-02-01T09-00-00Z"
    _seed_merged_session(rec, sid, segments=120)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as e:  # pragma: no cover
                pytest.skip(f"Chromium not available: {e}")
                return  # unreachable; for static analysers
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            captured: dict = {}

            async def _route(route):
                captured.update(route.request.post_data_json or {})
                await route.fulfill(status=200, content_type="application/json", body='{"ok": true}')

            await page.route("**/api/transcribe-session", _route)
            await page.goto(rr.base_url + "/#transcript", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-slot="srcSwHost"] .srcsw', timeout=6000)

            stripped_btn = page.locator('[data-slot="srcSwHost"] [data-src="stripped"]')
            assert not await stripped_btn.is_disabled(), "stripped must enable once a stripped/ folder exists"

            await stripped_btn.click()
            await page.wait_for_timeout(700)
            assert await page.locator('[data-slot="srcSwHost"] [data-src="stripped"].is-on').count() == 1
            picker_rows = await page.locator('[data-slot="wavList"] .wavrow').count()
            assert picker_rows >= n_clips, (
                f"switching to stripped must list the {n_clips} clips in the picker, got {picker_rows}"
            )

            await page.locator('[data-slot="txRangeBtn"]').click()
            await page.wait_for_timeout(700)
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            put_payloads: list[dict] = []

            async def _route(route):
                if route.request.method == "PUT":
                    put_payloads.append(route.request.post_data_json or {})
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
            orig_tags = await page.locator('[data-slot="cacheBody"] .cacherow__src.is-original').count()
            strip_tags = await page.locator('[data-slot="cacheBody"] .cacherow__src.is-stripped').count()
            assert orig_tags >= 1 and strip_tags >= 2, f"orig={orig_tags} stripped={strip_tags}"
            rows_before = await page.locator('[data-slot="cacheBody"] .cacherow').count()

            # Flipping to Stripped must NOT change the cache list.
            await page.locator('[data-slot="srcSwHost"] [data-src="stripped"]').click()
            await page.wait_for_timeout(700)
            rows_after = await page.locator('[data-slot="cacheBody"] .cacherow').count()
            assert rows_after == rows_before, f"cache list changed on toggle: {rows_before} -> {rows_after}"
            assert await page.locator('[data-slot="cacheBody"] .cacherow__src.is-stripped').count() >= 2

            # 'Set' on the non-primary stripped row sends source=stripped (404 fix).
            set_btn = page.locator('[data-slot="cacheBody"] [data-slot="primary"]', has_text="set")
            assert await set_btn.count() == 1, "expected one non-primary 'set' row (a stripped variant)"
            await set_btn.first.click()
            await page.wait_for_timeout(500)
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

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")

            # Local is the default source now (#86); switch to Command to reveal
            # its CLI template field, then wait for Generate to enable once the
            # seeded transcript lands on a poll (proves the view sees the marker).
            await page.wait_for_selector('[data-src="command"]', timeout=6000)
            await page.click('[data-src="command"]')
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
    real; Local + Command are wired and only the API source stays disabled."""
    rr = running_recorder
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            await page.goto(rr.base_url + "/#summary", wait_until="domcontentloaded")
            # Anchor on the source selector (always visible) — the command field
            # is hidden under the Local default now.
            await page.wait_for_selector('[data-src="local"]', timeout=6000)

            assert await page.locator("#viewRoot .mocktag").count() == 0, (
                "the mock·not-wired tag must be gone"
            )
            # Local + Command enabled; only API disabled now (#85 not yet wired).
            seg = page.locator("#viewRoot .segctl--wide .segctl__opt")
            assert await seg.count() == 3
            disabled = await page.locator("#viewRoot .segctl--wide .segctl__opt[disabled]").count()
            assert disabled == 1, f"only API must be disabled, got {disabled} disabled options"
            # Local is the bundled-offline default selected source (#86).
            assert await page.locator('#viewRoot .segctl--wide [data-src="local"].is-on').count() == 1
        finally:
            await browser.close()


async def test_summary_stage_local_is_default_and_toggles_command_field(running_recorder: RunningRecorder):
    """Local (bundled, offline) is the default source in the Summary view (#86):
    its pane shows no CLI field, and switching to Command reveals the command
    template (and back to Local hides it). This is the source-selector wiring
    the Local slice adds on top of the Command tracer bullet — no model download
    needed, so it runs offline on CI."""
    rr = running_recorder
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return
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
            await page.click('[data-src="command"]')
            await page.locator('[data-slot="sumCmd"]').wait_for(state="visible", timeout=4000)

            # Switch back to Local → it hides again, and Local is is-on.
            await page.click('[data-src="local"]')
            await page.locator('[data-slot="sumCmd"]').wait_for(state="hidden", timeout=4000)
            assert await page.locator('[data-src="local"].is-on').count() == 1
        finally:
            await browser.close()
