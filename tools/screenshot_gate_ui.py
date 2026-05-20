"""Drive the live dashboard with Playwright and capture screenshots of
the new gate / buffer-transcription UI.

Standalone — not part of the pytest suite — because pytest's e2e
harness loads Silero on the test recorder, which is slower than we
want for a screenshot script. This file stands up uvicorn against a
hand-rolled recorder, injects a synthetic ActiveStream + live_info
state via the test seams, and saves four PNGs under
docs/gate-ui-shots/ for the PR description.

Run with:
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \\
    python tools/screenshot_gate_ui.py
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import ActiveStream, Recorder

SHOTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "gate-ui-shots"
CHROMIUM_PATH = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _AliveProc:
    """LiveChannel.running() returns True iff `_proc.poll() is None`."""

    def poll(self):
        return None


async def _seed_state(recorder: Recorder) -> None:
    """Inject a couple of ActiveStream rows that look like a real
    meeting — one with an in-flight buffer, one without — plus mark
    the live channel as "running" so the UI doesn't hide gate
    controls behind the "stopped" actions panel."""
    recorder.live._proc = _AliveProc()  # type: ignore[assignment]
    recorder.live.info["state"] = "running"
    recorder.live.info["pid"] = "12345"
    recorder.live.info["started_at"] = datetime.now(timezone.utc).isoformat()

    await recorder.streams.register(
        ActiveStream(
            conn_id="conn-alice",
            identity="alice",
            name="Alice Example",
            filename="alice.wav",
            started_at=datetime.now(timezone.utc),
            bytes_received=320_000,
            level=0.45,
            lag_s=0.4,
            buffer_transcription="could you reinstall the database",
            record=True,
            live=True,
        )
    )
    await recorder.streams.register(
        ActiveStream(
            conn_id="conn-bob",
            identity="bob",
            name="Bob Example",
            filename="bob.wav",
            started_at=datetime.now(timezone.utc),
            bytes_received=180_000,
            level=0.0,
            lag_s=0.1,
            buffer_transcription="",
            record=True,
            live=True,
        )
    )


def _boot_uvicorn(port: int) -> threading.Thread:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # Poll briefly until the port answers.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return t


async def main() -> None:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Per-run tmp dirs so we don't clobber any real recordings.
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="tapscribe-ui-shot-"))
    (tmp / "recordings").mkdir()
    (tmp / "config").mkdir()
    _config.RECORDINGS_DIR = tmp / "recordings"
    _config.CONFIG_DIR = tmp / "config"
    _config.AUTH_ENABLED = False
    _config.AUTO_START_LIVE = False

    recorder = Recorder(
        recordings_dir=tmp / "recordings",
        config_dir=tmp / "config",
        # Default gate_kind="tapscribe" so the screenshot shows the
        # new selector + sliders at their defaults.
        live_config=LiveConfig(
            model="tiny.en",
            language="en",
            host="127.0.0.1",
            port=18000,
        ),
        use_mlx=False,
        auth_password_file=tmp / ".auth-password",
    )
    app.state.recorder = recorder
    app.dependency_overrides[get_recorder] = lambda: recorder
    await _seed_state(recorder)

    port = _free_port()
    _boot_uvicorn(port)
    base_url = f"http://127.0.0.1:{port}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=CHROMIUM_PATH
        )
        try:
            ctx = await browser.new_context(viewport={"width": 1500, "height": 1000})
            page = await ctx.new_page()
            await page.goto(base_url, wait_until="domcontentloaded")

            # Let the dashboard's /api/state poll land at least once
            # so all panels render with the seeded state.
            await page.wait_for_selector("#liveGateKindSelect", timeout=5_000)
            await page.wait_for_function(
                "document.querySelectorAll('.stream-row').length >= 2",
                timeout=5_000,
            )
            # Brief settle so the buffer-transcription row's hidden→shown
            # transition lands in the layout.
            await page.wait_for_timeout(300)

            # 1) Full dashboard — overview of the new controls in context.
            await page.screenshot(
                path=str(SHOTS_DIR / "01-dashboard-default-tapscribe-gate.png"),
                full_page=True,
            )

            # 2) Focused crop of the live-channel card showing the gate
            #    selector + the three knobs at their defaults.
            # The dashboard uses <article> or <section> wrappers per
            # panel; the simplest reliable crop is the element that
            # contains the live header element ("LIVE CHANNEL").
            live_card_handle = await page.evaluate_handle(
                """() => {
                    const gate = document.getElementById('liveGateKindSelect');
                    if (!gate) return null;
                    let el = gate;
                    while (el && el.parentElement) {
                        const cs = getComputedStyle(el);
                        if (el.offsetWidth > 220 && el.offsetHeight > 200 &&
                            (cs.border !== '0px none rgb(0, 0, 0)' || cs.background !== 'rgba(0, 0, 0, 0)')) {
                            return el;
                        }
                        el = el.parentElement;
                    }
                    return gate.closest('section, article, aside, div');
                }"""
            )
            live_card = live_card_handle.as_element()
            if live_card is None:
                live_card = await page.query_selector("#liveGateKindSelect")
            assert live_card is not None
            await live_card.screenshot(
                path=str(SHOTS_DIR / "02-live-channel-gate-controls.png")
            )

            # 3) Tap-row crop showing the in-flight buffer_transcription
            #    line beneath Alice's row.
            alice_row = await page.query_selector(
                ".stream-row-wrap:has(.fg:text('Alice Example'))"
            )
            if alice_row is None:
                alice_row = await page.query_selector("section.card:has(.stream-row-wrap)")
            assert alice_row is not None
            # Screenshot the row + its associated buffer line (template
            # wraps both in the same root .stream-row template fragment).
            await alice_row.screenshot(
                path=str(SHOTS_DIR / "03-tap-row-with-in-flight-buffer.png")
            )

            # 4) Flip the gate selector to "backend" to show the
            #    inactive-but-tunable state for the screenshots.
            await page.select_option("#liveGateKindSelect", "backend")
            await page.wait_for_timeout(200)
            await page.screenshot(
                path=str(SHOTS_DIR / "04-gate-kind-set-to-backend.png"),
                full_page=True,
            )

            print(f"Wrote screenshots to {SHOTS_DIR}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
