# spacialchat-bridge

Chrome (MV3) extension that taps remote audio in a spatial.chat tab and
streams it to a running TapScribe Recorder: one `/tap` WebSocket per
active speaker per utterance, raw 16 kHz mono int16 PCM
in 20 ms (640-byte) frames, tap token carried in the WS subprotocol. The full
wire contract is `../README.md`; the Recorder owns everything past the
socket (live captions, per-utterance WAVs).

## How it works

- `content.js` (ISOLATED world, `https://app.spatial.chat/*`) injects
  `page-script.js` into the MAIN world to reach `window.room` (LiveKit).
- `page-script.js` subscribes to remote audio tracks and resamples them
  via an inline AudioWorklet, posting PCM frames to the content script.
  A per-tick `reconcile()` sweep re-derives the roster from
  `room.remoteParticipants`, so speakers whose subscribe/connect events
  were dropped across a reconnect or proximity resubscribe are still
  tapped, and departed ones untapped.
- `content.js` opens a `/tap` WS on the first non-muted frame and closes
  it on mute — mute is the end-of-utterance signal; the Recorder
  finalises one WAV per utterance.
- `control-client.js` is a shared classic global
  (`TapscribeControlClient`, loaded into both the content script and the
  popup — MV3 content scripts can't be ES modules) owning every
  tap-token control call: scheme derivation, mixed-content guard, bearer
  header, subprotocol builder, timeouts. Its surface is create / trigger
  / poll / rotate / probe only — deliberately no delete, so a leaked tap
  token's blast radius stays bounded (deletion is a dashboard
  Basic-auth action).

## Meetings (Start / End)

By default taps land in the Recorder's global session. **Start meeting**
mints a detached session (`POST /api/tap/new-session` with
`{"detached": true}`) and stamps `&session=<id>` onto every tap opened
while it's active; the affiliation is snapshotted per utterance, and the
session survives spatial.chat room changes. **End meeting** drains every
open tap (Drain-on-mute semantics, so trailing audio isn't clipped),
waits for all to reach `CLOSED`, then triggers the end-of-meeting
pipeline (`POST /api/tap/sessions/<id>/pipeline` — the body carries no
model/summarizer fields; the Recorder uses operator defaults). A `409`
surfaces as "Recorder busy"; re-triggering is safe once the session is
free.

The popup's **meeting card** polls `GET /api/tap/sessions/<id>/pipeline`
for the stored session id and shows per-stage progress while running,
the finished summary with **Copy**, or a failed stage with a
human-readable reason. It holds no local cache — it re-derives from the
stored id on every open (the Recorder serves the persisted summary even
after a restart) until **Dismiss** or the next **Start meeting** clears
it.

## Load unpacked (dev)

1. Start the Recorder (`bash start.sh`, or `.\start.ps1` on Windows).
2. `chrome://extensions/` → Developer mode → **Load unpacked** → this
   directory.
3. In the popup: set **Recorder host** and **Port** (`8001`), paste the
   **/tap bearer token** from the recorder's startup banner (also stored
   in `.tap-token`), tick **Use TLS** if the recorder runs `--tls`,
   **Save**.
4. Reload the spatial.chat tab. The popup shows a per-speaker tap table
   and a tap-token pill reflecting whether the server accepted the token.

To sanity-check the Recorder pipeline without the extension, use
`../local-test-bridge/` — it exercises the same `/tap` contract from the
local mic.

## Permissions and transport

The manifest requests broad `http://*/*` + `https://*/*` host
permissions so the popup's `/health` probe and the `/tap` WS can reach a
Recorder on the LAN; the content script itself only runs on
`https://app.spatial.chat/*`. Tighten `host_permissions` if you only use
`localhost`. Browsers block `ws://` from an `https://` page as mixed
content, so any non-localhost Recorder effectively needs `--tls` +
**Use TLS**; trust the self-signed cert once by visiting
`https://<host>:<port>/` in a normal tab.

## Testing / gates

- Unit tests: `node --test tests/*.test.js` — the DOM-free modules
  (popup presenter/actions, pipeline and taps view-models, control
  client, drain, meeting flows, page-script, wire constants). CI runs
  them in the main test job.
- Typecheck: `cd typecheck && npm install && npm run typecheck` — the
  `bridge-typecheck` CI job, `tsc --noEmit --strict` over the
  `// @ts-check`'d modules (`control-client.js`, `pipeline-view.js`,
  `popup-presenter.js`, `popup-actions.js`, `taps-view.js`, `popup.js`),
  `types.d.ts`, and the vendored `lib/` + `components/`. Incremental:
  `content.js` / `page-script.js` join as they're typed.
- Popup E2E: `cd e2e && npm install && npx playwright install chromium
  && npm test` — drives `popup.html` in Chromium with `chrome.*` shimmed
  and the recorder stubbed.

## Vendored files

`lib/`, `components/` and `tokens.css` are copy-verbatim from
vanilla-components (`@48bf2bf`) — never edit them in place; they stay
deliberately stale until a dedicated re-vendor pass. Re-copy from the
toolkit to update.
