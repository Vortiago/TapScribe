---
status: accepted
date: 2026-07-26
---

# Playback is shell-owned and lives outside the tick

Dashboard audio playback (#191). Every obvious home for an `<audio>` element
is a place the render architecture destroys it, so ownership is a decision,
not a detail.

There is exactly ONE **Player**: a single `<audio controls>` owned by the
shell, mounted outside `#viewRoot`, hidden until a WAV is loaded. Views own
only **play affordances** that hand it a **seek target** (CONTEXT.md:
Player · seek target · open WAV · playhead). It is driven by media events
(`play`/`pause`/`timeupdate`/`ended`/`error`), never by the `/api/state`
poll, so it sits outside the interaction hold and the render signatures
entirely. Three structural facts force this:

- A **region** swaps whole — a player in the merged transcript pane dies on
  every re-transcribe.
- A **keyed-list row** is rebuilt when its key changes, and `rowKey` folds
  size and the transcript stamp — a player in a WAV row dies on a strip, a
  transcribe, or the tap closing.
- A **view host** is detached on stage navigation, and removing a media
  element from a `Document` pauses it — a per-view player stops on the walk
  from Recordings to Transcript, precisely the walk the feature supports.

Rejected:

- **One player per view**: duplicates the transport, permits two players at
  once, silently stops playback on every stage switch, repeats the hold
  reasoning under two render shapes.
- **A custom transport over the vendored `vc/` components**: rejected on the
  founding rule — use the platform. Native `<audio controls>` ships
  scrubbing, keyboard access, screen-reader labelling and media-key
  integration correctly. Accepted cost: the bar looks faintly foreign next
  to `next.css`.
- **Mapping segments onto the original WAV's timeline** instead of playing
  the file the words came from: exact for *owned* clips, but a clip whose
  owner was deleted is deliberately displayed under a non-owner original
  (`build_session_files`'s fallback bucket), so the projection is silently
  wrong for orphans. A seek target names the exact file `source_wav`
  records.
- **The waveform as display only**: click-to-seek on a visible waveform is
  the most natural gesture in an audio tool, so the canvas is a control
  surface too — with a **strict-identity playhead** shown only when the
  loaded file *is* the displayed file (a projected position is a confident
  lie for orphaned clips).

## Consequences

- The Player's docked bar is shell chrome in `next.html` — its layout row
  must not collide with the floating taps-rail button.
- The playhead is a transform-driven DOM element over the canvas, updated by
  `requestAnimationFrame` while playing — never a canvas repaint, so the
  O(bins) peaks draw stays behind `lastWaveSig`.
- An **open WAV** is unplayable (its RIFF header is patched only at tap
  close). The listing emits `open`, and because that mask flips exactly once
  when the tap closes, playability enables itself at the right moment with
  no new signature.
- Deleting the loaded WAV needs **explicit eviction**, not an error
  listener: the browser has the bytes buffered, so playback of a deleted
  recording otherwise continues to the end with no error at all. The media
  `error` event remains as the backstop for changes the dashboard didn't
  initiate, and pays for itself on truncated / zero-byte WAVs.
- Per-line seek affordances reuse the timestamp node as a `<button>` and
  resolve the target at **click time**. Pre-disabling them would put
  `files_sig` into `txSig`, and a hand-maintained signature with a forgotten
  dependency goes stale invisibly (ADR-0004, ADR-0016).
