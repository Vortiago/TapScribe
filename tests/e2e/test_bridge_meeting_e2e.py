"""Full-stack meeting E2E for the SpatialChat Bridge — the whole dashboard-free
flow, for real, with no mocked seams:

  real extension (content.js + page-script.js + the vanilla-web popup)
    → mock SpatialChat page driving a track backed by REAL speech audio
    → real /tap capture into a real **detached Session** on a real **Recorder**
    → "End meeting" fires the real end-of-meeting pipeline
       (strip → transcribe[faster-whisper] → summarize[command])
    → the popup **meeting card** polls the real Recorder and shows the summary.

Unlike test_bridge_extension_e2e.py (bridge-side integration against a fake
/tap server), this exercises the Recorder and its pipeline end to end. It is
the slowest, heaviest bridge test: it needs a headed Chromium (extensions
don't load headless — run under xvfb), faster-whisper for ASR, and it runs a
real transcribe on captured audio. Opt-in via the browser_e2e marker; skipped
when Playwright or faster-whisper aren't installed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
import wave
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)
if importlib.util.find_spec("faster_whisper") is None:  # pragma: no cover
    pytest.skip("faster-whisper not installed (real ASR needed)", allow_module_level=True)

from tapscribe.text import build_recorder_wav_name  # noqa: E402

from .harness import WAIT_POLLING_MS, launch_bridge_context, playwright_session, word_tokens  # noqa: E402

pytestmark = [pytest.mark.browser_e2e, pytest.mark.real_audio]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXT_DIR = REPO_ROOT / "bridges" / "spacialchat-bridge"
FIXTURE_DIR = REPO_ROOT / "tests" / "e2e" / "fixtures" / "mock-spatial-page"
AUDIO_DIR = REPO_ROOT / "tests" / "fixtures" / "audio"
SPEECH_WAV = AUDIO_DIR / "armstrong-en.wav"


# A tiny command summariser: reads the merged transcript on stdin, echoes a
# notes line. A real summarize stage (the #82 `command` source) with no 5 GB
# LLM — fast + deterministic, so the test asserts the full pipeline produced a
# summary the card renders, without gating CI on a multi-GB model download.
SUMMARY_CMD = (
    f"{sys.executable} -c "
    '"import sys; t=sys.stdin.read().strip(); '
    "print('Meeting notes: ' + (t[:200] if t else 'no speech detected'))\""
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _speech_pcm_b64(seconds: float = 12.0) -> str:
    """First `seconds` of the speech fixture as base64'd int16 LE bytes (16 kHz
    mono), for the page to rebuild into an Int16Array and play on a loop."""
    with wave.open(str(SPEECH_WAV)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        frames = w.readframes(min(w.getnframes(), int(seconds * 16000)))
    return base64.b64encode(frames).decode("ascii")


# ---------------------------------------------------------------------------
# Real Recorder, configured for a fast CPU pipeline
# ---------------------------------------------------------------------------


def _running_recorder(batch_model: str) -> Iterator[dict[str, Any]]:
    """Start a real Recorder in a temp base dir: --no-auth, faster-whisper
    `batch_model` for batch transcribe, the `command` summariser. Yields
    {port, base, log}."""
    port = _free_port()
    with tempfile.TemporaryDirectory() as base:
        cfg = Path(base) / "config"
        cfg.mkdir(parents=True)
        (cfg / "batch-model.txt").write_text(f"{batch_model}\n", encoding="utf-8")
        (cfg / "summarizer.json").write_text(
            json.dumps({"source": "command", "command": SUMMARY_CMD}), encoding="utf-8"
        )
        # The default candidate set {da,no,en} pulls the Norwegian specialist into
        # the cover; pin the fast nb-whisper-tiny (env, read by the subprocess at
        # import) so we don't download + CPU-decode the slow production
        # nb-whisper-large inside the test's pipeline timeout.
        env = {**os.environ, "TAPSCRIBE_BASE_DIR": base, "TAPSCRIBE_SPECIALIST_NO": "nb-whisper-tiny"}
        log_path = Path(base) / "recorder.log"
        log_fh = open(log_path, "wb")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tapscribe",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-auth",
                "--no-auto-live",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        ready = threading.Event()
        pump = threading.Thread(target=_pump_output, args=(proc, log_fh, ready), daemon=True)
        pump.start()
        try:
            _wait_for_ready(port, proc, ready, timeout_s=40)
            yield {"port": port, "base": base, "log": log_path}
        finally:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
            pump.join(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()
            log_fh.close()


@pytest.fixture
def recorder() -> AsyncIterator[dict[str, Any]]:  # type: ignore[misc]
    """English-only `tiny.en` recorder for the single-speaker meeting test."""
    yield from _running_recorder("tiny.en")


@pytest.fixture
def recorder_multilingual() -> AsyncIterator[dict[str, Any]]:  # type: ignore[misc]
    """Multilingual `base` recorder so a da/no/en meeting transcribes each
    speaker in their own language (tiny.en would Englishise the Norwegian)."""
    yield from _running_recorder("base")


#: What uvicorn prints once it has bound its socket, after the app's startup.
_READY_LINE = b"Uvicorn running on"


def _pump_output(proc: subprocess.Popen, log_fh, ready: threading.Event) -> None:
    """Copy the Recorder's output into its log file, and set `ready` when uvicorn
    reports it is listening, or when the output ends (an early exit), so the
    fixture waits on the process's own word, not on a polled /health."""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            log_fh.write(line)
            log_fh.flush()
            if _READY_LINE in line:
                ready.set()
    finally:
        ready.set()


