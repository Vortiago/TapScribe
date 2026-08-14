---
status: accepted
date: 2026-06-17
---

# Bridge device selection: follow-default sentinels, not just pinned ids

The Windows tray Bridge persists which audio devices to tap as a list of
**selections**, each *either* a **Follow-default** sentinel (per device flow —
microphone or system-audio loopback) *or* a **Pinned** concrete endpoint id,
plus the operator-editable `identity` and `name` it streams under. Resolution
is **late-bound at Start meeting** (`DeviceSelection.Resolve` in
`Bridge.Core`): a follow-default entry binds to whatever device is the current
default for its flow, a pinned-but-absent entry is reported as *missing*
(non-fatal), and zero resolved devices aborts the meeting with a clear message
before any audio device is opened. The default saved selection is
*Follow-default microphone + Follow-default loopback*, so first run and
upgrades keep the old hardcoded behaviour with no migration.

## Why follow-default wins

- **Matches operator intent** — people choose "my mic" and "my system audio",
  not a GUID.
- **Survives default churn.** The concrete default endpoint changes when the
  operator switches output (Bluetooth headset → speakers, dock). A frozen id
  silently keeps capturing the old endpoint, or nothing, discovered only when
  the recording comes back empty; follow-default binds to the new default at
  the next Start.
- **Power users keep precision** — a pinned id remains available for "this
  specific USB interface regardless of the default".
- **Resolve-at-Start is a pure seam**: `(selections, available devices) →
  (resolved, missing, verdict)` has no WASAPI or WinForms dependency, so the
  device logic is unit-tested on the cross-platform CI runner while the
  WinForms picker stays a thin editor of the persisted list.

Rejected: **pinned endpoint ids only** (the common case — "tap my mic and my
system audio" — is exactly the one a frozen id gets wrong, and the failure is
silent); **matching on friendly names** (they collide and are localised —
ambiguous and brittle).

## Trade-offs accepted

- **Late binding means "what will Start capture?" is only fully answerable at
  Start** — the Devices tab shows the selection, not a frozen device. That is
  the point: it tracks the default.
- **An unplugged pinned device is dropped with a warning, not an error** — the
  meeting starts on the remaining devices. The zero-resolved verdict is the
  only hard stop, so a single missing pinned device still fails clearly rather
  than yielding a silent empty session.

## Consequences

- `DeviceSelection` (model + resolver) and `BridgeSettings` both live in
  `Bridge.Core`; the settings persist a `List<DeviceSelection>` alongside a
  token the platform's `ITapTokenStore` protects (DPAPI on Windows).
- `BridgeRuntime` resolves via `DeviceSelection.Resolve`, surfacing `missing` as
  a non-fatal balloon and the zero-resolved / duplicate-identity verdicts as a
  pre-start error (see the Level-gate and detached-session entries in
  CONTEXT.md).
