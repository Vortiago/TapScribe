---
status: accepted
date: 2026-08-06
---

# The macOS tray Bridge is a net10.0-macos shell over the shared core, tapping system audio via Core Audio process taps

The macOS member of the **tray Bridge** family (see CONTEXT.md) reuses
`TapScribe.Bridge.Core` unchanged and adds only the two thin layers the
Windows shell already proved thin: a platform layer (audio capture,
device enumeration, storage) and a tray UI shell. Both are C# on the
first-party `net10.0-macos` AppKit workload, in one process — not
Avalonia, not a Swift app, not a daemon split. System audio (the
multi-person "them" tap) comes from Core Audio **process taps**, which
sets the platform floor at macOS 14.4, arm64 only.

## Context

The Windows tray Bridge was deliberately built as a cross-platform core
plus a thin OS shell: Core (net10.0, ~3.3k LOC — CaptureOrchestrator,
MeetingController, TapClient/ControlClient, the Level gate, the wire
constants in `TapWire.cs`) builds and runs its full test suite on Linux
CI, including a real-Recorder meeting E2E. The Windows-only surface is
~1k LOC of WASAPI + storage behind `IAudioCapture` /
`IAudioDeviceEnumerator`, and ~1.6k LOC of WinForms. A macOS Bridge
therefore rewrites nothing in Core; the open questions were only the UI
technology, the system-audio capture API (macOS had no WASAPI-loopback
equivalent for years), and packaging.

## Decision

- **Shell: `net10.0-macos` (the dotnet/macios AppKit workload), C#, one
  process.** The UI is real AppKit (NSStatusItem, NSMenu, AppKit
  windows) written in C#, and the workload's ObjC bindings also carry
  the capture-API interop, so no hand-rolled native shim. Verified
  actively maintained (bindings track current Xcode; .NET 10 is LTS to
  Nov 2028). Known cost: IDE support for the TFM is poor — CLI-only
  development, which is how this repo is built anyway.
- **System audio: Core Audio process taps** (`CATapDescription` +
  `AudioHardwareCreateProcessTap` → aggregate device → HAL IOProc),
  **floor macOS 14.4** — the release that opened the API to any app
  under the "System Audio Recording" TCC prompt
  (`NSAudioCaptureUsageDescription`). The tap is one more
  `IAudioCapture`; Core's `Resampler` already converts device format to
  the wire format, so the platform layer delivers raw buffers plus an
  `AudioFormat`.
- **Mic mute parity (#159)**: `IsMuted`/`MuteChanged` over
  `kAudioDevicePropertyMute` where the device supports it; elsewhere
  the Level gate stays the only Mute source — exactly the seam the
  glossary reserved for a future macOS backend.
- **Layout**: `bridges/windows-tray-bridge/` is renamed
  `bridges/tray-bridge/` and houses Core + both platform layers + both
  shells in one solution (`TapScribe.Bridge.Mac`,
  `TapScribe.MacTrayBridge` join as projects). The dir name stops
  claiming Windows ownership of shared code; the stamper's `_TRAY`
  site path and CI paths move with it.
- **Packaging**: a zipped `.app`, `osx-arm64` only, shipped **unsigned
  in v1** (Developer ID signing + notarization is a follow-up once the
  Apple Developer membership exists). Tap token in the Keychain (the
  DPAPI analogue); other settings/state as JSON under
  `~/Library/Application Support/TapScribe/`. Scope is full functional
  parity with the Windows shell.

## Considered options

- **Avalonia** — one shell for macOS + a future Linux tray, plain
  net10.0. Rejected on the repo's minimal-external-dependency ethos: a
  large third-party UI framework to avoid writing a thin shell twice.
- **Swift/AppKit shell + headless C# daemon** — most native authoring
  experience, but two processes, an IPC seam Core wasn't designed for,
  and daemon lifecycle supervision. Once `net10.0-macos` proved healthy
  its advantage evaporated: the C# shell *is* AppKit at runtime.
- **MAUI** — targets Mac Catalyst, the wrong shape for a menu-bar app.
- **ScreenCaptureKit audio** (macOS 13+) — two extra majors of
  compatibility, paid for with the Screen Recording permission and
  Sequoia's periodic re-approval nag. No target Mac needs pre-14.4.

## Consequences

- Unsigned v1 means Gatekeeper's "Open Anyway" dance on first launch
  and — because ad-hoc signatures change per build — TCC re-prompts
  (mic, system audio) on every update. Documented in the Get-a-bridge
  card; retired by the signing follow-up.
- CI gains a macOS job: build the Mac projects, run non-TCC unit
  tests, plus an upstream-contract smoke test over the P/Invoked
  CoreAudio symbols (sibling of the NAudio reflection test). Real tap
  capture cannot run on headless CI (TCC has no grant path there); it
  gets a manual smoke checklist, while Core behaviour keeps riding the
  Linux real-Recorder meeting E2E.
- The meeting-card window mirrors whatever shape the planned reusable
  per-meeting window has landed in at build time; if it hasn't, the Mac
  shell mirrors today's `MeetingForm` and both shells migrate together.
