# local-test-bridge

Dev tool, not a deployable Bridge: a Python CLI that taps the local mic
and streams to the Recorder's `/tap` endpoint, exercising the whole
pipeline (recording, live captions, session merge) without a meeting
platform. Runs in the same venv as the Recorder.

## Setup

```bash
pip install -e ".[dev]"   # provides sounddevice
```

Linux needs PortAudio: `sudo apt install libportaudio2` (or your
distro's equivalent). macOS and Windows work out of the box.

## Usage

```bash
bash start.sh                                          # terminal 1: Recorder
python bridges/local-test-bridge/local_test_bridge.py  # terminal 2
```

**ENTER** toggles idle ↔ recording; each cycle is one utterance = one
WAV under `recordings/<session>/`. **Ctrl+C** quits cleanly (closes any
in-flight WS first so the WAV finalises). While streaming, the dashboard
lists your identity under active streams, and settled captions land in
live transcripts if the live channel is running.

Multi-speaker: run two terminals with different `--identity` values;
session merge interleaves their segments by absolute timestamp.

## Flags

| Flag | Default | Notes |
|---|---|---|
| `--host` / `--port` | `localhost` / `8001` | Recorder address. |
| `--identity` | `$USER` / `$USERNAME` / `local-tester` | Stable per-speaker id: WAV filename slug + settled-line attribution. |
| `--name` | `Local Tester` | Display name on the dashboard. |
| `--mic` | system default | sounddevice device name or index; list with `python -c "import sounddevice as sd; print(sd.query_devices())"`. |
| `--tap-token` | `$TAPSCRIBE_TAP_TOKEN` | Bearer token the recorder requires on `/tap` (carried via `Sec-WebSocket-Protocol`). Leave empty only with a `--no-auth` recorder. |
| `--tls` | off | Connect over `wss://` (recorder started with `--tls`). |
| `--session` | global current session | Detached-session id, sent as `?session=` on each `/tap` WS. See "Detached sessions" in `bridges/README.md`. |
