---
status: accepted
date: 2026-07-19
---

# LiveChannelBase + the `live_control` reconcile seam

This completes the `LiveChannel` seam ADR-0003 opened. ADR-0003
introduced the `LiveChannel` Protocol so a second engine could slot in
without touching the Recorder; `MoonshineLiveChannel` (PRD #120) proved
the seam real. But it was only half-built, and this ADR records the
decisions that finished it — each is reasonable to re-suggest in a future
architecture review, so the rejection (or the shape chosen) is written
down here.

## Context

Two things were duplicated across the (now two) concrete channels and the
two call sites that drive them:

1. **The transition surface.** `apply_gate_knobs` was byte-for-byte
   identical between `WhisperLiveKitChannel` and `MoonshineLiveChannel`;
   `matches` / `begin_transition` / `_mirror_gate_info` were near-
   identical; and `moonshine_live.py` imported name-mangled privates
   (`_changed_gate_knobs`, `_transition_replacements`) across the module
   boundary. The `LiveChannel` Protocol's `matches` also under-specified
   the real signature (4 params vs the 8 both impls take), which
   `@runtime_checkable` masked.

2. **The swap-and-restart orchestration.** `/api/live/start` ran a
   ~130-line validate → family-swap → `matches` short-circuit →
   `begin_transition` → stop → restart sequence, and the boot auto-start
   re-derived a subset of it. Neither had a FastAPI-free test surface.

## Decision

### 1. A shared `LiveChannelBase` implements the Protocol

Both concrete channels inherit `LiveChannelBase` (`live.py`), which owns
the transition surface (`matches` / `apply_gate_knobs` /
`begin_transition` / `_mirror_gate_info`). Construction and the engine
lifecycle (`running` / `start` / `stop`) stay abstract in the subclass.
The two real divergences are expressed as capability flags, parallel to
the existing `supports_native_vad`:

- `fixed_language` (Moonshine `"en"`; `None` = multilingual) — a language
  change never forces a restart (`matches`) and `info` reports that fixed
  language (`begin_transition`).
- `supports_confidence_validation` (Moonshine `False`) — `info` reports
  `confidence_validation` as `""` ("not applicable") rather than a
  misleading on/off, keeping Moonshine's `/api/state` payload unchanged.

The `LiveChannel` Protocol stays as the seam the Recorder types against
(ADR-0003); the base is one implementation of it, and its `matches`
signature is corrected to match reality. This mirrors the transcriber
side's own pattern (`ChunkedTranscriber`, `VoxtralTranscriberBase`).

**Why not drop the Protocol and type everything as `LiveChannelBase`.**
Simpler, but it reopens ADR-0003's Protocol-seam decision and forces
every future channel to inherit the base. Keeping the Protocol lets a
from-scratch engine satisfy the interface without the shared base.

### 2. Reconcile is free functions on the slot, not a `LiveController`

The transition lives in `tapscribe/live_control.py` as two FastAPI-free
functions that both `/api/live/start` and the boot path call:

- `plan_live(current, desired, *, use_mlx) -> LivePlan` — **pure**:
  resolves the swap, validates, decides no-op / gate-knob-only / restart,
  and raises a `LiveReconcileError` before touching anything.
- `apply_live(current, plan, *, set_live)` — the side effects, with the
  slot owner passing a `set_live` callback so the route/lifespan keep
  control of `recorder.live`.

**Why not a `LiveController` object that owns the live slot** (the
"deepest" option — `recorder.live` becomes `controller.channel`).
Rejected on blast radius: `recorder.live` is read at ~38 sites across 10
modules, almost all encapsulated-method calls (`recorder.live.info`,
`.running()`, `.stop()`). A controller that owns the slot would reroute
all of them for no behavioural gain. The free-function seam concentrates
the orchestration at the two call sites that actually swap the channel
and leaves the read sites untouched. A controller can still be introduced
later if a second concern needs to own the slot; nothing here precludes
it.

**Why the pure `plan` / side-effecting `apply` split.** It makes the
`#334` invariant ("a rejected request leaves a running channel
untouched") **structural** rather than a matter of ordering discipline in
the route: `plan_live` cannot mutate, so a validation failure provably
can't disturb a running engine. It also gives the transition a test
surface with no subprocess, engine, or `TestClient`
(`tests/test_live_control.py`).

### 3. Validation raises domain errors, mapped centrally

`plan_live` raises `LiveModelUnknown` / `GateKindUnsupported` (→ 400) and
`apply_live` raises `LiveStartFailed` (→ 500), all registered in
`app._DOMAIN_ERROR_STATUS`. The route stops raising `HTTPException`
inline — the same thin-shim doctrine as the batch orchestrators, and
`live_control` is added to the `test_domain_errors_fastapi_free` sweep.

## Consequences

- The byte-identical channel methods and the cross-module private import
  are gone; a future `ParakeetLiveChannel` inherits the base (or
  implements the Protocol) and needs no new orchestration code.
- The family swap is resolved **unconditionally** in `plan_live` (not
  gated on a changed model string), preserving the #259 boot-swap fix:
  a persisted Moonshine default swaps even though `config.model` is
  unchanged.
- `apply_live` preserves the double-`begin_transition` choreography so
  `/api/state` stays on "starting" through a reload.
- The `/api/live/start` route shrinks from ~130 lines to a parse-and-
  delegate shim; the boot auto-start shares the same seam and swallows
  `LiveReconcileError` so a failed weights fetch never crashes startup.
