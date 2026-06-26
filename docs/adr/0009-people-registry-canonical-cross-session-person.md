---
status: accepted
date: 2026-06-26
---

# People Registry — a canonical, cross-session Person model

## Context

The **People** view (`tapscribe/web/js/next/views/people.js`) has two panels:

- **"In this session · names"** — a per-session alias editor. It derives the
  speakers present in the *focused* session and renders an editable display
  name per speaker, saved to that session's `session_meta.aliases` via
  `PUT /api/session-meta/{session}`. The map is keyed by the **name slug**
  (`parse_wav_speaker_slug`, e.g. `"Atle_Havso"`), falling back to a live
  stream's `identity` only when there is no recording yet.
- **"People · across all sessions"** — a read-only aggregator that groups every
  session's aliases by the **typed display name** (the alias *value*) into one
  row per distinct name.

This shape is wrong for the job operators actually have ("who are the people I
record, across all my meetings, and what is each one called"):

1. **The registry is keyed by the typed name, so it is empty until you name
   someone.** You must name a speaker before they are visible in the
   cross-session view — backwards from "show me everyone so I can name them."
2. **The only editable surface is session-scoped.** To improve a name you must
   first select the right session; the editor's content swaps every time the
   selected session changes. There is no place to do the naming/mapping work
   *across* sessions.
3. **There is no canonical "person."** The only thing linking two sessions is
   typing the identical display name in both. Nothing recognises that the same
   participant recurs across meetings.
4. **The stable join key that *does* exist is discarded.** Each `/tap` carries a
   bridge-stamped `identity` that is stable across sessions for our bridges (for
   SpatialChat it is `participant.identity` = the account `user.id`, constant
   across meetings; Windows-tray is per-device; local-test is the OS user). But
   `/api/state` stopped shipping per-WAV detail, so for recorded speakers only
   the **name slug** survives to the dashboard. On disk the identity persists
   only as the filename's `safe_name(identity)[:10]` slug — *truncated*, so two
   opaque ids sharing a 10-char prefix collide, and the live (untruncated) vs
   recorded (`[:10]`) forms of one person fail to match.

The operator wants to map recurring participants to a single named person and
have that name apply everywhere — and to *see* the device/identity behind each
name and how a person is recognised across sessions.

## Decision

Introduce a **canonical, global Person model**, recognised by the bridge-stamped
**Identity**, with the People view rebuilt as the single editable registry.

1. **Person (goal).** A global entity = one display name + a set of member
   Identities. Naming a Person once propagates to every session.
2. **Recognition by Identity.** Auto-group occurrences by the full Identity.
   **Manual merge** is the escape hatch for "same human, genuinely different
   tokens" (one person across two devices/platforms; an old truncation-collision
   split). The display name is **never** the join key.
3. **Auto-bind.** Every new Identity auto-binds to its own Person on first
   sighting, default-named from the bridge-sent display `name`. The registry is
   never empty; the work becomes *rename + merge*, not *create from scratch*.
