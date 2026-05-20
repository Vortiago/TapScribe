"""Capture three UX variants of the speech-filter knob layout so the
operator can pick the one they want shipped.

Each variant injects a CSS override (and a tiny DOM tweak where needed)
into the live dashboard before screenshotting the live-channel card.
The recorder + state seeding are the same as screenshot_gate_ui.py;
this file only differs in what it does to the gate-knob rows.

Run with:
    PYTHONPATH=. PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \\
    python tools/screenshot_gate_ux_options.py
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
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
    def poll(self):
        return None


# --- CSS overrides + DOM patches per option ----------------------------------
#
# All three options keep the existing HTML structure; we restyle (and in
# option A inject tooltips) via add_style_tag / evaluate. That way the
# operator's "I prefer option X" is a small CSS-only follow-up, not a
# template rewrite.

# Option A: helper text moves into a tooltip on a small (ⓘ) icon.
#           Units pulled into the label so the help text is purely
#           "what does the number mean". Tightest vertical layout.
OPTION_A_CSS = """
  /* Override .live-row grid so the wider labels fit on one line. */
  .gate-knob-row.live-row {
    grid-template-columns: 110px 1fr auto;
    padding-left: 12px;
    align-items: center;
    margin-bottom: 4px;
  }
  .gate-knob-row .lbl {
    white-space: nowrap;
    font-size: 10px;
  }
  .gate-knob-row .input { max-width: 100%; }
  .gate-knob-row .dim.tiny {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px; height: 16px;
    border-radius: 50%;
    border: 1px solid var(--hairline-2);
    color: var(--fg-3);
    font-style: normal;
    cursor: help;
    font-size: 10px;
    text-indent: 0;
    line-height: 1;
  }
"""
OPTION_A_DOM = """
  () => {
    const replace = (id, tipText) => {
      const inp = document.getElementById(id);
      if (!inp) return;
      const helper = inp.parentElement.querySelector('.dim.tiny');
      if (!helper) return;
      helper.title = helper.textContent;
      helper.textContent = 'i';
    };
    // Move the units into the label so the tooltip carries only the
    // "what / why" text.
    const rename = (id, newLabel) => {
      const inp = document.getElementById(id);
      if (!inp) return;
      const lbl = inp.parentElement.querySelector('.lbl');
      if (lbl) lbl.textContent = newLabel;
    };
    rename('liveGateThreshold', 'voice threshold');
    rename('liveGateHangover',  'pause length (ms)');
    rename('liveGatePreRoll',   'lead-in (ms)');
    replace('liveGateThreshold');
    replace('liveGateHangover');
    replace('liveGatePreRoll');
  }
"""

# Option B: keep helper text but force the row onto a single line and let
#           the helper consume the remaining horizontal space (white-space
#           normal so it wraps gracefully if the column gets very narrow).
OPTION_B_CSS = """
  /* Three columns on one row: label | input | helper. Override the
     base .live-row grid (70px 1fr) so the wider label fits. */
  .gate-knob-row.live-row {
    grid-template-columns: 110px 80px 1fr !important;
    align-items: center;
    gap: 6px 8px;
    padding-left: 12px;
    margin-bottom: 4px;
  }
  .gate-knob-row .lbl {
    font-size: 10px;
    color: var(--fg-2);
    white-space: nowrap;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .gate-knob-row .input { max-width: 100%; }
  .gate-knob-row .dim.tiny {
    color: var(--fg-3);
    font-style: italic;
    white-space: normal;
    line-height: 1.25;
    font-size: 10px;
  }
"""

# Option C: label gets its own line; input + helper sit side by side on
#           the next line so the helper has room without wrapping to 4
#           lines. Slightly more vertical than A, less than current.
OPTION_C_CSS = """
  /* Stacked layout: label on its own row, input + helper on the next
     row so the helper has the full panel width minus the input. */
  .gate-knob-row.live-row {
    grid-template-columns: 90px 1fr !important;
    grid-template-areas:
      "lbl lbl"
      "inp hint";
    align-items: center;
    column-gap: 8px;
    row-gap: 2px;
    padding-left: 12px;
    margin-bottom: 6px;
  }
  .gate-knob-row .lbl  { grid-area: lbl; }
  .gate-knob-row .input { grid-area: inp; max-width: 100%; }
  .gate-knob-row .dim.tiny {
    grid-area: hint;
    font-style: italic;
    white-space: normal;
    line-height: 1.25;
    align-self: center;
  }
"""

OPTIONS = [
    ("A-tooltip-compact", OPTION_A_CSS, OPTION_A_DOM),
    ("B-single-row-helper", OPTION_B_CSS, None),
    ("C-stacked-tight", OPTION_C_CSS, None),
]


async def _seed_state(recorder: Recorder) -> None:
    recorder.live._proc = _AliveProc()  # type: ignore[assignment]
    recorder.live.info["state"] = "running"
    recorder.live.info["pid"] = "12345"
    recorder.live.info["started_at"] = datetime.now(timezone.utc).isoformat()


async def main() -> None:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="tapscribe-ux-opts-"))
    (tmp / "recordings").mkdir()
    (tmp / "config").mkdir()
    _config.RECORDINGS_DIR = tmp / "recordings"
    _config.CONFIG_DIR = tmp / "config"
    _config.AUTH_ENABLED = False
    _config.AUTO_START_LIVE = False

    recorder = Recorder(
        recordings_dir=tmp / "recordings",
        config_dir=tmp / "config",
        live_config=LiveConfig(
            model="tiny.en", language="en", host="127.0.0.1", port=18000
        ),
        use_mlx=False,
        auth_password_file=tmp / ".auth-password",
    )
    app.state.recorder = recorder
    app.dependency_overrides[get_recorder] = lambda: recorder
    await _seed_state(recorder)

    # Register one tap so the live-channel state shows "running" + has
    # data, but the gate-knob rows are what we're actually capturing.
    await recorder.streams.register(
        ActiveStream(
            conn_id="conn-alice",
            identity="alice",
            name="Alice Example",
            filename="alice.wav",
            started_at=datetime.now(timezone.utc),
            bytes_received=100_000,
            gate_open=True,
            buffer_transcription="",
        )
    )

    port = _free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    base_url = f"http://127.0.0.1:{port}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=CHROMIUM_PATH
        )
        try:
            for name, css, dom_script in OPTIONS:
                ctx = await browser.new_context(viewport={"width": 1500, "height": 1000})
                page = await ctx.new_page()
                await page.goto(base_url, wait_until="domcontentloaded")
                await page.wait_for_selector("#liveGateKindSelect", timeout=5_000)
                await page.add_style_tag(content=css)
                if dom_script is not None:
                    await page.evaluate(dom_script)
                await page.wait_for_timeout(150)

                handle = await page.evaluate_handle(
                    """() => {
                        const gate = document.getElementById('liveGateKindSelect');
                        if (!gate) return null;
                        let el = gate;
                        while (el && el.parentElement) {
                            if (el.matches('section, article, aside')) return el;
                            el = el.parentElement;
                        }
                        return gate.closest('section, article, aside, div');
                    }"""
                )
                el = handle.as_element()
                assert el is not None
                await el.screenshot(path=str(SHOTS_DIR / f"ux-option-{name}.png"))
                await ctx.close()
            print(f"Wrote 3 UX option screenshots under {SHOTS_DIR}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
