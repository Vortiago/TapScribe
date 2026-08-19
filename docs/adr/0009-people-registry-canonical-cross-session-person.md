---
status: accepted
date: 2026-06-26
---

# People Registry — a canonical, cross-session Person model

## Context

Naming used to live only in per-session `session_meta.aliases`, with the
cross-session view grouped by the *typed display name*: circular (a person is
invisible until named), editable only session-by-session, no canonical
"person" — and the stable join key that does exist was discarded. Each `/tap`
carries a bridge-stamped `identity` that is stable across sessions
(SpatialChat: `participant.identity` = the account `user.id`; Windows-tray:
per-device; local-test: the OS user), but on disk it survived only as the
filename's `safe_name(identity)[:10]` slug — truncated, so two ids sharing a
10-char prefix collide, and the live (untruncated) vs recorded (`[:10]`)
forms of one person fail to match.

## Decision

A **canonical, global Person model**, recognised by the bridge-stamped
**Identity**, with the People view as the single editable registry
(CONTEXT.md: Person · Identity · Roster · People Registry):

1. **Person.** One display name + a set of member Identities; naming once
   propagates to every session.
2. **Recognition by Identity.** Occurrences auto-group by the full Identity;
   **manual merge** is the escape hatch for "same human, genuinely different
   tokens". The display name is **never** the join key.
3. **Auto-bind.** Every new Identity binds to its own Person on first
   sighting, default-named from the bridge-sent display `name`. The registry
   is never empty; the work is *rename + merge*, not *create from scratch*.
4. **Identity is the only membership atom.** A Voice is session-local, so
   `identity#<voice>` is not unique across sessions and never enters the
   registry; a diarized segment resolves through the session's own `voices`
   map (ADR-0021). Auto recognition joins stable Identities only. The
   one-`/tap`-WS-=-one-speaker invariant is unchanged.
5. **Single source of truth (server-resolved).** Names live in one global
   `people.json`; the **server** resolves `identity → Person → name` when
   building `/api/state` and the merged transcript, shipping the name-map
   shape the frontend already renders — the guarded render path (ADR-0004)
   is untouched.
6. **Full-identity fidelity.** The **untruncated** identity is persisted per
   occurrence in a per-session **Roster** (`session-roster.json`), written by
   the tap path; the WAV filename format is unchanged. This removes the
   truncation-collision and live↔recorded-split hazards.
7. **Reversible merge.** Merge combines two Persons (survivor's name wins,
   member Identities join — mirroring `absorb_session`'s "target wins");
   whole-Identity **detach** pulls an Identity back into its own Person as
   the undo. Sub-cluster split is deferred to diarization.
8. **Registry-primary UI.** The global registry is *the* editable surface:
   one row per Person with editable name, Identity token(s), ● live /
   recorded source, session count, merge + detach. Selecting a session only
   **highlights/filters** the list — never replaces it. Per-session override
   is a per-row action.
9. **Fresh start (no backfill).** Existing `session_meta.aliases` are not
   migrated; they remain harmless per-session overrides and the registry
   fills from new occurrences.
10. **Override precedence.** Server resolution order: per-session
    **Override** (`session_meta.aliases`) › **Person** name (via Identity
    membership) › bridge display name / slug fallback. Rosterless old
    sessions resolve by slug and keep rendering.

### Storage & API

- **Roster** — per-session sidecar `session-roster.json`, machine-written at
  tap open/close: `full identity → { name, source, wav refs }`. Separate from
  the operator-editable `session_meta.json` and from the per-identity
  `tap_settings` gate record (ADR-0007).
- **People Registry** — `people.json` at the recordings root (global).
- **API** — `GET /api/people`; `PUT /api/people/{id}` (rename);
  `POST /api/people/merge`; `POST /api/people/{id}/detach`. Overrides stay on
  `PUT /api/session-meta`.
- **`/api/state`** — merged-transcript speakers are re-keyed on Identity at
  build time (`parse_wav_speaker_ident` for recorded, `a.identity` for live,
  the Roster for new sessions); it ships the resolved per-session name map
  plus the registry, joined from every session's Roster and the live
  identities (a live identity counts as present in the meeting).
- **Security (CLAUDE.md).** Identities reaching any filename round-trip
  through `safe_name`; `person_id` is server-generated; `/api/people` body
  `person_id` / `identity` are validated against the known registry before
  touching a path.

## Rejected

A future change proposing display-name keys, the truncated slug, or a
copy-out registry should consult these:

- **Join on display name** (old status quo): circular (must name first) and
  fragile — a typo splits one person, name reuse ("Mike") fuses two.
- **Visibility-only fix** (read-only identity-keyed registry, naming stays
  per-session): the mapping must persist and propagate to be worth anything.
- **Copy-out source of truth** (fan names into every session's stored
  aliases): stores the name N times; diverges the first time a session is
  absorbed, re-transcribed, or hand-edited.
- **Truncated `[:10]` slug as the key** (no Roster): neither unique
  (collisions fuse two people, unrecoverable) nor complete (live vs recorded
  split), precisely in the SpatialChat-across-meetings case that motivates
  the feature.
- **One-way merge**: merge is the only information-losing op; without the
  cheap undo (detach) every misclick is permanent registry corruption.
- **Two-panel UI with flipped roles**: keeps a per-session panel whose
  content swaps on selection — reads as "the registry changes per session".
- **Backfill existing aliases** (BF2): near-zero existing naming, and a
  slug-keyed import would duplicate identity-keyed Persons and demand merges.
  Revisit only for a deployment with substantial accumulated naming.
