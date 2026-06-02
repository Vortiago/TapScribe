# Stages — the meeting's life as a guided journey

## The lens

A TapScribe session is not a flat collection of features — it is a thing that
**moves through processing states**. Audio arrives and is captured live; those
recordings get tuned + transcribed into one merged transcript; and some
configuration (per-mic speaker profiles, dual language) outlives any single
session.

So the dashboard is organized as an **ordered journey** down a slim left
**spine**. Each stage is a stop that carries a live **status chip** (what state
it is in, what it needs). You work IN one stage at a time; the spine is always
visible so you always know *where the session is* and *what each stage still
needs* — but only ONE rich, logically-grouped workspace fills the canvas.

## Taps — the GLOBAL ingress (above the journey, not part of it)

A **Tap** (a Bridge's `/tap` connection) and its settings are **global**: they
persist across sessions. The session is just *where the captured audio lands* —
the taps are "always there." So **Taps** is pinned at the **top of the spine**
as a distinct, **un-numbered**, always-on entry, set apart from the numbered
journey by a small live indicator and a `session` divider, and it does **not**
contribute to the session's progress fill.

The **Taps view** is the dense, single-focus place to see and configure every
tap: each row carries its identity + device, a live level meter, lag, gate
state, rec/live toggles, the language it is transcribed as, and — the **key
control** — a **single-person vs multi-person** switch:

- **Single** — one speaker on this tap (Atle, Mette, James).
- **Multi** — diarize the tap into multiple speakers. The **Oslo Conference
  Room** is multi → **Speaker A (nb, 58%)** / **Speaker B (en, 42%)**, each with
  its language and talk-share shown right under the tap.

Incoming taps (a Bridge mid-handshake) appear here too, so you can set their
mode the moment they connect. This is the go-to place to turn diarization on or
off for a room.

## The three journey stages (per-session)

1. **Capture** — now **session-scoped**: this session's live recording. It keeps
   the **live captions** IRC feed (with diarized A/B attribution *visible*),
   **Capture health** (gates / recording / lag / languages), and **Capture
   settings** (recording, prompt, hotwords, hallucination rules). The per-tap
   *configuration* (single/multi, gate, the heavy knobs) has moved to **Taps**;
   Capture shows only a **lighter read-only "taps feeding this session"**
   reference (name + level + which are recording) with a `configure →` jump to
   Taps. Diarization stays **visible** in Capture but is **configured** in Taps.
   Status: `2 live` (or `no taps yet` for a fresh session).
2. **Transcript** — the merged stage, and the heart of the tool. The
   **dense IRC merged transcript dominates the canvas** as the single primary
   focus; recordings + waveform + strip-silence tuning are folded in as a
   **secondary side panel**; the engine controls live in a **compact header
   popover**. Status: `1 suppressed` / `3 to tune` / `not run`.
3. **People** — the cross-session roster: per-**mic** profiles (gate, noise
   floor) reused across sessions, primary+secondary language with a quick
   "transcribe as" switch. Status: `4 profiles`.

A **New session** button sits right under the session picker in the spine
header. It drops you into a fresh, empty journey at Capture ("no taps yet"),
with the later stages reading "nothing yet / not run". Because taps are global,
the **Taps** entry stays pinned and live even for a brand-new session.

## What changed in this iteration (and why)

This is a refinement of the well-liked Stages direction — same spine, same dark
dense aesthetic, same one-focus-at-a-time flow.

### Latest: Taps split out as a GLOBAL view; Capture trimmed to session scope

Taps and their settings are **global** (they persist across sessions), so they
no longer belong inside a single session's Capture stage:

- **Taps** is added as a **pinned, un-numbered** spine entry *above* the session
  block (small live indicator + a `session` divider). It does not count toward
  the session progress fill. Its dense view is the **single place to configure
  single-person vs multi-person** (diarization) per tap, plus language and the
  rec/live + gate readout. The Oslo room is **multi → Speaker A / Speaker B**.
- **Capture** is trimmed to **this session**: live captions, health, and capture
  settings stay, but the per-tap *configuration* moved to Taps. Capture now shows
  only a **read-only "taps feeding this session"** reference and a `configure →`
  jump. Diarized A/B attribution stays **visible** in Capture; it's just
  **configured** in Taps.

The earlier refinements below still hold.

### The transcript (and live captions) are true IRC

The earlier merged transcript was columnar — time, speaker and text in three
wide columns with big gutters — which read "message-like" and airy. Both the
merged transcript and the Capture live-caption feed now share **one tight IRC
renderer** (`ircLine`): one line per utterance, monospace, `[m:ss] Speaker:
text`, the speaker in their own colour, minimal gutters, rows packed tight.
The treatments ride on the same row: low-confidence is dimmed + dashed
underline + a small conf% chip; suppressed hallucinations are struck-through +
dim with a `⨯ rule` chip; a translation badge reads `nb→en`. A compact
speaking-time bar sits on top of the transcript, and a collapsible filter audit
folds in at the bottom.

### Recordings folded into Transcript (4 stages → 3)

Recordings and Transcript used to be two co-equal stages. They are now **one
Transcript stage** where the transcript is unambiguously the primary focus and
recordings are a *contextual* secondary:

- The "Recordings & tuning" side panel shows the per-WAV clip list.
- **Selecting a clip reveals** its waveform (with strip-silence cut markers)
  and the gap / floor / pad knobs — the marquee **live re-cut** still shines
  (`computeRegions` re-runs on every drag; the clip count + kept-speech update
  live). Deselecting collapses it back to the list.
- The **engine controls** (model-by-family, backend chips with **cuda
  disabled**, Canary source→target language selects) live in a **compact
  popover** opened from an "⚙️ Engine" button in the stage header — not a large
  co-equal panel.

This is the crucial discipline: density with **one clear primary focus** and
**contextual disclosure**, never an everything-at-once wall.

### The advance-CTAs are gone

The journey-gate buttons ("Tune the recordings →", "Run the transcript →",
"Review people →") are removed. **The spine is the navigation** — click any
stage to go there. Genuine actions remain, but styled as ordinary actions
(`.act`), not forced next-step gates: **Re-run / Transcribe**, strip-silence
**re-cut / reset**, and **New session**.

### New session + empty states

A prominent **New session** button, plus a real fresh/empty state across all
three stages (Capture: "no taps yet"; Transcript: "nothing to transcribe yet";
the recordings panel: "no recordings yet"). Taps stays pinned and live — global
ingress is independent of any one session.

## Why this is "not too much at once / logically grouped"

- **One focus, always.** The canvas shows exactly one view (global Taps, or one
  journey stage); the rest collapse to a single line each on the spine (icon +
  label + live chip). On the Transcript stage, the transcript is the dominant
  panel and everything else is secondary or disclosed on demand.
- **Dense within, calm between.** Inside a view we pack tightly (the IRC log, the
  taps cards); *between* views there is hard separation — you cross a boundary to
  change concern.
- **Grouping follows the data's own lifecycle.** Capture → Transcript → People
  is the real order a recording passes through (or, for People, the config that
  spans sessions); Taps sits *outside* that order because it's global, always-on
  ingress. That causal grouping is stronger than an arbitrary tab bar.
- **The journey carries progress.** Each chip answers "what does this stage
  need from me?"; a faint progress fill on the spine shows how far the session
  has advanced. Picking a *different session* (or New session) re-seeds it.

## How it differs from plain top-tabs

Plain tabs are an unordered, stateless set of equal peers. Stages is ordered
(the spine reads top-to-bottom as the real pipeline), each stop is stateful
(live chips + a progress fill, so the spine doubles as a session readout), and
it models a journey, not a menu — empty/late stages read as "not reached yet,"
which a tab bar can't express.

## Variations considered

- **(A) Vertical spine + single workspace (CHOSEN).** Slim left rail lists the
  ordered stages with live chips + progress fill; the rest of the canvas is the
  one active stage. Strongest fit for "one focus + compact map of the rest,"
  reads unmistakably as a pipeline, and the rail stays narrow so the workspace
  is wide and dense.
- **(B) Horizontal stepper / wizard ribbon.** A top ribbon competes with the
  in-stage header for the same band, the chips get cramped, and it risks the
  rejected "top-tabs + sub-tabs" look. Rejected.
- **(C) Card-deck / kanban of stage cards.** The board view *is* the
  everything-at-once wall we're told to avoid. Rejected.

## Theme

Dark, calm "control surface" palette (deep slate, one warm amber accent for the
active stage + the live pulse, per-speaker palette slots for identity). Mono for
the IRC log, numbers and timecodes; system-ui for prose. A quiet,
instrument-panel dark with a single accent doing the "where am I / what's live"
work.
