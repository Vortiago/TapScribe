---
status: accepted
---

# Multi-language transcription: operator declares languages, a uniform per-region run covers them

## Decision

TapScribe transcribes mixed-language meetings (motivating case: 1–2 Danish +
1–2 Norwegian speakers) by having the operator declare the **expected
languages** for a meeting — a [candidate-language set](../../CONTEXT.md#candidate-languages--language-pin)
— **not** by picking a model. One uniform run handles every recording: for each
**region** (a stripped speech-region WAV, or any per-utterance WAV), TapScribe
runs the model(s) that **cover** the candidate languages, a pluggable
**selector** picks the best transcript per region, the winner becomes that
WAV's `_primary` sidecar, and the existing `merge_session` stitches a
mixed-language transcript. The default candidate set is `{da, no, en}`, so the
feature works with no configuration ("catch-all" default).

## Why

Danish and Norwegian Bokmål are orthographically near-identical, so Whisper's
per-file language auto-detect flips between them unpredictably — the root cause
of today's poor mixed-meeting results. Naming the *languages* removes the
guess, and is a far simpler mental model than choosing a model (only
`whisper-large` does both da+no; `nb-whisper` is Norwegian-only; Parakeet does
da-not-no) — the operator should not have to hold that matrix in their head.
Expressing every case as one run — a singleton set → one model (a "pin", for
free); a multi-set → an ensemble + selection — avoids special-case code paths
and reuses what already exists: the per-WAV cache already stores multiple
`(backend, model)` sidecars behind a `_primary` pointer, so "N transcripts,
pick one" is already the on-disk model.

## Scope

v1 covers the **after-meeting batch path** — the end-of-meeting pipeline and
`transcribe_session` — because batch quality matters more for this feature than
live (operator's stated priority). The pipeline already does
strip → transcribe(regions) → summarize, so the run slots into its transcribe
stage. Manual single-WAV transcribe and the live channel are unchanged in v1.

## Seams (deliberately swappable, for later experimentation)

- **Generalist model** = the existing `config/batch-model.txt` (default
  `whisper-large-v3-turbo`). Pointing it at `parakeet-tdt-0.6b-v3` is the
  sanctioned way to A/B the Danish/generalist engine without touching the
  pipeline.
- **Specialist table** — a small catalog map `language → purpose-built model`
  for languages where a specialist beats the generalist. v1: `{no →
  nb-whisper}`. The models run for a candidate set `S` = `{generalist} ∪
  {specialist[l] for l in S if l in table}`.
- **Selector** — picks the winning sidecar per region. Default:
  **specialist-routing** — route each region to the model the specialist table
  names for the language the generalist detected there (the table *declares*
  which model is best per language), falling back to **acoustic confidence**
  (avg_logprob) where the detected language has no specialist or the candidates
  are cross-architecture (the acoustic fallback keeps an unscored Parakeet/
  Voxtral generalist rather than losing to a scored nb-whisper). This replaced a
  pure-acoustic default after the **da/no routing benchmark**
  (`tests/e2e/test_pipeline_e2e.py::test_da_no_routing_benchmark`, on the
  committed `solen-da` + `marlene-nb` fixtures) measured that acoustic confidence
  mis-routed the Norwegian region — the generalist's weaker Norwegian (recall
  0.77) outscored nb-whisper's better one (0.92) on avg_logprob. Specialist-
  routing makes the Danish region go to the generalist (best Danish) and the
  Norwegian region to nb-whisper (best Norwegian), which the benchmark now pins
  at 100% correct routing. Still swappable for a **text-LID** or **LLM-judge**
  selector for cross-architecture pairs (where the generalist's per-region
  language detection is unreliable); the benchmark is the yardstick for any such
  change and for every future model.

## Considered and rejected (for v1)

- **Per-speaker manual language pins + a single-/multi-person code branch.**
  More UI and more code for a marginal v1 gain; deferred as an "improvement
  later" that composes with diarization (#78). The
  [single-/multi-person tap](../../CONTEXT.md#single-person-tap--multi-person-tap)
  concept stays in the glossary, and per-identity language aggregation (vote a
  single-person tap's regions to one language) lands with diarization.
- **LID-first, single decode** (detect the language, then run only that model).
  Cheapest, but the LID is exactly the confusable da/no decision and a wrong
  call leaves no second transcript to recover from.
- **Operator picks a model directly.** Forces the operator to internalise the
  model/language capability matrix; replaced by "declare languages, system
  routes to models."

## Consequences

- Running both models on every region wastes a decode on monolingual taps.
  Accepted: batch latency is the cheap axis. The later optimisation — stop
  re-running the ensemble once a single-person tap has voted itself to one
  language — arrives with diarization + per-identity aggregation.
- The end-of-meeting pipeline's model resolution changes from "one
  `batch-model.txt`" to "the candidate set + the map", with `batch-model.txt`
  retained as the generalist slot. The candidate set's operator default lives
  in a new `config/languages.txt` (catalog-validated, same pattern as
  `batch-model.txt`), with a per-meeting override on session-meta.
