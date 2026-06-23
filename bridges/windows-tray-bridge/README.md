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

**Now (the core audio lifecycle + multi-device capture + the tray shell):** capture
behind a core interface for both **microphones** and **system-audio loopback** (WASAPI
is the Windows impl of each), device **enumeration** (mics + loopback-capable render
devices), a resampler to 16 kHz mono int16, a **level gate** that opens/closes
Utterances on speech with pre-roll so leading consonants aren't clipped (the
Bridge-side Mute — a loopback device has no mute event), a resilient `/tap` stream
with `utterance_id` **reconnect** across blips, a bounded during-gap buffer, and
bounded **Drain**, a **multi-pipeline orchestrator** that runs N devices
concurrently — each under its own stable `identity`/`name` — co-located in one
**detached session**, a control client, a tray runner with at-a-glance **status**
(idle / streaming / error — event-driven, no idle polling) and Start meeting / Stop
meeting / Quit, and a **3-tab Settings dialog** — Connection (host / port / TLS /
tap token + Test connection), **Devices** (capture the mic and/or system audio, each
with one Name **and its own sensitivity slider**, plus an Advanced expander to pin
specific endpoints), and **Level gate** (the shared hangover/pre-roll in ms) — all persisted to
`%APPDATA%`, with the token protected at rest by Windows DPAPI. Start meeting **resolves** the saved selection against the devices present now
(follow-default binds to the current default), so a bad token or unreachable Recorder
fails with a clear, classified message *before* any device opens.

**Deferred to a later PRD #99 slice:** the end-of-meeting pipeline trigger with
progress and summary display (#107). The depth lives in the cross-platform core
(`CaptureOrchestrator`, `DeviceSelection`, `StatusView`, `GateTuning`) so #107 (and a
future macOS/Linux shell) builds on it.

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
│   │   ├── CaptureOrchestrator.cs       # runs N per-identity pipelines (mic + loopback) concurrently
│   │   ├── DeviceSelection.cs           # follow-default/pinned selections -> Resolve() -> verdict + ToTapOptions (ADR-0005)
│   │   ├── StartFailure.cs              # classify a Start error: TokenRejected / Unreachable / Other
│   │   ├── StatusView.cs                # TrayStatus -> menu header + icon + tooltip (pure)
│   │   └── GateTuning.cs                # sensitivity slider <-> linear RMS threshold
│   ├── TapScribe.Bridge.Windows/       # net10.0-windows — WASAPI + settings (NAudio + DPAPI)
│   │   ├── WasapiCaptureBase.cs         # shared WASAPI normalisation + lifecycle (one authority)
│   │   ├── WasapiAudioCapture.cs        # IAudioCapture over a microphone (default or specific)
│   │   ├── WasapiLoopbackAudioCapture.cs # IAudioCapture over a render endpoint (system-audio loopback)
│   │   ├── WasapiDeviceEnumerator.cs    # IAudioDeviceEnumerator over NAudio MMDeviceEnumerator
│   │   └── BridgeSettings.cs            # %APPDATA% persistence; DPAPI-protected token
│   └── TapScribe.TrayBridge/           # net10.0-windows WinForms tray runner (GUI only)
│       ├── Program.cs, TrayContext.cs   # NotifyIcon: status header + Start / Stop / Settings / Quit
│       ├── TrayIcons.cs                 # the 3 status icons, drawn at runtime (idle/streaming/error)
│       └── SettingsForm.cs              # 3-tab dialog: Connection / Devices / Level gate
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

## Packaging: a self-contained single-file exe

To hand someone a single `.exe` that runs without a .NET install, publish the tray
runner self-contained:

```powershell
# from this directory (bridges/windows-tray-bridge/)
dotnet publish src/TapScribe.TrayBridge -c Release -r win-x64 `
  --self-contained `
  -p:PublishSingleFile=true `
  -p:IncludeNativeLibrariesForSelfExtract=true
```

The exe lands at
`src/TapScribe.TrayBridge/bin/Release/net10.0-windows/win-x64/publish/TapScribe.TrayBridge.exe`.
It bundles the .NET runtime and the NAudio native bits (the `IncludeNative…` flag
self-extracts them on launch), so it runs on a clean Windows 10/11 box. Use
`-r win-arm64` for ARM machines. No installer, code signing, or auto-update — those
are explicitly out of scope for this PRD (#99); it's a copy-and-run exe.

## Configuring the Recorder target

Right-click the tray icon → **Settings…** to edit everything in a tabbed dialog — no
environment variables required. Settings are saved to
`%APPDATA%\TapScribe\windows-tray-bridge.json` and remembered across restarts.
The **tap token is never written in cleartext**: it is protected at rest with
Windows DPAPI (CurrentUser scope), so only the same Windows user can read it.

### Connection tab

| Field | Default | Meaning |
|---|---|---|
| Recorder host | `localhost` | Recorder host |
| Port | `8001` | Recorder port |
| Use TLS | off | connect over `wss://` (Recorder started with `--tls`) |
| Tap token | empty | tap token; **empty = `--no-auth`** (offer no subprotocol) |