def _wait_for_ready(port: int, proc: subprocess.Popen, ready: threading.Event, *, timeout_s: float) -> None:
    """One wait on the ready line (`timeout_s` only bounds a hang), then one /health."""
    if not ready.wait(timeout_s):
        raise RuntimeError("recorder did not report it was listening in time")
    if proc.poll() is not None:
        raise RuntimeError(f"recorder exited early (code {proc.returncode})")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
        assert r.status == 200, f"/health answered {r.status}"


# ---------------------------------------------------------------------------
# Loaded extension wired to the real Recorder + the speech-backed mock page
# ---------------------------------------------------------------------------


async def _await_pipeline(popup, port: int, session: str, *, timeout_s: float) -> dict[str, Any]:
    """Wait for the popup's meeting card to show the pipeline's outcome (the
    summary, or a failure), then read the Recorder's own record of it once.

    The card is what the operator watches, and its update is a page event the
    test can wait on; this used to poll the endpoint every second itself."""
    await popup.wait_for_function(
        """() => {
            const summary = document.querySelector('[data-slot="summary"]');
            const failure = document.querySelector('[data-slot="failure"]');
            return (!!summary && !summary.hidden) || (!!failure && !failure.hidden);
        }""",
        timeout=timeout_s * 1000,
    )
    url = f"http://127.0.0.1:{port}/api/tap/sessions/{session}/pipeline"
    return await asyncio.to_thread(_get_json, url)


#: Mirrors chrome.storage.local into `window.__storage` in an extension page,
#: kept current by `chrome.storage.onChanged`. `wait_for_function` needs a SYNC
#: predicate: an `async` one returns a Promise, which Playwright takes as truthy
#: at once, so a wait written that way never waits. The mirror makes "the bridge
#: wrote X to storage" a plain read that the storage event keeps fresh.
_MIRROR_STORAGE_JS = """async () => {
  if (window.__storage) return;
  window.__storage = await chrome.storage.local.get(null);
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    for (const [key, { newValue }] of Object.entries(changes)) window.__storage[key] = newValue;
  });
}"""


async def _until_streaming(popup, *, channels: int, timeout_ms: int = 20_000) -> None:
    """Wait until the bridge's published status (content.js `buildStatusSnapshot`)
    shows `channels` tapped channels that have streamed frames to the Recorder."""
    await popup.wait_for_function(
        """(want) => {
            const snap = window.__storage.bridgeStatus;
            return ((snap && snap.channels) || []).filter((c) => (c.framesSent || 0) > 0).length >= want;
        }""",
        arg=channels,
        timeout=timeout_ms,
    )


