---
status: accepted
date: 2026-05-14
---

# Transcriber protocol, A2 pipeline, and the Recorder context

Four decisions, each reasonable to re-suggest in a future architecture
review; this ADR exists so the rejection doesn't have to be re-derived.

## 1. Stateful Transcribers, one instance per `(backend × model_name)`

A `Transcriber` instance *is* one loaded model — it owns the model
object, the model name, and the device label. The contract is the single
`transcribe(path, *, initial_prompt, hotwords) -> TranscriptionResult`
method; the factory `load_transcriber` caches instances. No tagged
tuple, no dispatcher.

**Rejected — stateless strategies** (one strategy class per backend,
model object passed per call): mixing models within one session is a
deliberate use case, and a strategy caller picks two things (strategy +
model) and dispatches between them anyway — the split relocates the
dispatcher, it doesn't remove it.

## 2. Pipeline-style post-processors via caller composition (A2)

`transcribe(...)` returns a raw `TranscriptionResult`; post-processors
(hallucination filtering today; PII redaction, phrase replacement later)
are pure functions the caller composes on the result.

**Rejected — baking hallucination filtering into `transcribe()`**: it
would privilege one post-step while the rest stay external, for no
current benefit. Accepted trade-off: callers must remember
`hallucinations.apply` — in practice every production call site goes
through `cached_transcribe` + `merge_session`, which does it once.

## 3. The Recorder context, dependency-injected via `app.state`

Runtime mutable state lives on a single `Recorder` instance that routes
receive via `Depends(get_recorder)`, which reads
`request.app.state.recorder`. `CONTEXT.md` lists the sub-components and
what each owns.

**Rejected — module-level globals** (the prior shape): tests had to
monkeypatch ~8 module attributes for isolation; with DI,
`app.state.recorder = Recorder(...)` isolates each test.
**Rejected — an importable singleton**: `app.state` is the well-trodden
FastAPI idiom, supports `app.dependency_overrides[get_recorder]`, and
doesn't tie the Recorder to import-time construction.

## 4. Backend preference (né `use_mlx`) is per-call, not per-process

`load_transcriber(model_name, *, backend: Literal["auto","mlx","cuda","cpu"])`
takes the preference as an explicit kwarg — the `Recorder` holds the
operator's choice (`--backend`; `--no-mlx` is a legacy alias) — and the
transcriber cache is keyed `(model_name, resolved_kind)`. The registry
resolves `auto` against each model's BackendBindings, walking
mlx → cuda → cpu and silently skipping a kind that is unavailable on
this machine or that the model has no binding for — so NB-Whisper, which
has no MLX binding, routes to faster-whisper under `auto` instead of
raising. (Originally `use_mlx: bool`; the ADR-0003 registry widened it,
preserving the per-call property. `use_mlx` remains a read-only
back-compat property on `Recorder`.)

**Rejected — a module-level `config.USE_MLX`**: with per-call, a
dashboard backend toggle is a field write plus cache invalidation, and
per-WAV backend choice falls out of the cache keying — both structurally
impossible with a global.

## Consequences

- Route-level tests get a real seam instead of monkeypatching.
- The merger reads cached per-WAV JSONs and needs no loaded model, so a
  transcribe-on-machine-A / merge-on-machine-B split stays mechanically
  simple.
- `tapscribe/config.py` is paths plus immutable boot-time booleans;
  runtime state lives on the Recorder.
