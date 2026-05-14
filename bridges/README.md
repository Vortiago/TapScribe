# Bridges

A **bridge** taps the remote audio of a meeting platform and forwards it
to the TapScribe backend as PCM frames over WebSocket. Bridges are
typically Chrome / Firefox extensions, but a bridge can equally well be
a native helper app (e.g. a Teams or Zoom plugin) — the backend doesn't
care which platform the audio came from. Every bridge talks the same
wire protocol, so each is a self-contained package living next to its
siblings here.

## Layout

Each bridge gets its own directory:

```
bridges/
├── spacialchat-bridge/      Chrome MV3 extension for spatial.chat
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

## Wire protocol the backend expects

The backend ingest contract is intentionally narrow:

- **WhisperLiveKit WebSocket** — `ws://<backend-host>:8000/asr?language=en`.
  One WebSocket per remote participant. Audio is **PCM signed 16-bit
  little-endian, 16 kHz mono, raw binary frames**, 20 ms per frame
  (320 samples = 640 bytes). Server responses are JSON with
  `committed_lines` arrays; the bridge typically logs these to console.

- **Recorder WebSocket** — `ws://<backend-host>:8001/record?identity=<id>&name=<display>`.
  Same audio format, but the backend records one WAV per WebSocket
  connection. The bridge should open one per utterance and close it on
  trackMuted / participant-left.

- **Live transcript POST** — `POST http://<backend-host>:8001/api/live-transcript`
  with `{ts, identity, name, text, session}`. Optional; the bridge can
  forward settled WhisperLiveKit lines so they appear in the dashboard's
  Live Transcripts feed.

A new bridge for a different platform only needs to wire those three
endpoints; everything else (transcription, sessions, hallucination
filter, dashboard) is shared backend logic.

## Adding a new bridge

1. Create `bridges/<platform>-bridge/`.
2. Add a `README.md` documenting target platform + how to load / install.
3. Implement the three integrations above against your platform's audio
   APIs. The existing `spacialchat-bridge` is a reference implementation
   to crib from.
