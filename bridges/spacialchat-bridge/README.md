# spacialchat-bridge

Chrome (MV3) extension that taps remote audio in a spatial.chat tab and
forwards it to a running TapScribe Recorder over the `/tap` WebSocket
endpoint.

See `../README.md` for the wire contract the Recorder expects. The short
version: one WebSocket per active speaker per utterance to
`ws://<recorder-host>:8001/tap?identity=<spatial-id>&name=<display>`
(plus `&session=<id>` while a [meeting](#bracketed-meetings-start-meeting)
is active), streaming raw 16 kHz mono int16 PCM in 20 ms (640-byte)
frames. The
Recorder fans that audio out internally to its supervised WhisperLiveKit
child (live captions) and to a per-utterance WAV on disk — the bridge
never sees the WhisperLiveKit protocol and doesn't POST settled lines
back. (See ADR-0002 for the architectural reasoning.)

## Files

```
spacialchat-bridge/
├── manifest.json      MV3 manifest
├── control-client.js  shared, classic-global tap-token control plane (loaded into both content.js and popup): new-session / detached-session create, pipeline trigger + poll, /health + tap-token probe
├── pipeline-view.js   pure poll → view-model mapper (ES module, popup-only): turns a raw pipeline poll into the card's phase / progress / summary / failure
├── content.js         ISOLATED-world content script: /tap WS lifecycle, status snapshot
├── page-script.js     MAIN-world script: LiveKit Room tap + 48k→16k AudioWorklet
├── popup.html         popup markup: tokens + popup.css + the module entry + the card <template>
├── popup.js           popup DOM shell (ES module): builds the UI from components, applies the presenter, wires actions
├── popup-presenter.js pure meetingView(state) — the popup's lifecycle decisions, DOM-free + unit-tested
├── popup-actions.js   DOM-free Start / End / Dismiss side-effects (inject control + storage), unit-tested
├── popup.css          popup shell layout on the shared tokens
├── tokens.css         design tokens (vendored from vanilla-components)
├── lib/               templates.js + component.js (vendored vanilla-web engine)
├── components/        vendored vanilla-components (button, status-dot, panel, table-shell, empty-state)
├── types.d.ts         ambient types for the popup module (the TapscribeControlClient global + chrome.*)
├── typecheck/         tsc --noEmit gate (devDep only, never shipped)
├── e2e/               Playwright popup E2E (devDep only, never shipped)
└── README.md          this file
```

### Popup architecture (vanilla-web)

The popup is a **vanilla-web** ES-module app — no build, no runtime deps. The
markup lives in `<template>`s (no HTML strings in JS); buttons, pills, the card
panel and the tap table come from the vendored **vanilla-components** library
(`components/`, each self-loading its own `@scope`'d CSS via `lib/templates.js`);
styling is the shared `tokens.css`. The split is **deep module + thin shell**:

- `popup-presenter.js` derives the whole meeting region's view-model
  (`meetingView(state)`) and `popup-actions.js` performs the Start/End/Dismiss
  effects — both **DOM-free**, so they're unit-tested with plain inputs / fakes
  (no jsdom). `pipeline-view.js` (the pure poll mapper) is tested the same way.
- `popup.js` is the thin DOM shell: it owns no "what to show" decisions, just
  applies the presenter's view-model to the components and re-derives from the
  recorder on every open. It's covered by the typecheck gate, and the rendering
  itself by a Playwright **popup E2E** (`e2e/`) that drives `popup.html` in
  Chromium — served statically, with `chrome.*` shimmed and the recorder
  stubbed — asserting the module graph loads, the components render, and each
  meeting-card state (running / done + copy / failed / active) shows correctly.
  Run it with `cd e2e && npm install && npx playwright install chromium && npm test`.

`control-client.js` stays a **classic global** (not an ES module): the content
script needs it in the isolated world, and MV3 content scripts can't be ES
modules. The module popup reads it as the `TapscribeControlClient` global
(typed via `types.d.ts`); `pipeline-view.js` is popup-only, so it's a normal ES
module the popup imports.

### Typecheck gate

`control-client.js`, `pipeline-view.js`, `popup-presenter.js`,
`popup-actions.js`, `popup.js`, the vendored `lib/` + `components/`, and
`types.d.ts` are all `// @ts-check`'d and gated by `tsc --noEmit --strict`
(the `bridge-typecheck` CI job, aligned with the canonical vanilla-web
tsconfig). Run it locally with:

```
cd bridges/spacialchat-bridge/typecheck && npm install && npm run typecheck
```

The gate is **incremental** — only `// @ts-check`'d files are checked;
`content.js` / `page-script.js` join it as they're typed.

`control-client.js` is loaded ahead of both `content.js` (via the
manifest's `content_scripts.js` array) and `popup.js` (via a `<script>`
tag in `popup.html`), exposing a single `TapscribeControlClient` global.
It centralises the tap-token HTTP/WS control calls — scheme derivation
from the TLS toggle, the mixed-content guard, the `Authorization: Bearer`
header, response parsing, and timeouts — so that logic lives in one
tested place rather than copy-pasted across the two worlds. Its surface
is create / trigger / poll / rotate / probe only: there is deliberately
no delete or prune call, keeping a leaked tap token's blast radius
bounded (deletion stays a dashboard Basic-auth action).

## How it works

1. `content.js` is injected into every `https://app.spatial.chat/*` page
   by the manifest and immediately injects `page-script.js` into the
   MAIN world so it can reach `window.room` (LiveKit Room instance).
2. `page-script.js` watches `window.room` (re-attaches every time
   spatial.chat swaps the room), subscribes to `trackSubscribed` /
   `trackMuted` / etc., and resamples each audio track to 16 kHz int16
   via an inline AudioWorklet. PCM frames are posted to the content
   script via `window.postMessage`. Because LiveKit can drop or coalesce
   those events across a reconnect or a proximity-driven resubscribe
   (spatial.chat is spatial audio), the same poll that watches for room
   swaps also runs a `reconcile()` sweep every tick: it re-derives the
   live roster from `room.remoteParticipants`, taps anyone whose
   subscribe/connect event was missed, and untaps anyone the room no
   longer lists. That self-healing pass is what stops an actively-talking
   participant from going missing while everyone else shows up.
3. `content.js` opens one `/tap` WebSocket per speaker per utterance on
   the first non-muted PCM frame and closes it on `trackMuted`. Muting
   is the end-of-utterance signal; the Recorder finalises a WAV on
   close and starts a fresh one when the next `/tap` opens.
4. A status snapshot is written to `chrome.storage.local` whenever
   anything changes, so the popup can show live per-speaker state
   without opening DevTools.

## Bracketed meetings (Start / End meeting)

By default the bridge taps into the Recorder's **global** session — the
`/tap` URL carries no `session` parameter and audio lands wherever the
Recorder's current session points. Click **Start meeting** in the popup
to bracket a recording into its own isolated **detached session**
instead:

1. The popup mints a detached session on the Recorder
   (`POST /api/tap/new-session` with `{"detached": true}`, via the shared
   `control-client.js`) and persists the server-minted id to
   `chrome.storage.local` as `meetingSessionId`.
2. The content script reads that id live (via `chrome.storage.onChanged`
   — no SpatialChat tab reload) and, while a meeting is active, stamps
   `&session=<id>` onto every `/tap` URL it opens. The affiliation is
   snapshotted at the start of each utterance, so an utterance — including
   any mid-utterance reconnect — always lands in one session, matching the
   Recorder snapshotting the session at WS open and stitching reconnects by
   `utterance_id`.
3. Capture stays automatic: speakers are tapped as they speak and mute
   still ends the utterance, exactly as without a meeting. The bracket
   only decides **which** session new taps feed.

The meeting's detached session **persists across SpatialChat room
changes** — moving rooms mid-meeting doesn't split or rotate it. While a
meeting is active, **Start meeting** is disabled (so a second start can't
orphan the first) and the in-page pill / tab title show a `meeting`
marker so you can tell capture is going into a bracketed session rather
than the global default. With no meeting active, taps carry no `session`
param and fall back to the global session, unchanged.

Click **End meeting** to finish and kick off processing — no dashboard:

1. The popup signals the content script (via a `meetingEndRequestedAt`
   marker in `chrome.storage.local`); the content script owns the /tap
   WebSockets and outlives the popup, so the teardown completes even if
   you close the popup.
2. **Drain, then close-all barrier.** Every open tap is closed honouring
   the same **Drain-on-mute** semantics as a mute: buffered trailing PCM
   is flushed before the WebSocket closes, so the last words of an
   in-flight utterance still land and are transcribed, never clipped. The
   trigger fires only once **every** tap has reached `CLOSED`, so the last
   utterance's WAV is part of the session before processing starts.
3. **Trigger.** The bridge calls the control client's
   `triggerPipeline(meetingSessionId)`
   (`POST /api/tap/sessions/<id>/pipeline`) to run the Recorder's
   end-of-meeting pipeline (strip → transcribe → summarize) as one session
   job, visible on the dashboard like any other. The trigger carries **no**
   model / backend / summarizer / prompt — the Recorder ignores the request
   body and uses the operator's configured defaults, so a low-privilege tap
   token can never choose what gets loaded.
4. Capture falls back to the global session: live tap routing stops, but the
   detached session id stays stored as the **meeting card's** durable poll
   target (below). It's cleared on the next **Start meeting** or an explicit
   **Dismiss**, never on End — so a popup re-opened after the meeting can
   still show its progress and summary.

If the Recorder is already running a job on that session it replies `409`
and the popup shows **"Recorder busy"** rather than failing silently or
hammering the endpoint; re-triggering is safe once the session is free.

### Meeting card (progress + summary)

Once a meeting is ending or processing, a **meeting card** in the popup
shows the live pipeline state and the finished result — no dashboard:

- The card **polls** `GET /api/tap/sessions/<id>/pipeline` for the stored
  session id on every popup open, and on a short interval while the
  pipeline is **running**. Each raw poll is mapped through the pure
  `pipeline-view.js` view-model mapper.
- **Per-stage progress** is shown while running — *Stripping silence…*,
  *Transcribing 3/12…*, *Summarizing…* — updated **in place** so a poll
  tick never disturbs the page.
- The finished **summary** is rendered once on the transition to *done*,
  with **Copy** to put it on the clipboard (and light `model · source`
  metadata). Rendering once — rather than rebuilding the pane every tick —
  applies the dashboard's *Interaction-hold* principle by hand, so a poll
  tick can't clobber a mid-copy text selection.
- A **failed** stage surfaces its name and a human-readable reason mapped
  from the Recorder's `error_kind` (e.g. *no usable audio*, *summarizer
  unavailable*).
- The card holds **no local summary cache**: it re-derives everything from
  the stored session id on each open, so closing and re-opening the popup
  (or a transient Recorder restart) always shows the true current state —
  the Recorder serves the persisted `session-summary.json` on the *done*
  branch even after a restart. The result is identical to a dashboard-run
  pipeline: the same persisted summary is visible on the dashboard for that
  session afterward.
- **Dismiss** clears the stored meeting state so the card stops re-deriving
  a finished meeting's result on every open.

> **Removed:** the old global **New session** button and the **start new
> session on room change** toggle are gone. The bracketed Start/End-meeting
> model replaces them; a SpatialChat room change now performs no session
> action.

## Loading unpacked (dev)

1. Start the TapScribe Recorder (`bash start.sh` from the repo root, or
   `.\start.ps1` on Windows).
2. Open `chrome://extensions/`, enable **Developer mode**, click
   **Load unpacked**, pick this directory.
3. Open `https://app.spatial.chat/...` in a new tab.
4. Click the extension icon → set **Recorder host** to `localhost` (or
   the LAN IP of the Recorder machine, if running cross-machine) and
   **Port** to `8001`. Paste the **/tap bearer token** the recorder
   printed at boot (also stored in `.tap-token`). Tick **Use TLS** if
   the recorder was started with `--tls`. Click **Save**.
5. Reload the spatial.chat tab. The content script will open one
   `/tap` WebSocket per remote participant per utterance, keyed by the
   participant's spatial.chat identity. The popup's "Tap token" pill
   reflects whether the server accepted your token.

The popup probes the Recorder's `/health` endpoint as soon as you open
it and shows a per-speaker table of currently-tapped channels (WS state,
frames sent, muted vs. active).

## Verifying the pipeline before debugging the bridge

You can confirm the Recorder + WhisperLiveKit pipeline works without
the bridge installed at all — the `bridges/local-test-bridge/` Python
tool taps the local mic and streams to `/tap`, exercising the same
contract this extension implements. If captions appear there, the
bridge is the only remaining variable.

## Notes on permissions

The manifest requests broad `http://*/*` + `https://*/*` host
permissions so the popup's `/health` probe and the `/tap` WebSocket can
reach a Recorder on the LAN (e.g. `192.168.1.50:8001`). The content
script itself is restricted to `https://app.spatial.chat/*`. If your
deployment only ever uses `localhost`, you can tighten the
`host_permissions` array in `manifest.json` accordingly.

## Notes on transport security

By default the bridge speaks plain `ws://` to a Recorder bound to
`localhost` or a trusted LAN. When the Recorder is started with `--tls`,
tick **Use TLS** in the popup and the bridge will switch to `wss://`
(and `https://` for the `/health` probe). The Recorder's self-signed
default cert will need a one-time browser trust prompt — visit
`https://<recorder-host>:<port>/` in a normal tab first and accept it.

The `/tap` bearer token, generated at boot and printed to the recorder
terminal, gates the WebSocket upgrade. Without it (or with the wrong
one) the recorder refuses the handshake; the popup surfaces this as a
"rejected" pill on the tap-token row.
