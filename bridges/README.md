# Bridges

A **bridge** taps the remote audio of a meeting platform and forwards it
to the TapScribe Recorder as PCM frames over WebSocket — a browser
extension, a native helper app, anything. Every bridge talks the same
wire protocol below; each lives as a self-contained package here, with
its own README for platform specifics.

## Layout

```
bridges/
├── spacialchat-bridge/      Chrome MV3 extension for spatial.chat
├── local-test-bridge/       Python dev tool: mic-to-Recorder for local testing
├── tray-bridge/             Native tray Bridge (C# / .NET): core + Windows and macOS shells
├── <future>-bridge/         drop another platform's bridge here
└── README.md                this file
```

## Wire protocol — one endpoint, one job

A bridge does exactly one thing: open a WebSocket per utterance to the
Recorder's `/tap` endpoint and stream raw PCM frames. The Recorder fans
the audio out internally to live captioning AND per-utterance WAV
recording — bridges don't talk to WhisperLiveKit themselves and don't
POST settled lines back (ADR-0002).

**Endpoint:** `ws://<recorder-host>:8001/tap?identity=<id>&name=<display>&utterance_id=<uuid>&tap_mode=<single|multi>`
(or `wss://...` when the recorder was started with `--tls`).

**Audio format:** PCM signed 16-bit little-endian, 16 kHz mono, raw
binary frames. Frame size: 20 ms (320 samples = 640 bytes). Send one
frame per WebSocket message — buffering multiple frames per send costs
live-caption granularity.

**Auth:** unless the recorder was started with `--no-auth`, every bridge
MUST offer a `Sec-WebSocket-Protocol` of the form
`tapscribe.v1.tap.<token>`, where `<token>` is the value the recorder
printed at boot (also stored in `.tap-token`). The server echoes the
subprotocol back on a successful upgrade and refuses the upgrade
otherwise. (The subprotocol slot is used because a browser content
script can't set arbitrary headers on a WebSocket — only the second
argument of `new WebSocket(url, [proto])`.)

**Lifecycle:**

| Bridge action | Recorder reaction |
|---|---|
| Open WS (speaker unmutes) | Opens a fresh WAV under `recordings/<session>/`, marks the connection in `ActiveStreams`, opens its own internal WS to its supervised WhisperLiveKit child for live captions. |
| Send PCM frame | Appends to the WAV AND forwards to WlK. Settled lines come back to the Recorder (not the bridge) and land in `LiveTranscripts` attributed to your `identity`/`name`. |
| Mute with PCM still buffered (**Drain**) | Bridge keeps its reconnect ladder running for up to `DRAIN_MAX_MS` instead of closing immediately, flushes the buffered tail to the next `/tap` WS that lands, then closes. See "Drain" below. |
| Close WS (speaker mutes) | Finalises the WAV (deletes it if zero bytes arrived) and closes the internal WlK relay, with a brief drain so tail captions for already-sent audio still get through. |

**Per-speaker isolation:** one `/tap` WS per active speaker; the
Recorder opens one internal WlK relay per `/tap`, so settled-line
attribution is automatic.

**Graceful degradation:** if WhisperLiveKit isn't running (not
auto-started by default, stopped from the dashboard, or crashed), WAV
recording proceeds unaffected; live captions just don't flow. The bridge
sees no errors — there's nothing it could do about WlK state anyway.

**Query parameters:**

