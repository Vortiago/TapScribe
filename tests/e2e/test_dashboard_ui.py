"""Browser-driven E2E: dashboard renders what the pipeline produces.

Two tests in this file, both driving real headless Chromium against
the running uvicorn server:

- `test_dashboard_shows_active_taps_live_feed_and_merged_transcript`
  is the fast plumbing check — synthetic WAVs through two bridges plus
  a `FakeTranscriber`, so it runs in CI without `faster-whisper`
  installed. Verifies every panel renders correctly under load.
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

    def _factory(model_name: str, *, use_mlx: bool) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.app as _app

    monkeypatch.setattr(_app, "load_transcriber", _factory)
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
