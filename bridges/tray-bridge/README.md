# tray-bridge

Home of the **tray Bridge** family (CONTEXT.md → Tray Bridge). A shell captures
the default microphone **and the system audio the machine is playing** and
streams each to the Recorder over the standard `/tap` wire contract as its own
speaker, so both sides of a meeting land as separately-attributed WAVs in one
**detached session**. Two shells share that job: the Windows tray, which takes
system audio off a WASAPI loopback, and the macOS menu bar, which uses a Core
Audio process tap because macOS has no loopback endpoint (ADR-0020). Each ships
as a zip on the newest release, which the dashboard's Settings → **Get a
bridge** card links; see Packaging below for what an operator does with one.
See `../README.md` for the wire contract every Bridge speaks and
`../../CONTEXT.md` for the vocabulary (Bridge, Tap, Recorder, Utterance,
Session).

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
  and Past-meetings state, every view-model, and the on-disk stores
  (settings / meeting state / meeting history) with the tap token's meaning at
  rest behind `ITapTokenStore` — all pure, so a future macOS/Linux shell
  reuses it. The whole meeting lifecycle is `BridgeRuntime`, a UI-free class
  behind two seams: `ITrayView` (status, notices, menu state, meeting window,
  shutdown) and `IDispatcher` (marshalling to the shell's UI thread, because
  .NET installs no `SynchronizationContext` on macOS). A shell implements
  those two and inherits the lifecycle with its tests.
- **`src/TapScribe.Bridge.Windows`** (net10.0-windows): WASAPI capture,
  loopback and enumeration (NAudio), plus the Windows half of the storage
  layer — `DpapiTapTokenStore` and the `TrayStores` binding of the Core stores
  to `%APPDATA%\TapScribe`.
- **`src/TapScribe.TrayBridge`** (net10.0-windows WinForms): the tray shell,
  which is Core's `ITrayView` over a `NotifyIcon` and a `ContextMenuStrip`.
  Widgets only: the menu (Start meeting / End meeting / Past meetings /
  Settings… / Quit), the 4-tab Settings dialog, and the per-meeting summary
  window with Copy. What a meeting DOES is `BridgeRuntime`'s.