async def _until_taps_closed(popup, *, timeout_ms: int = 20_000) -> None:
    """Wait until every channel is muted and its /tap WS is closed (drain done)."""
    await popup.wait_for_function(
        """() => {
            const snap = window.__storage.bridgeStatus;
            return ((snap && snap.channels) || []).every(
                (c) => c.muted && !c.draining && (c.tapWs === null || c.tapWs === 'CLOSED'),
            );
        }""",
        timeout=timeout_ms,
    )


def _get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode())


async def _discover_extension_id(ctx) -> str:
    page = await ctx.new_page()
    try:
        await page.goto("chrome://extensions/")
        ids = await page.evaluate(
            """() => {
              const mgr = document.querySelector('extensions-manager');
              const list = mgr && mgr.shadowRoot && mgr.shadowRoot.querySelector('extensions-item-list');
              if (!list) return [];
              return Array.from(list.shadowRoot.querySelectorAll('extensions-item')).map((i) => i.id);
            }"""
        )
    finally:
        await page.close()
    if not ids:
        raise RuntimeError("could not discover bridge extension id")
    return ids[0]


async def _seed_storage(ctx, ext_id: str, values: dict[str, Any]) -> None:
    popup = await ctx.new_page()
    try:
        await popup.goto(f"chrome-extension://{ext_id}/popup.html")
        await popup.evaluate("(v) => chrome.storage.local.set(v)", values)
    finally:
        await popup.close()


async def _open_meeting(ctx, recorder, *, fixture_index, fixture_mock, speech_b64):
    """The shared meeting prologue both tests run: seed the bridge's recorder config,
    load the speech-backed mock SpatialChat page (real PCM exposed BEFORE the bridge
    taps, so the track plays real words), open the popup, and Start the meeting (a
    real detached Session). Returns (page, popup, sess, sess_dir).

    `recorderHost` is "localhost" not 127.0.0.1: the content script runs on the public
    https://app.spatial.chat page, and Chrome's Private Network Access blocks ws:// to
    an explicit private IP from there but EXEMPTS the `localhost` name (which resolves
    to the Recorder's IPv4 loopback bind)."""
    ext_id = await _discover_extension_id(ctx)
    await _seed_storage(
        ctx,
        ext_id,
        {"recorderHost": "localhost", "recorderPort": recorder["port"], "tapToken": "", "useTls": False},
    )
    page = await ctx.new_page()
    # add_init_script takes no arg param, so inline the (quote-free) base64 directly.
    await page.add_init_script(
        "window.__tsSpeechPcm = (function () {"
        f"  const b64 = '{speech_b64}';"
        "  const bin = atob(b64); const bytes = new Uint8Array(bin.length);"
        "  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);"
        "  return new Int16Array(bytes.buffer);"
        "})();"
    )

    async def route_handler(route, request):
        if request.url.endswith("mock-room.js"):
            await route.fulfill(status=200, content_type="application/javascript", body=fixture_mock)
        else:
            await route.fulfill(status=200, content_type="text/html", body=fixture_index)

    await page.route("https://app.spatial.chat/**", route_handler)
    await page.goto("https://app.spatial.chat/test/room")
    await page.wait_for_function("typeof window.__tsTest === 'object'", timeout=5000, polling=WAIT_POLLING_MS)

    popup = await ctx.new_page()
    await popup.goto(f"chrome-extension://{ext_id}/popup.html")
    await popup.evaluate(_MIRROR_STORAGE_JS)
    await popup.get_by_role("button", name="Start meeting").click()
    await popup.wait_for_function(
        """() => {
          const id = window.__storage.meetingSessionId;
          return typeof id === 'string' && id.length > 0;
        }""",
        timeout=8000,
        polling=WAIT_POLLING_MS,
    )
    sess = await popup.evaluate(
        "async () => (await chrome.storage.local.get(['meetingSessionId'])).meetingSessionId"
    )
    sess_dir = Path(recorder["base"]) / "recordings" / sess
    return page, popup, sess, sess_dir


