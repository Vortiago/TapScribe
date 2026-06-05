# TapScribe UI prototypes — "show and throw"

Three **disposable** explorations of where the TapScribe dashboard UI/UX could
go. They are not wired to the backend — each is a self-contained static
HTML/CSS/JS mockup that renders the **same** hand-built scenario from
[`_shared/mock-data.js`](./_shared/mock-data.js), so they're directly
comparable. They deliberately showcase features the backend doesn't implement
yet (waveform + strip-silence cut preview, cross-session per-microphone
speaker profiles, diarization of a multi-person room tap, primary/secondary
language per speaker).

Pick a direction (or mix ideas); then we build the chosen one for real. Nothing
here is meant to be merged as-is.

## The three directions

| Prototype | Paradigm | Feel |
|---|---|---|
| [`studio/`](./studio/) | **Pro-audio / DAW timeline** — a horizontal multi-track waveform workspace; strip-silence cuts and diarization live *on* the timeline; track headers are mixer strips. | Reaper / Audition (dark, saturated) |
| [`console/`](./console/) | **Live-ops terminal** — a dense status board of tiles + sortable data tables, sparklines, status pills, and a ⌘K command palette. | Trading terminal / Grafana (near-black, neon) |
| [`clarity/`](./clarity/) | **Calm document app** — content-first, light mode, the transcript reads like an article; a People directory and a session setup wizard. | Notion / Linear-light (airy, rounded) |

Screenshots of every screen live in
[`../docs/prototype-shots/`](../docs/prototype-shots/).

## Viewing one locally

The pages load `../_shared/mock-data.js` as an ES module, which Chromium blocks
over `file://` (CORS, origin `null`). Serve over HTTP instead:

```bash
# from the repo root
python3 -m http.server 8000
# then open http://localhost:8000/prototypes/studio/index.html
```

Each prototype exposes `window.gotoView('<name>')` to switch screens (the names
differ per prototype; see its `index.html`).

## Regenerating the screenshots

[`_shared/shoot.py`](./_shared/shoot.py) drives headless Chromium, serving the
repo over an ephemeral HTTP port (so module imports work) and **failing on any
page error** so a broken prototype can't ship a blank PNG. In Claude Code's
managed environment (Chromium pre-installed under `/opt/pw-browsers`):

```bash
python -m venv /tmp/pw-venv && /tmp/pw-venv/bin/pip install 'playwright==1.56.*'
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /tmp/pw-venv/bin/python \
  prototypes/_shared/shoot.py \
  --html prototypes/studio/index.html \
  --out docs/prototype-shots/studio/01-overview.png \
  --eval "window.gotoView('overview')" --delay 350
```

## Shared scenario

All three render **"Nordic Sync — 2026-05-28"**, a ~48-minute mixed-language
meeting: Atle Håvsø (Shure MV7, Norwegian/English), Mette Sørensen (AirPods
Pro 2, Danish/English), an **Oslo Conference Room** shared mic (Jabra Speak
710) carrying two people that diarization splits into Speaker A (nb) / Speaker
B (en), and guest James Park (English). `computeRegions()` in `mock-data.js` is
a faithful-enough stand-in for the real Silero + low-energy-filter strip-silence
pipeline, so dragging the gap/pad/floor knobs re-cuts the waveform live and
identically across all three prototypes.
