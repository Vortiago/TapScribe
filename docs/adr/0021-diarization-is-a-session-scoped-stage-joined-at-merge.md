---
status: proposed
---

# Diarization is a session-scoped stage, joined to the transcript at merge

A [multi-person tap](../../CONTEXT.md#single-person-tap--multi-person-tap) is
split into **Voices** by a diarization stage that owns its own artifact and its
own engine seam. The transcript learns *who* only at merge time, by joining that
artifact on absolute session time. #78.

## Voice

A **Voice** is one speaker the diarizer distinguishes within one multi-person
tap, in one session. Operator-facing and engine-neutral — "cluster" is one
implementation's word for it (an end-to-end model emits labels without
clustering anything), so the glossary says Voice everywhere.

A Voice is **session-local**: Monday's `Speaker A` is not Tuesday's. Only an
acoustic voiceprint could make a Voice recognisable across sessions, and a
durable per-Person voiceprint is a different feature with its own privacy
surface — out of scope here, and #78's "carries across sessions" bullet is
wrong as written. What *does* carry per identity is the Tap profile below.

## The mapping is People Registry membership

Mapping a Voice to a Person is **not a new store**. ADR-0009 §4 already reserves
the membership atom: the speaker key is `identity` for a single-person tap and
`identity#<voice>` for a diarized one. Mapping = adding that key to a Person;
`rename` / `merge` / `detach` already are the verbs, and resolution stays
server-side at `/api/state` build time — so a Voice mapped *after* the
transcript was built needs no rebuild, exactly as renaming a Person doesn't
today.

Decompose the key with `rsplit("#", 1)`, never `split`. A slug is `#`-free
(`safe_name` keeps only alnum / `-` / `_` / `.`), but a full **identity** is the
raw bridge-sent query param and is not validated — only the suffix we mint is
guaranteed clean. One helper constructs the key, one decomposes it.

## The artifact, and why it is session-scoped

`session-voices.json` — one machine-written sidecar per session (the
`session-roster.json` family), holding per identity a set of Voices, each with
speech spans in **absolute session time**.

Clustering runs **over all of an identity's audio in the session at once**, not
per WAV. A level-gated tap emits one WAV per utterance — hundreds in a meeting —
and independently diarized WAVs produce labels that do not join, which is the
same label-churn failure #383 flags for chunked diarization, at session scale.

Absolute time is what makes the join free: every recorder WAV and every
strip-silence region clip already carries its absolute start in its filename
(`build_recorder_wav_name`, `parse_wav_start`), and `merge_session` already
computes `abs_start`/`abs_end` per segment.

## Consequences

- `TranscriptionSegment` and the per-WAV cache sidecar are **untouched**.
  Diarization is not a transcriber concern, so it re-runs without re-paying for
  transcription, and every backend gets it at once. A one-pass
  transcribe+diarize adapter (#383) is therefore an alternative *engine*, not
  the shape — it would couple diarization to one platform's transcriber and
  diarize per WAV.
- `SessionSegment.speaker` becomes `slug#<voice>` on a diarized tap. A segment
  spanning a Voice change is split on its existing word timestamps.
- `name_resolution.session_occurrences`' slug backfill must be Voice-aware, or
  every `slug#0` in a merged transcript's `speakers` backfills a junk Person.
- Pipeline order is strip → **diarize** → transcribe → summarize; diarize is a
  no-op for a session with no multi-person tap.
- Live captions stay undiarized, consistent with ADR-0010's live exclusion.

## Tap profile

A durable, per-identity record of what the **operator** knows about a source and
the bridge cannot tell us. v1 holds one thing: whether this is a multi-person
tap. Global sidecar at the recordings root, keyed on the full identity.

`TapSettings` (in-memory, `record`/`live`) stays as it is — those are
deliberately ephemeral. The wire is unchanged: a bridge's flow (mic vs.
loopback) is only a guess at how many humans a stream carries, and the tap wire
contract is stamped across four languages and every doc (ADR-0019), so it is the
expensive place to put an operator's declaration. A wire hint can seed the
default later without breaking anything.

This is also the home ADR-0010's parked per-identity language pin was waiting
for — voting a single-person tap's regions to one language needs the profile,
not diarization, so it is separable from this work.

## Engine

Behind a `Diarizer` protocol, the `Transcriber` / `Summarizer` shape. The
default implementation must satisfy the constraints that already bind this repo:
**no torch** in core (#374 dropped 773 MB deliberately), cross-platform (the
Windows tray bridge's system-audio capture is the main producer of multi-person
taps), and onnxruntime is already a core dependency running a vendored ONNX
model with a `PROVENANCE.md` (`tapscribe/vad/`). An ONNX segmentation +
embedding pair fits that mould; pyannote's torch stack does not.

## Open — needs a call before this leaves `proposed`

1. **Voice vs. cluster.** Adopting Voice amends ADR-0009 §4 and CONTEXT.md:718
   (prose only — the key format is unchanged).
2. **Engine package**, and whether its models are vendorable or must be
   fetched. Wants a spike against the wheel matrix and the model licences.
3. **Tap profile naming** and whether it earns a glossary entry now or when the
   language pin joins it.
