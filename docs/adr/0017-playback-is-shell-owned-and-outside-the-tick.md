---
status: accepted
date: 2026-07-26
---

# Playback is shell-owned and lives outside the tick

The dashboard gained audio playback (#191). Every obvious home for an
`<audio>` element is a place the render architecture destroys it, so the
element's ownership is a decision rather than a detail.

There is exactly ONE **Player**: a single `<audio controls>` owned by the
shell and mounted outside `#viewRoot`, hidden until a WAV is loaded. Views
own only **play affordances** that hand it a **seek target**. It is driven by
media events (`play`/`pause`/`timeupdate`/`ended`/`error`), never by the
`/api/state` poll, so it sits outside the interaction hold and the render
signatures entirely.

Three structural facts forced this, none of them visible from the issue that
asked for the feature:

- A **region** swaps whole — an element inside `renderRegion` is detached
  whenever its `sig` changes, so a player in the merged transcript pane dies
  on every re-transcribe.
- A **keyed-list row** is rebuilt when its key changes, and `rowKey` folds
  size and the transcript stamp — so a player in a WAV row dies on a strip, a
  transcribe, or the tap closing.
- A **view host** is detached on stage navigation (`mount(root, built.host)`),
  and removing a media element from a `Document` pauses it — so a per-view
  player stops the moment the operator walks from Recordings to Transcript,
  which is precisely the walk the feature exists to support.

## Considered options

**One player per view**, mounted in each view's chrome. Less machinery and
each view stays self-contained, but it duplicates the transport, permits two
players at once, silently stops playback on every stage switch, and repeats
the hold reasoning in two views with two different render shapes around it.

**A custom transport** over the vendored `vc/` components, which would match
the dashboard's sharp visual language. Rejected on the project's founding
rule: use the platform. Native `<audio controls>` ships scrubbing, keyboard
access, screen-reader labelling and media-key integration correctly, all of
which are easy to half-build. The cost accepted is that the bar looks faintly
foreign next to `next.css`, since Chrome's control internals aren't ours to
style.

**Mapping every segment onto the original WAV's timeline** instead of playing
the file the words came from. `abs_start` is wall-clock and clips carry their
own `wav_start`, so this is exact for *owned* clips — but a clip whose owner
was deleted is deliberately displayed under a non-owner original
(`build_session_files`'s fallback bucket), so the projection is silently wrong
for orphans. A seek target therefore names an exact file: the one
`source_wav` records.

**The waveform as display only**, letting the native scrub bar be the sole
seek surface. Defensible and cheaper, but click-to-seek on a visible waveform
is the most natural gesture in an audio tool, so the canvas is a control
surface too — with a **strict-identity playhead** that appears only when the
loaded file *is* the displayed file, rather than a projected position that
would be a confident lie for orphaned clips.

## Consequences

- New shell chrome: a docked bar in `next.html` needs a layout row and must
  not collide with the floating taps-rail button.
- The playhead is a transform-driven DOM element over the canvas, updated by
  `requestAnimationFrame` while playing — never a canvas repaint, so the
  O(bins) peaks draw stays behind `lastWaveSig`.
- An **open WAV** is unplayable (its RIFF header is patched only at tap
  close), and the client had no way to know which WAV is open — `open_wavs`
  existed solely to mask `files_sig`. The listing now emits `open`, and
  because that mask flips exactly once when the tap closes, playability
  enables itself at the right moment with no new signature.
- Deleting the loaded WAV needs **explicit eviction**, not an error listener:
  the browser has the bytes buffered, so playback of a deleted recording
  otherwise continues to the end with no error at all. The media `error`
  event remains as the backstop for changes the dashboard didn't initiate,
  and pays for itself on truncated / zero-byte WAVs, which this system
  demonstrably leaves on disk.
- Per-line seek affordances reuse the timestamp node as a `<button>` and
  resolve the target at **click time**. Pre-disabling them would put
  `files_sig` into `txSig`, and a hand-maintained signature with a forgotten
  dependency goes stale invisibly (ADR-0004, ADR-0016).
