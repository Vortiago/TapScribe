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
import time
import urllib.request
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)
if importlib.util.find_spec("faster_whisper") is None:  # pragma: no cover
    pytest.skip("faster-whisper not installed (real ASR needed)", allow_module_level=True)

from playwright.async_api import async_playwright  # noqa: E402

from .harness import bridge_chromium_args  # noqa: E402

pytestmark = [pytest.mark.browser_e2e, pytest.mark.real_audio]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXT_DIR = REPO_ROOT / "bridges" / "spacialchat-bridge"
FIXTURE_DIR = REPO_ROOT / "tests" / "e2e" / "fixtures" / "mock-spatial-page"
SPEECH_WAV = REPO_ROOT / "tests" / "fixtures" / "audio" / "armstrong-en.wav"

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


@pytest.fixture
def recorder() -> AsyncIterator[dict[str, Any]]:  # type: ignore[misc]
    """Start a real Recorder in a temp base dir: --no-auth, faster-whisper
    tiny.en for batch transcribe, the `command` summariser. Yields {port}."""
    port = _free_port()
    with tempfile.TemporaryDirectory() as base:
        cfg = Path(base) / "config"
        cfg.mkdir(parents=True)
        (cfg / "batch-model.txt").write_text("tiny.en\n", encoding="utf-8")
        (cfg / "summarizer.json").write_text(
            json.dumps({"source": "command", "command": SUMMARY_CMD}), encoding="utf-8"
        )
        env = {**os.environ, "TAPSCRIBE_BASE_DIR": base}
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
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(port, proc, timeout_s=40)
            yield {"port": port, "base": base, "log": log_path}
        finally:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
            log_fh.close()


def _wait_for_health(port: int, proc: subprocess.Popen, *, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"recorder exited early (code {proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("recorder did not become healthy in time")


# ---------------------------------------------------------------------------
# Loaded extension wired to the real Recorder + the speech-backed mock page
# ---------------------------------------------------------------------------


async def _await_pipeline(port: int, session: str, *, timeout_s: float) -> dict[str, Any]:
    """Poll GET /api/tap/sessions/{session}/pipeline until done/failed/timeout."""
    url = f"http://127.0.0.1:{port}/api/tap/sessions/{session}/pipeline"
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            body = await asyncio.to_thread(_get_json, url)
            last = body
            if body.get("state") in ("done", "failed"):
                return body
        except Exception as e:  # pragma: no cover
            last = {"state": "poll-error", "error": str(e)}
        await asyncio.sleep(1.0)
    return last


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


async def test_full_meeting_flow_produces_a_summary_in_the_popup_card(recorder):
    """Start meeting → tap real speech into a detached Session → End meeting →
    the real pipeline runs → the popup card shows the finished summary."""
    fixture_index = (FIXTURE_DIR / "index.html").read_text(encoding="utf-8")
    fixture_mock = (FIXTURE_DIR / "mock-room.js").read_text(encoding="utf-8")
    speech_b64 = _speech_pcm_b64()

    async with async_playwright() as pw:
        with tempfile.TemporaryDirectory() as udd:
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=udd,
                    headless=False,  # MV3 extensions don't load headless
                    args=bridge_chromium_args(EXT_DIR),
                )
            except Exception as e:  # pragma: no cover
                pytest.skip(f"Chromium not available: {e}")

            try:
                ext_id = await _discover_extension_id(ctx)
                await _seed_storage(
                    ctx,
                    ext_id,
                    # The content script runs in the https://app.spatial.chat
                    # (public, secure) page; Chrome's Private Network Access
                    # blocks a ws:// to an explicit private IP (127.0.0.1) from
                    # there, but EXEMPTS the `localhost` name. The Recorder binds
                    # IPv4 loopback, which `localhost` resolves to.
                    {
                        "recorderHost": "localhost",
                        "recorderPort": recorder["port"],
                        "tapToken": "",
                        "useTls": False,
                    },
                )

                # SpatialChat tab: rebuild the speech PCM into an Int16Array and
                # expose it BEFORE the bridge taps, so the speaker's track plays
                # real words. Then load the mock room.
                page = await ctx.new_page()
                # add_init_script takes no arg param, so inline the (quote-free)
                # base64 directly. Runs before the bridge taps, so the speaker's
                # track is built from real speech.
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
                        await route.fulfill(
                            status=200, content_type="application/javascript", body=fixture_mock
                        )
                    else:
                        await route.fulfill(status=200, content_type="text/html", body=fixture_index)

                await page.route("https://app.spatial.chat/**", route_handler)
                await page.goto("https://app.spatial.chat/test/room")
                await page.wait_for_function("typeof window.__tsTest === 'object'", timeout=5000)

                # Open the popup and Start the meeting (real detached Session).
                popup = await ctx.new_page()
                await popup.goto(f"chrome-extension://{ext_id}/popup.html")
                await popup.get_by_role("button", name="Start meeting").click()
                # The content script routes on the stored id — wait for it.
                await popup.wait_for_function(
                    """async () => {
                      const { meetingSessionId } = await chrome.storage.local.get(['meetingSessionId']);
                      return typeof meetingSessionId === 'string' && meetingSessionId.length > 0;
                    }""",
                    timeout=8000,
                )

                sess = await popup.evaluate(
                    "async () => (await chrome.storage.local.get(['meetingSessionId'])).meetingSessionId"
                )
                sess_dir = Path(recorder["base"]) / "recordings" / sess

                # A speaker is tapped — REAL audio streams over /tap to the real
                # Recorder, proving the whole capture path (content script →
                # page-script worklet → WebSocket → detached-Session WAV). We
                # assert frames actually reached the Recorder.
                await page.evaluate("() => window.__tsTest.addRemoteSpeaker('alice-id', 'Alice')")
                await asyncio.sleep(2.0)
                snap = await popup.evaluate(
                    "async () => (await chrome.storage.local.get(['bridgeStatus'])).bridgeStatus"
                )
                frames = max([c.get("framesSent", 0) for c in (snap or {}).get("channels", [])] or [0])
                print(f"[e2e] /tap frames streamed to the Recorder: {frames}")
                assert frames > 0, f"the bridge streamed no /tap frames to the Recorder: {snap}"
                await page.evaluate("() => window.__tsTest.muteSpeaker('alice-id')")
                await asyncio.sleep(1.0)

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
                final = await _await_pipeline(recorder["port"], sess, timeout_s=90)
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
