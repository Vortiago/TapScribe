---
status: accepted
date: 2026-06-17
---

# Bridge device selection: follow-default sentinels, not just pinned ids

The Windows tray Bridge persists which audio devices to tap as a list of
**selections** that are *either* a **Follow-default** sentinel (per device
flow — microphone or system-audio loopback) *or* a **Pinned** concrete
endpoint id. Each selection also carries the operator-editable `identity`
and `name` it streams under. At **Start meeting** the selection list is
resolved against the devices actually present right now (`DeviceSelection.Resolve`
in `Bridge.Core`); a follow-default entry binds to whatever device is the
current default for its flow, a pinned-but-absent entry is reported as
*missing* (non-fatal), and a resolution that yields zero devices aborts the
meeting with a clear message before any audio device is opened.

The default saved selection is *Follow-default microphone + Follow-default
loopback*, which reproduces the pre-#106 hardcoded behaviour, so first run
and upgrades are unchanged.

## Considered alternatives

**Pin concrete endpoint ids only.** Persist each chosen `CaptureDevice.Id`
(the WASAPI `MMDevice` endpoint id) and resolve those ids at Start.

Rejected because the common case — "tap my mic and my system audio" — is
exactly the case a pinned id gets wrong. The concrete default render/capture
endpoint changes when the operator switches output (Bluetooth headset →
laptop speakers, dock plugged/unplugged). A frozen id then silently keeps
capturing the *old* endpoint, or nothing, while the operator believes
"system audio" is still being tapped. The failure is silent and discovered
only when the recording comes back empty.

**Store a human label / friendly name and match on it.** Friendly names
collide ("Microphone (USB Audio Device)" is not unique) and are localised,
so matching on them is both ambiguous and brittle across machines.

## Why follow-default sentinels win

- **Matches how operators think.** People choose "my microphone" and "my
  system audio," not a GUID. Follow-default captures that intent directly.
- **Survives default churn.** Switching the Windows default output mid-day
  doesn't strand the loopback tap — the next meeting binds to the new
  default automatically.
- **Backward-compatible by construction.** The default selection is the old
  hardcoded pair, so no migration step and no behaviour change for existing
  installs.
- **Power users keep precision.** A pinned id is still available for "tap
  this specific USB interface regardless of the default," so the sentinel
  model adds capability without removing any.
- **Resolve-at-Start is a pure, testable seam.** `(selections, available
  devices) → (resolved, missing, verdict)` lives in `Bridge.Core` with no
  WASAPI or WinForms dependency, so the device logic is unit-tested on the
  cross-platform CI runner while the WinForms picker stays a thin editor of
  the persisted list.

## Trade-offs accepted

- **Resolution is late-bound, so a saved selection can resolve differently
  on different days.** That is the point (it tracks the default), but it
  means "what will Start capture?" is only fully answerable at Start time,
  not at save time — the Devices tab shows the selection, not a frozen
  device.
- **A pinned device that is unplugged is dropped with a warning, not an
  error.** A meeting still starts on the remaining devices. The explicit
  zero-resolved verdict is the only hard stop, so an operator who pinned a
  single missing device gets a clear failure rather than a silent empty
  session.

## Consequences

- The selection model type (`DeviceSelection`) lives in `Bridge.Core`
  (consumed by the `Bridge.Core` resolver); `BridgeSettings` in
  `Bridge.Windows` persists a `List<DeviceSelection>` alongside the existing
  DPAPI-protected token.
- `TrayContext` replaces its hardcoded `PickDefault(Capture)` /
  `PickDefault(Render)` with `DeviceSelection.Resolve`, surfacing `missing`
  as a non-fatal balloon and the zero-resolved/duplicate-identity verdicts as
  a pre-start error (see the Level-gate and detached-session entries in
  `CONTEXT.md`).
