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
| Mute, with PCM still buffered on the bridge (**Drain**) | Bridge keeps its reconnect ladder running for up to `DRAIN_MAX_MS` (recommended 8 s) instead of closing immediately, flushes the buffered tail to the next `/tap` WS that lands, then closes cleanly. See "Drain — flush-then-close on mute" below. |
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
2. Reopen `/tap` with the **same** `utterance_id` after a short backoff.
3. Buffer PCM frames during the gap so audio captured while disconnected
   isn't lost when the WS comes back.
4. On mute, don't close immediately if PCM is still buffered — **drain**
   first (see below).

The two bundled production bridges (`spacialchat-bridge/content.js` and
`windows-tray-bridge`'s `TapStream.cs` / `TapStreamOptions.cs`) have
converged on the same concrete numbers below; treat them as the
**recommended defaults** for a new bridge rather than re-deriving your own
loss budget from scratch.

- **Backoff ladder:** jittered exponential — `200, 400, 800, 1600, 3200 ms`,
  capped at `5000 ms`, with **±25 % jitter** on each delay so a roomful of
  reconnecting bridges doesn't synchronise its retries
  (`bridges/spacialchat-bridge/content.js:227-228,261-263`;
  `TapStreamOptions.cs`'s `Backoff` / `BackoffCap` / `BackoffJitter`).
- **Gap buffer:** cap PCM buffered while disconnected at **96 000 bytes**
  (≈ 3 s of 16 kHz mono int16), dropping the **oldest** frames past the cap
  so a long outage loses only its tail instead of growing memory without
  bound (`content.js:229-233` `MAX_BUFFER_BYTES`; `TapStreamOptions.cs`'s
  `MaxBufferBytes`).
- **Drain — flush-then-close on mute.** This is the Bridge-side half of the
  [Drain invariant](../CONTEXT.md#drain): trailing PCM buffered when mute
  fires hasn't necessarily reached the Recorder yet, so don't close `/tap`
  immediately — keep the reconnect ladder running, and once a WS lands,
  flush the buffer to it *before* closing. A bridge that closes on mute
  without draining silently truncates the WAV whenever mute lands mid-blip.
  **Bound the wait** with a `DRAIN_MAX_MS` (recommended **8000 ms**): past
  that deadline, give up and close anyway — an unreachable Recorder must
  never wedge the utterance forever
  (`content.js:234-238` `DRAIN_MAX_MS`, `restartDrainTimer` /
  `endUtterance`; `TapStreamOptions.cs`'s `DrainBudget`, `TapStream.cs`'s
  `BeginDrain` / `DrainAndDisposeAsync`).

**First-connect-failure semantics — an open choice, pick one deliberately:**
the two bundled bridges diverge here and neither is wrong, but a new bridge
should choose consciously rather than copy both halves:

- `spacialchat-bridge/content.js`'s `shouldReconnect` (`content.js:421-423`)
  keeps the reconnect ladder running on **any** unclean close — including
  the very first connect attempt for an utterance — as long as the speaker
  hasn't muted or the tap hasn't stopped. A Recorder that's briefly
  unreachable right as an utterance starts (e.g. mid-restart) still catches
  up once it comes back, at the cost of retrying indefinitely against a
  genuinely wrong host/token/session with no user-visible give-up (the
  popup surfaces an `error` status, but nothing stops the ladder short of
  mute/stop).
- `windows-tray-bridge`'s `TapStream.cs` (`TapStream.cs:19-24,172-182`)
  treats a failure on the utterance's **first** connect as terminal: it
  surfaces the exception via `onTerminalFailure` and stops immediately,
  reasoning that an unreachable Recorder / refused token / unknown session
  is a configuration problem, not a transient blip, matching the wire
  contract's fail-loudly stance (mirroring the `/tap` bad-token 4401 close
  and the bad-`session` 404 upgrade refusal above). Only a **mid-utterance**
  failure — the stream had connected at least once — gets the reconnect
  ladder.

Both are defensible: content.js optimises for "never truncate a meeting
just because the Recorder was mid-restart," TapStream.cs optimises for
"don't spin forever against a config typo." A new bridge should document
which one it picked and why, rather than silently blending the two.

The bundled `spacialchat-bridge` implements the reconnect/buffer/drain
recipe above; see `bridges/spacialchat-bridge/content.js` for a reference,
or `bridges/windows-tray-bridge/src/TapScribe.Bridge.Core/TapStream.cs` for
the terminal-first-failure variant.

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

## Control endpoint — end-of-meeting pipeline

A bridge that brackets a meeting (**Start meeting → End meeting** — see the
[Bracketed meeting](../CONTEXT.md#bracketed-meeting) entry in CONTEXT.md) can
ask the Recorder to run its whole post-processing chain — strip silence →
batch-transcribe the stripped output → summarize — as **one** job on a
finished session, then poll for progress and the finished summary. This is
the [end-of-meeting pipeline](../CONTEXT.md#end-of-meeting-pipeline); a
bridge only ever triggers it against its own [detached
session](#detached-sessions--per-bridge-isolation), once **End meeting** has
closed every open tap (honouring Drain, above) so there's nothing left to
race the strip stage's WAV glob.

**Trigger — `POST http://<recorder-host>:8001/api/tap/sessions/<session>/pipeline`**
(`https://...` under `--tls`).

- **Auth:** the same tap bearer token as `/api/tap/new-session`
  (`Authorization: Bearer <token>`) — the same TAP-BEARER scheme that gates
  every route under `/api/tap`.
- **Request body is ignored — entirely, never parsed.** There is no way to
  choose the transcribe model, backend, or summarizer from this call: they
  resolve from operator-side configuration only (`batch-model.txt`, the
  Recorder's launch backend preference, the summarizer's configured
  default), so a leaked tap token can never make the Recorder load or
  download an attacker-chosen model.
- **Response:** `202` with `{"ok": true, "session": "<id>", "state":
  "running"}` once the job slot is claimed — fire-and-forget; the chain runs
  in the background. `session` must be a session id that's already on disk
  (an unknown/invalid id 404s, same path-safety seam as the `session` query
  parameter on `/tap`).
- **Busy semantics:** the pipeline claims the session's single job slot
  up front, in the request path, so the trigger's `202` is a deterministic
  commitment. A concurrent trigger on the same session, or a manual
  transcribe/strip already in flight, gets `409` instead — the same
  one-heavy-job-per-session rule as every other batch route.
- **Failure semantics:** a stage failure (nothing usable to strip, nothing
  usable to transcribe after stripping, no transcript text to summarize, a
  summarizer misconfiguration, …) aborts the whole chain; it does **not**
  retry the remaining stages. The poll response's `failed` state below
  surfaces which stage failed and why.

**Poll — `GET http://<recorder-host>:8001/api/tap/sessions/<session>/pipeline`**
(same tap-bearer auth). Response `state` is one of:

- `"running"` — includes the live job snapshot's `stage` (`"strip"` |
  `"transcribe"` | `"summarize"`), `status`, `current`/`total` (per-stage
  progress), and `current_file`.
- `"done"` — includes the persisted `summary` (the same shape written to
  that session's `session-summary.json`).
- `"failed"` — includes `stage` (which stage aborted the chain) and
  `error`/`error_kind` (the domain error's message and class name).
- `"idle"` — this session has no in-memory pipeline record and no persisted
  summary: either never triggered, or the Recorder restarted since the last
  trigger and that run never reached a persisted summary (see restart
  survival below — a restart after a *successful* run still answers
  `"done"`).

**Restart survival:** the `"done"` answer is read from the persisted
`session-summary.json` on disk, not from in-memory state — so a bridge that
polls across a Recorder restart (its own crash, an update, a reboot) still
gets `"done"` with the summary once the file exists, even though the
in-memory run record (`"running"`/`"failed"`) is lost on restart and
degrades to `"idle"` if polled before the summary file exists. A meeting
card that only holds the session id (not a local summary cache) and
re-derives from this poll on each open survives that restart transparently.

The bundled `spacialchat-bridge` and `windows-tray-bridge` both call this
pair from their **End meeting** flow; see `control-client.js`
(`TapscribeControlClient`) in `spacialchat-bridge/` or `ControlClient.cs` in
`windows-tray-bridge/` for reference implementations.

## Adding a new bridge

1. Create `bridges/<platform>-bridge/`.
2. Add a `README.md` documenting target platform + how to load / install.
3. Implement: tap audio from the platform → resample/convert to 16 kHz
   mono int16 → open `/tap` WebSocket per utterance → stream frames →
   **drain** any trailing buffered PCM on mute (bounded by a `DRAIN_MAX_MS`
   budget, above) → close WS on utterance end.

`bridges/local-test-bridge/` is the simplest reference implementation —
it taps the local mic and streams to `/tap` on ENTER toggle. Cribbing
its WS lifecycle is the fastest way to get a new bridge bootstrapped.
