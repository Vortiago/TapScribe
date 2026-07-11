# Bridges

A **bridge** taps the remote audio of a meeting platform and forwards it
to the TapScribe Recorder as PCM frames over WebSocket. Bridges are
typically Chrome / Firefox extensions, but a bridge can equally well be
a native helper app (e.g. a Teams or Zoom plugin) — the Recorder doesn't
care which platform the audio came from. Every bridge talks the same
wire protocol, so each is a self-contained package living next to its
siblings here.

## Layout

Each bridge gets its own directory:

```
bridges/
├── spacialchat-bridge/      Chrome MV3 extension for spatial.chat
├── local-test-bridge/       Python dev tool: mic-to-Recorder for local testing
├── windows-tray-bridge/     Native Windows tray app (C# / .NET): mic capture
├── <future>-bridge/         drop another platform's bridge here
└── README.md                this file
```

For a browser-extension bridge, the directory typically contains:

- `manifest.json` (Chrome MV3) or equivalent for other browsers
- `src/` or top-level JS files for content script, background script, popup
- `icons/` for the toolbar icon
- A `README.md` documenting target platform, required permissions, and
  how to load it unpacked during development

Native-app bridges (e.g. a Teams add-in) follow whatever layout their
host platform expects; the only contract is the wire protocol below.

## Wire protocol — one endpoint, one job

Every bridge does exactly one thing: open a WebSocket per utterance to
the Recorder's `/tap` endpoint and stream raw PCM frames. The Recorder
fans the audio out internally to live captioning AND per-utterance WAV
recording — bridges don't talk to WhisperLiveKit themselves and don't
POST settled lines back. (See ADR-0002 for why.)

**Endpoint:** `ws://<recorder-host>:8001/tap?identity=<id>&name=<display>&utterance_id=<uuid>`
(or `wss://...` when the recorder was started with `--tls`).

**Audio format:** PCM signed 16-bit little-endian, 16 kHz mono, raw
binary frames. Frame size: 20 ms (320 samples = 640 bytes). Send one
frame per WebSocket message; don't buffer multiple frames per send if
you want clean live caption granularity.

**Auth:** unless the recorder was started with `--no-auth`, every bridge
MUST offer a `Sec-WebSocket-Protocol` of the form
`tapscribe.v1.tap.<token>` where `<token>` is the value the recorder
printed at boot (also stored in `.tap-token`). The server echoes the
same subprotocol back on a successful upgrade and refuses the upgrade
otherwise. Browsers can only set the subprotocol via the second
argument of `new WebSocket(url, [proto])` — there is no way to set
arbitrary headers from a content script, which is why we use the
subprotocol slot instead of `Authorization`.

**Lifecycle:**

| Bridge action | Recorder reaction |
|---|---|
| Open WS → speaker starts speaking (e.g. unmute) | Recorder opens a fresh WAV under `recordings/<session>/`, marks the connection in `ActiveStreams`, opens its own internal WS to its supervised WhisperLiveKit child for live captions. |
| Send PCM frame | Recorder appends to the WAV AND forwards to WlK. WlK's settled lines come back to the Recorder (not the bridge) and land in `LiveTranscripts` attributed to your `identity`/`name`. |
| Close WS → speaker stops speaking (e.g. mute) | Recorder finalises the WAV (or deletes it if zero bytes were received) and closes the internal WlK relay (with a brief drain so tail captions for already-sent audio still get through). |

**Per-speaker isolation:** when multiple speakers are active
simultaneously, the bridge opens one `/tap` WS per speaker. The Recorder
opens one internal WlK relay per `/tap`. Settled-line attribution is
automatic — the line came from *this* WS, which we know belongs to
`identity=alice`.

**Graceful degradation:** if WhisperLiveKit isn't running (not
auto-started by default, operator clicked Stop on the dashboard, or
the child crashed), the WAV recording proceeds unaffected; live captions
just don't flow until the live channel is restarted. The bridge sees no
errors — there's nothing for it to do about WlK state anyway.

**Query parameters:**
- `identity` (required-ish): a stable per-speaker identifier. Used as
  the WAV filename slug and as the `identity` field on settled-line
  entries. Falls back to `unknown` if not supplied.
- `name`: human-readable display name (e.g. "Alice"). Used on the
  dashboard. Falls back to empty.
- `utterance_id` (recommended): a per-utterance id the bridge mints once
  at the start of an unmuted speech segment and keeps stable across
  reconnects within that utterance. If a /tap WS dies mid-utterance and
  the bridge reopens with the same `utterance_id` within
  `UtteranceIndex.RESUME_WINDOW_SECONDS` (60 s by default), the Recorder
  appends to the same WAV instead of producing a second file. Clear it
  on mute / end-of-utterance and mint a fresh one on the next unmute.
  Omitting this still works (each WS gets its own WAV), but bridges that
  want blip resilience should supply it.
