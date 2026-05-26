"""Browser-driven E2E: dashboard renders what the pipeline produces.

Three tests in this file, all driving real headless Chromium against
the running uvicorn server:

- `test_dashboard_shows_active_taps_live_feed_and_merged_transcript`
  is the fast plumbing check — synthetic WAVs through two bridges plus
  a `FakeTranscriber`, so it runs in CI without `faster-whisper`
  installed. Verifies every panel renders correctly under load.
- `test_dashboard_renders_strip_silence_region_sub_rows` exercises the
  per-WAV region sub-rows the strip-silence pipeline mints — splits a
  multi-burst WAV, expects N indented `.wav-row.strip-sub` rows under
  the original, transcribes one of them, and expands its inline
  transcript.
- `test_dashboard_with_real_audio_and_whisper` is the full-fat check:
  streams the committed `armstrong-en.wav` fixture through the bridge,
  clicks the dashboard's **▶ transcribe whole session** button, and
  waits for real `faster-whisper` output to render in the merged
  transcript panel. Gated by `@pytest.mark.real_audio` and skipped
  unless `faster-whisper` + the audio fixture are present.

Skipped entirely when Playwright's Chromium isn't installed. Install
with `pip install playwright && python -m playwright install chromium`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
from pathlib import Path

import pytest

from tapscribe import transcribers as _transcribers

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


@pytest.fixture
def synthetic_wavs(tmp_path: Path) -> dict[str, Path]:
    return {
        "alice": synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0),
        "bob": synth_speech_like_wav(tmp_path / "bob.wav", seconds=0.6, freq_hz=440.0),
    }


async def test_dashboard_shows_active_taps_live_feed_and_merged_transcript(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001 — keeps the patched factory
    synthetic_wavs: dict[str, Path],
):
    """End-to-end through real Chromium: stream → see in UI → click
    transcribe → see merged transcript in UI.

    The dashboard polls `/api/state` once per second; every UI wait is
    on a DOM condition (text appearance, count change), never a fixed
    sleep, so the test scales with the poll cadence rather than racing
    it.
    """
    rec = running_recorder.recorder
    fake_wlk = running_recorder.fake_wlk
    ws_base = running_recorder.ws_base_url

    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

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
            # The dashboard polls /api/state every second so it's never
            # network-idle — wait on DOM ready instead.
            await page.goto(running_recorder.base_url, wait_until="domcontentloaded")

            # The dashboard's idle render — the "live transcripts" panel
            # starts at 0 and the active-taps panel says "No taps".
            await page.wait_for_selector("#activeTapsBody .empty", timeout=5000)
            assert await page.locator("#liveFeedCount").inner_text() == "0"
            await page.screenshot(path=str(SHOTS_DIR / "01-idle.png"), full_page=True)

            alice_task = asyncio.create_task(
                stream_wav_via_tap(
                    ws_base_url=ws_base,
                    identity="alice",
                    name="Alice",
                    wav_path=synthetic_wavs["alice"],
                    utterance_id="utt-ui-alice",
                    frame_interval_s=0.025,
                )
            )
            bob_task = asyncio.create_task(
                stream_wav_via_tap(
                    ws_base_url=ws_base,
                    identity="bob",
                    name="Bob",
                    wav_path=synthetic_wavs["bob"],
                    utterance_id="utt-ui-bob",
                    frame_interval_s=0.025,
                )
            )

            # Active taps panel must surface both speakers while their
            # WSes are open. Poll cadence is 1 s; allow a couple of ticks.
            await page.wait_for_function(
                """
                () => {
                  const rows = Array.from(
                    document.querySelectorAll("#activeTapsBody .stream-row .name .fg"),
                  );
                  const names = rows.map((n) => n.textContent.trim());
                  return names.includes("Alice") && names.includes("Bob");
                }
                """,
                timeout=5000,
            )
            assert await page.locator("#activeCount").inner_text() == "2"
            await page.screenshot(path=str(SHOTS_DIR / "02-active-taps.png"), full_page=True)

            # Settled lines from the fake WhisperLiveKit must surface in
            # the live transcripts panel, attributed to each speaker.
            # FakeWlk broadcasts to every connected relay → each push
            # lands twice (once tagged Alice, once Bob).
            fake_wlk.push_committed("first ui settled line")
            fake_wlk.push_committed("second ui settled line")

            await page.wait_for_function(
                """
                () => {
                  const lines = Array.from(
                    document.querySelectorAll("#liveFeedShell .feed-body .line"),
                  );
                  const pairs = lines.map((l) => ({
                    who: l.querySelector(".who")?.textContent.trim(),
                    txt: l.querySelector(".txt")?.textContent.trim(),
                  }));
                  const has = (who, txt) =>
                    pairs.some((p) => p.who === who && p.txt === txt);
                  return (
                    has("Alice", "first ui settled line") &&
                    has("Bob", "first ui settled line") &&
                    has("Alice", "second ui settled line") &&
                    has("Bob", "second ui settled line")
                  );
                }
                """,
                timeout=5000,
            )
            # The header count tracks the deque length — 4 lines pushed,
            # broadcast to 2 relays = 8 entries.
            assert int(await page.locator("#liveFeedCount").inner_text()) >= 4
            await page.screenshot(path=str(SHOTS_DIR / "03-live-transcripts.png"), full_page=True)

            await asyncio.gather(alice_task, bob_task)
            assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

            # The sessions panel must list the current session with the
            # two WAVs the bridges wrote.
            await page.wait_for_function(
                f"""
                () => {{
                  const folder = document.querySelector(
                    '.sess-folder',
                  );
                  return folder && folder.textContent.trim() === '{rec.session_start}';
                }}
                """,
                timeout=5000,
            )
            await page.wait_for_function(
                """
                () => document.querySelectorAll('.wav-list .wav-row').length >= 2
                """,
                timeout=5000,
            )
            await page.screenshot(path=str(SHOTS_DIR / "04-sessions.png"), full_page=True)

            # The dashboard's headline button: ▶ transcribe whole session.
            tx_button = page.locator(f'[data-tx-sess="{rec.session_start}"]')
            await tx_button.wait_for(state="visible", timeout=5000)
            await tx_button.click()

            # And the merged transcript must render in .sess-main with
            # both speakers' scripted text. We assert on `innerText` (the
            # rendered, layout-aware string) rather than `textContent`
            # (raw concatenated text). The transcript container is
            # `white-space: pre-wrap`, so any stray whitespace/newlines
            # between sibling spans would split a single segment across
            # multiple visual lines while still passing a textContent
            # `.includes()` check — innerText catches that.
            await page.wait_for_function(
                f"""
                () => {{
                  const region = document.querySelector('.sess-main .transcript');
                  if (!region) return false;
                  const visible = region.innerText;
                  // Each speaker's label and body must appear adjacent on
                  // the same visual line — i.e. "Alice: <text>" — not on
                  // separate lines with a wrapped break in between.
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

            # ⎘ copy merged must copy the speaker display names (aliases
            # applied) — what the user sees on screen — not the raw
            # speaker keys from the backend's `plain_text`. Set an alias
            # and verify the clipboard reflects it.
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
            # Wait for the new aliases to render in the merged transcript.
            await page.wait_for_function(
                """
                () => {
                  const t = document.querySelector('.sess-main .transcript')?.innerText || '';
                  return t.includes('Ms. Smith: ') && t.includes('Mr. Jones: ');
                }
                """,
                timeout=5000,
            )
            copy_btn = page.locator(f'[data-copy-sess="{rec.session_start}"]')
            await copy_btn.click()
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
            # The click must give visible confirmation — otherwise the user
            # has no way to tell the silent clipboard write happened. The
            # button briefly swaps to "✓ copied" with the `just-completed`
            # flash animation.
            await page.wait_for_function(
                f"""
                () => {{
                  const b = document.querySelector(
                    '[data-copy-sess="{rec.session_start}"]',
                  );
                  return b && b.textContent.trim() === '✓ copied'
                      && b.classList.contains('just-completed');
                }}
                """,
                timeout=2000,
            )
        finally:
            await browser.close()


async def test_dashboard_renders_strip_silence_region_sub_rows(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    tmp_path: Path,
):
    """Each original WAV's row shows one indented `.wav-row.strip-sub`
    per region the strip-silence splitter wrote to `<session>/stripped/`,
    each with its own download link + transcribe button + expandable
    inline transcript.

    This is the load-bearing UX guard for the sub-row UI: the splitter
    refactor (PR #49) silently dropped the old 1:1 stripped sibling UI,
    so a regression here would leave region WAVs invisible to the
    operator. The test streams a 3-burst WAV, splits it, asserts 3
    sub-rows render under the parent, then exercises one sub-row's
    transcribe + expand controls end-to-end.
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
            await page.goto(base, wait_until="domcontentloaded")

            # The original WAV row must render, with exactly 3 indented
            # `.wav-row.strip-sub` siblings under it (the three regions).
            await page.wait_for_function(
                """
                () => document.querySelectorAll('.wav-list .wav-row.strip-sub').length === 3
                """,
                timeout=10000,
            )
            # Sub-rows live inside the same `.wav-list` as the original —
            # i.e. not in some sibling list. Asserts the DOM topology so a
            # future refactor that re-parents them is flagged here.
            total_rows = await page.locator(".wav-list .wav-row").count()
            assert total_rows == 4, f"expected 1 original + 3 region rows, got {total_rows}"

            # Each region row carries its own transcribe button targeting
            # source=stripped and the region's own name.
            for r in region_wavs:
                btn_sel = (
                    f'.wav-row.strip-sub button[data-tx-source="stripped"]'
                    f'[data-tx-wav="{rec.session_start}/{r.name}"]'
                )
                btn = page.locator(btn_sel)
                await btn.wait_for(state="visible", timeout=3000)

            # Click the first region's transcribe button and wait for the
            # row to flip from "transcribing…" to "took Xms".
            first_region = region_wavs[0]
            first_btn_sel = (
                f'.wav-row.strip-sub button[data-tx-source="stripped"]'
                f'[data-tx-wav="{rec.session_start}/{first_region.name}"]'
            )
            await page.locator(first_btn_sel).click()
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector(
                    '.wav-row.strip-sub button[data-tx-wav="{rec.session_start}/{first_region.name}"]'
                  )?.closest('.wav-row');
                  return row && /took\\s+\\d/.test(row.innerText);
                }}
                """,
                timeout=10000,
            )

            # Clicking the region's name expands the inline transcript
            # right below the sub-row. Its text must be the scripted text
            # the FakeTranscriber returned.
            name_sel = (
                f'.wav-row.strip-sub [data-toggle-wav="{rec.session_start}/{first_region.name}@stripped"]'
            )
            await page.locator(name_sel).click()
            await page.wait_for_function(
                f"""
                () => {{
                  const tx = document.querySelectorAll('.wav-list .expand-tx');
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
            # its (unique) original filename — sub-rows must not have
            # taken its place in the DOM.
            orig_row = page.locator(f'.wav-list [data-toggle-wav="{rec.session_start}/{original_name}"]')
            await orig_row.wait_for(state="visible", timeout=2000)
        finally:
            await browser.close()


@pytest.mark.real_audio
async def test_dashboard_with_real_audio_and_whisper(
    running_recorder: RunningRecorder,
):
    """Headline real-deal check: stream the committed Apollo 11 audio
    fixture through the bridge, click the dashboard's
    **▶ transcribe whole session** button, wait for real
    `faster-whisper` to produce a merged transcript, and assert the
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
            await page.goto(running_recorder.base_url, wait_until="domcontentloaded")

            # Wait for the dashboard's first /api/state poll to land — then
            # the sessions list will have the session we just streamed into.
            await page.wait_for_selector(".wav-list .wav-row", timeout=10000)
            # The truncMid renderer can elide "armstrong" out of the
            # visible name on long filenames, but it's preserved in the
            # `title` attribute.
            await page.wait_for_function(
                """
                () => Array.from(document.querySelectorAll('.wav-list .wav-row .wav-name'))
                  .some((a) => (a.title || a.textContent || "").toLowerCase().includes('armstrong'))
                """,
                timeout=5000,
            )

            # Default model picker is `small.en` (244 MB). tiny.en is in
            # the picker and only 75 MB — better fit for a test that
            # might run on a fresh machine.
            await page.select_option("[data-model-pick]", "tiny.en")

            tx_button = page.locator(f'[data-tx-sess="{rec.session_start}"]')
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
                      const region = document.querySelector('.sess-main .transcript');
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
                raise AssertionError(
                    f"real-Whisper transcript never rendered: model={whisper_model!r}, "
                    f"waited {timeout_s:.0f}s "
                    f"(override via TAPSCRIBE_E2E_WHISPER_TIMEOUT_S). "
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
    local UI state (here, expanding a WAV's inline transcript) must apply
    from the client-side cache, NOT wait on a fresh /api/state fetch.

    We transcribe a WAV, let the dashboard cache it, then kill every
    further /api/state poll and click to expand. If the expand still
    happens, the re-render came from cache. Before the fix the handler
    awaited /api/state (now dead), so the expand never rendered — exactly
    the "I click and nothing happens until I wait" symptom.
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
            await page.goto(base, wait_until="domcontentloaded")

            # Wait for the transcribed WAV row to render — proves a poll
            # has populated the client cache with this WAV's transcript.
            await page.wait_for_function(
                f"""
                () => {{
                  const row = document.querySelector(
                    '[data-toggle-wav="{rec.session_start}/{wav_name}"]'
                  )?.closest('.wav-row');
                  return row && /took\\s+\\d/.test(row.innerText);
                }}
                """,
                timeout=10000,
            )

            # Kill every further /api/state poll. From here the dashboard
            # has no fresh server data; a UI-only click must apply from
            # the client-side cache or not at all.
            async def _kill_state(route):
                await route.fulfill(status=503, body="down")

            await page.route("**/api/state", _kill_state)

            await page.locator(f'.wav-list [data-toggle-wav="{rec.session_start}/{wav_name}"]').click()
            # The 1500ms bound is below what any rescuing poll could deliver
            # (polls are dead) — so a pass means the expand rendered from
            # cache on click, not from a network round trip.
            await page.wait_for_function(
                f"""
                () => Array.from(document.querySelectorAll('.wav-list .expand-tx'))
                  .some((el) => el.innerText.includes({scripted!r}))
                """,
                timeout=1500,
            )
        finally:
            await browser.close()


async def test_model_select_change_does_not_block_re_render(
    running_recorder: RunningRecorder,
    tmp_path: Path,
):
    """The model picker is a <select> inside the detail pane. Changing it
    leaves the select focused, which trips the focused-input guard in
    renderSessionsIfChanged and would block a cache re-render entirely —
    so the model-change handler must blur the select before re-rendering.

    With polls killed, the only thing that can re-render the pane is the
    change handler itself; the fix rebuilds the pane, so focus leaves the
    (recreated) select. Self-skips where the catalog has <2 models (the
    synthetic CI e2e job installs no transcriber backends), since a change
    event needs a second option to select.
    """
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url

    src = synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0)
    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name="Alice",
        wav_path=src,
        utterance_id="utt-resp-model",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return  # unreachable; for static analysers
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded")

            await page.wait_for_selector("[data-model-pick]", timeout=10000)
            # The change handler needs a second option to switch to.
            values = await page.eval_on_selector_all(
                "[data-model-pick] option", "els => els.map(e => e.value)"
            )
            current = await page.eval_on_selector("[data-model-pick]", "el => el.value")
            target = next((v for v in values if v and v != current), None)
            if target is None:
                pytest.skip("model catalog has <2 options in this env")

            # Kill further polls so only the change handler can re-render.
            async def _kill_state(route):
                await route.fulfill(status=503, body="down")

            await page.route("**/api/state", _kill_state)

            # Reproduce the real-user precondition: the <select> is the
            # focused element when its change event fires. `select_option`
            # alone doesn't DOM-focus it, so focus + value + change are
            # dispatched together — exactly the state that trips the
            # focused-input guard in renderSessionsIfChanged.
            await page.eval_on_selector(
                "[data-model-pick]",
                """
                (el, value) => {
                  el.focus();
                  el.value = value;
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                target,
            )
            # The handler must blur the select and re-render the pane, so
            # focus leaves it. Before the fix the select kept focus and the
            # dependent UI never updated until a later poll.
            await page.wait_for_function(
                """
                () => {
                  const el = document.activeElement;
                  return !el || !el.hasAttribute('data-model-pick');
                }
                """,
                timeout=1500,
            )
        finally:
            await browser.close()