- `identity` (required-ish): stable per-speaker identifier — the WAV
  filename slug and the `identity` on settled lines. Falls back to
  `unknown`.
  **`__probe__` is RESERVED** — the identity for verifying the tap token
  ("Test connection"). The Recorder special-cases it to leave no durable
  and no live-visible state: no WAV, no roster occurrence, no
  `ActiveStreams` row. `tapscribe/tap_fan_out.py`'s `PROBE_IDENTITY` is
  the source of truth; probe with that exact string, never a
  human-looking one — an ordinary identity writes a roster occurrence at
  WS open, which auto-binds a durable Person in `people.json`: one junk
  speaker in the operator's GLOBAL registry per Test-connection click.
  Both bundled bridges do this
  (`spacialchat-bridge/control-client.js`'s `probeTapToken`,
  `tray-bridge`'s `ConnectionTester.cs`).
- `name`: human-readable display name for the dashboard. Falls back to
  empty.
- `utterance_id` (recommended): minted once at the start of an unmuted
  speech segment, kept stable across reconnects within that utterance.
  Reopening with the same `utterance_id` within
  `UtteranceIndex.RESUME_WINDOW_SECONDS` (60 s by default) appends to
  the same WAV instead of producing a second file. Clear on mute; mint
  fresh on the next unmute. Omitting it still works (each WS gets its
  own WAV) but forfeits blip resilience.
- `tap_mode` (recommended): does this tap carry **one** human or
  **several**? Exactly **`single`** or **`multi`**; absent or unrecognised
  reads as `single`. Only a multi-person tap is diarized into Voices, so a
  bridge that mixes several speakers into one stream (a system-audio /
  loopback capture, a room mic, a mixed NDI feed) should declare `multi`;
  a per-participant tap declares `single`. The declaration is only a
  DEFAULT — the operator can override it per identity, and that override
  wins. Never send a guess as `multi`: a diarizer splits one human across
  a channel or noise change, manufacturing Voices to clean up.
  `tapscribe/tap_mode.py` is the source of truth (ADR-0021).
- `session` (optional): a session id to direct this tap into — normally
  a **detached session** minted via the control endpoint below. Present,
  the WAV and the tap's live-feed lines land there; absent, the global
  current session is used. The id is validated against the sessions on
  disk: an unknown or invalid id **refuses the WS upgrade** with an HTTP
  404 denial — the same fail-loudly shape as a bad tap token (its 4401
  close), so a misconfigured bridge errors instead of recording into the
  wrong session. Only pass ids of eagerly-created detached sessions: the
  global current session materialises lazily on its first WAV, so
  passing *its* id can 404 — omit the param to target it. Affiliation is
  snapshotted at WS open — a session rotation never re-homes an
  already-open tap.

**Reconnect / blip resilience:** `/tap` is bridge-initiated; the
Recorder never reconnects. To recover from a transient WS failure
(network blip, recorder restart):

1. Detect close-with-code != 1000 (or `onerror`).
2. Reopen `/tap` with the **same** `utterance_id` after a short backoff.
3. Buffer PCM frames during the gap.
4. On mute, don't close while PCM is still buffered — **drain** first.

The two bundled bridges (`spacialchat-bridge/content.js`,
`tray-bridge`'s `TapStream.cs` / `TapStreamOptions.cs`) converged on the
concrete numbers below — the **Blip-resilience recipe** (CONTEXT.md).
Recommended starting point, not wire contract: the Recorder has no
opinion, and a bridge with a good reason may deviate. The bundled two
may not drift from each other — `tests/test_tap_wire_contract.py` holds
them to it.

- **Backoff ladder:** jittered exponential — `200, 400, 800, 1600, 3200 ms`,
  capped at `5000 ms`, with **±25 % jitter** on each delay so a roomful
  of reconnecting bridges doesn't synchronise its retries
  (`content.js`: `BACKOFF_MS` / `BACKOFF_CAP_MS` / `nextBackoffMs()`;
  `TapStreamOptions.cs`: `Backoff` / `BackoffCap` / `BackoffJitter`).
- **Gap buffer:** cap PCM buffered while disconnected at **96 000 bytes**
  (≈ 3 s of 16 kHz mono int16), dropping the **oldest** frames past the
  cap so a long outage loses only its tail instead of growing memory
  without bound (`content.js`: `MAX_BUFFER_BYTES`, enforced in
  `bufferPush()`; `TapStreamOptions.cs`: `MaxBufferBytes`).
- **Drain — flush-then-close on mute**, the Bridge-side half of the
  [Drain invariant](../CONTEXT.md#drain): trailing PCM buffered when
  mute fires hasn't necessarily reached the Recorder, so keep the
  reconnect ladder running and flush the buffer to the next WS that
  lands *before* closing — closing on mute without draining silently
  truncates the WAV whenever mute lands mid-blip. **Bound the wait**
  with a `DRAIN_MAX_MS` (recommended **8000 ms**): past that deadline,
  close anyway — an unreachable Recorder must never wedge the utterance
  forever (`content.js`: `DRAIN_MAX_MS`, `restartDrainTimer()` /
  `endUtterance()`; `TapStreamOptions.cs`: `DrainBudget`;
  `TapStream.cs`: `BeginDrain` / `DrainAndDisposeAsync`).

**First-connect-failure semantics — an open choice, pick one deliberately.**
The two bundled bridges diverge here on purpose; a new bridge should
choose consciously, document its pick, and not blend the two:

- `spacialchat-bridge/content.js` (`shouldReconnect()`) keeps the ladder
  running on **any** unclean close — including the utterance's very
  first connect — until mute or tap-stop. A Recorder briefly unreachable
  as an utterance starts still catches up, at the cost of retrying
  indefinitely against an unreachable host or an unknown `session`
  (whose 404 upgrade refusal the browser only surfaces as an abnormal
  close, indistinguishable from a blip) with no give-up beyond the
  popup's `error` status. Two failures are carved out as **sticky**
  because retrying them can only thrash: a `4401` tap-token rejection
  (`tap-auth-failed`) and a `ws://`-from-`https://` mixed-content
  configuration (`tls-required`). Both stop the ladder *and* the
  dial-on-next-PCM-frame path in `openTapWs`, and both are cleared by a
  settings change (`reconnectAllForSettingsChange`) — fixing the token
  or ticking Use TLS in the popup redials without a tab reload.
- `tray-bridge`'s `TapStream.cs` (the first-connect branch in `RunAsync`)
  treats a failure on the utterance's **first** connect as terminal: it
  surfaces the exception via `onTerminalFailure` and stops — an
  unreachable Recorder / refused token / unknown session is a
  configuration problem, not a blip, matching the wire contract's
  fail-loudly stance. Only a **mid-utterance** failure — the stream had
  connected at least once — gets the reconnect ladder.

Both are defensible: content.js optimises for "never truncate a meeting
because the Recorder was mid-restart"; TapStream.cs for "don't spin
forever against a config typo".

## Control endpoint — start a new session

A bridge can rotate the Recorder to a fresh recording session without
the operator touching the dashboard (one meeting ends, another begins).

**Endpoint:** `POST http://<recorder-host>:8001/api/tap/new-session`
(`https://...` under `--tls`). Fire-and-forget; no body for the legacy
rotate. A JSON body of `{"detached": true}` creates a **detached
session** instead — see below.

**Auth:** the same tap token as `/tap`, carried as
`Authorization: Bearer <token>` (an HTTP `fetch()` can set headers, so
no subprotocol trick). Optional under `--no-auth`; a missing or wrong
token returns `401` and does not rotate.

**Effect:** rotates `session_start`/`session_dir` to a fresh UTC-stamped
folder. Already-open `/tap` WebSockets keep writing to their original
folder; only new opens land in the new one. It **rotates only — it
deletes nothing**: the tap token is a lower-privilege credential, so
pruning empty session folders stays a dashboard (Basic-auth) action.

**Idempotency:** if the current session has received no audio, the call
is a no-op (no timestamp churn). Response: `{"ok": true, "rotated":
true|false, "previous": "...", "current": "<session-id>", "path": "..."}`.

### Detached sessions — per-bridge isolation

A rotation moves the **global** current session, which every plain tap
shares. Two bridges tapping two meetings against one Recorder should
each work in their own **detached session**:

1. `POST /api/tap/new-session` with body `{"detached": true}` (same
   bearer auth). The Recorder mints a fresh session directory and
   returns it **without** touching the global current session:
   `{"ok": true, "detached": true, "session": "<id>", "path": "...",
   "current": "<the untouched global id>"}`.
2. Open every `/tap` WS with `?session=<id>`.

Detached sessions are ordinary sessions — same on-disk layout, dashboard
listing, transcription and maintenance operations. Two notes: a freshly
created detached session is empty until its first WAV, so the
dashboard's prune-empties actions can delete it (create it
just-in-time); and the id lives only in the bridge — the Recorder won't
redirect plain taps to it.

Demo — two terminals, one Recorder, WAVs land in two session folders
(`TAPSCRIBE_TAP_TOKEN` is the boot-printed tap token, which the
local-test-bridge also reads for its `--tap-token` default; under
`--no-auth` drop the Authorization header):

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

A bridge that brackets a meeting ([Bracketed
meeting](../CONTEXT.md#bracketed-meeting)) can run the Recorder's whole
post-processing chain — strip silence → batch-transcribe the stripped
output → summarize — as **one** job on a finished session, then poll for
progress and the summary: the [end-of-meeting
pipeline](../CONTEXT.md#end-of-meeting-pipeline). Trigger it only
against your own [detached
session](#detached-sessions--per-bridge-isolation), after **End
meeting** has closed every open tap (honouring Drain) so nothing races
the strip stage's WAV glob.

**Trigger — `POST http://<recorder-host>:8001/api/tap/sessions/<session>/pipeline`**
(`https://...` under `--tls`).

- **Auth:** the same tap bearer token as `/api/tap/new-session` — the
  TAP-BEARER scheme that gates every route under `/api/tap`.
- **Request body is ignored — entirely, never parsed.** The transcribe
  model, backend and summarizer resolve from operator-side configuration
  only, so a leaked tap token can never make the Recorder load or
  download an attacker-chosen model.
- **Response:** `202` with `{"ok": true, "session": "<id>", "state":
  "running"}` once the job slot is claimed; the chain runs in the
  background. `session` must already exist on disk (an unknown/invalid
  id 404s — the same path-safety seam as `/tap`'s `session` param).
- **Busy semantics:** the pipeline claims the session's single job slot
  in the request path, so the `202` is a deterministic commitment. A
  concurrent trigger, or a manual transcribe/strip in flight, gets `409`
  — the same one-heavy-job-per-session rule as every other batch route.
- **Failure semantics:** a stage failure (nothing to strip, nothing to
  transcribe, no transcript text, a summarizer misconfiguration, …)
  aborts the whole chain — remaining stages are not retried. The poll's
  `failed` state surfaces which stage and why.

**Poll — `GET http://<recorder-host>:8001/api/tap/sessions/<session>/pipeline`**
(same tap-bearer auth). Response `state` is one of:

- `"running"` — includes the live job snapshot's `stage` (`"strip"` |
  `"transcribe"` | `"summarize"`), `status`, `current`/`total`
  (per-stage progress), and `current_file`.
- `"done"` — includes the persisted `summary` (the shape written to the
  session's `session-summary.json`).
- `"failed"` — includes `stage` plus `error`/`error_kind` (the domain
  error's message and class name).
- `"idle"` — no in-memory pipeline record and no persisted summary:
  never triggered, or the Recorder restarted and that run never reached
  a persisted summary.

**Restart survival:** `"done"` is read from `session-summary.json` on
disk, not memory — a bridge polling across a Recorder restart still gets
`"done"` with the summary once the file exists, while an in-flight
`"running"`/`"failed"` record is lost on restart and degrades to
`"idle"`. A meeting card that holds only the session id and re-derives
from this poll on each open survives the restart transparently.

Both bundled bridges call this pair from **End meeting**:
`control-client.js` (`TapscribeControlClient`) in `spacialchat-bridge/`,
`ControlClient.cs` in `tray-bridge/`.

## Keeping the languages honest

The wire constants above are declared in JS, C#, Python and this prose,
and are held in lock-step mechanically:

- **`tapscribe/` is the source** — the Recorder serves `/tap`, so its
  constants *are* the contract. A wire change is a hand edit there (the
  subprotocol lives in `tapscribe/auth.py`).
- **`python3 tools/stamp_tap_wire.py`** rewrites the matching literal in
  every bridge and every doc, this file included. Idempotent — prints
  `already consistent` when nothing changed.
- **`tests/test_tap_wire_contract.py`** fails on drift — including a
  hand edit that skipped the tool, and a declaration site nobody listed.

A bridge in a new language is one new `Site` row in the stamper's table;
Python on the Recorder's side needs no row — it imports the constants.
ADR-0019 has the reasoning, including why this is a stamper rather than
codegen.

## Adding a new bridge

1. Create `bridges/<platform>-bridge/` with a `README.md` (target
   platform + how to load / install).
2. Implement: tap platform audio → resample/convert to 16 kHz
   mono int16 → open `/tap` WebSocket per utterance → stream frames →
   **drain** trailing buffered PCM on mute (bounded by `DRAIN_MAX_MS`) →
   close WS on utterance end.

`bridges/local-test-bridge/` is the simplest reference implementation —
it taps the local mic and streams to `/tap` on ENTER toggle.
