# local-test-bridge

The simplest possible Bridge: a Python CLI that taps your local
microphone and streams to the Recorder's `/tap` endpoint, so you can
test the whole TapScribe pipeline (recording + live captions + session
merge) without a full spatial.chat room or any other meeting platform.

This is a **dev tool**, not a deployable Bridge. Real bridges
(`spacialchat-bridge`, future Teams / Meet / Zoom add-ins) live in
their own platform's deployment story; this one runs in the same venv
as the Recorder for zero-friction local development.

## Setup

From the repo root, install the dev extras (gives you sounddevice):

```bash
pip install -e ".[dev]"
```

`websockets` is already a base TapScribe dependency.

PortAudio note: `sounddevice` is a Python wrapper around PortAudio.
- macOS: works out of the box.
- Windows: works out of the box.
- Linux: `sudo apt install libportaudio2` (or your distro's equivalent).

## Usage

In one terminal, start the Recorder:

```bash
bash start.sh
```

In another, start the test bridge:

```bash
python bridges/local-test-bridge/local_test_bridge.py
```

The CLI prints `[idle]` and waits. Press **ENTER** to begin streaming —
a fresh `/tap` WS opens and the Recorder starts a new WAV under
`recordings/<session>/`. Press **ENTER** again to pause — the WS closes
and the WAV is finalised. Each ENTER cycle = one utterance = one WAV.
**Ctrl+C** quits cleanly (closes any in-flight WS first so the WAV
finalises).

While streaming, the dashboard's "active streams" panel shows a row for
your identity, and (if the live channel is running) settled captions
land in the "live transcripts" panel attributed to your identity.

## CLI flags

| Flag | Default | Notes |
|---|---|---|
| `--host` | `localhost` | Recorder host. Set this to the LAN IP if running the bridge on a different machine. |
| `--port` | `8001` | Recorder port. |
| `--identity` | `$USER` / `$USERNAME` / `local-tester` | Stable per-speaker identifier. Used as the WAV filename slug + the `identity` field on settled-line entries. Override with different values to simulate multi-speaker scenarios from multiple terminals. |
| `--name` | `Local Tester` | Display name shown on the dashboard. |
| `--mic` | system default | sounddevice input device name or index. List devices with `python -c "import sounddevice as sd; print(sd.query_devices())"`. |
| `--session` | global current session | Detached-session id to direct this bridge's taps into (sent as `?session=` on each `/tap` WS). Mint one with `POST /api/tap/new-session` body `{"detached": true}`; see "Detached sessions" in `bridges/README.md`. Two terminals — one with `--session`, one without — demo per-bridge isolation against a single Recorder. |

## Multi-speaker testing

Run two terminals with different `--identity` values. They'll both stream
to the same Recorder, each producing its own per-utterance WAVs and its
own attributed live captions. The session-merge endpoint will interleave
their segments by absolute timestamp.

```bash
# Terminal 1
python bridges/local-test-bridge/local_test_bridge.py --identity alice --name Alice

# Terminal 2
python bridges/local-test-bridge/local_test_bridge.py --identity bob --name Bob
```

## Verifying the pipeline

This bridge is the easiest way to test the Recorder end-to-end. If it
captures audio + the dashboard shows it + captions land in
`live transcripts` + the WAV transcribes correctly, the entire
non-bridge half of TapScribe is working. After that, debugging a real
platform bridge (e.g. spacialchat-bridge) becomes "is the bridge
producing the right PCM?" — the rest is already known to work.
