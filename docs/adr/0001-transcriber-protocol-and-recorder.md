---
status: accepted
date: 2026-05-14
---

# Transcriber protocol, A2 pipeline, and the Recorder context

This ADR captures the load-bearing architectural decisions from the
deepening pass that produced Lands 1–4 (Transcriber protocol, session
merge, live-cmd builder, Recorder context). The four decisions below are
each reasonable to re-suggest in a future architecture review; this ADR
exists so the rejection doesn't have to be re-derived.

## 1. Stateful Transcribers, one instance per `(backend × model_name)`

A `Transcriber` instance *is* one loaded model — it owns the underlying
model object, the model name, and the device label. The factory
(`load_transcriber(name, *, use_mlx)`) caches per `(model_name, use_mlx)`.
There is no tagged tuple, no dispatcher; the contract is the single
`transcribe(path, *, initial_prompt, hotwords) -> TranscriptionResult`
method.

**Why not stateless strategies** (one `FasterWhisperStrategy` /
`MlxStrategy` / `VoxtralStrategy`, each taking a model object per call):
mixing models within one session — e.g. an `nb-whisper-medium` for
Norwegian speakers and a different model for Danish — is a deliberate
near-future use case. With stateful Transcribers the caller picks one
thing (which Transcriber for this WAV); with stateless strategies the
caller picks two (which strategy + which model) and has to dispatch
between them anyway. The strategy split would relocate the dispatcher
we set out to eliminate, not remove it.

## 2. Pipeline-style post-processors via caller composition (A2)

`Transcriber.transcribe(...)` returns a raw `TranscriptionResult` — no
hallucination filtering. Post-processors are pure functions that the
caller composes:

```python
result = transcriber.transcribe(wav, initial_prompt=…, hotwords=…)
result = hallucinations.apply(result, rules)
# future: result = apply_pii_redaction(result, …)
# future: result = apply_phrase_replacement(result, …)
```

**Why not bake hallucination filtering into `transcribe()`** (option A3
during grilling): future post-processors (PII removal, name
canonicalisation) are concrete enough roadmap items that keeping the
Transcriber's contract narrow buys real optionality. Burying the first
post-step inside `transcribe()` would create asymmetric handling — one
step privileged, others external — for no current benefit.

Trade-off accepted: the route handler has to remember to call
`hallucinations.apply` on the way through. The merger in Land 2
(`cached_transcribe` + `merge_session`) does this once, so in practice
every production callsite goes through that helper.

## 3. The Recorder context, dependency-injected via `app.state`

Runtime mutable state lives on a single `Recorder` instance that
FastAPI routes receive via `Depends(get_recorder)`, where `get_recorder`
reads from `request.app.state.recorder`. Five sub-components compose
the Recorder: `LiveChannel`, `ActiveStreams`, `LiveTranscripts`,
`JobTracker`, `AuthState` (see `CONTEXT.md` for what each owns).

**Why not module-level globals** (the pre-refactor shape): tests cannot
construct a clean instance — they have to monkeypatch ~8 module
attributes across `config.py`, `auth.py`, `live.py`, `sessions.py`.
With a Recorder + DI, `TestClient(app)` with
`app.state.recorder = Recorder(...)` gives full per-test isolation.

**Why not a module-level singleton accessed directly** (e.g.
`from tapscribe.recorder import current_recorder`): the FastAPI idiom
of `app.state` is well-trodden, supports test overrides cleanly via
`app.dependency_overrides[get_recorder]`, and doesn't tie the Recorder
to import-time construction.

## 4. `use_mlx` is per-call, not per-process

`use_mlx` is a field on `Recorder`, set from `--no-mlx` at boot.
`load_transcriber(model_name, *, use_mlx: bool)` takes it as an explicit
kwarg; the transcriber cache is keyed by `(model_name, use_mlx)`.

**Why not a module-level `config.USE_MLX`** (its prior location): two
forward-looking cases are easier with per-call:

- A future "use MLX" dashboard toggle becomes `recorder.use_mlx = False`
  plus cache invalidation. With a global, the factory contract has to
  change.
- Per-WAV MLX choice (e.g. "force CPU for this one suspicious WAV")
  becomes naturally possible — different `use_mlx` per call yields
  different cache entries. Structurally impossible with a global.

NB-Whisper's "always faster-whisper regardless of MLX" rule is
unaffected; that's routing logic inside `load_transcriber`, not a
question about where `use_mlx` lives.

## Consequences

- Tests gain a real seam for route-level testing without monkeypatching.
- A future "transcribe via Backend X on machine A, merge on machine B"
  workflow becomes mechanically simpler — the merger reads cached
  per-WAV JSONs; nothing in it requires a loaded model.
- The wire format for per-WAV JSON and `session-transcript.json`
  changes (`"backend"` → `"transcriber"`, parallel speaker arrays →
  dict, `abs_hms` dropped). Locally cached JSONs from before this
  refactor will be invalidated and re-merged on next access.
- `tapscribe/config.py` shrinks to paths + immutable boot-time
  booleans; runtime state moves to the Recorder.
