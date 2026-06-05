#!/usr/bin/env python3
"""Reusable screenshotter for the TapScribe UI prototypes.

The prototypes are static HTML/CSS/JS with NO backend. Each one loads the
shared `../_shared/mock-data.js` as an ES module, and Chromium blocks ES-module
imports over file:// (CORS, origin "null"). So instead of file://, this tool
spins up a throwaway in-process static HTTP server rooted at the repo, shoots
the page over http://127.0.0.1:<ephemeral>, then tears the server down. One
self-contained invocation per screenshot — no server to manage.

It mirrors the proven Playwright recipe in tests/e2e/test_dashboard_ui.py
(headless Chromium, fixed viewport, wait_until="domcontentloaded", full_page)
and FAILS LOUDLY (exit 3) on any uncaught pageerror or console.error so a
broken prototype can't ship a blank PNG.

Example (run from the repo root):

    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \\
      /tmp/pw-venv/bin/python prototypes/_shared/shoot.py \\
        --html prototypes/studio/index.html \\
        --out docs/prototype-shots/studio/01-overview.png \\
        --wait-for "#app" \\
        --click "[data-shot='session']" --wait-for ".waveform" \\
        --delay 350

Ordered actions (--wait-for / --click / --eval) fire in the order written.
"""

from __future__ import annotations

import argparse
import functools
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_a):  # silence the access log
        pass


def _start_server(root: Path) -> tuple[ThreadingHTTPServer, int]:
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", help="prototype html, relative to --serve-root")
    ap.add_argument("--url", help="explicit url (skips the static server)")
    ap.add_argument("--serve-root", default=".", help="dir served over http (default: cwd / repo root)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--wait-for", action="append", default=[], dest="wait_for")
    ap.add_argument("--click", action="append", default=[], dest="click")
    ap.add_argument("--eval", action="append", default=[], dest="eval_js")
    ap.add_argument("--delay", type=int, default=200)
    ap.add_argument("--no-full", action="store_true")
    ap.add_argument("--allow-errors", action="store_true")
    args = ap.parse_args()

    if not args.url and not args.html:
        ap.error("one of --html or --url is required")

    # Ordered action stream rebuilt from raw argv so wait/click/eval interleave
    # in the order the caller wrote them (argparse alone loses cross-flag order).
    actions: list[tuple[str, str]] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--wait-for", "--click", "--eval") and i + 1 < len(argv):
            actions.append((a.lstrip("-"), argv[i + 1]))
            i += 2
        else:
            i += 1

    httpd = None
    try:
        if args.url:
            url = args.url
        else:
            root = Path(args.serve_root).resolve()
            html = Path(args.html).resolve()
            rel = html.relative_to(root)  # raises if html escapes the root
            httpd, port = _start_server(root)
            url = f"http://127.0.0.1:{port}/{rel.as_posix()}"

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)

        errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": args.width, "height": args.height},
                device_scale_factor=2,  # crisp text in the PNGs
            )
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None,
            )
            page.goto(url, wait_until="domcontentloaded")

            for kind, val in actions:
                if kind == "wait-for":
                    page.wait_for_selector(val, timeout=8000)
                elif kind == "click":
                    page.click(val, timeout=8000)
                    page.wait_for_timeout(250)
                elif kind == "eval":
                    page.evaluate(val)
                    page.wait_for_timeout(150)

            if args.delay:
                page.wait_for_timeout(args.delay)

            page.screenshot(path=str(out), full_page=not args.no_full)
            browser.close()

        if errors and not args.allow_errors:
            print(f"FAIL: {len(errors)} page error(s) while rendering {url}:", file=sys.stderr)
            for e in errors[:20]:
                print(f"  - {e}", file=sys.stderr)
            return 3

        print(f"OK  {out}  ({url})")
        return 0
    finally:
        if httpd is not None:
            httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
