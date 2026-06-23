---
status: accepted
date: 2026-06-23
---

# Bridge level gate: per-device tuning, keyed by identity

The Windows tray Bridge tunes its **Level gate** (the bridge-side Mute —
see CONTEXT.md) **per device** rather than with one global setting. Each
capture device a meeting taps carries its own sensitivity / hangover /
pre-roll, so the system-loopback gate can be made more sensitive than the
mic gate: the far end of a meeting is quiet and should open the gate
readily, while the mic should stay conservative so room noise doesn't
over-trigger it.

The tuning rides on the **device selection** and is routed to the running
pipeline **by identity** — the same per-identity channel that
`CaptureOrchestrator` already buckets sessions by (ADR-0005,
`CaptureOrchestrator.StartAll`). This unifies three slices:

- **At Start (#151):** `DeviceSelection.Resolve` carries each selection's
  gate to its `ResolvedDevice`; `TrayContext` builds each `PipelineSpec`
  with that device's `GateOptions`, so every `LevelGate` is constructed
  from its own device's tuning.
- **Live (#153):** on Settings → Save during a meeting, `TrayContext`
  pushes `BridgeSettings.ToGateOptionsByIdentity()` to
  `CaptureOrchestrator.UpdateGates(map)`, which routes each device's new
  tuning to the pipeline running under that identity. A device whose
  identity has no running pipeline (unplugged / not in this meeting) is
  skipped; a pipeline whose identity isn't in the map keeps its current
  tuning — so re-tuning one device never disturbs another's gate or its
  open utterance.
- **Re-tune-in-place (#149):** unchanged — `LevelGate.UpdateTuning` still
  publishes the new tuning as one atomic snapshot swap, safe against the
  capture thread.

## Model

`GateSettings(int Sensitivity, int HangoverMs, int PreRollMs)` lives in
`Bridge.Core` next to `GateTuning` (the slider↔threshold map) and
`GateOptions` (the engine units). It is the **operator-unit** per-device
tuning; `ToGateOptions()` converts it to the engine-unit `GateOptions` the
`LevelGate` consumes. `GateSettings.DefaultForFlow(flow)` encodes the
per-flow split: the **capture (mic)** default is pinned to the historical
global default (≈0.02 RMS), so upgrading does not re-tune an existing mic;
the **render (system loopback)** default is more sensitive.

`DeviceSelection` gains a **nullable** `GateSettings? Gate`. Nullable so a
pre-per-device settings file (no `gate` key) deserialises with no tuning
attached; the concrete gate is then filled in at the boundary that knows
the right default:

- `BridgeSettings.EffectiveDevices` normalises every selection's gate:
  an explicit per-device value wins; else the **migrated legacy global
  value** (below); else `DefaultForFlow`.
- `DeviceSelection.Resolve` is a final flow-keyed fallback for any direct
  caller, defaulting by the *resolved* device's actual flow.

So everything downstream sees a concrete gate per device with no nulls to
special-case, and a resolved pipeline always has a tuning.

## Migration

The legacy **global** knobs (`GateSensitivity` / `GateHangoverMs` /
`GatePreRollMs` on `BridgeSettings`) become **nullable, migration-only**
fields, omitted from new files. On load, `LegacyGlobalGate()`
reconstructs a `GateSettings` from them iff any was present; normalisation
then fills every gateless device with that value — so an upgrading
operator's single tuning lands as each device's default with **no reset on
upgrade**. A brand-new file has no legacy value, so its default pair gets
the per-flow defaults instead. Once the per-device UI saves, the legacy
fields are null (omitted), and the per-device gates on each
`DeviceSelection` are authoritative — the migration is one-way and sticky.

## UI placement

Per-device **sensitivity** is a slider on each device row of the
**Devices** tab (the differentiator operators reach for). **Hangover** and
**pre-roll** stay on the **Level-gate** tab and apply to every device:
they rarely need to differ per device, and one shared pair keeps the
Devices tab uncluttered. The model (`GateSettings`) still carries all
three per device, so a future slice can expose per-device hangover /
pre-roll without a data change. Pinned devices (the Advanced grid) have no
per-row slider in this slice; they keep their saved sensitivity or take
the per-flow default, stamped with the shared hangover / pre-roll.

## Considered alternatives

**Global default + per-identity overrides.** Keep one global tuning and a
sparse override map for devices that differ. Rejected: two sources of
truth for the same fact (a device's tuning), and the UI must explain "this
device differs from the default" — more state and more explaining than
"every device has its own tuning." Migration is the only thing it makes
trivial, and migrating a single value into per-device defaults is already
cheap.

**Per-flow pair (one mic gate, one system gate), keyed by flow.** Two
fixed tunings, routed to a pipeline by its device flow. Rejected: it
diverges from the by-identity channel #149 already built (the orchestrator
buckets by identity, not flow), and it gives a pinned or third device no
tuning of its own — a meeting that taps two mics under different identities
couldn't tune them apart.

## Why per-device-on-the-selection wins

- **One fact, one place.** A device's tuning lives on the device's
  selection — co-located with its identity/name, round-tripping through
  the same polymorphic JSON, carried through resolution to its pipeline.
- **Reuses the identity channel.** Routing by identity matches how
  `CaptureOrchestrator` already keys sessions, so the live re-tune is a
  dictionary lookup per device with skip-on-absent semantics for free.
- **Behaviour-preserving migration.** The mic default equals the old
  global default and the old global value migrates into each device, so no
  existing install is re-tuned by the upgrade.
- **Pure, testable seams.** `GateSettings.ToGateOptions` /
  `DefaultForFlow`, `Resolve` carrying the gate, `UpdateGates(map)`
  routing, and `BridgeSettings` migration / `ToGateOptionsByIdentity` are
  all unit-tested in `Bridge.Core` / `Bridge.Windows` on the
  cross-platform CI runner, with the WinForms tab a thin editor over them.

## Trade-offs accepted

- **Renaming a device's identity mid-meeting and saving** routes the live
  re-tune under the new identity, which the running pipeline (still on the
  old identity) won't match — so that device is skipped until the next
  Start. Identity/device changes already apply only at Start
  (`TrayContext.OpenSettings`); only the gate knobs are live. Acceptable
  and consistent with the existing rule.
- **Hangover / pre-roll are shared in the UI** though per-device in the
  model. A future slice can expose them per device; until then the model
  is more capable than the UI surfaces, which is the safe direction.
- **A pinned device can't have its sensitivity edited in this slice.** It
  keeps its saved value or a flow default. Pinning is the advanced path;
  the mic-vs-system split is the headline case and is fully editable.

## Consequences

- `GateSettings` is added to `Bridge.Core`; `DeviceSelection` /
  `ResolvedDevice` / `PipelineSpec` each gain a gate; `GateOptions` is
  unchanged (still the engine unit).
- `CaptureOrchestrator.UpdateGates(GateOptions)` becomes
  `UpdateGates(IReadOnlyDictionary<string, GateOptions>)`; `StartAll`
  builds each pipeline from its spec's gate (falling back to the shared
  `gate` argument used by tests).
- `BridgeSettings` drops its global `ToGateOptions()` in favour of
  `ToGateOptionsByIdentity()`; the global gate fields become nullable
  migration-only inputs (see the Level-gate entry in CONTEXT.md).
