# Stages — the meeting's life as a guided journey

## The lens

A TapScribe session is not a flat collection of features — it is a thing that
**moves through processing states**. Audio arrives, gets captured live, lands as
WAV recordings, those recordings get tuned + transcribed, and the merged
transcript gets reviewed. Some configuration (per-mic speaker profiles, dual
language) outlives any single session.

So the dashboard is organized as an **ordered journey** down a slim left
**spine**. Each stage is a stop on the journey that carries a live **status
chip** (what state it is in, what it needs):

1. **Capture** — live taps streaming in: level, lag, speech gate, rec/live
   toggles, the language each is transcribed as, and *diarization shown inline
   as a property of the room tap*. Status: `2 live`.
2. **Recordings** — the WAV clips this session produced; pick a clip, see its
   **waveform with strip-silence cut points that re-cut live** as you drag the
   knobs. Status: `4 clips need tuning`.
3. **Transcript** — the merged, **dense line-oriented** transcript
   (`time · speaker · text`), the engine/model picker (by family, backend
   chips, Canary source→target), speaking-time summary, low-confidence +
   suppressed-hallucination audit. Status: `1 suppressed`.
4. **People** — the cross-session roster: per-**mic** profiles (gate, noise
   floor) reused across sessions, primary+secondary language with a quick
   "transcribe as" switch. Status: `4 profiles`.

You work IN one stage at a time. The spine is always visible so you always know
*where the session is* and *what each stage still needs* — but only ONE rich,
logically-grouped workspace fills the canvas at once.

## Why this is "not too much at once / logically grouped"

- **One focus, always.** The canvas shows exactly one stage. The other three
  stages collapse to a single line each on the spine (icon + label + status
  chip). The user sees one workspace plus a compact map of the rest — never the
  firehose of every feature at once.
- **Dense within, calm between.** Inside a stage we pack information tightly:
  Capture is a tight table of taps with inline sparkline + gate + meters;
  Recordings puts the clip list, the live waveform, and the knobs in three
  clearly-separated panels. But *between* stages there is hard separation — you
  cross a boundary to change concern, you don't scroll past it.
- **Grouping follows the data's own lifecycle**, which is the most honest
  grouping available: the four stages are literally the four states a
  recording passes through (or, for People, the config that spans them). That
  is stronger than an arbitrary tab bar because the grouping is *causal*.
- **The journey carries progress.** Each chip answers "what does this stage
  need from me?" (`4 clips need tuning`, `1 suppressed`). A faint progress
  fill on the spine shows how far the session has advanced. This is the bit
  plain tabs can't do.

## How it differs from plain top-tabs

Plain tabs are an unordered, stateless set of equal peers — you click whichever
you like and they never tell you anything about each other. Stages is different
on three axes:

1. **Order is meaningful.** Capture → Recordings → Transcript is the real
   processing order; the spine reads top-to-bottom as a pipeline, with a "next
   step" affordance ("Tune 4 clips →") that *advances* you, not just navigates.
2. **Each stop is stateful.** A tab label is static text; a stage chip is live
   (`2 live`, `1 suppressed`, `needs tuning`) and the spine shows a progress
   fill, so the spine doubles as a session-status readout.
3. **It models a journey, not a menu.** The empty/late stages (e.g. a session
   with no transcript yet) read as "not reached yet," which a tab bar can't
   express. Picking a *different session* re-seeds the whole journey.

## Variations considered

- **(A) Vertical spine + single workspace (CHOSEN).** Slim left rail lists the
  four ordered stages with live chips + progress fill; the rest of the canvas
  is the one active stage. Strongest fit for "one focus + compact map of the
  rest," reads unmistakably as a pipeline (top→bottom), and the rail is narrow
  so the workspace stays wide and dense. Session picker sits at the top of the
  spine so switching sessions re-seeds the journey.

- **(B) Horizontal stepper / wizard ribbon.** Stages as a breadcrumb stepper
  across the top (① ② ③ ④ with connectors), workspace below. Reads as a journey
  too, but a top ribbon competes with the in-stage header for the same band,
  the status chips get cramped, and it risks looking like the rejected
  "top-tabs + sub-tabs" paradigm. Rejected.

- **(C) Card-deck / kanban of stage cards.** Four big stage cards on a board,
  click one to zoom it full-bleed. Pretty, but the board view *is* the
  everything-at-once wall we're told to avoid, and zoom-in/zoom-out adds a
  navigation layer without adding clarity. Rejected.

Chosen: **(A)**. The vertical spine is the clearest "you are here on a journey"
device, keeps a permanent compact map of the other stages without showing their
contents, and leaves a wide canvas for one dense, well-grouped workspace.

## Theme

Dark, calm "control surface" palette (deep slate, one warm amber accent for the
active stage + the live pulse, per-speaker palette slots for identity). Mono for
numbers/timecodes, system-ui for prose. Distinct from a light document concept
and from a neon live-ops board: this is a quiet, instrument-panel dark with a
single accent doing the "where am I / what's live" work.
