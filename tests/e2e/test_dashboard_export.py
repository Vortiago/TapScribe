"""#208 — text export: the merged transcript and the summary leave the
dashboard as FILES, not only through the clipboard.

The end product of the whole pipeline is text the operator wants elsewhere
(notes doc, email, subtitle file). Today the Transcript view has a
clipboard copy and nothing else, and the Summary pane has neither. The
operator harm the issue names is concrete: `navigator.clipboard` requires a
SECURE CONTEXT, so on a plain-HTTP LAN dashboard — the normal way this
product is run — there is currently NO one-click export at all.

WHY THIS CONTRACT IS AN E2E: every limb of this feature is client-side
(the issue's own preferred shape: a Blob download, no backend route), so
`ruff` + `pytest` are blind to all of it and `tsc --noEmit` cannot see a
wrong URL, a missing alias mapping or a NaN timestamp. The dashboard's node
tests are pure-logic and cannot click. A download is only observable by
driving the real browser and catching the real download — so that is what
this does.

THE TRAP THIS SLICE IS BUILT AROUND — read before designing:

`tapscribe/web/js/next/subtitles.js` already exists (shipped by PR #299 for
this issue, with ZERO consumers — wiring it is the remaining work). Its
`toSRT`/`toVTT` take segments shaped `{start, end, text, speaker?}` where
`start`/`end` are **seconds as numbers**.

The merged transcript's segments are NOT that shape. `_segment_to_dict`
(`tapscribe/session_merge.py`) emits **`abs_start` / `abs_end` as absolute
ISO-8601 timestamp strings**. Passing them straight to `toSRT` gives
`Math.round(undefined * 1000)` → `NaN` → cues reading `NaN:NaN:NaN,NaN`,
which still downloads a plausible-looking .srt and passes any assertion
that only checks "a file arrived". The cue timings below are pinned for
exactly this reason.

The same segments carry RAW speaker keys (`Spk0`), while the operator's
display names live in `session-meta.json`'s alias map. `subtitles.js`'s
`line()` interpolates `seg.speaker` verbatim, so a direct wiring exports
raw keys — the same defect the merged-copy button already guards against
in `test_dashboard_ui.py` ("copy merged leaked raw speaker keys"). The
Transcript view already owns the correct path: it maps every segment's
speaker through `aliasOf` (`tapscribe/web/js/speakers.js`) when it builds
the copy text. The seeded `plain_text` below deliberately carries the raw
keys, so an implementation that exports the backend's pre-built string
instead of rebuilding alias-applied lines fails here.

DELIBERATELY NOT PINNED — the implementer's call: whether the seconds
conversion lives in `subtitles.js`, at the call site, or in a new helper;
what the origin of the relative clock is (first segment vs session start —
only the DELTA between cues is pinned); how the download helper is
factored; the button labels; whether the summary download uses the same
helper. Assertions here are on the downloaded BYTES, never on the module
structure that produced them.

The ONE design decision this contract does make for you is the `data-slot`
name of each new control, because an end-to-end test has to click
something. They follow the existing `txCopyBtn` / `sumSaveSession`
convention.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from .conftest import RunningRecorder
from .harness import playwright_session, synth_speech_like_wav

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)


# Two segments five seconds apart, each three seconds long. Absolute ISO
# stamps, exactly as `_segment_to_dict` writes them — the shape the exporters
# must actually cope with.
SEG_ONE_TEXT = "We agreed to keep recordings for ninety days."
SEG_TWO_TEXT = "I will write that up before Friday."
RAW_SPEAKERS = ("Spk0", "Spk1")
ALIASES = {"Spk0": "Ms. Smith", "Spk1": "Mr. Jones"}
SUMMARY_TEXT = "## Decisions\n\n- Retention set to ninety days.\n"

# The gap between the two segments' starts, and each segment's duration.
# Pinned as DELTAS so the implementation stays free to choose the origin of
# the relative clock (first segment, or the session's own start).
GAP_S = 5.0
DUR_S = 3.0

SRT_STAMP = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}$")
VTT_STAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}$")


def _stamp_seconds(stamp: str) -> float:
    """`HH:MM:SS,mmm` / `HH:MM:SS.mmm` → seconds. Raises on a malformed
    stamp, which is the point: a NaN cue must not silently parse to 0."""
    hh, mm, rest = stamp.split(":")
    ss, ms = re.split(r"[,.]", rest)
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def _seed_transcribed_session(rec) -> Path:
    """Give the CURRENT session an on-disk merged transcript, an alias map and
    a persisted summary — everything the export controls read, without running
    the pipeline (this contract is about export, not transcription)."""
    d = rec.recordings_dir / rec.session_start
    d.mkdir(parents=True, exist_ok=True)
    synth_speech_like_wav(d / f"{rec.session_start}_Spk0_spk0_0000a000.wav", seconds=0.3, freq_hz=220.0)
    (d / "session-transcript.json").write_text(
        json.dumps(
            {
                "transcribed_at": "2025-02-01T10:00:00+00:00",
                "segments": [
                    {
                        "abs_start": "2025-02-01T09:00:00+00:00",
                        "abs_end": "2025-02-01T09:00:03+00:00",
                        "speaker": RAW_SPEAKERS[0],
                        "text": SEG_ONE_TEXT,
                        "source_wav": "a.wav",
                        "low_confidence": False,
                    },
                    {
                        "abs_start": "2025-02-01T09:00:05+00:00",
                        "abs_end": "2025-02-01T09:00:08+00:00",
                        "speaker": RAW_SPEAKERS[1],
                        "text": SEG_TWO_TEXT,
                        "source_wav": "b.wav",
                        "low_confidence": False,
                    },
                ],
                # RAW keys on purpose: exporting this pre-built string instead
                # of rebuilding alias-applied lines is the defect being pinned.
                "plain_text": f"{RAW_SPEAKERS[0]}: {SEG_ONE_TEXT}\n{RAW_SPEAKERS[1]}: {SEG_TWO_TEXT}\n",
                "speakers": list(RAW_SPEAKERS),
                "speaking_seconds": {RAW_SPEAKERS[0]: 3.0, RAW_SPEAKERS[1]: 3.0},
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
    (d / "session-meta.json").write_text(
        json.dumps({"label": "Retention policy", "aliases": ALIASES}), encoding="utf-8"
    )
    (d / "session-summary.json").write_text(
        json.dumps(
            {
                "summary": SUMMARY_TEXT,
                "source": "local",
                "model": "fake-summarizer",
                "summarized_at": "2025-02-01T11:00:00+00:00",
                "transcribed_at": "2025-02-01T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return d


async def _download_text(page, slot: str) -> tuple[str, str]:
    """Click the control at `data-slot=slot`, catch the download it starts and
    return `(suggested_filename, body)`. Fails loudly rather than hanging
    forever if the control never produces one."""
    async with page.expect_download(timeout=10_000) as info:
        await page.click(f'#viewRoot [data-slot="{slot}"]')
    dl = await info.value
    path = await dl.path()
    assert path is not None, f"{slot} produced a download with no readable body"
    return dl.suggested_filename, Path(path).read_text(encoding="utf-8")


async def _open_transcript_view(page, base_url: str, session: str) -> None:
    await page.goto(base_url, wait_until="domcontentloaded")
    await page.wait_for_function(
        f"""() => {{
          const sel = document.querySelector('[data-slot="sessionPick"]');
          return sel && Array.from(sel.options).some((o) => o.value === {session!r});
        }}""",
        timeout=10000,
    )
    await page.evaluate('() => window.gotoView("transcript")')
    # The merged body must have LOADED — the copy button's enabled state is the
    # view's own signal for exactly that, and the export controls ride on the
    # same loaded body.
    await page.wait_for_function(
        """() => {
          const b = document.querySelector('#viewRoot [data-slot="txCopyBtn"]');
          return b && !b.disabled;
        }""",
        timeout=15000,
    )


# ---------------------------------------------------------------------------
# The merged transcript, as a file.
# ---------------------------------------------------------------------------


async def test_transcript_downloads_as_text_with_display_names_applied(
    running_recorder: RunningRecorder,
):
    """The plain-text export is the copy text, as a file: alias-applied, with
    no raw speaker key surviving."""
    rec = running_recorder.recorder
    _seed_transcribed_session(rec)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await _open_transcript_view(page, running_recorder.base_url, rec.session_start)

            name, body = await _download_text(page, "txDownloadTxt")

            assert name.endswith(".txt"), f"transcript text export must be a .txt: {name!r}"
            assert rec.session_start in name, f"the file must name its session: {name!r}"
            assert SEG_ONE_TEXT in body and SEG_TWO_TEXT in body, f"transcript body incomplete: {body!r}"
            assert "Ms. Smith" in body and "Mr. Jones" in body, (
                f"the .txt export ignored the operator's display names: {body!r}"
            )
            for raw in RAW_SPEAKERS:
                assert f"{raw}:" not in body, (
                    f"the .txt export leaked the raw speaker key {raw!r} "
                    f"(exported the backend's plain_text instead of the alias-applied lines): {body!r}"
                )
        finally:
            await browser.close()


async def test_transcript_downloads_as_srt_with_real_cue_timings(running_recorder: RunningRecorder):
    """SubRip export. The cue clock is the whole point of a subtitle file — a
    naive `toSRT(segments)` reads `start`/`end` that the merged schema does
    not have (`abs_start`/`abs_end`, ISO strings) and emits NaN stamps."""
    rec = running_recorder.recorder
    _seed_transcribed_session(rec)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await _open_transcript_view(page, running_recorder.base_url, rec.session_start)

            name, body = await _download_text(page, "txDownloadSrt")
            assert name.endswith(".srt"), f"subtitle export must be a .srt: {name!r}"

            blocks = [b for b in body.strip().split("\n\n") if b.strip()]
            assert len(blocks) == 2, f"expected one cue per segment, got {len(blocks)}: {body!r}"

            starts: list[float] = []
            for i, block in enumerate(blocks, start=1):
                lines = block.splitlines()
                assert lines[0].strip() == str(i), f"cue {i} must be numbered {i}: {block!r}"
                start_s, arrow, end_s = lines[1].split()
                assert arrow == "-->", f"cue {i} is not a SubRip timing line: {lines[1]!r}"
                assert SRT_STAMP.match(start_s) and SRT_STAMP.match(end_s), (
                    f"cue {i} has malformed timestamps {lines[1]!r} — the merged schema's "
                    "abs_start/abs_end (ISO strings) were not converted to seconds"
                )
                starts.append(_stamp_seconds(start_s))
                assert _stamp_seconds(end_s) - _stamp_seconds(start_s) == pytest.approx(DUR_S, abs=0.05), (
                    f"cue {i} does not span its segment's {DUR_S}s: {lines[1]!r}"
                )

            assert starts[1] - starts[0] == pytest.approx(GAP_S, abs=0.05), (
                f"the two cues must be {GAP_S}s apart, got {starts[1] - starts[0]}s — "
                "the cue clock does not track the segments"
            )
            assert "Ms. Smith" in body and "Mr. Jones" in body, (
                f"the .srt cues ignored the operator's display names: {body!r}"
            )
            for raw in RAW_SPEAKERS:
                assert f"{raw}:" not in body, f"the .srt export leaked the raw speaker key {raw!r}: {body!r}"
        finally:
            await browser.close()


async def test_transcript_downloads_as_vtt_with_a_webvtt_header(running_recorder: RunningRecorder):
    """WebVTT export — same cues, the format's own header and `.`-separated
    milliseconds (a .vtt that is really SubRip is not playable)."""
    rec = running_recorder.recorder
    _seed_transcribed_session(rec)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await _open_transcript_view(page, running_recorder.base_url, rec.session_start)

            name, body = await _download_text(page, "txDownloadVtt")
            assert name.endswith(".vtt"), f"subtitle export must be a .vtt: {name!r}"
            assert body.startswith("WEBVTT"), f"a WebVTT file must open with its header: {body[:40]!r}"

            timing_lines = [ln for ln in body.splitlines() if "-->" in ln]
            assert len(timing_lines) == 2, f"expected one cue per segment: {body!r}"
            starts: list[float] = []
            for ln in timing_lines:
                start_s, _, end_s = ln.split()
                assert VTT_STAMP.match(start_s) and VTT_STAMP.match(end_s), (
                    f"WebVTT stamps are dot-separated and must be real numbers: {ln!r}"
                )
                starts.append(_stamp_seconds(start_s))
            assert starts[1] - starts[0] == pytest.approx(GAP_S, abs=0.05), (
                f"the two cues must be {GAP_S}s apart, got {starts[1] - starts[0]}s"
            )
            assert "Ms. Smith" in body and "Mr. Jones" in body, (
                f"the .vtt cues ignored the operator's display names: {body!r}"
            )
        finally:
            await browser.close()


async def test_transcript_exports_work_without_a_clipboard(running_recorder: RunningRecorder):
    """The operator harm the issue is really about: on a plain-HTTP LAN
    dashboard `navigator.clipboard` is undefined (it needs a secure context),
    and today that leaves NO way to get the text out. The download path must
    not route through the clipboard, so it must still work with the API gone."""
    rec = running_recorder.recorder
    _seed_transcribed_session(rec)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            # Before ANY app code runs, make navigator.clipboard undefined —
            # the shape a non-secure-context browser presents.
            await page.add_init_script(
                "Object.defineProperty(navigator, 'clipboard', { get: () => undefined, configurable: true });"
            )
            await _open_transcript_view(page, running_recorder.base_url, rec.session_start)

            assert await page.evaluate("() => navigator.clipboard === undefined"), (
                "the no-clipboard precondition did not hold — this test proves nothing"
            )

            _, body = await _download_text(page, "txDownloadTxt")
            assert SEG_ONE_TEXT in body, f"the .txt export needs a clipboard to work: {body!r}"
            _, srt = await _download_text(page, "txDownloadSrt")
            assert "-->" in srt, f"the .srt export needs a clipboard to work: {srt!r}"
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# The summary, as a file — and as a clipboard copy, which it has never had.
# ---------------------------------------------------------------------------


async def test_summary_copies_and_downloads_as_markdown(running_recorder: RunningRecorder):
    """The Summary pane's output is a copy target by design but ships with no
    control to copy or save it — the operator hand-selects the text. It gets
    the same pair the Transcript view has: copy, and download as `.md`."""
    rec = running_recorder.recorder
    _seed_transcribed_session(rec)

    async with playwright_session() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = await context.new_page()
            await page.goto(running_recorder.base_url, wait_until="domcontentloaded")
            await page.wait_for_function(
                f"""() => {{
                  const sel = document.querySelector('[data-slot="sessionPick"]');
                  return sel && Array.from(sel.options).some((o) => o.value === {rec.session_start!r});
                }}""",
                timeout=10000,
            )
            await page.evaluate('() => window.gotoView("summary")')
            # The persisted summary has rendered into the output pane.
            await page.wait_for_function(
                """() => (document.querySelector('#viewRoot [data-slot="sumOut"]')?.innerText || '')
                         .includes('ninety days')""",
                timeout=15000,
            )

            name, body = await _download_text(page, "sumDownloadMd")
            assert name.endswith(".md"), f"the summary export must be markdown: {name!r}"
            assert rec.session_start in name, f"the file must name its session: {name!r}"
            assert "ninety days" in body, f"the .md export lost the summary body: {body!r}"
            assert "## Decisions" in body, (
                f"the .md export must carry the summary's markdown source, not the rendered text: {body!r}"
            )

            await page.click('#viewRoot [data-slot="sumCopyBtn"]')
            clipboard = await page.evaluate("() => navigator.clipboard.readText()")
            assert "ninety days" in clipboard, f"the summary copy button copied nothing useful: {clipboard!r}"
        finally:
            await browser.close()
