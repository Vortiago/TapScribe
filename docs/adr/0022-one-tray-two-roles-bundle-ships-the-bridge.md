---
status: proposed (amends ADR-0015, extends ADR-0012)
date: 2026-08-28
---

# One tray per OS carries both roles; a Bundle ships the Tray Bridge

There is ONE tray executable per OS — the [Tray Bridge](../../CONTEXT.md) —
carrying two roles: the Bridge role always, and the **host role** (boot,
supervise and reap a co-located Recorder; be the way in to it) when a host
payload sits beside it on disk. `BundleLayout` resolving an interpreter and
exactly one wheel IS the role test, so the role is a fact about the install
rather than a flag, a build variant or a setting anyone can misconfigure.

Two artifacts per OS, and only two:

| | carries | shape |
|---|---|---|
| bridge-only | the tray | zip (win) / `.pkg` (mac) |
| [Bundle](../../CONTEXT.md#bundle) | interpreter + wheel + the tray | Inno Setup `.exe` (win) / `.pkg` (mac) |

The **Launcher is retired** — as a word and as an executable. It named "the
thing that starts the Recorder and isn't the Recorder", and once the Tray
Bridge is that thing, nothing is confusable enough to need naming;
`RecorderSupervisor` names the mechanism in code. `TapScribe.Bundle.Launcher`
goes: `LauncherContext` / `LauncherIcons` / `Program` die (the tray has all
three), and `RecorderSupervisor` / `JobObject` / `RotatingLogWriter` move into
`TapScribe.Bundle.Core`, the one host-role assembly the shells reference.

`JobObject` is Win32-only, so supervision needs the same cross-platform-core +
per-OS-seam split the capture side already has (`IAudioCapture`): a job object
on Windows, a process group on macOS.

**Quit stops only a Recorder the tray started.** Ownership is recorded at
spawn, not configured. A Recorder that was already running when the tray
launched (a `start.sh` in a terminal, another user's install holding port 8001)
outlives the tray's Quit and shows as running-but-unmanaged. Start / Stop
Recorder are separate menu items, so stopping the server does not mean quitting
the tray.

A Bundle still is not a Bridge: shipping one inside it is composition, not
identity, and CONTEXT.md's identity claim is unchanged.

## Considered options

**A third host-only package.** Rejected: with the roles derived, host-only and
host+tray have an identical payload and differ only in whether a menu item is
shown — a runtime state, not a build. "Host only" is installing the Bundle and
never connecting a bridge; headless Linux is already served by the systemd unit,
the Docker image and the wheel. The one argument that could bring it back is
macOS-specific: `TapScribe.TrayBridge.MacOS/Info.plist` declares
`NSMicrophoneUsageDescription` and `NSAudioCaptureUsageDescription`, so a
host-only operator gets an app that asks for audio it will never capture. That
is a `.plist` variant and a second publish job — cheap to add later, impossible
to un-ship once released.

**The Tray Bridge growing its own interpreter, shipped as a third artifact
beside the Bundle.** Rejected: two things that both install a host, diverging
from the first release.

**Installing both trays.** Rejected: two icons in one notification area, one
tapping and one supervising, is the worst outcome for the operator this whole
change is for.
