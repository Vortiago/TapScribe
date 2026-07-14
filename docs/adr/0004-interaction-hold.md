---
status: accepted
date: 2026-06-04
---

# Interaction hold: per-tick renders defer to operator interaction state

The Stages dashboard re-renders its regions on every ~500 ms
`/api/state` poll. A DOM swap (or an in-place `textContent` rewrite)
destroys whatever transient interaction state the operator holds inside
the touched nodes. Each kind produced a real operator-reported bug
before this rule existed:

- a focused `<select>` snapped shut mid-pick (dropdown clobbering);
- a text selection dissolved mid-copy ("it keeps flashing after I mark
  text" — the live log dialog, then four more sites on sweep);
- an opened init-prompt `<details>` snapped shut every time the live
  child logged a line;
- the log dialog yanked the scroll position back to the bottom each
  refresh while the operator read older lines.

## Decision

One rule, named the **Interaction hold** (see CONTEXT.md): a per-tick
render that would destroy operator interaction state inside its region
is **deferred, not performed** — and the deferral must **skip without
advancing the render gate** (signature variable, `lastSig`-style memo),
so the held-back render lands on the first tick after the interaction
clears. Updates are *delayed by the operator's own interaction*, never
lost.

The hold lives at shared seams, not per view:

- `renderRegion` (`web/js/templates.js`) — the per-tick region
  primitive — checks focused controls AND text selections before its
  per-host signature, so every region rendered through it gets the hold
  for free.
- `selectionInside(host)` (same module) — for updaters that mutate
  text/rows in place rather than swapping a region (`active-taps.js`,
  `live-feed.js`, the live log dialog) and for view-level render gates
  (`transcript.js` merged pane, `recordings.js` WAV list).
- Tail-follow scrolling is sticky (`wasAtBottom` read before the
  rewrite), never unconditional.
- Render-signature hygiene (CLAUDE.md) is the hold's complement: a
  volatile value (job tick, log tail) must not share a signature with
  an O(content) region, otherwise the region rebuilds so often that
  transient state (an open `<details>`) can't survive between holds.

## Considered alternatives

**DOM-diffing library (morphdom / lit-html).** Rejected: reduces node
churn but cannot preserve a selection whose text node genuinely changed
— which is exactly when per-tick regions rebuild — and the dashboard is
deliberately vanilla no-build JS (`type: module` files served as-is).

**Capture-and-restore (re-select / re-scroll after rebuild).** Rejected:
restoring a selection by offsets is fragile precisely when it matters
(the text changed underneath), and a silently *moved* selection corrupts
what the operator copies — worse than holding the update. Restores also
have to be re-implemented per state kind; the hold is one rule.

**Pause the poll while the operator interacts.** Rejected: freezes every
panel globally for one local interaction; the operator loses live
awareness (tap levels, captions) because they selected a line of text.

**Per-view bespoke guards (the status quo).** Rejected by experience:
the guards drifted (`editableIds` allow-lists, a cache-priming hack in
the Taps view, focus-only guards that missed selections). One primitive
plus one exported predicate replaced all of them.

## Consequences

- A region's data can be momentarily stale while the operator interacts
  with it. This is by design and bounded by the interaction itself —
  releasing focus/selection lets the next tick catch up.
- **The trap to avoid**: advancing the render gate on a skipped render
  loses the update forever (the next tick sees an "unchanged" sig). Any
  new hold must follow defer-without-advancing; the appliers listed in
  CLAUDE.md are the reference implementations.
- New per-tick regions inherit the hold by rendering through
  `renderRegion` — the convention CI enforces via the focus-clobber
  sweep (`test_next_poll_render_does_not_clobber_open_controls`) and
  the selection guard test
  (`test_live_log_dialog_refresh_preserves_text_selection`).
- The People editor has since adopted `renderRegion` too (`web/js/next/views/people.js`)
  — the exception this section originally left open is closed; there is no
  remaining bespoke-guard carve-out.