4. **Diarization-ready atom.** A Person's membership atom is a *speaker key* —
   `identity` today, `identity#cluster` once diarization (#78) splits one
   Identity into several voices. Auto-join only covers stable Identities;
   diarized clusters are session-local and need manual merge (or future
   voice-embedding matching). The schema does not change when diarization lands;
   only the *granularity* of the keys does. This preserves the current
   one-`/tap`-WS-=-one-speaker invariant unchanged.
5. **Single source of truth (server-resolved).** Names live in one global
   `people.json`. The **server resolves** `identity → Person → name` when it
   builds `/api/state` and the merged transcript, and ships the existing
   name-map shape the frontend already renders — so the guarded render path
   (Interaction hold, ADR-0004) is untouched.
6. **Full-identity fidelity.** Persist the **untruncated** identity per
   occurrence in a per-session **Roster** (`session-roster.json`), written by the
   tap path. The WAV filename format is unchanged. This removes the
   truncation-collision and live↔recorded-split hazards and supplies the
   device-id the view surfaces.
7. **Reversible merge.** Merge combines two Persons (survivor's name wins,
   member Identities join — mirroring `absorb_session`'s "target wins"); a
   whole-Identity **detach** pulls an Identity back into its own Person as undo.
   Sub-cluster split is deferred to diarization.
8. **Registry-primary UI.** The global registry is *the* editable surface: one
   row per Person with editable name, device Identity token(s), ● live /
   recorded source, and an expandable "N sessions" count, plus merge + detach.
   Selecting a session only **highlights/filters** the list — it never replaces
   it. Per-session **override** is a per-row action.
9. **Fresh start (no backfill).** Existing `session_meta.aliases` are *not*
   migrated into the registry. They remain as harmless per-session overrides so
   old transcripts still render; the registry fills from new occurrences. (The
   operator had named almost no one, so backfill earns nothing.)
10. **Override precedence.** Server resolution order is: per-session **Override**
    (`session_meta.aliases`) › **Person** name (via Identity membership) ›
    bridge display name / slug fallback. Rosterless old sessions resolve by slug
    and keep rendering — no regression.

### Storage & API

- **Roster** — per-session sidecar `session-roster.json`, machine-written at tap
  open/close: `full identity → { name, source, wav refs }`. Kept separate from
  the operator-editable `session_meta.json`, and distinct from the per-identity
  `tap_settings` gate record (ADR-0007).
- **People Registry** — `people.json` at the recordings root (global).
- **API** — `GET /api/people` (registry for the view); `PUT /api/people/{id}`
  (rename); `POST /api/people/merge`; `POST /api/people/{id}/detach`. Per-session
  override stays on `PUT /api/session-meta`.
- **`/api/state`** — re-key merged-transcript speakers on Identity at build time
  (recover via `parse_wav_speaker_ident` for recorded, `a.identity` for live, the
  Roster for new sessions); ship the resolved per-session name map plus the
  registry.
- **Security (CLAUDE.md).** Identities reaching any filename round-trip through
  `safe_name`; `person_id` is server-generated; `/api/people` body `person_id` /
  `identity` are validated against the known registry before touching a path —
  the same allowlist discipline as the summarizer-model catalog.

## Considered alternatives

- **Join on display name (status quo).** Rejected: circular (must name first)
  and fragile — a typo splits one person, name reuse ("Mike") fuses two.
- **Visibility-only fix** (identity-keyed read-only registry, naming stays
  per-session). Rejected as the destination: "map them to things" only makes
  sense if the mapping persists and propagates. Its read-only registry survives
  as the first visible layer of the build, not the endpoint.
- **Copy-out source of truth** (registry fans names into every session's stored
  aliases; render path untouched). Rejected: the name is stored N times and
  diverges the first time a session is absorbed, re-transcribed, or hand-edited
  — turning "name once" into "why is she called two different things."
- **Keep the truncated `[:10]` slug as the key** (no Roster). Rejected: builds
  the whole feature on a key that is neither unique (collisions fuse two people,
  unrecoverable without split) nor complete (live vs recorded split), precisely
  in the SpatialChat-across-meetings case that motivates it.
- **One-way merge.** Rejected: merge is the only information-losing op; shipping
  it without undo when the undo (whole-Identity detach) is cheap turns every
  misclick into permanent registry corruption.
- **Two-panel UI with flipped roles.** Rejected: keeps a per-session panel whose
  content swaps on selection — the exact thing that reads as "the registry
  changes per session."
- **Backfill existing aliases.** Rejected *for now* (BF2): near-zero existing
  naming to preserve, and a slug-keyed legacy import would duplicate the new
  identity-keyed Persons and demand merges. Revisit only if a deployment has
  substantial accumulated naming.
- **Vertical slices vs one PR.** Slicing was recommended; the operator chose a
  single PR. Override (decision 8/10) is the one piece safe to cut if the diff
  grows unwieldy.

## Consequences

- A new global store (`people.json`) and a new per-session sidecar
  (`session-roster.json`) join `session_meta.json` in the session-modules layer;
  CONTEXT.md gains a **Person · Identity · Roster · People Registry** entry.
- The merged-transcript / `/api/state` build path re-keys speakers on Identity —
  the one concentrated backend change — while the frontend keeps consuming a
  resolved name map, so the Interaction-hold render guards (ADR-0004) are
  unaffected.
- `session_meta.aliases` is demoted from source-of-truth to a per-session
  **Override** layer; old rosterless sessions resolve by slug and are unchanged.
- Diarization (#78) lands as a *data* change (cluster-grained speaker keys), not
  a schema change; the one-WS-one-speaker invariant and CaptureOrchestrator's
  "me vs. them" split are preserved until then.
- A future change that proposes keying People on the display name, or
  reintroducing the truncated-slug key, or a copy-out registry, should consult
  this ADR first — each was considered and rejected above.
