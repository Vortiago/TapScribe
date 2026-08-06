---
status: accepted
---

# Multi-language transcription: operator declares languages, a uniform per-region run covers them

## Decision

TapScribe transcribes mixed-language meetings (motivating case: Danish +
Norwegian speakers) by having the operator declare the meeting's **expected
languages** — a [candidate-language set](../../CONTEXT.md#candidate-languages--language-pin)
— **not** by picking a model. One uniform run handles every recording: for each
**region** (a stripped speech-region WAV, or any per-utterance WAV), TapScribe
runs the model(s) that **cover** the candidate languages, a pluggable
**selector** picks the best transcript per region, the winner becomes that
WAV's `_primary` sidecar, and `merge_session` stitches the mixed-language
transcript. Default set: `{da, no, en}`, so the feature works unconfigured.

## Why

Danish and Norwegian Bokmål are orthographically near-identical, so Whisper's
per-file language auto-detect flips between them unpredictably — the root cause
of poor mixed-meeting results. Naming the *languages* removes the guess and
spares the operator the model/language capability matrix (only `whisper-large`
does both da+no; `nb-whisper` is Norwegian-only; Parakeet does da-not-no). One
run expresses every case — a singleton set → one model (a "pin", for free); a
multi-set → ensemble + selection — and the per-WAV cache already stores
multiple `(backend, model)` sidecars behind a `_primary` pointer, so "N
transcripts, pick one" is already the on-disk model.

## Scope

v1 covers the after-meeting batch path — the end-of-meeting pipeline's
transcribe stage and `transcribe_session` — because batch quality matters more
than live for this feature.

> **Extended by [ADR-0011](0011-interactive-batch-is-language-driven.md):** the
> Transcript page dropped its model picker and became language-driven, bringing
> the manual single-WAV path into the cover. The live channel stays out of
> scope.

## Seams (deliberately swappable)

- **Generalist** = `config/batch-model.txt` (default `whisper-large-v3-turbo`).
  Pointing it at another model (e.g. `parakeet-tdt-0.6b-v3`) is the sanctioned
  way to A/B the generalist without touching the pipeline.
- **Specialist table** — a catalog map `language → purpose-built model`; v1:
  `{no → nb-whisper-large}`. The models run for a set `S` = `{generalist} ∪
  {specialist[l] for l in S if l in table}`. nb-**large**, not medium: a
  20-clip FLEURS comparison vs a `large-v3-turbo` generalist showed nb-large
  win-or-tie 19/20 on Norwegian (~40% lower WER) while nb-medium only tied —
  medium doesn't earn its extra decode. (Constrained language *detection* was
  77/77 across da/no/sv/en in that run.)
- **Selector** — default **specialist-routing**: route each region to the model
  the specialist table names for the language the generalist detected there;
  no specialist for that language → keep the generalist. There is NO acoustic
  fallback: `avg_logprob` is not comparable across different-language models
  even within the Whisper family (a confident nb-whisper rendering English as
  Norwegian wins that comparison — the real bug the real-audio tests caught),
  and FLEURS shows nb-large genuinely wins on Norwegian while `avg_logprob`
  doesn't reflect it. `AcousticConfidenceSelector` (duration-weighted mean
  avg_logprob) is retained as a same-family, non-default alternative. Still
  swappable for a **text-LID** or **LLM-judge** selector for cross-architecture
  pairs, where the generalist's per-region language detection is unreliable;
  `test_da_no_routing_benchmark` and the FLEURS harness are the yardsticks for
  any such change and every future model.

## Considered and rejected (for v1)

- **Per-speaker manual language pins + a single-/multi-person code branch** —
  more UI and code for marginal gain; per-identity aggregation (vote a
  [single-person tap](../../CONTEXT.md#single-person-tap--multi-person-tap)'s
  regions to one language) lands with diarization (#78).
- **LID-first, single decode** — cheapest, but the LID is exactly the
  confusable da/no decision, and a wrong call leaves no second transcript.
- **Operator picks a model directly** — forces the capability matrix on the
  operator.

## Consequences

- The cover runs every model on every region, wasting a decode on monolingual
  taps. Accepted: batch latency is the cheap axis; skipping the ensemble once
  a single-person tap has voted itself to one language arrives with
  diarization + per-identity aggregation.
- The candidate set's operator default lives in `config/languages.txt`
  (catalog-validated, same pattern as `batch-model.txt`), with a per-meeting
  override on session-meta; `batch-model.txt` stays the generalist slot.