The **Test connection** button (like the SpatialChat bridge) probes `GET /health`
for reachability and then opens a throwaway `/tap` handshake to confirm the tap
token is accepted, reporting "Recorder reachable; tap token accepted" or the
specific failure. The **Recorder host** field is tolerant — a plain hostname, an
IP, or a pasted `wss://host:9000/`-style value all work; the scheme/port/path are
stripped and the Port/TLS fields stay authoritative. The tap token is the value the
Recorder prints at boot (also stored in `.tap-token`). (Per-source names are edited on
the **Devices** tab, below — not here.)

### Devices tab

The common case is two checkboxes, each with a single **Name**:

- **Capture my microphone** — your mic.
- **Capture system audio (the other side of the meeting)** — the system loopback.

The Name labels the source on the dashboard *and* tags it in the recording filenames
— the Recorder makes a filename-safe version automatically, so you only fill in one
thing. Give the two different names (a shared name is refused at Start, since the
Recorder would cross-attribute them into one speaker).

Both are *follow-default* selections: they bind to whatever your current default
device is *at Start*, so switching your default output (Bluetooth ↔ speakers) keeps
"system audio" working without reconfiguring (see ADR-0005). If no default is
configured but devices exist, the first device of that kind is used.

Each row also has its **own Sensitivity slider** (per-device tuning — ADR-0007). The
mic defaults less sensitive (so room noise doesn't open it) and the system loopback
more sensitive (so the quiet far end is captured). Changing a slider and Saving during
a live meeting re-tunes **only that device's** pipeline, with no Stop/Start.

**▸ Advanced — pin specific devices…** expands a grid of every concrete endpoint so a
power user can pin a specific interface (e.g. a particular USB mic) instead of
following the default, again with its own Name. **Refresh devices** re-enumerates
after you plug something in. A pinned device that's currently unplugged is kept (not
erased) so re-plugging it restores the pin.

With nothing selected, the Bridge falls back to the default mic + system-audio pair
(the pre-#106 behaviour). A selection where nothing is available is refused at Start
with a clear message rather than recording an empty session.

### Level gate tab

The Bridge-side gate that turns sound into Utterances (a loopback device has no mute
event, so the level gate *is* the Mute — see CONTEXT.md). **Sensitivity** is set per
device on the **Devices** tab (it maps to the gate's linear RMS threshold; higher =
opens on quieter sound). This tab holds the two knobs shared across every device:
**Hangover** (ms) is how long silence must last before an Utterance closes, and
**Pre-roll** (ms) is how much leading audio is replayed when it opens so the first
consonants aren't clipped. An old settings file's single global tuning migrates into
each device's default on upgrade (no reset — ADR-0007).

On first run the **Connection** fields are pre-seeded from the legacy `TAPSCRIBE_HOST` /
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

### Live per-device re-tune demo (#153)

To see per-device sensitivity take effect mid-meeting:

1. **Start meeting** with the far end (system audio) quiet — quiet enough that the
   system loopback gate doesn't open, so no `system` WAVs appear.
2. Open **Settings… → Devices**, raise **only** the system-audio Sensitivity, and
   **Save** (don't Stop). The far end starts being captured immediately, with no
   Stop/Start, and the mic pipeline is untouched.

This routes by identity through `CaptureOrchestrator.UpdateGates(map)`; the
`UpdateGates_RoutesEachUpdateToItsOwnPipeline_ByIdentity` /
`UpdateGates_SkipsAnIdentityWithNoRunningPipeline_WithoutError` /
`UpdateGates_DoesNotDisturbAnotherPipelinesOpenUtterance` tests pin the routing in CI.

### Isolation demo (per-bridge sessions)

To see that the tray Bridge's **detached** session is isolated from other bridges,
run a second bridge concurrently against the same Recorder:

1. Start the Recorder and **Start meeting** from the tray (above).
2. In parallel, run the developer bridge **without** a session id, e.g.
   `python bridges/local-test-bridge/local_test_bridge.py` (it taps `/tap` with no
   `?session=`, so it lands in the Recorder's global current session).
3. On the dashboard you'll see **two** sessions filling at once: the tray Bridge's
   detached session (mic + `system`) and the local-test-bridge's global one — never
   muddled into one folder.

That isolation is a Recorder-side property (the `?session=` routing landed in #100,
covered by the Recorder's Python tests); on the bridge side, `ResolveResult.ToTapOptions`
stamps the detached session id onto **every** device's tap, which is asserted by
`DeviceSelectionTests.ToTapOptions_StampsTheDetachedSessionAndPerDeviceIdentityName`.

## Wire contract (summary)

One `/tap` WebSocket per Utterance; raw PCM, 16 kHz mono int16, 20 ms (640-byte)
binary frames; tap token via the `tapscribe.v1.tap.<token>` subprotocol. The level
gate mints a fresh `utterance_id` per speech segment and the stream keeps it stable
across reconnects, so a mid-Utterance blip appends to the same WAV; **Drain**
flushes the trailing buffered audio (bounded) when an Utterance ends while
reconnecting. The Bridge sends only PCM — no JSON, no control messages, and it
never talks to WhisperLiveKit (ADR-0002). The full contract lives in `../README.md`.
