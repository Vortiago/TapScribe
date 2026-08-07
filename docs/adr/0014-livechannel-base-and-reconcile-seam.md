---
status: accepted
date: 2026-07-19
---

# LiveChannelBase + the `live_control` reconcile seam

This completes the `LiveChannel` seam ADR-0003 opened. The Protocol let a
second engine slot in (`MoonshineLiveChannel`, PRD #120), but the transition
surface was duplicated near-byte-for-byte across the two channels — with
`moonshine_live.py` importing name-mangled privates across the module
boundary, and the Protocol's `matches` under-specifying the real signature
(masked by `@runtime_checkable`) — and the ~130-line `/api/live/start`
validate → swap → restart sequence was re-derived by the boot auto-start,
with no FastAPI-free test surface.

## Decision

### 1. A shared `LiveChannelBase` implements the Protocol

Both concrete channels inherit `LiveChannelBase` (`live.py`), which owns the
transition surface (`matches` / `apply_gate_knobs` / `begin_transition` /
`_mirror_gate_info`); construction and the engine lifecycle (`running` /
`start` / `stop`) stay abstract. The real divergences are capability flags,
parallel to `supports_native_vad`:

- `fixed_language` (Moonshine `"en"`; `None` = multilingual) — a language
  change never forces a restart, and `info` reports the fixed language.
- `supports_confidence_validation` (Moonshine `False`) — `info` reports
  `confidence_validation` as `""` ("not applicable"), and `matches` ignores a
  `conf` change the engine wouldn't honour.

The `LiveChannel` Protocol stays as the seam the Recorder types against
(ADR-0003); the base is one implementation of it, mirroring the transcriber
side (`ChunkedTranscriber`, `VoxtralTranscriberBase`). **Rejected: typing
everything as `LiveChannelBase`** — reopens ADR-0003 and forces every future
channel to inherit the base; the Protocol lets a from-scratch engine satisfy
the interface without it.

### 2. Reconcile is free functions on the slot, not a `LiveController`

The transition lives in `tapscribe/live_control.py` as two FastAPI-free
functions that both `/api/live/start` and the boot path call:

- `plan_live(current, desired, *, use_mlx) -> LivePlan` — **pure**: resolves
  the swap, validates, decides no-op / gate-knob-only / restart, and raises
  `LiveReconcileError` before touching anything.
- `apply_live(current, plan, *, set_live)` — the side effects; the slot owner
  passes `set_live` so the route/lifespan keep control of `recorder.live`.

**Rejected: a `LiveController` that owns the live slot** (`recorder.live` →
`controller.channel`) — blast radius: `recorder.live` is read at ~38 sites
across 10 modules, almost all encapsulated method calls; a controller would
reroute all of them for no behavioural gain. The free functions concentrate
orchestration at the two sites that actually swap the channel; a controller
can still be introduced later if a second concern needs to own the slot.

**Why the pure plan / side-effecting apply split**: it makes the #334
invariant ("a rejected request leaves a running channel untouched")
structural — `plan_live` cannot mutate — and gives the transition a test
surface with no subprocess, engine, or `TestClient`
(`tests/test_live_control.py`).

### 3. Validation raises domain errors, mapped centrally

`plan_live` raises `LiveModelUnknown` / `GateKindUnsupported` (→ 400),
`apply_live` raises `LiveStartFailed` (→ 500), all registered in
`app._DOMAIN_ERROR_STATUS`; the route raises no `HTTPException` inline — the
batch-orchestrator thin-shim doctrine, covered by the
`test_domain_errors_fastapi_free` sweep.

## Consequences

- A future channel inherits the base (or implements the Protocol) and needs
  no new orchestration code.
- The family swap is resolved **unconditionally** in `plan_live`, not gated
  on a changed model string — the #259 boot-swap fix: a persisted Moonshine
  default swaps even though `config.model` is unchanged.
- `apply_live` preserves the double-`begin_transition` choreography so
  `/api/state` stays on "starting" through a reload.
- `/api/live/start` is a parse-and-delegate shim; the boot auto-start shares
  the seam and swallows `LiveReconcileError` so a failed weights fetch never
  crashes startup.