- `session` (optional): a session id to direct this tap into — normally a
  **detached session** the bridge minted via the control endpoint below.
  Present, the WAV (and the tap's live-feed lines) land in that session;
  absent, the Recorder's global current session is used. The id is
  validated against the existing sessions on disk: an unknown or invalid
  id **refuses the WS upgrade** with an HTTP 404 denial — the same
  fail-loudly shape as a bad tap token (which refuses with its 4401
  close), so a misconfigured bridge errors instead of recording into the
  wrong session. Only pass ids of eagerly-created detached sessions: the
  global current session materialises lazily on its first WAV, so passing
  *its* id can 404 — omit the param to target it. Affiliation is
  snapshotted at WS open — a session rotation never re-homes an
  already-open tap.

**Reconnect / blip resilience:** the Recorder will not auto-reconnect to
the bridge — `/tap` is bridge-initiated. A bridge that wants to recover
from a transient WS failure (network blip, recorder restart) should:

1. Detect close-with-code != 1000 (or `onerror`).
2. Reopen `/tap` with the **same** `utterance_id` after a short backoff
   (e.g. jittered exponential, 200 ms → 5 s).
3. Buffer PCM frames during the gap so audio captured while disconnected
   isn't lost when the WS comes back. The SpatialChat bridge keeps a
   ~3 s ring buffer; tune this to the loss budget you can accept.

The bundled `spacialchat-bridge` implements all of this; see
`bridges/spacialchat-bridge/content.js` for a reference.

## Control endpoint — start a new session

Besides streaming audio over `/tap`, a bridge can ask the Recorder to
**rotate to a fresh recording session** without the operator touching the
dashboard — handy when one meeting ends and another begins in the same
place (daily → refinement), or when the platform moves you to a different
room.

**Endpoint:** `POST http://<recorder-host>:8001/api/tap/new-session`
(`https://...` under `--tls`). Fire-and-forget; no request body for the
legacy rotate. With a JSON body of `{"detached": true}` it creates a
**detached session** instead — see below.

**Auth:** the **same tap token** as `/tap`, but carried as an
`Authorization: Bearer <token>` header. Unlike the WebSocket handshake, an
HTTP `fetch()` *can* set arbitrary headers, so there's no need for the
subprotocol trick here. Under `--no-auth` the header is optional; a
missing or incorrect token returns `401` and does not rotate.

**Effect:** the Recorder rotates `session_start`/`session_dir` to a fresh
UTC-stamped folder (already-open `/tap` WebSockets keep writing to their
original folder; only new opens land in the new one). It **rotates only — it
deletes nothing.** Removing empty session folders stays a dashboard
(Basic-auth) action: the dashboard's "+ new session" button rotates *and*
prunes empties, and there's a separate "prune empty" action. The tap token is
a lower-privilege credential, so it can start a session but not delete folders.

**Idempotency:** if the current session has received no audio yet, the call is
a no-op rotation (it won't churn the session timestamp). The JSON response is
`{"ok": true, "rotated": true|false, "previous": "...", "current":
"<session-id>", "path": "..."}`.

The `spacialchat-bridge` calls this from its popup's **New session** button
and — when the operator ticks **"start new session on room change"** —
automatically whenever SpatialChat swaps rooms.

### Detached sessions — per-bridge isolation

A rotation moves the **global** current session, which every plain tap
shares. When two people tap two different meetings against one Recorder,
each bridge should instead work in its own **detached session**:

1. `POST /api/tap/new-session` with a JSON body of `{"detached": true}`
   (same bearer auth). The Recorder mints a fresh session directory and
   returns it **without** touching the global current session:
   `{"ok": true, "detached": true, "session": "<id>", "path": "...",
   "current": "<the untouched global id>"}`.
2. Open every `/tap` WS with `?session=<id>` so the bridge's audio lands
   there, isolated from concurrent taps that use the global session.

Detached sessions are ordinary sessions — same on-disk layout, dashboard
listing, transcription and maintenance operations. Two practical notes:
a freshly created detached session is empty until its first WAV, so the
dashboard's prune-empties actions can delete it (create it just-in-time);
and the id only lives in the bridge — the Recorder won't redirect plain
taps to it.

Demo with two terminals against one Recorder (the WAVs land in two
different session folders). `TAPSCRIBE_TAP_TOKEN` is the tap token the
recorder printed at boot (also stored in `.tap-token`); the
local-test-bridge reads the same env var for its `--tap-token` default,
and under `--no-auth` you can drop the Authorization header entirely:

```
# terminal 1 — a detached meeting
id=$(curl -s -X POST -H "Authorization: Bearer $TAPSCRIBE_TAP_TOKEN" \
     -H 'Content-Type: application/json' -d '{"detached": true}' \
     http://localhost:8001/api/tap/new-session | jq -r .session)
python bridges/local-test-bridge/local_test_bridge.py --session "$id"

# terminal 2 — the global current session, unaffected
python bridges/local-test-bridge/local_test_bridge.py
```

## Adding a new bridge

1. Create `bridges/<platform>-bridge/`.
2. Add a `README.md` documenting target platform + how to load / install.
3. Implement: tap audio from the platform → resample/convert to 16 kHz
   mono int16 → open `/tap` WebSocket per utterance → stream frames →
   close WS on utterance end.

`bridges/local-test-bridge/` is the simplest reference implementation —
it taps the local mic and streams to `/tap` on ENTER toggle. Cribbing
its WS lifecycle is the fastest way to get a new bridge bootstrapped.
