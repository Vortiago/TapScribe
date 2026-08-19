---
status: accepted
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

`session-voices.json` — one **machine-written** sidecar per session (the
`session-roster.json` family), holding per identity a set of Voices, each with
speech spans in **absolute session time**, under the `run_id` of the diarization
that produced them.

It holds no operator input. ADR-0009 already draws that line — the Roster is
machine-written and "separate from the operator-editable `session_meta.json`" —
and diarization is exactly where ignoring it would hurt: re-diarizing with a
better engine rewrites every span and label, so a mapping stored alongside them
is clobbered or, worse, silently re-pointed at a different human.

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
lives in the operator-editable half: a `voices` map on `session-meta.json`,
`identity#<voice> → { person_id, run_id }`. Names still live only in
`people.json` (ADR-0009 §5 intact) — the map holds a pointer, never a name.

The `run_id` is what makes re-diarization safe: a mapping stamped with a
superseded run is **not applied**, and surfaces as needing re-mapping. Silently
carrying it over would attribute Monday's Kari to whoever the new engine happens
to label `A`.

An unmapped Voice therefore **does not auto-bind** to a Person. ADR-0009 §3's
auto-bind earns its keep because an Identity is stable across sessions and worth
recognising; a Voice is neither, so auto-binding it would buy nothing and grow
the global registry by every unmapped voice in every diarized session — the junk
speaker `bridges/README.md` already warns about for `__probe__`, once per voice
per meeting. An unmapped Voice renders as `Speaker A` straight from the sidecar.

Resolution for a diarized tap: transcript key `slug#<voice>` → identity (the
Roster's existing slug join) → the meta map's `person_id` → Person name, falling
back to the Voice label.

### A Person can exist without an Identity

Mapping a Voice by **typing a name** creates a Person that owns no Identity —
only a voice pointer. Two things in `people.py` assume that cannot happen:

- `_coerce_people` **drops** any row whose `identities` list is empty
  (people.py:91-92), so a typed-name Person would be written and then vanish on
  the next load. That drop is correct today — an identity-less Person is
  unreachable — but reachability now also comes from a session's `voices` map,
  so the rule must widen rather than stay.
- There is no **create** verb: `POST` does not exist in ADR-0009's API, because
  creation was always auto-bind's job. Rather than add a bare create, the
  voice-mapping PUT accepts a name and creates the Person **as part of the
  mapping** — so a Person still never exists unattached, and there is no orphan
  window. Session maintenance prunes a Person left with neither an Identity nor
  a live voice pointer.

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
- A per-session **Override** (`session_meta.aliases`) matches the transcript key
  **exactly**: `sysaudio#A` overrides that one Voice, `sysaudio` overrides only
  the undiarized key. An override must never fan out from a base slug to its
  Voices — they are different humans. ADR-0009's ladder (Override › Person ›
  default) is otherwise unchanged.
- Pipeline order is strip → **diarize** → transcribe → summarize; diarize is a
  no-op for a session with no multi-person tap.
- Live captions stay undiarized, consistent with ADR-0010's live exclusion.

## Left to implementation

The **engine package** (wheel matrix, whether its models are vendorable or must
be fetched — wants a spike) and the **durable store for the per-identity
single/multi override** (`TapSettings` is in-memory by design). Neither changes
a decision above.