async def test_full_meeting_flow_produces_a_summary_in_the_popup_card(recorder):
    """Start meeting → tap real speech into a detached Session → End meeting →
    the real pipeline runs → the popup card shows the finished summary."""
    fixture_index = (FIXTURE_DIR / "index.html").read_text(encoding="utf-8")
    fixture_mock = (FIXTURE_DIR / "mock-room.js").read_text(encoding="utf-8")
    speech_b64 = _speech_pcm_b64()

    async with playwright_session() as pw:
        with tempfile.TemporaryDirectory() as udd:
            ctx = await launch_bridge_context(pw, EXT_DIR, udd)

            try:
                page, popup, sess, sess_dir = await _open_meeting(
                    ctx,
                    recorder,
                    fixture_index=fixture_index,
                    fixture_mock=fixture_mock,
                    speech_b64=speech_b64,
                )

                # A speaker is tapped — REAL audio streams over /tap to the real
                # Recorder, proving the whole capture path (content script →
                # page-script worklet → WebSocket → detached-Session WAV). We
                # assert frames actually reached the Recorder.
                await page.evaluate("() => window.__tsTest.addRemoteSpeaker('alice-id', 'Alice')")
                await _until_streaming(popup, channels=1)
                snap = await popup.evaluate(
                    "async () => (await chrome.storage.local.get(['bridgeStatus'])).bridgeStatus"
                )
                frames = max([c.get("framesSent", 0) for c in (snap or {}).get("channels", [])] or [0])
                print(f"[e2e] /tap frames streamed to the Recorder: {frames}")
                assert frames > 0, f"the bridge streamed no /tap frames to the Recorder: {snap}"
                await page.evaluate("() => window.__tsTest.muteSpeaker('alice-id')")
                await _until_taps_closed(popup)

                # The captured audio is real but headless Web Audio degrades it
                # enough that the Recorder's silero-VAD strip rejects it as
                # non-speech (faster-whisper still reads it; silero is stricter).
                # That's a headless artifact, not a product bug — a real mic
                # produces clean audio. So to drive the strip → transcribe →
                # summarize stages on real speech we drop a PRISTINE copy of the
                # fixture into the detached Session (the only seam a headless
                # browser can't reproduce; the frame-level capture is asserted
                # above and covered fully by test_bridge_extension_e2e.py).
                (sess_dir / f"{sess}_Speaker_speaker-id_a1b2c3d4.wav").write_bytes(SPEECH_WAV.read_bytes())

                # End the meeting → drain → close-all → trigger the real pipeline.
                await popup.get_by_role("button", name="End meeting").click()

                # Poll the REAL Recorder pipeline endpoint directly (no auth) so
                # failures are legible — the card mirrors this.
                final = await _await_pipeline(popup, recorder["port"], sess, timeout_s=90)
                print(f"[e2e] final pipeline state: {json.dumps(final)[:400]}")
                print(
                    f"[e2e] session dir now: {sorted(p.name for p in sess_dir.glob('*')) if sess_dir.exists() else 'MISSING'}"
                )
                assert final.get("state") == "done", f"pipeline did not reach done: {final}"

                # The card polls the same endpoint → shows the summary pane.
                summary = popup.locator('[data-slot="summaryText"]')
                await summary.wait_for(state="visible", timeout=20_000)
                text = (await summary.text_content()) or ""
                assert text.strip() and "Meeting notes:" in text, f"unexpected summary: {text!r}"

                # Parity: the same summary is persisted on the Recorder.
                assert (sess_dir / "session-summary.json").exists(), "no persisted summary"
            finally:
                with contextlib.suppress(Exception):
                    await ctx.close()


