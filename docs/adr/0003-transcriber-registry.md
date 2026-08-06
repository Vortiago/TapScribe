---
status: accepted
date: 2026-05-19
---

# TranscriberRegistry: one declarative table for every model

> **Note (2026-06-21):** the Canary family added here was later removed
> and Parakeet's CUDA/CPU adapter moved from NeMo to `transformers` —
> see ADR-0006. The registry design below stands; only the Canary-
> specific examples are superseded.

## Context

Pre-registry, model knowledge was scattered across five files that
didn't know about each other — two hardcoded JS dropdown arrays, a
prefix-string router in the factory, prefix heuristics in `live.py` and
`transcribers/base.py`, and a per-adapter repo table. Adding a model
meant five coordinated edits, and the two dropdowns silently drifted
from each other and from the routing rules.

## Decision

`TranscriberRegistry` (`tapscribe/transcribers/catalog.py`) is the
single declarative source of truth: a tuple of frozen `ModelEntry`
dataclasses, with the module-level `REGISTRY` instance what every other
layer consults.

- `load_transcriber(model_name, *, backend)` looks up the entry and
  picks the adapter from `entry.backends`.
- `GET /api/models?context=batch|live` serialises a filtered view; the
  dashboard renders both model pickers from it, plus the backend chip
  row (from `available_backends`, so operators see which backends this
  machine has) and each model's dynamic input fields (from
  `entry.inputs` — Whisper gets prompt + hotwords).
- The `auto` backend preference resolves against `entry.backends`,
  walking mlx → cuda → cpu to the first kind both available on this
  machine and supported by the model (ADR-0001 §4).

Adding a model is one new `ModelEntry` literal in `_DEFAULT_ENTRIES`.

## Rejected alternatives

- **Keep the prefix-string router, add branches per family**: each new
  family adds routing invariants; three families' worth already barely
  fit in one function.
- **A registration decorator (`@register("parakeet-*", ...)`)**: import
  order would dictate `auto`'s dispatch order, and the dashboard needs
  the full entry list up-front, not whatever the call site imported.
- **Two registries — one for the UI, one for factory routing**: keeping
  them in sync is exactly the bug the registry exists to eliminate.

## Consequences

- The registry is the test surface: one parametrised test ("every
  entry's loader resolves; languages non-empty; batch-only entries
  rejected by the live filter") covers a class of bugs that had no
  seam before.
- `tapscribe/live.py` gains the `LiveChannel` Protocol (the existing
  class became `WhisperLiveKitChannel`), so a second live engine slots
  in without touching the Recorder — the seam ADR-0014 later completed.
- `load_transcriber`'s signature widens from `use_mlx: bool` to
  `backend: Literal["auto","mlx","cuda","cpu"]`, cache key following —
  see the ADR-0001 §4 amendment.
- `TranscriptionResult` gains `source_language` (it now carries the
  ADR-0010 language pin). The Canary-era `target_language` thread,
  `SelectInput`, and the translation badge were deleted when Canary
  left with ADR-0006.
- Whether a family is live-capable is a registry fact (`contexts=`); a
  new live adapter flips its entry, nothing else. Per-WAV backend
  override is a UI affordance question only — every API call already
  carries `backend`.
