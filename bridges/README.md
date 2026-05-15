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

**Endpoint:** `ws://<recorder-host>:8001/tap?identity=<id>&name=<display>`
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

## Adding a new bridge

1. Create `bridges/<platform>-bridge/`.
2. Add a `README.md` documenting target platform + how to load / install.
3. Implement: tap audio from the platform → resample/convert to 16 kHz
   mono int16 → open `/tap` WebSocket per utterance → stream frames →
   close WS on utterance end.

`bridges/local-test-bridge/` is the simplest reference implementation —
it taps the local mic and streams to `/tap` on ENTER toggle. Cribbing
its WS lifecycle is the fastest way to get a new bridge bootstrapped.
