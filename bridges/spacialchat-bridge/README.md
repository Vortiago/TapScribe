# spacialchat-bridge

Chrome (MV3) extension that taps remote audio in a spatial.chat tab and
forwards it to a running TapScribe backend.

## Status

Source upload pending. Once added, this directory should contain at minimum:

```
spacialchat-bridge/
├── manifest.json          MV3 manifest
├── background.js          (or src/background.ts)
├── content.js             content-script injected into app.spatial.chat
├── popup.html / popup.js  configuration UI (backend host, language)
├── icons/                 16/48/128 px
└── README.md              this file
```

See `../README.md` for the wire protocol the Recorder expects. The
short version: open one WebSocket per active speaker per utterance to
`ws://<recorder-host>:8001/tap?identity=<spatial-id>&name=<display>`
and stream raw 16 kHz mono int16 PCM. That's the entire job — the
Recorder fans the audio out to the WAV write AND to its supervised
WhisperLiveKit child for live captions. Settled lines come back to
the Recorder directly; the bridge does NOT see WhisperLiveKit's
protocol or post settled lines back. (See ADR-0002 for the
architectural reasoning.)

## Loading unpacked (dev)

1. Start the TapScribe Recorder (`bash start.sh` from the repo root).
2. Open `chrome://extensions/`, enable **Developer mode**, click
   **Load unpacked**, pick this directory.
3. Open `https://app.spatial.chat/...` in a new tab.
4. Click the extension icon → set **Recorder host** to `localhost`
   (or the LAN IP of the Recorder machine if running cross-machine)
   → Save.
5. Reload the spatial.chat tab. The extension's content script will
   open one `/tap` WebSocket per remote participant per utterance,
   keyed by the participant's spatial.chat identity.

## Verifying the pipeline before debugging the bridge

You can confirm the Recorder + WhisperLiveKit pipeline works without
the bridge installed at all — the `bridges/local-test-bridge/` Python
tool taps the local mic and streams to `/tap`, exercising the same
contract this extension implements. If captions appear there, the
bridge is the only remaining variable.
