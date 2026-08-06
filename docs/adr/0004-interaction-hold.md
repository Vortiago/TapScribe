---
status: accepted
date: 2026-06-04
---

# Interaction hold: per-tick renders defer to operator interaction state

The Stages dashboard re-renders its regions on every ~500 ms `/api/state`
poll, and a DOM swap (or in-place `textContent` rewrite) destroys whatever
transient interaction state the operator holds inside the touched nodes.
Each kind was an operator-reported bug:

- a focused `<select>` snapped shut mid-pick;
- a text selection dissolved mid-copy;
- an opened `<details>` snapped shut every time the live child logged a line;
- tail-follow scroll yanked back to the bottom mid-read.

## Decision

One rule, the **Interaction hold** (CONTEXT.md): a per-tick render that
would destroy operator interaction state inside its region is **deferred,
not performed**, skipping **without advancing the render gate** (signature
memo) so the held render lands on the first tick after the interaction
clears. Updates are delayed by the operator's own interaction, never lost.

The hold lives at shared seams, not per view:

- `renderRegion` (`web/js/templates.js`) — the per-tick region primitive —
  applies all three holds (focused control, open popover/`<dialog>`, text
  selection touching the host); every region rendered through it gets them
  for free.
- `renderList` (same module) — the keyed-list dual (rows matched by key,
  updated in place) — adds two holds a swapped region has no equivalent
  for: the **per-row hold** (a focused row keeps its own update held,
  coarse like a region holding whole) and the **removal hold** (a focused
  row whose key left the incoming items is retained in place, dropped on a
  one-shot `focusout` — removal is the most destructive case; deferring
  the whole list instead froze it for the duration of the focus). Removal
  destroys focus on anything **focusable**, while an in-place write only
  threatens **editable** state, so the removal hold takes the wider
  predicate and the write the narrower. Canon `reconcileList` is not
  re-exported, so a keyed list cannot be rendered un-held.
- A raw swap needs the hold explicitly: `deferIfInteractionInside` (focus
  **or** selection) for a gate replacing a host's children outright;
  `deferIfSelectionInside` (over the exported `selectionInside(host)`)
  only where the write cannot detach a focused node — the in-place
  updaters (`active-taps.js`, `live-feed.js`, the live log dialog) and the
  one cold mode switch that raw-swaps into a keyed list's host
  (`sessions.js`'s cross-session transcript search).
- Tail-follow scrolling is sticky (`wasAtBottom` read before the rewrite),
  never unconditional.
- The poll pacer must not back off while a hold is outstanding:
  `interactionHeld()` reports document-wide interaction state so a held
  render lands on the next fast tick, not a backoff interval later.
- Render-signature hygiene (CLAUDE.md) is the hold's complement: a
  volatile value must not share a signature with an O(content) region, or
  the region rebuilds so often transient state can't survive between holds.

## Considered alternatives

- **DOM-diffing library (morphdom / lit-html).** Cannot preserve a
  selection whose text node genuinely changed — exactly when regions
  rebuild — and the dashboard is deliberately vanilla no-build JS.
- **Capture-and-restore.** Fragile precisely when the text changed, a
  silently *moved* selection corrupts what the operator copies, and each
  state kind needs its own restore.
- **Pause the poll while the operator interacts.** Freezes every panel
  globally for one local interaction; the operator loses live awareness
  (tap levels, captions) because they selected a line of text.
- **Per-view bespoke guards.** Drifted in practice (allow-lists,
  focus-only guards missing selections); one primitive plus one exported
  predicate replaced all of them. No carve-out remains — the People
  editor renders through `renderRegion` too.

## Consequences

- A region can be momentarily stale while the operator interacts with it —
  by design, bounded by the interaction itself.
- **The trap**: advancing the render gate on a skipped render loses the
  update forever (the next tick sees an "unchanged" sig). Any new hold
  must defer without advancing; the appliers listed in CLAUDE.md are the
  reference implementations.
- New per-tick regions inherit the hold by rendering through
  `renderRegion` — enforced by the focus-clobber sweep
  (`test_next_poll_render_does_not_clobber_open_controls`) and
  `test_live_log_dialog_refresh_preserves_text_selection`.
- **Amended by ADR-0016** (mechanism only; the rule is unchanged):
  `renderRegion` checks its per-host signature **before** the holds — a
  region with nothing to render must not mark a retry, or an idle focused
  control re-runs `renderAll` every tick — and a held render lands via the
  tick-retry only; the seam no longer reaches canon's listener-based
  self-flush.