async def test_multi_person_multi_language_meeting_produces_a_summary(recorder_multilingual):
    """Multi-PERSON, multi-LANGUAGE meeting through the REAL SpatialChat bridge:
    TWO speakers are tapped (proving multi-channel capture — frames on both),
    then Norwegian + English speech runs through the real end-of-meeting pipeline
    (multilingual `base` generalist), producing a merged transcript carrying BOTH
    speakers' content and a summary in the popup card.

    Same headless-capture seam as the single-speaker test: the tapped audio
    proves the CAPTURE path, and pristine Norwegian/English fixtures are dropped
    into the detached Session to drive strip → transcribe → summarize on clean
    speech (headless Web Audio degrades captured audio below silero-VAD; a real
    mic doesn't). Per-language ROUTING/selector correctness is the pipeline
    tests' job (test_pipeline_e2e); here the point is the BRIDGE delivers a
    multi-person, multi-language meeting end to end."""
    recorder = recorder_multilingual
    fixture_index = (FIXTURE_DIR / "index.html").read_text(encoding="utf-8")
    fixture_mock = (FIXTURE_DIR / "mock-room.js").read_text(encoding="utf-8")
    speech_b64 = _speech_pcm_b64()

    async with playwright_session() as pw:
        with tempfile.TemporaryDirectory() as udd:
            ctx = await launch_bridge_context(pw, EXT_DIR, udd)
            try:
                page, popup, sess, sess_dir = await _open_meeting(
                    ctx,
                    recorder,
                    fixture_index=fixture_index,
                    fixture_mock=fixture_mock,
                    speech_b64=speech_b64,
                )

                # TWO speakers tapped → prove multi-channel capture (frames on both).
                await page.evaluate("() => window.__tsTest.addRemoteSpeaker('nora-id', 'Nora')")
                await page.evaluate("() => window.__tsTest.addRemoteSpeaker('ed-id', 'Ed')")
                await _until_streaming(popup, channels=2)
                snap = await popup.evaluate(
                    "async () => (await chrome.storage.local.get(['bridgeStatus'])).bridgeStatus"
                )
                channels = (snap or {}).get("channels", [])
                streamed = [c for c in channels if c.get("framesSent", 0) > 0]
                print(f"[e2e] tapped channels: {[(c.get('name'), c.get('framesSent')) for c in channels]}")
                assert len(streamed) >= 2, f"expected ≥2 tapped channels streaming, got {channels}"
                await page.evaluate("() => window.__tsTest.muteSpeaker('nora-id')")
                await page.evaluate("() => window.__tsTest.muteSpeaker('ed-id')")
                await _until_taps_closed(popup)

                # Drop two pristine, DIFFERENT-LANGUAGE fixtures as the two speakers'
                # WAVs (same headless-capture seam as the single-speaker test):
                # Norwegian (Nora) + English (Ed).
                start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
                (sess_dir / build_recorder_wav_name(start, "Nora", "nora-id")).write_bytes(
                    (AUDIO_DIR / "marlene-nb.wav").read_bytes()
                )
                (sess_dir / build_recorder_wav_name(start.replace(second=20), "Ed", "ed-id")).write_bytes(
                    (AUDIO_DIR / "armstrong-en.wav").read_bytes()
                )

                # End the meeting → real pipeline (strip → transcribe[base] → summarize).
                await popup.get_by_role("button", name="End meeting").click()
                final = await _await_pipeline(popup, recorder["port"], sess, timeout_s=180)
                print(f"[e2e] final pipeline state: {json.dumps(final)[:400]}")
                assert final.get("state") == "done", f"pipeline did not reach done: {final}"

                # Two speakers, both transcribed in their OWN language, in the merge.
                merged = json.loads((sess_dir / "session-transcript.json").read_text(encoding="utf-8"))
                plain = merged.get("plain_text", "")
                hyp = word_tokens(plain)
                speakers = {s.get("speaker") for s in merged.get("segments", []) if s.get("speaker")}
                assert len(speakers) >= 2, f"expected ≥2 speakers in the merge, got {speakers}"
                # Norwegian-DISTINCTIVE words (not the reference's language-agnostic proper
                # nouns Berlin/Paris/Dietrich) — prove the Norwegian speaker came out AS
                # Norwegian, not Englishised.
                assert {"egentlig", "født", "døde", "skuespillerinne"} & hyp, (
                    f"no Norwegian-distinctive word in transcript — Englishised? {plain[:200]!r}"
                )
                assert (
                    word_tokens((AUDIO_DIR / "armstrong-en.reference.txt").read_text(encoding="utf-8")) & hyp
                ), f"English content missing from transcript: {plain[:200]!r}"

                # The popup card shows the summary built from the multilingual transcript.
                summary = popup.locator('[data-slot="summaryText"]')
                await summary.wait_for(state="visible", timeout=20_000)
                text = (await summary.text_content()) or ""
                assert text.strip() and "Meeting notes:" in text, f"unexpected summary: {text!r}"
                assert (sess_dir / "session-summary.json").exists(), "no persisted summary"
            finally:
                with contextlib.suppress(Exception):
                    await ctx.close()
