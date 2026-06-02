# Columns — progressive drill-down (Miller / macOS-Finder columns)

## The lens
Navigate TapScribe's data **left → right by drilling in**. Each column holds
exactly **one entity type** (a clean logical group). You select an item in a
column and its children appear in the column to its right. Only **2–3 columns**
are ever visible at once; the **rightmost column is the rich detail/work area**
where density lives. A breadcrumb across the top shows the current path; the
whole thing is keyboard-navigable (←/→ move between columns, ↑/↓ within one).

This is the natural shape of TapScribe's domain, which is a strict containment
tree:

```
ROOTS ─┬─ Live ─────────── tap ──────── live captions / live waveform
       ├─ Sessions ─────── session ──── speaker (in session) ─┬─ clip (waveform + strip-silence)
       │                                                       └─ transcript (merged, dense lines)
       ├─ Speakers ─────── speaker ───── per-mic profile + languages + sessions seen
       └─ Engines ──────── family ────── model (Canary → source/target selects)
```

A **diarized room tap is just a column item that drills into its sub-speakers**
(Speaker A / Speaker B) — diarization is a *property of the tap*, surfaced as
"this item has children", never a separate screen. That falls straight out of
the columns metaphor for free.

## Why it honors "not too much at once / logically grouped"
- **You only ever see the current path, never everything.** The firehose is
  structurally impossible: at most three columns, and the two navigation
  columns are deliberately thin, scannable lists. The whole live-ops board /
  every-pipeline-stage approach is rejected by construction.
- **Each column is ONE concern** with an obvious vertical rule between it and
  its neighbour — grouping is the layout itself, not a styling afterthought.
- **Dense within a group, calm between groups.** The nav columns are compact
  (one line per item, level meter + lag inline) but the eye rests on the single
  highlighted item; the detail column is information-rich (full waveform with
  live re-cut, dense transcript) but it is the *only* rich thing on screen.
- **Progressive disclosure is the interaction, not a toggle.** You earn detail
  by drilling; you never get a wall of forms.

## Three variations considered

### A. Strict equal-width Finder columns (3 panes, all the same width)
Classic macOS Finder. Pure and predictable. **Rejected as the primary:** the
detail content (waveform + knobs, dense transcript) wants to *breathe wider*
than a 1/3 slice, and forcing it into an equal column either truncates it or
makes the nav columns wastefully wide. It also wastes horizontal space when the
path is shallow.

### B. Accordion columns / Miller "spring-loaded" (only 1 column expands)
Columns collapse to thin spines when not focused; clicking one springs it open
and pushes the others to spines. **Rejected:** clever but fiddly — the user
loses the parent context they just clicked through, which is exactly the
"where am I" calm the breadcrumb is supposed to give. Too much motion for a
"calm between groups" brief.

### C. **Weighted drill columns — CHOSEN.** A fixed-width **Roots rail** on the
far left (the 4 entry points: Live / Sessions / Speakers / Engines), then **two
fluid columns**: a narrow **navigator** (the list you're drilling) and a wide
**detail/work pane** (the rightmost, always the rich area). As you drill
deeper, the navigator column shows the *parent list* and the detail pane shows
the *selected leaf*; a breadcrumb keeps the full path. This keeps nav lists
genuinely scannable AND gives detail the width it needs — best of A without its
truncation, without B's disorienting motion. Roots rail is always visible so
switching concern (Live ↔ Sessions ↔ Speakers ↔ Engines) is one click and the
mental model is obvious.

## Layout (variation C)
```
┌──────────────────────────────── breadcrumb: Sessions › Nordic Sync › Atle › Clip ──┐
├──────────┬───────────────────┬──────────────────────────────────────────────────────┤
│ ROOTS    │  NAVIGATOR        │  DETAIL / WORK PANE (dense)                            │
│ rail     │  (one entity type)│  (rightmost = rich)                                   │
│          │                   │                                                       │
│ ● Live   │  ▸ Atle           │  ┌ waveform + strip-silence cut preview ───────────┐ │
│ ○ Sess.  │  ▸ Mette          │  │ marquee re-cuts live as knobs drag             │ │
│ ○ Speak. │  ▸ Oslo Room ▸    │  └────────────────────────────────────────────────┘ │
│ ○ Engine │  ▸ James          │  knobs: silence gap · edge pad · speech floor        │
│          │                   │  clip listing · transcript toggle                    │
└──────────┴───────────────────┴──────────────────────────────────────────────────────┘
```

## Feature → location map (all 9 reachable, none crammed together)
1. **Live taps (level+lag+gate+rec/live)** — Roots▸Live navigator: each tap one
   dense row with inline level meter, lag, gate dot, rec/live pills.
2. **Live captions by speaker+language** — drill a live tap → detail pane shows
   its live caption stream + live waveform; diarized room tap splits to A/B.
3. **Sessions + detail** — Roots▸Sessions navigator → session detail header.
4. **Backend chips (cuda disabled) + model-by-family + Canary src/tgt** —
   Roots▸Engines: families as navigator, model detail pane with backend chips
   and Canary's source/target selects.
5. **Waveform + strip-silence live re-cut (marquee)** — Sessions › session ›
   speaker › **Clip** detail: canvas waveform, cut regions, 3 knobs that
   recompute regions live via `computeRegions`.
6. **Diarization as a property of a tap** — the Oslo Room item (Live AND in a
   session) carries a ▸ and drills into Speaker A (nb) / Speaker B (en).
7. **Cross-session per-mic profiles** — Roots▸Speakers › speaker detail: mic
   card (gate threshold, noise floor) + "reused across N sessions" badge.
8. **Primary+secondary language + quick switch** — same speaker detail: lang
   chips with a "transcribe as EN / NB / DA" quick-switch row.
9. **Dense line-oriented merged transcript** — Sessions › session ›
   **Transcript** detail: speaking-time summary bar + `t · speaker · text`
   lines, low-confidence muted, suppressed/audit row, nb→en translation badge;
   per-WAV/clip listing lives in the speaker→clips navigator.
