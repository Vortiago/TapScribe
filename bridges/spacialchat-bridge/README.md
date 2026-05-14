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

See `../README.md` for the wire protocol the backend expects.

## Loading unpacked (dev)

1. Start the TapScribe backend (`bash start.sh` from the repo root).
2. Open `chrome://extensions/`, enable **Developer mode**, click
   **Load unpacked**, pick this directory.
3. Open `https://app.spatial.chat/...` in a new tab.
4. Click the extension icon → set **Backend host** to `localhost` (or the
   LAN IP of the backend machine if running cross-machine) → Save.
5. Reload the spatial.chat tab. The extension's content script will open
   one WhisperLiveKit WebSocket per remote participant for live captions
   and one recorder WebSocket per utterance for per-WAV transcription.

## Verifying the pipeline

The repo's "Independent verification" check still works:

1. Open `http://localhost:8000` in any browser.
2. Click record in WhisperLiveKit's bundled UI.
3. Say "testing one two three" into your microphone.
4. **Expected**: transcript appears in that UI within ~2 s.

That isolates "is the backend healthy?" from "is the bridge healthy?"
before chasing extension bugs.
