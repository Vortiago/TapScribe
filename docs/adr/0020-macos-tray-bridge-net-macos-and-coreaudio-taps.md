---
status: accepted
date: 2026-08-06
---

# The macOS tray Bridge is a net10.0-macos shell over the shared core, tapping system audio via Core Audio process taps

The macOS member of the **tray Bridge** family (CONTEXT.md → Tray Bridge)
reuses `TapScribe.Bridge.Core` unchanged; the Mac work is a platform
layer (capture, device enumeration, storage) plus a tray shell — both C#
on the first-party `net10.0-macos` AppKit workload, one process
(CLI-only tooling; no IDE supports the TFM).

- **System audio**: Core Audio **process taps** (`CATapDescription` →
  aggregate device → HAL IOProc); floor **macOS 14.4** — the release
  that opened the API to any app under the "System Audio Recording" TCC
  prompt — arm64 only. The tap is one more `IAudioCapture`; Core's
  `Resampler` produces the wire format.
- **Mic mute parity (#159)**: `IsMuted`/`MuteChanged` over
  `kAudioDevicePropertyMute` where the device supports it; elsewhere the
  Level gate stays the only Mute source.
- **Layout**: `bridges/tray-bridge/` houses Core + all platform layers +
  all shells in one solution (`TapScribe.TrayBridge.slnx`).
- **Packaging**: zipped `.app`, `osx-arm64`, unsigned in v1 (Developer
  ID + notarization once the Apple membership exists). Ad-hoc signatures
  change per build, so every update re-prompts TCC — documented on the
  Get-a-bridge card. Tap token in the Keychain; other state as JSON
  under `~/Library/Application Support/TapScribe/`. Scope: full
  functional parity with the Windows shell.

Rejected: **Avalonia** (a large external UI dependency against the
repo's minimal-dependency ethos), **Swift shell + C# daemon** (two
processes and an IPC seam for no gain — the C# shell *is* AppKit at
runtime), **MAUI** (Mac Catalyst; wrong shape for a menu-bar app),
**ScreenCaptureKit audio** (Screen Recording permission + Sequoia's
re-approval nag, buying pre-14.4 reach no target Mac needs).

CI: a macOS job builds the Mac projects and runs the non-TCC unit tests
plus a CoreAudio upstream-contract smoke test; real tap capture has a
manual smoke checklist (TCC has no headless grant path).
