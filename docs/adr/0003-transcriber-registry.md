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

Pre-refactor, the set of supported models was scattered:
- A hardcoded `MODEL_OPTS` array in `web/js/components/session-detail.js`.
- A separate hardcoded `LIVE_MODELS` array in `live-channel.js`.
- A prefix-string router inside `transcribers/__init__._build_transcriber`:
  `voxtral-*` → Voxtral, `nb-whisper-*` → NB-Whisper, else Whisper.
- A separate prefix-string predicate `tapscribe.live.is_nb_whisper`.
- A separate `MLX_REPO_TABLE` mapping in `transcribers/mlx_whisper.py`.
- A `default_language_for(model_name)` heuristic in
  `transcribers/base.py` also keyed off prefixes.

Adding a model meant editing five separate files — none of them aware
of each other — and getting the prefix conventions right in each.
Worse, the two JS dropdowns were silently allowed to drift out of
sync with each other and from the factory's routing rules.

This PR adds support for two new model *families* (Parakeet, Canary)
each shipping with two adapters (MLX + CUDA/CPU). That would have
turned the five edits into roughly ten, plus reverse-engineering
which prefix conventions each new family should use.

## Decision

Introduce a `TranscriberRegistry` in `tapscribe/transcribers/catalog.py`
as the single declarative source of truth. The registry is a tuple of
`ModelEntry` frozen-dataclasses; the module-level `REGISTRY` instance
is what every other layer consults:

- `load_transcriber(model_name, *, backend)` looks up the entry and
  picks the right adapter from `entry.backends`.
- `GET /api/models?context=batch|live` serialises a filtered view of
  the registry into JSON; the dashboard renders both pickers from it.
- The dashboard's per-model dynamic input fields (initial_prompt,
  hotwords, source_lang, target_lang) come from `entry.inputs`.
- The "auto" backend preference resolves against `entry.backends`
  walking `mlx → cuda → cpu`, returning the first kind both available
  on this machine AND supported by the model.

Adding a model is now one new `ModelEntry` literal in `_DEFAULT_ENTRIES`.

## Considered alternatives

**Keep the prefix-string router, add Parakeet/Canary as more branches.**
Rejected because each new family was adding another invariant the
router had to track (e.g. "Parakeet refuses CPU because MLX is the
only Apple Silicon adapter and HF transformers handles CUDA/CPU; but
on `auto` should it fall back to HF?"). Three families' worth of these
rules is already on the edge of fitting in one function; five would
not.

**Use a stateless registration decorator (`@register("parakeet-*", ...)`).**
Rejected because import order would dictate the dispatch order — which
matters when `auto` walks the bindings — and because the dashboard
needs the *full* list of entries up-front to render the dropdown, not
just whatever subset has been imported by the current call site.
Eager declarative construction is simpler.

**Two registries — one for "what to show in the UI", one for "what to
route to in the factory".** Rejected because keeping them in sync was
exactly the bug the refactor exists to eliminate.

## Consequences

- `tapscribe/web/js/components/session-detail.js` no longer carries a
  `MODEL_OPTS` array; it builds the model select from
  `/api/models?context=batch`. Same for `live-channel.js` with
  `?context=live`.
- The dashboard gains a **backend chip row** above the model select,
  driven by the registry's `available_backends` field. Operators see
  immediately which backends are installed on this machine.
- The dashboard renders **per-model dynamic input fields** from
  `entry.inputs`. Whisper gets prompt + hotwords; Voxtral/Parakeet get
  nothing; Canary gets source_lang + target_lang selects.
- `load_transcriber`'s signature widens: `use_mlx: bool` →
  `backend: Literal["auto","mlx","cuda","cpu"]`. The cache key follows.
  See ADR-0001 amendment.
- The registry is the test surface: a single parametrised test
  ("every entry's loader resolves; every entry's languages are
  non-empty; every batch-only entry is rejected by the live filter")
  covers a class of bugs that had no test seam before.
- `tapscribe/live.py` introduces a `LiveChannel` Protocol; the existing
  class became `WhisperLiveKitChannel`. The seam exists so a future
  PR's pseudo-streaming `ParakeetLiveChannel` slots in without
  touching the Recorder.
- `TranscriptionResult` gains `source_language` + `target_language`
  fields. Canary uses both; everything else leaves `target_language`
  empty and the dashboard's translation badge stays hidden.
  *Update 2026-07-08:* Canary left with ADR-0006, and the vestiges this
  ADR describes were later deleted — `SelectInput`, the
  `target_lang`/`target_language` thread, and the translation badge are
  gone (`source_language` remains; it carries the ADR-0010 language
  pin). The registry design itself stands; read the input-field
  references above as point-in-time history.

## What this ADR explicitly does NOT decide

- **Live Parakeet/Canary.** This PR leaves them batch-only. The
  registry declares `contexts={"batch"}` for the two families. A
  follow-up PR will add concrete `ParakeetLiveChannel` /
  `CanaryLiveChannel` implementations and flip those entries to
  `contexts={"batch","live"}`.
- **Per-call MLX override at the dashboard level.** The backend chip
  is dashboard-wide today. Per-WAV override is a UI affordance change,
  not a routing change — the registry already supports it because
  every API call carries the `backend` field.
