# tray-bridge

Home of the **tray Bridge** family (CONTEXT.md → Tray Bridge). Its one shell
today is the Windows one: it captures the default microphone **and the system
audio output (WASAPI loopback)** and streams each to the Recorder over the
standard `/tap` wire contract as its own speaker, so both sides of a meeting
land as separately-attributed WAVs in one **detached session**. A macOS shell
is being built alongside it (ADR-0020, #419); today it is the bundle and the
version floor, no UI yet. See `../README.md` for the wire contract every
Bridge speaks and `../../CONTEXT.md` for the vocabulary (Bridge, Tap,
Recorder, Utterance, Session).

## What's here

- **`src/TapScribe.Bridge.Core`** (net10.0, cross-platform): the capture seam
  (`IAudioCapture` / `IAudioDeviceEnumerator`), resampling to 16 kHz mono
  int16, the **level gate** that opens/closes Utterances on speech with
  pre-roll (the Bridge-side Mute — a loopback device has no mute event), a
  resilient `/tap` stream (stable `utterance_id` across reconnect blips,
  bounded gap buffer, bounded **Drain**), a multi-pipeline orchestrator
  running N devices concurrently under stable identities, the control client
  and connection tester, the End-meeting flow (drain → trigger the Recorder's
  strip → transcribe → summarize pipeline → poll → summary), restart-resume
  and Past-meetings state, and every view-model — all pure, so a future
  macOS/Linux shell reuses it.
- **`src/TapScribe.Bridge.Windows`** (net10.0-windows): WASAPI capture,
  loopback and enumeration (NAudio) plus `%APPDATA%` persistence with the tap
  token DPAPI-protected at rest.
- **`src/TapScribe.TrayBridge`** (net10.0-windows WinForms): the tray runner —
  event-driven status (idle / streaming / processing / error), Start meeting /
  End meeting / Past meetings / Settings… / Quit, the 4-tab Settings dialog,
  and the per-meeting summary window with Copy.
- **`src/TapScribe.Bridge.MacOS`** (net10.0-macos): the Mac platform layer.
  Today just the macOS 14.4 floor and the sysctl that reads this Mac's
  version; Core Audio process-tap capture, device enumeration and Keychain
  storage land here next. Anything it needs from the OS goes through
  P/Invoke, never the managed ObjC bindings, because the bindings cannot be
  constructed under `dotnet test`.
- **`src/TapScribe.TrayBridge.MacOS`** (net10.0-macos app bundle): the Mac
  menu-bar shell. Today the bundle, its `Info.plist` (menu-bar only, mic +
  audio-capture permissions, and deliberately no Screen Recording key) and
  the launch-time floor check.

**The cross-platform invariant:** `TapScribe.Bridge.Core` references **no
NAudio and no Windows API**. CI's `dotnet-core-crossplatform` job builds and
tests the core on Linux and fails the moment it takes a Windows dependency.

## Prerequisites

- **.NET 10 SDK** (`global.json` pins the band; `dotnet --version` should
  report 10.0.x). Get it from <https://dotnet.microsoft.com/download>.
- Windows 10/11 to run the Windows tray app. The core and its tests build and
  run on any OS.
- macOS 14.4 or newer, on Apple silicon, plus Xcode and `dotnet workload
  install macos`, for the Mac projects.

## Build, test, run

**No single OS builds the solution whole.** `TapScribe.TrayBridge.slnx` is the
union view for an editor; a net10.0-windows project needs Windows and a
net10.0-macos one needs Xcode, so each OS names its own projects. That is what
CI does too: a `(windows)` job, a `(macos)` job, and the ubuntu core job.

On Windows:

```powershell
# from this directory (bridges/tray-bridge/)
dotnet test  tests/TapScribe.Bridge.Core.Tests/TapScribe.Bridge.Core.Tests.csproj -c Release
dotnet test  tests/TapScribe.Bridge.Windows.Tests/TapScribe.Bridge.Windows.Tests.csproj -c Release
dotnet test  tests/TapScribe.TrayBridge.Tests/TapScribe.TrayBridge.Tests.csproj -c Release
dotnet build src/TapScribe.TrayBridge/TapScribe.TrayBridge.csproj -c Release
dotnet run   --project src/TapScribe.TrayBridge                 # launch the tray app
```

On macOS:

```bash
# from this directory (bridges/tray-bridge/)
dotnet test  tests/TapScribe.Bridge.MacOS.Tests/TapScribe.Bridge.MacOS.Tests.csproj -c Release
dotnet build src/TapScribe.TrayBridge.MacOS/TapScribe.TrayBridge.MacOS.csproj -c Release
```

Cross-platform core only (what the ubuntu CI job runs, works on Linux/macOS
and on Windows):

```bash
dotnet test tests/TapScribe.Bridge.Core.Tests/TapScribe.Bridge.Core.Tests.csproj -c Release
```

`TapClientWebSocketTests` covers `/tap` negotiation + binary framing (tokened
and `--no-auth`) against an in-process Kestrel server, and
`RealRecorderMeetingE2ETests` runs a full meeting against a real Python
Recorder (self-skips where faster-whisper isn't importable) — so wire
regressions are caught in CI without a live Recorder on your desk.

## Packaging: a self-contained single-file exe

```powershell
# from this directory (bridges/tray-bridge/)
dotnet publish src/TapScribe.TrayBridge -c Release -r win-x64 `
  --self-contained `
  -p:PublishSingleFile=true `
  -p:IncludeNativeLibrariesForSelfExtract=true
```

The exe lands at
`src/TapScribe.TrayBridge/bin/Release/net10.0-windows/win-x64/publish/TapScribe.TrayBridge.exe`
and runs on a clean Windows 10/11 box (use `-r win-arm64` for ARM). No
installer, code signing, or auto-update — it's a copy-and-run exe.

## Configuration

Right-click the tray icon → **Settings…** — no environment variables
required. Settings are saved to `%APPDATA%\TapScribe\windows-tray-bridge.json`
and remembered across restarts.
(The filename deliberately keeps the pre-rename `windows-tray-bridge` spelling —
it is the on-disk contract pinned by `BridgeSettingsStore.SettingsFileName`, and
renaming it would orphan every operator's saved settings and protected token.)
The **tap token is never written in cleartext**: it is protected with Windows
DPAPI (CurrentUser scope). On first run the Connection fields are seeded from
the legacy `TAPSCRIBE_HOST` / `TAPSCRIBE_PORT` / `TAPSCRIBE_TLS` /
`TAPSCRIBE_TLS_ALLOW_SELF_SIGNED` / `TAPSCRIBE_IDENTITY` / `TAPSCRIBE_NAME` /
`TAPSCRIBE_TAP_TOKEN` environment variables when present; the dialog is the
source of truth thereafter.

- **Connection** — Recorder host (tolerant: a hostname, an IP, or a pasted
  `wss://host:9000/` all work; Port/TLS stay authoritative), port (default
  `8001`), **Use TLS**, and the tap token (from the Recorder's boot log /
  `.tap-token`; **empty = `--no-auth`**). **Allow self-signed certificate
  (insecure)** is the `curl -k` equivalent — accepts **any** cert on every
  connection, TLS-gated, off by default, local testing only. **Test
  connection** probes `GET /health`, then a throwaway `/tap` handshake to
  confirm the token.
- **Devices** — two checkboxes, each with one **Name**: capture the mic
  and/or the system audio. The Name labels the source on the dashboard and
  tags the recording filenames (the two must differ, or Start refuses). Both
  are *follow-default*: they bind to the current default device at Start
  (ADR-0005). Each has its own **Sensitivity** slider (per-device tuning —
  ADR-0007) with a live input-level meter drawing that device's threshold on
  the same scale; Saving mid-meeting re-tunes only that device's pipeline, no
  Stop/Start. **▸ Advanced** pins specific endpoints (an unplugged pin is
  kept, not erased); **Refresh devices** re-enumerates.
- **Level gate** — the two knobs shared across devices: **Hangover** (how
  long silence lasts before an Utterance closes) and **Pre-roll** (leading
  audio replayed on open so first consonants aren't clipped), both in
  milliseconds.
- **Meeting** — **Transcribe and summarize automatically when the meeting
  ends** (default on): End meeting drains and closes every tap, triggers the
  Recorder's end-of-meeting pipeline, tracks per-stage progress in the status
  line, and pops the summary window. Off = *record-only*: End meeting still
  drains and closes every tap but fires no pipeline — strip / transcribe /
  summarize from the dashboard later. The active session id is persisted, so
  a restarted tray resumes an in-flight pipeline or the finished summary.

## Dev loop (the acceptance check)

1. Start a Recorder: `python -m tapscribe --no-auth` (or `./start.ps1`).
2. `dotnet run --project src/TapScribe.TrayBridge`.
3. Right-click → **Start meeting**. Play meeting audio while you speak,
   pausing between sentences, then **End meeting**.
4. **Two** sets of WAVs appear under the Recorder's new detached session —
   your identity (the mic) and "System audio" (the loopback) — each split
   into Utterances by the level gate, and the pipeline runs through to a
   summary window with **Copy**.
5. Tokened path: paste the Recorder's token into **Settings… → Connection**,
   Save, then Start meeting against a Recorder started **without**
   `--no-auth`.

## Wire contract (summary)

One `/tap` WebSocket per Utterance; raw PCM, 16 kHz mono int16, 20 ms
(640-byte) binary frames; tap token via the `tapscribe.v1.tap.<token>`
subprotocol. The gate mints a fresh `utterance_id` per speech segment and the
stream keeps it stable across reconnects; **Drain** flushes the bounded
trailing buffer when an Utterance ends while reconnecting. The Bridge sends
only PCM — no JSON, no control messages (ADR-0002). The full contract lives
in `../README.md`.
