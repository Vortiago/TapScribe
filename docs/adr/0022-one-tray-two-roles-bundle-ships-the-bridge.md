---
status: proposed (amends ADR-0015, extends ADR-0012)
date: 2026-08-28
---

# One tray per OS carries both roles; a Bundle ships the Tray Bridge

There is ONE tray executable per OS — the [Tray Bridge](../../CONTEXT.md) —
carrying two roles: the Bridge role always, and the **host role** (boot,
supervise and reap a co-located Recorder; be the way in to it) when a host
payload sits beside it on disk. The role test is the **presence of the payload
folder**, so the role is a fact about the install rather than a flag, a build
variant or a setting anyone can misconfigure.

The test is deliberately NOT "does `BundleLayout` resolve" — `Resolve` is pure
path construction that probes nothing, and `ResolveWheel` is designed to THROW
`BundleLayoutException` on zero-or-several wheels, because that is a packaging
bug an operator must see. Folded into a boolean role probe, a Bundle whose
`wheel/` was wiped would silently degrade to a bridge-only tray and the
operator's Recorder would just vanish from the menu. Payload folder present ⇒
host role; `ResolveWheel`'s errors then stay loud INSIDE that role.

Two INSTALLABLE artifacts per OS, and only two:

| | carries | shape |
|---|---|---|
| bridge-only | the tray | zip (win) / `.pkg` (mac) |
| [Bundle](../../CONTEXT.md#bundle) | interpreter + wheel + the tray | Inno Setup `.exe` (win) / `.pkg` (mac) |

The macOS `.app` **zip stays published beside the bridge-only `.pkg`**, per
ADR-0012 and ADR-0020 — it is the artifact for anyone who wants the bundle
without an installer, and nothing here changes that. On acceptance, ADR-0012's
asset table gains the two Bundle rows, and ADR-0015's "two tray icons stay
separate; the additive fix is a 'local Recorder' section in the tray Bridge,
not a merge" consequence is reversed by this ADR and must be annotated there.
The **Launcher is retired** — as a word and as an executable. It named "the
thing that starts the Recorder and isn't the Recorder", and once the Tray
Bridge is that thing, nothing is confusable enough to need naming;
`RecorderSupervisor` names the mechanism in code. `TapScribe.Bundle.Launcher`
goes: `LauncherContext` / `LauncherIcons` / `Program` die (the tray has all
three), and `RecorderSupervisor` / `JobObject` / `RotatingLogWriter` move into
`TapScribe.Bundle.Core`, the one host-role assembly the shells reference.

Supervision therefore splits the way capture already does (`IAudioCapture`):
the lifecycle in `Bundle.Core`, the platform behind a seam. `JobObject` is
Win32 P/Invoke, so it lives BEHIND that seam, not in the Core the Linux CI leg
tests.

The two platforms are not equivalent and the gap is named here rather than
discovered: `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` reaps on tray DEATH, crash
included — which is the whole reason `JobObject` exists. A POSIX process group
only reaps when the tray exits cleanly enough to signal it, so a crashed macOS
tray orphans the Recorder AND its WhisperLiveKit grandchild, holding port 8001
with no tray to stop them. The macOS seam therefore pairs the process group
with a parent-death watch in the child's lifetime (kqueue `EVFILT_PROC` on the
tray's pid), so a dead parent is a signal rather than a leak.

**Quit stops only a Recorder the tray started.** Ownership is recorded at
spawn, not configured. A Recorder that was already running when the tray
launched (a `start.sh` in a terminal, another user's install holding port 8001)
outlives the tray's Quit and shows as running-but-unmanaged. Start / Stop
Recorder are separate menu items, so stopping the server does not mean quitting
the tray.

Unmanaged is decided by the spawn attempt, not by probing: the tray starts its
child, and an `EADDRINUSE` exit plus a `/health` that answers means someone
else's Recorder holds the port. An `EADDRINUSE` plus a `/health` that does not
answer is a crash-loop, which the tray reports as failed rather than adopting.
"Open dashboard" and the login link always target the LOCAL Recorder, never
whatever host the bridge settings point at — a tray may legitimately supervise
one Recorder and tap into another.

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
