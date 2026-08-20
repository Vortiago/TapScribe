---
status: accepted
---

# Diarization is a session-scoped stage, joined to the transcript at merge

A [multi-person tap](../../CONTEXT.md#single-person-tap--multi-person-tap) is
split into [Voices](../../CONTEXT.md#voice) by its own stage, with its own
artifact and engine seam. The transcript learns *who* at merge time, by joining
that artifact on absolute session time. #78.

## The artifact

`session-voices.json` — machine-written, one per session (the
`session-roster.json` family): per identity, a set of Voices with speech spans
in **absolute session time**, under the `run_id` of the diarization that
produced them. It holds no operator input, the line ADR-0009 draws between the
Roster and `session-meta.json`.

Clustering runs over **all of an identity's audio in the session at once**. A
level-gated tap emits one WAV per utterance — hundreds in a meeting — and
independently diarized WAVs produce labels that do not join: the label churn
that #383 flags for chunked diarization, at session scale.

Absolute time makes the join free: every recorder WAV and region clip carries
its absolute start in its filename (`build_recorder_wav_name`,
`parse_wav_start`), and `merge_session` already computes `abs_start`/`abs_end`
per segment.

## The mapping is a pointer — amending ADR-0009 §4

The registry's membership atom is the plain **Identity**. A Voice is never one:
it is session-local, so `identity#<voice>` is not unique across sessions and a
global entry would claim every later session's Voice A for whoever was mapped
first. (§4 reserved `identity#cluster` for this; it does not hold.)

The session-local fact lives in the operator-editable half — a `voices` map on
`session-meta.json`, `identity#<voice> → { person_id, run_id }`. Names stay in
`people.json` alone (ADR-0009 §5); the map holds a pointer.

A mapping stamped with a superseded `run_id` is **not applied**, and surfaces as
needing re-mapping — carrying it over would attribute a named human to whatever
the new engine labels `A`.

An unmapped Voice **does not auto-bind** to a Person. ADR-0009 §3's auto-bind
pays off for an Identity, which is stable across sessions and worth recognising;
a Voice is neither, and auto-binding would add a Person per unmapped voice per
diarized session — the junk speaker `bridges/README.md` warns about for
`__probe__`. An unmapped Voice renders as `Speaker A` from the sidecar.

Resolution: `slug#<voice>` → identity (the Roster's slug join) → the meta map's
`person_id` → Person name, falling back to the Voice label.

### A Person can own no Identity

Mapping a Voice by typing a name creates one. Two consequences in `people.py`:

- `_coerce_people` keeps an identity-less row that carries a NAME: a session's
  `voices` map reaches a Person by `person_id`, so identities are no longer the
  only reachability. A row the one-Identity-one-Person dedup EMPTIED is still
  dropped — that is a torn-file repair, not a Voice mapping.
- There is no bare **create** verb on the route surface. The voice-mapping PUT
  accepts a name and creates the Person as part of the mapping, so a Person is
  never left unattached, and session maintenance prunes one left with neither an
  Identity nor a live voice pointer.

## Surfaces

The operator maps a Voice on the **Transcript** stage, reading `Speaker A: …` —
picking an existing Person or typing a name. That is also the enrollment point
for later cross-session recognition (#439): a human confirming *this voice is
that person* is the label a voiceprint needs.

**Taps** (`GLOBAL · Ingress`) holds the single/multi control — a property of a
live source — and not the voice list, which belongs to a finished recording.

## Who declares single vs. multi

The **Bridge**, per tap on the wire: the tray's system-audio capture is
multi-person by construction, a SpatialChat per-participant tap is single. The
operator overrides it, durably per identity — an NDI bridge (#54) cannot tell a
room mic from a per-participant feed, so a declaration is only ever a default.
Precedence follows ADR-0009's name ladder: **operator override › bridge
declaration › default single**.

The reserved value spellings are stamped like `probe_identity` — one `Site` row
per bridge (ADR-0019), never a hand edit in `bridges/`.

It gates diarization: a single-person tap is not diarized and keeps a plain
`identity` speaker key. Beyond cost, a diarizer splits one human across a
channel or noise change, manufacturing Voices to clean up.

ADR-0010's parked per-identity language pin rides on this declaration rather
than on diarization, so it is separable work. `TapSettings` (`record`/`live`)
stays in-memory.

## Engine

A `Diarizer` protocol, the `Transcriber` / `Summarizer` shape, with a
**session-scoped** contract: *given one identity's audio for a session, produce
Voices with absolute-time spans*. A per-WAV contract would foreclose the
standalone path, which is why the seam sits at the session.

- **Standalone** (segmentation → embedding → one global clustering) satisfies it
  directly, and is the default.
- **One-pass transcribe+diarize** (#383) satisfies it as an adapter, over
  concatenated session audio or with its own cross-WAV join. It pays a full
  decode for "who" while transcribe decodes again, unless the adapter also feeds
  the per-WAV cache. An engine trade-off, not an architecture one.

The default binds to this repo's existing constraints: **no torch** in core
(#374), cross-platform (the Windows tray bridge's system-audio capture is the
main producer of multi-person taps), and onnxruntime already core, running a
vendored ONNX model with a `PROVENANCE.md` (`tapscribe/vad/`). An ONNX
segmentation + embedding pair fits; pyannote's torch stack does not.

## Consequences

- `TranscriptionSegment` and the per-WAV cache are untouched — diarization
  re-runs without re-paying for transcription, and every backend gets it.
- `SessionSegment.speaker` is `slug#<voice>` on a diarized tap; a segment
  spanning a Voice change splits on its word timestamps. Decompose with
  `rsplit("#", 1)`: a slug is `#`-free (`safe_name`), a full identity is not.
- `name_resolution.session_occurrences`' slug backfill skips `slug#<voice>`
  keys, or every Voice backfills a junk Person.
- A per-session **Override** (`session_meta.aliases`) matches the transcript key
  exactly — `sysaudio#A` overrides one Voice, `sysaudio` only the undiarized
  key. An override never fans out from a base slug to its Voices.
- Pipeline order is strip → **diarize** → transcribe → summarize, a no-op
  without a multi-person tap.
- Live captions stay undiarized (ADR-0010's live exclusion).

## Left to implementation

The **engine package** (wheel matrix; vendorable models or fetched — wants a
spike) and the diarize stage that runs it, the **voice-mapping PUT** with its
Person prune, and the two **surfaces** above — the Transcript stage's mapping
control and the Taps single/multi control. The durable override store is
`tapscribe/tap_mode.py` (`tap-modes.json`, `PUT /api/tap-mode`).
