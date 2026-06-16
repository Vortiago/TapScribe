# windows-tray-bridge

A native Windows **Bridge** for TapScribe: it captures the default microphone
**and the system audio output (WASAPI loopback)** and streams each to the Recorder
over the standard `/tap` wire contract as its own speaker, so both sides of a
meeting are recorded under distinct identities in one session. Built on the C#
stack proven by the tracer bullet (PRD #99, issues #103/#105): capture → resample →
level gate → `/tap` → separately-attributed WAVs in the Recorder's session.

It is the repo's first native desktop Bridge; see `../README.md` for the wire
contract every Bridge speaks and `../../CONTEXT.md` for the domain vocabulary
(Bridge, Tap, Recorder, Utterance, Session).

## What's here / what's deferred

**Now (the core audio lifecycle + multi-device capture):** capture behind a core
interface for both **microphones** and **system-audio loopback** (WASAPI is the
Windows impl of each), device **enumeration** (mics + loopback-capable render
devices), a resampler to 16 kHz mono int16, a **level gate** that opens/closes
Utterances on speech with pre-roll so leading consonants aren't clipped (the
Bridge-side Mute — a loopback device has no mute event), a resilient `/tap` stream
with `utterance_id` **reconnect** across blips, a bounded during-gap buffer, and
bounded **Drain**, a **multi-pipeline orchestrator** that runs N devices
concurrently — each under its own stable `identity`/`name` (the mic under the
operator's identity, the loopback under `system`) — co-located in one **detached
session**, a control client, a tray runner (Start meeting / Stop meeting / Quit),
and a **Settings dialog** (host / port / TLS / identity / name / tap token)
persisted to `%APPDATA%`, with the token protected at rest by Windows DPAPI.

**Deferred to later PRD #99 slices:** a tray **device-picker UI** to choose exactly
which devices to tap and edit per-device identities/names (#106 — the runner here
auto-taps the default mic + default loopback); and the end-of-meeting pipeline
trigger with summary display (#107). The tray here is intentionally no-frills; the
depth lives in the cross-platform core (`CaptureOrchestrator`) so those slices (and
a future macOS/Linux shell) build on it.

## Layout

```
windows-tray-bridge/
├── global.json                         # pins the .NET 10 SDK band
├── TapScribe.WindowsTrayBridge.slnx
├── src/
│   ├── TapScribe.Bridge.Core/          # net10.0 — CROSS-PLATFORM, no NAudio
│   │   ├── TapWire.cs                   # 16 kHz / mono / 640-byte frame constants
│   │   ├── AudioFormat.cs, IAudioCapture.cs   # the capture seam
│   │   ├── Resampler.cs                 # device format -> 16 kHz mono int16
│   │   ├── FrameChunker.cs              # -> exact 640-byte / 20 ms frames
│   │   ├── LevelGate.cs                 # Bridge-side Mute: gate Utterances on level + pre-roll (+ GateOptions.cs)
│   │   ├── TapConnectionOptions.cs      # URL + subprotocol builders; host normalisation
│   │   ├── ITapConnection.cs            # the connection seam TapStream drives (TapClient is the impl)
│   │   ├── TapClient.cs                 # one /tap WebSocket (implements ITapConnection)
│   │   ├── TapStream.cs                 # resilient Utterance: reconnect + gap buffer + Drain (+ TapStreamOptions.cs)
│   │   ├── ControlClient.cs             # tap-bearer POST /api/tap/new-session; GET /health
│   │   ├── ConnectionTester.cs          # "Test connection": /health + tap-token probe
│   │   ├── TapSession.cs                # pipeline: capture -> resampler -> level gate -> a TapStream per Utterance
│   │   ├── CaptureDevice.cs             # platform-neutral device descriptor + DeviceFlow (Capture/Render)
│   │   ├── IAudioDeviceEnumerator.cs    # device-listing seam: List() + Open() (WASAPI is the impl)
│   │   └── CaptureOrchestrator.cs       # runs N per-identity pipelines (mic + loopback) concurrently
│   ├── TapScribe.Bridge.Windows/       # net10.0-windows — WASAPI + settings (NAudio + DPAPI)
│   │   ├── WasapiCaptureBase.cs         # shared WASAPI normalisation + lifecycle (one authority)
│   │   ├── WasapiAudioCapture.cs        # IAudioCapture over a microphone (default or specific)
│   │   ├── WasapiLoopbackAudioCapture.cs # IAudioCapture over a render endpoint (system-audio loopback)
│   │   ├── WasapiDeviceEnumerator.cs    # IAudioDeviceEnumerator over NAudio MMDeviceEnumerator
│   │   └── BridgeSettings.cs            # %APPDATA% persistence; DPAPI-protected token
│   └── TapScribe.TrayBridge/           # net10.0-windows WinForms tray runner (GUI only)
│       ├── Program.cs, TrayContext.cs   # NotifyIcon: Start meeting / Stop meeting / Settings / Quit
│       └── SettingsForm.cs              # settings dialog incl. "Test connection"
└── tests/
    ├── TapScribe.Bridge.Core.Tests/     # net10.0 xUnit — cross-platform (most of the suite, incl. CaptureOrchestrator)
    └── TapScribe.Bridge.Windows.Tests/  # net10.0-windows xUnit — DPAPI / settings + NAudio upstream-contract smoke test
```

**The cross-platform invariant:** `TapScribe.Bridge.Core` references **no
NAudio and no Windows API**. Audio capture sits behind `IAudioCapture`; WASAPI
is just the Windows implementation, isolated in `TapScribe.Bridge.Windows`. The
resampler and the `/tap` client live in the core. CI enforces this: the
`dotnet-core-crossplatform` job builds and tests the core on Linux, which fails
the moment the core takes a Windows dependency.

## Prerequisites

- **.NET 10 SDK** (`global.json` pins the band; `dotnet --version` should report
  a 10.0.x). Get it from <https://dotnet.microsoft.com/download>.
- Windows 10/11 to run the tray app (the WASAPI backend). The core and its
  tests build and run on any OS.

## Build, test, run

```powershell
# from this directory (bridges/windows-tray-bridge/)
dotnet build TapScribe.WindowsTrayBridge.slnx -c Release       # whole solution
dotnet test  TapScribe.WindowsTrayBridge.slnx -c Release       # runs all tests (core + Windows)
dotnet run   --project src/TapScribe.TrayBridge                # launch the tray app
```

Cross-platform core only (what the ubuntu CI job runs — works on Linux/macOS):

```bash
dotnet test tests/TapScribe.Bridge.Core.Tests/TapScribe.Bridge.Core.Tests.csproj -c Release
```

## Configuring the Recorder target

Right-click the tray icon → **Settings…** to edit everything in a dialog — no
environment variables required. Settings are saved to
`%APPDATA%\TapScribe\windows-tray-bridge.json` and remembered across restarts.
The **tap token is never written in cleartext**: it is protected at rest with
Windows DPAPI (CurrentUser scope), so only the same Windows user can read it.

The dialog has a **Test connection** button (like the SpatialChat bridge): it
probes `GET /health` for reachability and then opens a throwaway `/tap` handshake
to confirm the tap token is accepted, reporting "Recorder reachable; tap token
accepted" or the specific failure. The **Recorder host** field is tolerant — a
plain hostname, an IP, or a pasted `wss://host:9000/`-style value all work; the
scheme/port/path are stripped and the Port/TLS fields stay authoritative.

| Field | Default | Meaning |
|---|---|---|
| Recorder host | `localhost` | Recorder host |
| Port | `8001` | Recorder port |
| Use TLS | off | connect over `wss://` (Recorder started with `--tls`) |
| Identity | OS username | the **microphone's** per-speaker identity (WAV filename slug) |
| Name | empty | the microphone's display name shown on the dashboard |
| Tap token | empty | tap token; **empty = `--no-auth`** (offer no subprotocol) |

The Identity/Name above apply to the **microphone** pipeline; the system-audio
loopback always streams under identity `system` / name `System Audio`, so the two
sides never collide. (A device-picker UI to tap more devices and edit each one's
identity is the next slice, #106.)

The tap token is the value the Recorder prints at boot (also stored in
`.tap-token`). With an empty token the Bridge offers no `Sec-WebSocket-Protocol`,
which only works against a Recorder started with `--no-auth`.

On first run the dialog is pre-seeded from the legacy `TAPSCRIBE_HOST` /
`TAPSCRIBE_PORT` / `TAPSCRIBE_TLS` / `TAPSCRIBE_IDENTITY` / `TAPSCRIBE_NAME` /
`TAPSCRIBE_TAP_TOKEN` environment variables when present, so an existing
env-based setup migrates automatically. They are optional, and the dialog is the
source of truth thereafter.

## Dev loop / demo (the acceptance check)

1. Start a Recorder. Simplest: `python -m tapscribe --no-auth` (or `./start.ps1`).
2. `dotnet run --project src/TapScribe.TrayBridge`. A TapScribe icon appears in
   the notification area.
3. Right-click → **Start meeting**. Play some meeting audio (a video call, a
   YouTube clip) while you speak, pausing between sentences, then **Stop meeting**.
4. **Two** sets of WAVs appear under the Recorder's new **detached** session — one
   under your identity (the microphone) and one under `system` (the loopback) — each
   split into Utterances by the level gate. Both sides of the meeting are recorded as
   separately-attributed speakers in the same session (issue #105's acceptance check).
   "Start meeting" mints the detached session and starts one pipeline per device;
   "Stop meeting" drains and closes them all (bounded, concurrently).
5. To exercise the tokened path: open **Settings…**, paste the token (from the
   Recorder's boot log / `.tap-token`) into the **Tap token** field, Save, then
   **Start meeting** — against a Recorder started **without** `--no-auth`.

The `TapClientWebSocketTests` cover the same negotiation + binary-frame
round-trip (both `--no-auth` and tokened) against an in-process Kestrel `/tap`
server, so a wire regression is caught in CI without needing a live Recorder.

## Wire contract (summary)

One `/tap` WebSocket per Utterance; raw PCM, 16 kHz mono int16, 20 ms (640-byte)
binary frames; tap token via the `tapscribe.v1.tap.<token>` subprotocol. The level
gate mints a fresh `utterance_id` per speech segment and the stream keeps it stable
across reconnects, so a mid-Utterance blip appends to the same WAV; **Drain**
flushes the trailing buffered audio (bounded) when an Utterance ends while
reconnecting. The Bridge sends only PCM — no JSON, no control messages, and it
never talks to WhisperLiveKit (ADR-0002). The full contract lives in `../README.md`.
