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

**Audio format:** PCM signed 16-bit little-endian, 16 kHz mono, raw
binary frames. Frame size: 20 ms (320 samples = 640 bytes). Send one
frame per WebSocket message; don't buffer multiple frames per send if
you want clean live caption granularity.

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

**Graceful degradation:** if WhisperLiveKit isn't running (operator
clicked Stop on the dashboard's live channel panel, or `--no-auto-live`
was set at boot), the WAV recording proceeds unaffected; live captions
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

## Adding a new bridge

1. Create `bridges/<platform>-bridge/`.
2. Add a `README.md` documenting target platform + how to load / install.
3. Implement: tap audio from the platform → resample/convert to 16 kHz
   mono int16 → open `/tap` WebSocket per utterance → stream frames →
   close WS on utterance end.

`bridges/local-test-bridge/` is the simplest reference implementation —
it taps the local mic and streams to `/tap` on ENTER toggle. Cribbing
its WS lifecycle is the fastest way to get a new bridge bootstrapped.
