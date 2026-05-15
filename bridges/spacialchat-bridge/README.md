# spacialchat-bridge

Chrome (MV3) extension that taps remote audio in a spatial.chat tab and
forwards it to a running TapScribe Recorder over the `/tap` WebSocket
endpoint.

See `../README.md` for the wire contract the Recorder expects. The short
version: one WebSocket per active speaker per utterance to
`ws://<recorder-host>:8001/tap?identity=<spatial-id>&name=<display>`,
streaming raw 16 kHz mono int16 PCM in 20 ms (640-byte) frames. The
Recorder fans that audio out internally to its supervised WhisperLiveKit
child (live captions) and to a per-utterance WAV on disk — the bridge
never sees the WhisperLiveKit protocol and doesn't POST settled lines
back. (See ADR-0002 for the architectural reasoning.)

## Files

```
spacialchat-bridge/
├── manifest.json     MV3 manifest
├── content.js        ISOLATED-world content script: /tap WS lifecycle, status snapshot
├── page-script.js    MAIN-world script: LiveKit Room tap + 48k→16k AudioWorklet
├── popup.html        configuration UI markup
├── popup.js          configuration UI logic (host/port, /health probe, status table)
└── README.md         this file
```

## How it works

1. `content.js` is injected into every `https://app.spatial.chat/*` page
   by the manifest and immediately injects `page-script.js` into the
   MAIN world so it can reach `window.room` (LiveKit Room instance).
2. `page-script.js` watches `window.room` (re-attaches every time
   spatial.chat swaps the room), subscribes to `trackSubscribed` /
   `trackMuted` / etc., and resamples each audio track to 16 kHz int16
   via an inline AudioWorklet. PCM frames are posted to the content
   script via `window.postMessage`.
3. `content.js` opens one `/tap` WebSocket per speaker per utterance on
   the first non-muted PCM frame and closes it on `trackMuted`. Muting
   is the end-of-utterance signal; the Recorder finalises a WAV on
   close and starts a fresh one when the next `/tap` opens.
4. A status snapshot is written to `chrome.storage.local` whenever
   anything changes, so the popup can show live per-speaker state
   without opening DevTools.

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
