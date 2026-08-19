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
surface — out of scope here, and #78's "carries across sessions" bullet is wrong
as written. What carries per identity is the single/multi declaration below.

## The artifact, and why it is session-scoped

`session-voices.json` — one machine-written sidecar per session (the
`session-roster.json` family), holding per identity a set of Voices, each with
speech spans in **absolute session time** and a nullable `person` pointer.

Clustering runs **over all of an identity's audio in the session at once**, not
per WAV. A level-gated tap emits one WAV per utterance — hundreds in a meeting —
and independently diarized WAVs produce labels that do not join, which is the
same label-churn failure #383 flags for chunked diarization, at session scale.

Absolute time is what makes the join free: every recorder WAV and every
strip-silence region clip already carries its absolute start in its filename
(`build_recorder_wav_name`, `parse_wav_start`), and `merge_session` already
computes `abs_start`/`abs_end` per segment.

## The mapping is a pointer, and it amends ADR-0009 §4

§4 reserved a "diarization-ready atom" — the registry's membership key becoming
`identity#cluster` once diarization lands. **That does not survive contact with
a session-local Voice.** `identity#A` is not unique: Monday's Voice A and
Tuesday's Voice A are different humans, so a global membership entry would
silently claim every future session's Voice A for whoever was mapped first.

So the registry's atom stays the plain **Identity**, and the session-local fact
lives in the session's own artifact: `session-voices.json` carries a
`person_id` pointer per Voice. Names still live only in `people.json` (ADR-0009
§5 intact) — the sidecar holds a pointer, never a name. Deleting a session takes
its mappings with it, and this is where a voiceprint would later sit.

An unmapped Voice therefore **does not auto-bind** to a Person. ADR-0009 §3's
auto-bind earns its keep because an Identity is stable across sessions and worth
recognising; a Voice is neither, so auto-binding it would buy nothing and grow
the global registry by every unmapped voice in every diarized session — the junk
speaker `bridges/README.md` already warns about for `__probe__`, once per voice
per meeting. An unmapped Voice renders as `Speaker A` straight from the sidecar.

Resolution for a diarized tap: transcript key `slug#<voice>` → identity (the
Roster's existing slug join) → the sidecar's `person` → Person name, falling
back to the Voice label. `/api/people` gains no verb; mapping is a PUT on the
session's voices.

## Mapping happens on the Transcript stage

The operator maps a Voice where the words are — reading `Speaker A: …` in the
merged transcript, they either pick an existing Person or type a name, which
creates one. That moment is also the natural enrollment point for later
cross-session recognition: it is where a human confirms *this voice is that
person*, which is exactly the label a voiceprint would need.

The mock in `taps.html` is wrong for this half and right for the other: Taps is
`GLOBAL · Ingress`, so it can hold the single/multi control (a property of a
live source) but never the voice list (a per-session artifact of a finished
recording).

## Who declares single vs. multi

A **bridge declares it per tap on the wire**, because the bridge often knows: the
tray's system-audio capture is multi-person by construction, a SpatialChat
per-participant tap is single. The operator can override it, and the override is
durable per identity — an NDI bridge (#54) genuinely cannot tell a room mic from
a per-participant feed, so a declaration can only ever be a default.

Precedence is the ladder ADR-0009 already established for names: **operator
override › bridge declaration › default single**. A bridge reconnecting never
stomps an override.

The reserved value spellings are stamped, following `probe_identity` — one new
`Site` row per bridge (ADR-0019), never a hand edit in `bridges/`.

Single/multi is what gates diarization: a single-person tap is not diarized, so
its speaker key stays a plain `identity` and its Person is untouched. This
matters beyond cost — a diarizer will happily split one human across a channel
or noise change, so diarizing a tap that carries one person manufactures Voices
the operator then has to clean up.

The durable per-identity home is also what ADR-0010's parked language pin was
waiting for: voting a single-person tap's regions to one language needs the
single/multi declaration, not diarization, so it is separable from this work.
`TapSettings` (in-memory, `record`/`live`) stays as it is — those are
deliberately ephemeral.

## Engine

Behind a `Diarizer` protocol, the `Transcriber` / `Summarizer` shape. Its
contract is **session-scoped** — *given one identity's audio for a session,
produce Voices with absolute-time spans* — and that is what makes the engine
swappable in both directions:

- A **standalone** diarizer (segmentation → embedding → one global clustering
  over the whole session) satisfies it directly. This is the default.
- A **one-pass transcribe+diarize** model (#383) satisfies it as an adapter, by
  consuming concatenated session audio or by bringing its own cross-WAV join. It
  pays a full decode to answer "who" while the transcribe stage decodes again —
  unless the adapter also feeds the per-WAV cache, which is strictly more
  machinery. An engine trade-off, not an architecture one.

A per-WAV contract would foreclose the standalone path entirely, which is why
the seam sits at the session.

The default implementation must satisfy the constraints that already bind this
repo: **no torch** in core (#374 dropped 773 MB deliberately), cross-platform
(the Windows tray bridge's system-audio capture is the main producer of
multi-person taps), and onnxruntime is already a core dependency running a
vendored ONNX model with a `PROVENANCE.md` (`tapscribe/vad/`). An ONNX
segmentation + embedding pair fits that mould; pyannote's torch stack does not.

## Consequences

- `TranscriptionSegment` and the per-WAV cache sidecar are **untouched**.
  Diarization is not a transcriber concern, so it re-runs without re-paying for
  transcription, and every backend gets it at once.
- `SessionSegment.speaker` becomes `slug#<voice>` on a diarized tap. A segment
  spanning a Voice change is split on its existing word timestamps. Decompose
  the key with `rsplit("#", 1)`, never `split`: a slug is `#`-free (`safe_name`
  keeps only alnum / `-` / `_` / `.`), but only the suffix we mint is guaranteed
  clean.
- `name_resolution.session_occurrences`' slug backfill must skip `slug#<voice>`
  keys, or every Voice in a merged transcript's `speakers` backfills a junk
  Person — the thing this ADR just spent a section avoiding.
- Pipeline order is strip → **diarize** → transcribe → summarize; diarize is a
  no-op for a session with no multi-person tap.
- Live captions stay undiarized, consistent with ADR-0010's live exclusion.

## Open — needs a call before this leaves `proposed`

1. **Voice vs. cluster.** Adopting Voice amends ADR-0009 §4 (already amended
   above on a harder point) and CONTEXT.md:718.
2. **Engine package**, and whether its models are vendorable or must be fetched.
   Wants a spike against the wheel matrix and the model licences.
3. **Where the per-identity single/multi override persists.** It needs a durable
   store keyed on the full identity; `TapSettings` is in-memory by design.