- **`src/TapScribe.Bridge.MacOS`** (net10.0): the Mac platform layer. Today the
  macOS 14.4 floor, the sysctl that reads this Mac's version, and the Mac half
  of the storage layer: `KeychainTapTokenStore` (the tap token in the login
  Keychain, so the settings file carries nothing about it) plus the `TrayStores`
  binding of the Core stores to `~/Library/Application Support/TapScribe`,
  and the Core Audio capture and device enumeration behind the portable seams:
  the microphone as an IOProc on the endpoint, and system audio as a **process
  tap** inside a private aggregate device, because macOS has no loopback
  endpoint (#420, ADR-0020). Everything it asks the OS goes through P/Invoke,
  never the managed ObjC bindings (`MacOSProductVersion` states that rule and
  why), with one exception `CoreAudioHal.Tap.cs` explains: `CATapDescription`
  is an ObjC class that `Microsoft.macOS` does not bind, so it is built through
  the runtime's own C entry points rather than through a hand-written binding
  that no test host could construct. The plain TFM is therefore still enough,
  and its tests run on every CI lane rather than only on a Mac.

  Recording system audio needs the **System Audio Recording** TCC grant, which
  macOS asks for the first time the IOProc starts rather than when the tap is
  created. A process with no bundle identity cannot be prompted, so a bare
  `dotnet run` against the HAL BLOCKS at `AudioDeviceCreateIOProcID`; only the
  built `.app` is a supported way to exercise that path.
- **`src/TapScribe.TrayBridge.MacOS`** (net10.0-macos app bundle): the Mac
  menu-bar shell, Core's `ITrayView` over an `NSStatusItem`: the same menu as
  the Windows tray, plus the Settings and per-meeting windows. It also holds
  the Mac `IDispatcher` (`DispatchQueue.MainQueue`; .NET installs no
  `SynchronizationContext` here, which is why the seam exists), the bundle and
  its `Info.plist` (menu-bar only, mic + audio-capture permissions, and
  deliberately no Screen Recording key), and the launch-time floor check. Its
  tests reference it, so they assert against the `Info.plist` inside the `.app`
  a build just produced rather than the source file the SDK is free to rewrite.

  **Nothing NSObject-derived in it can carry a unit test**: constructing one
  under the `dotnet test` host throws inside `ObjCRuntime`, because the bridge
  is never initialised. So the AppKit types (`TrayShell`, `MeetingWindow`,
  `SettingsWindow`) are covered by the build and by a manual check on a Mac,
  and every decision that could live below them does: the glyph per state
  (`StatusSymbols`), the notice line (`MenuNotice`), the draft seed
  (`SettingsSeed`), the field parse (`SettingsFields`), the launch decision
  (`Program.Run`, which takes the menu-bar launch as a parameter) and the whole
  meeting lifecycle (`BridgeRuntime`). An `if` inside an AppKit class is a
  decision that has escaped its test.

**The cross-platform invariant:** `TapScribe.Bridge.Core` references **no
NAudio and no Windows API**. CI's `dotnet-core-crossplatform` job builds and
tests the core on Linux and fails the moment it takes a Windows dependency.

## Prerequisites

- **.NET 10 SDK** (`global.json` pins the band; `dotnet --version` should
  report 10.0.x). Get it from <https://dotnet.microsoft.com/download>.
- Windows 10/11 to run the Windows tray app. The core and its tests build and
  run on any OS.
- macOS 14.4 or newer, on Apple silicon, plus `dotnet workload install macos`
  and the Xcode whose SDK matches that workload, to build the **Mac shell**
  (see below). The Mac platform layer needs neither.

## Build, test, run

**No single OS builds the solution whole.** `TapScribe.TrayBridge.slnx` is the
union view for an editor; a net10.0-windows project needs Windows and a
net10.0-macos one needs Xcode, so each OS names its own projects. That is what
CI does too: a `(windows)` job, a `(macos)` job, and the ubuntu job.

Only the two Mac SHELL projects are net10.0-macos. Everything else, including
the Mac platform layer and its tests, is plain net10.0 and builds anywhere.

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
dotnet test  tests/TapScribe.TrayBridge.MacOS.Tests/TapScribe.TrayBridge.MacOS.Tests.csproj -c Release
dotnet build src/TapScribe.TrayBridge.MacOS/TapScribe.TrayBridge.MacOS.csproj -c Release
open src/TapScribe.TrayBridge.MacOS/bin/Release/net10.0-macos/osx-arm64/TapScribe.TrayBridge.MacOS.app
```

`open` (rather than `dotnet run`) because the app has to launch as a **bundle**:
the `Info.plist` beside the binary is what makes it a menu-bar app and what
carries the microphone usage string TCC shows in its prompt. Run the inner
binary directly and macOS has no manifest to read, so the app takes a Dock icon
and the mic prompt has nothing to say. Launching it from a terminal IS the way
to see its stderr and to seed `TAPSCRIBE_*` (see Configuration).

**Both lines need Xcode matching the installed `macos` workload** (Xcode 26.4
or newer for workload 26.4; `dotnet workload list` prints the version), plus
`dotnet workload install macos`. The shell targets a deployment version older
than that SDK, so the build has to read from the matching SDK's headers which
symbols existed in macOS 14.4; an older Xcode fails with `MM0179`, and no
build setting works around it.

Anywhere (Linux, macOS, Windows), which is what the ubuntu CI job runs:

```bash
dotnet test tests/TapScribe.Bridge.Core.Tests/TapScribe.Bridge.Core.Tests.csproj -c Release
dotnet test tests/TapScribe.Bridge.MacOS.Tests/TapScribe.Bridge.MacOS.Tests.csproj -c Release
```

The Mac policy tests take the running macOS version as a parameter, so they
mean the same thing on any host. The handful that ask the running OS itself
carry `[RequiresMacOS]` and skip at discovery off a Mac, each naming the
capability it wanted, so the skip list itself says what went unexercised and
no prose here has to keep a second list of them. `[RequiresNonMacOS]` is the
mirror, for the tests that pin what the Mac layer answers when it is NOT on a
Mac.

`TapClientWebSocketTests` covers `/tap` negotiation + binary framing (tokened
and `--no-auth`) against an in-process Kestrel server, and
`RealRecorderMeetingE2ETests` runs a full meeting against a real Python
Recorder (self-skips where faster-whisper isn't importable) — so wire
regressions are caught in CI without a live Recorder on your desk.

## Packaging

Both shells are built by `.github/workflows/release.yml` on a `v*` tag and
attached to the GitHub Release under stable, unversioned filenames, which is
what makes `releases/latest/download/<asset>` a permanent URL (ADR-0012). The
commands below are what those jobs run, minus the `-p:Version=<tag>` they add.

### Windows: a self-contained single-file exe

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
installer, code signing, or auto-update: it's a copy-and-run exe. Ships as
`TapScribe.TrayBridge-win-x64.zip`.

### macOS: an installer package, and a zip beside it

```bash
# from this directory (bridges/tray-bridge/)
dotnet publish src/TapScribe.TrayBridge.MacOS -c Release -p:CreatePackage=false
APP=src/TapScribe.TrayBridge.MacOS/bin/Release/net10.0-macos/osx-arm64/TapScribe.TrayBridge.MacOS.app
tools/build-macos-pkg.sh "$APP" 0.0.0 TapScribe.TrayBridge-osx-arm64.pkg
ditto -c -k --keepParent "$APP" TapScribe.TrayBridge-osx-arm64.zip
```

The **`.pkg` is the artifact the dashboard offers**, and the reason is
Gatekeeper rather than taste: see *Installing the Mac build* below. The zip
stays published for anyone who wants the bundle without running an installer.

Four things about those lines, each of which silently ships a broken artifact
if it is wrong:

- The bundle lands in the RID output directory, **not** `publish/`. That one
  receives only the installer package, which `CreatePackage=false` suppresses
  because its filename carries the version a permanent URL cannot have.
- **`ditto`, never `zip -r`**: a `.app` is symlinks and executable bits, and a
  naive zip drops both, producing an archive that unpacks into something macOS
  will not launch.
- The release job adds `-p:Version=<tag>` and then asserts the bundle's
  `CFBundleShortVersionString` against the tag. The `Info.plist` declares no
  version of its own and the csproj derives both version keys from `$(Version)`,
  so a publish missing the flag stamps the SDK's own default without failing.
- **`tools/build-macos-pkg.sh` owns the packaging, and both workflows call it.**
  The release job builds the shipped package with it and CI's *Prove
  installer-written payload is not quarantined* step asserts against it, so the
  gate cannot drift into testing a package nobody downloads. Its header explains
  the one non-obvious flag: `BundleIsRelocatable` defaults to true, which makes
  `installer` write payload over any existing copy of the bundle id it finds and
  ignore `--install-location`. The script refuses its own output if that leaks
  back in. `--version` carries the tag for `pkgutil --pkg-info`; the filename
  stays unversioned because a permanent `releases/latest/download/` URL cannot
  carry one (ADR-0012).

### Installing the Mac build

1. Download `TapScribe.TrayBridge-osx-arm64.pkg` from Settings → **Get a
   bridge** (or the Releases page) and open it.
2. macOS blocks it once, because v1 is un-notarised (ADR-0020). Go to **System
   Settings → Privacy & Security**, find the blocked item in the **Security**
   section, and click **Open Anyway**. The button disappears about an hour after
   the blocked attempt, so if it is not there, try opening the package again
   first. On macOS 26 this step also asks for an admin password.
3. Let the installer put `TapScribe.TrayBridge.MacOS.app` in `/Applications`,
   then open it. The icon appears in the **menu bar**, with no Dock icon and no
   window, which is what `LSUIElement` buys. **No Terminal step, and no second
   Gatekeeper prompt.**

**Why a package and not the zip.** The bundle carries an ad-hoc signature (the
macOS SDK applies one, and arm64 will not execute a bundle with none at all),
and Gatekeeper reads a signature it cannot validate as tampering. So a
*quarantined* ad-hoc bundle is reported as **damaged**, offering only Move to
Trash: there is no in-UI way forward at all, and right-click → **Open** does
not rescue it, that being the bypass for the milder "unidentified developer"
dialog a signed-but-not-notarised app gets. An unsigned *package* gets that
milder treatment instead, and `installer` writes its payload outside the path
that applies quarantine, so the installed app is not quarantined and launches
straight away. CI asserts that last part on a real runner rather than trusting
it, since Apple documents none of it.

If you took the zip instead, the `xattr` step is still the only escape:

```bash
xattr -dr com.apple.quarantine /Applications/TapScribe.TrayBridge.MacOS.app
```

Notarisation is what removes the block in step 2, and it needs a paid Apple
Developer account. Until then the block is the policy working as designed, not
a broken download.

Apple silicon only, and macOS 14.4 or newer: Launch Services refuses the bundle
on an older Mac from its `LSMinimumSystemVersion`, and the shell's own floor
check catches the copies Launch Services never sees.

## Configuration

Right-click the tray icon → **Settings…** — no environment variables
required. Settings are saved to `%APPDATA%\TapScribe\windows-tray-bridge.json`
and remembered across restarts.
(The filename deliberately keeps the pre-rename `windows-tray-bridge` spelling —
it is the on-disk contract pinned by `TrayStores.SettingsFileName`, and renaming
it would orphan every operator's saved settings and protected token.)
The **tap token is never written in cleartext**: it is protected with Windows
DPAPI (CurrentUser scope). On first run the Connection fields are seeded from
the legacy `TAPSCRIBE_HOST` / `TAPSCRIBE_PORT` / `TAPSCRIBE_TLS` /
`TAPSCRIBE_TLS_ALLOW_SELF_SIGNED` / `TAPSCRIBE_IDENTITY` / `TAPSCRIBE_NAME` /
`TAPSCRIBE_TAP_TOKEN` environment variables when present; the dialog is the
source of truth thereafter.

On **macOS** the same fields live behind the menu-bar icon → **Settings…**,
saved to `~/Library/Application Support/TapScribe/macos-tray-bridge.json` (its
own on-disk contract, `TrayStores.SettingsFileName` again) with the tap token in
the **login Keychain** rather than in the file at all. Two differences worth
knowing before you reach for one:

- **`TAPSCRIBE_*` seeding does not reach a Finder-launched `.app`.** Those
  variables seed the first run, and a bundle opened from Finder, the Dock or
  `open -a` inherits `launchd`'s environment, not your shell's, so an export in
  `~/.zshrc` is simply not there. Launching the binary inside the bundle from a
  terminal
  (`TAPSCRIBE_HOST=… TapScribe.TrayBridge.MacOS.app/Contents/MacOS/TapScribe.TrayBridge.MacOS`)
  does seed it, and so does typing the values into Settings once, which is the
  supported route.
- **The Settings window is the smaller half of the Windows dialog for now**:
  connection, the microphone row, the system-audio row, the shared gate timings
  and the end-of-meeting behaviour. The Advanced pin grid and the live level
  meters are device parity (#421). A saved pin still survives a Save even though
  there is no grid showing it.
- **System audio needs a permission the Mac asks for at the first Start**, not
  at install and not when Settings is opened: macOS prompts for **System Audio
  Recording** the first time a meeting's process tap actually runs. Dismiss it
  and the meeting records your microphone only, and says so. The grant is per
  signature, so an ad-hoc build re-prompts on every update (ADR-0020).

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
2. `dotnet run --project src/TapScribe.TrayBridge`. On macOS launch the built
   bundle instead (`open …/TapScribe.TrayBridge.MacOS.app`, or its inner binary
   from a terminal when you want the stderr): a process without bundle identity
   is one macOS refuses to prompt for, so `dotnet run` never reaches the capture
   paths at all.
3. Right-click → **Start meeting**. Play meeting audio while you speak,
   pausing between sentences, then **End meeting**.
4. **Two** sets of WAVs appear under the Recorder's new detached session —
   your identity (the mic) and "System audio" (the loopback) — each split
   into Utterances by the level gate, and the pipeline runs through to a
   summary window with **Copy**.
5. Tokened path: paste the Recorder's token into **Settings… → Connection**,
   Save, then Start meeting against a Recorder started **without**
   `--no-auth`.
6. **Blip resilience.** Mid-sentence, with an Utterance open, stop the Recorder
   and start it again. The reconnect ladder keeps the same `utterance_id`,
   buffers the gap up to its bounded budget and flushes the tail into the next
   `/tap` that lands, so the sentence arrives as ONE WAV with a hole in it
   rather than as two Utterances. `../README.md` holds the recipe and the
   budgets, which are the numbers to check against.

On **macOS** the first **Start meeting** of a fresh install is also where the
TCC prompts land: the microphone, and then **System Audio Recording** once the
process tap's IOProc starts. Answer both, or the meeting records the mic alone
and says so in the status line. A grant is per signature, so an ad-hoc build
asks again after every rebuild, and a downloaded release asks again after every
update.

## Wire contract (summary)

One `/tap` WebSocket per Utterance; raw PCM, 16 kHz mono int16, 20 ms
(640-byte) binary frames; tap token via the `tapscribe.v1.tap.<token>`
subprotocol. The gate mints a fresh `utterance_id` per speech segment and the
stream keeps it stable across reconnects; **Drain** flushes the bounded
trailing buffer when an Utterance ends while reconnecting. The Bridge sends
only PCM — no JSON, no control messages (ADR-0002). The full contract lives
in `../README.md`.
