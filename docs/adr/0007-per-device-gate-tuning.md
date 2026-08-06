---
status: accepted
date: 2026-06-23
---

# Bridge level gate: per-device tuning, keyed by identity

The Windows tray Bridge tunes its **Level gate** (the bridge-side Mute — see
CONTEXT.md) **per device**, not globally: each capture device a meeting taps
carries its own sensitivity / hangover / pre-roll, so the system-loopback gate
can open readily for a quiet far end while the mic stays conservative against
room noise. The tuning rides on the **device selection** and is routed to the
running pipeline **by identity** — the same per-identity channel
`CaptureOrchestrator` buckets sessions by (ADR-0005):

- **At Start**, `DeviceSelection.Resolve` carries each selection's gate to its
  `ResolvedDevice`; `TrayContext` builds each `PipelineSpec` with that
  device's `GateOptions`, so every `LevelGate` is constructed from its own
  device's tuning.
- **Live** (Settings → Save mid-meeting), `TrayContext` pushes
  `BridgeSettings.ToGateOptionsByIdentity()` to
  `CaptureOrchestrator.UpdateGates(map)`. An identity with no running pipeline
  is skipped; a pipeline whose identity isn't in the map keeps its tuning — so
  re-tuning one device never disturbs another's gate or its open utterance.
  `LevelGate.UpdateTuning` applies the new tuning as one atomic snapshot swap,
  safe against the capture thread.

## Model

`GateSettings(int Sensitivity, int HangoverMs, int PreRollMs)` lives in
`Bridge.Core` next to `GateTuning` (the slider↔threshold map) and
`GateOptions` (the engine units); `ToGateOptions()` converts operator units to
engine units. `GateSettings.DefaultForFlow(flow)` encodes the per-flow split:
the **capture (mic)** default is pinned to the historical global default
(≈0.02 RMS) so upgrading never re-tunes an existing mic; the **render (system
loopback)** default is more sensitive.

`DeviceSelection.Gate` is **nullable** so a pre-per-device settings file (no
`gate` key) deserialises cleanly; the concrete gate is filled in at the
boundary that knows the right default: `BridgeSettings.EffectiveDevices`
normalises every selection (explicit per-device value wins, else the migrated
legacy global value, else `DefaultForFlow`), with `DeviceSelection.Resolve` as
a final fallback keyed by the *resolved* device's actual flow. Everything
downstream sees a concrete gate per device, no nulls, and a resolved pipeline
always has a tuning.

## Migration

The legacy **global** knobs (`GateSensitivity` / `GateHangoverMs` /
`GatePreRollMs` on `BridgeSettings`) are **nullable, migration-only** fields,
omitted from new files. On load, `LegacyGlobalGate()` reconstructs a
`GateSettings` iff any was present, and normalisation fills every gateless
device with it — **no reset on upgrade** (pins included). A brand-new file has
no legacy value, so its default pair gets the per-flow defaults. Once the
per-device UI saves, the legacy fields are omitted and the per-device gates
are authoritative: the migration is one-way and sticky.

## UI placement

Per-device **sensitivity** is a slider on each Devices-tab row (the
differentiator operators reach for); **hangover** and **pre-roll** stay shared
on the Level-gate tab. The model still carries all three per device, so a
future slice can expose them per device without a data change. Pinned devices
(the Advanced grid) have no per-row slider; they keep their saved sensitivity
or take the per-flow default, stamped with the shared hangover / pre-roll.

Rejected: **global default + per-identity overrides** (two sources of truth
for one fact, and a UI that must explain "this device differs from the
default"); **a per-flow pair keyed by flow** (diverges from the identity
channel the orchestrator already keys by, and gives a pinned or third device —
two mics under different identities — no tuning of its own).

## Trade-offs accepted

- **Renaming a device's identity mid-meeting and saving** routes the live
  re-tune under the new identity, which the running pipeline (still on the old
  identity) won't match — that device is skipped until the next Start.
  Consistent with the existing rule that identity/device changes apply only at
  Start; only the gate knobs are live.
- **Hangover / pre-roll are shared in the UI** though per-device in the model
  — the model is more capable than the UI surfaces, the safe direction.
- **A pinned device's sensitivity isn't editable in this slice**; pinning is
  the advanced path, and the mic-vs-system split is the headline case and is
  fully editable.

## Consequences

`GateSettings` is in `Bridge.Core`; `DeviceSelection` / `ResolvedDevice` /
`PipelineSpec` each carry a gate; `GateOptions` stays the engine unit.
`CaptureOrchestrator.UpdateGates` takes
`IReadOnlyDictionary<string, GateOptions>`; `StartAll` builds each pipeline
from its spec's gate (falling back to the shared `gate` argument used by
tests). `BridgeSettings` exposes `ToGateOptionsByIdentity()` in place of a
global `ToGateOptions()` (see the Level-gate entry in CONTEXT.md).
