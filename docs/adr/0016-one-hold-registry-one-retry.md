---
status: accepted (amends ADR-0004)
date: 2026-07-26
---

# One hold registry, one retry: the seam owns the region gate

ADR-0004's **Interaction hold** stands unchanged; this amends *how* the
`renderRegion` seam applies it. The dashboard vendors the vanilla-web
canon copy-verbatim under `web/js/lib/` and wraps it in
`web/js/templates.js`. Canon `renderRegion` applies the same three holds
and then **self-flushes**: a deferred swap arms a per-host listener
(`focusout`, the overlay's `toggle`/`close` plus a MutationObserver,
`selectionchange`) and replays the captured build the instant the hold
clears (canon's #42). Two things make that a poor fit here:

**Canon's focus flush lands on the incoming focus.** Its `focusout`
listener re-enters `renderRegion` while `document.activeElement` is
`<body>`, so every guard passes and the swap detaches whatever was about
to receive focus. `focusout`'s `relatedTarget` names the incoming
element — the question the flush needs to ask; canon doesn't.

**Only a region can self-flush at all.** Of the four render shapes, only
a region has a captured build closure to replay. A keyed list
(`renderList`) is driven by materialized `items` and must re-derive from
live state (a captured replay would only serve stale rows sooner); the
in-place updaters (`active-taps.js`, `live-feed.js`, the live-log
dialog) have no build closure; and a selection *straddling* a host has
no event of its own to listen for. All three already landed held renders
through the tick-retry flag (`markDeferredRender` →
`consumeDeferredRender` in `next/main.js`).

Two retry mechanisms means two hold registries able to diverge on one
host — a real, if latent, hazard. The sequence: canon's `_pendingFlush`,
armed through its overlay branch, freezes at that tick's build while the
seam's focus hold absorbs every newer tick; on overlay close, canon's
flush hits its buggy focus branch, re-arms on `focusout`, and lands the
stale build on the next focus move — destroying focus, showing
superseded content — while a 304-quiet poll strands the seam's fresh
build waiting on a `focusout` that can no longer come. No `renderRegion`
host contains a popover or `<dialog>` today, so this is unreachable as
shipped; the first one added arms it.

## Decision

**One hold registry and one retry mechanism.** `web/js/templates.js`
owns the region gate outright:

- The **per-host render signature** lives in the seam and is read
  **before** the holds. The ordering is load-bearing: a caret parked in
  a region with nothing changing server-side must not mark the
  tick-retry every tick, which would defeat `main.js`'s 304
  short-circuit and re-run `renderAll` forever (#245). `renderList`
  orders its gates the same way.
- **All three holds** (focused editable control, open popover/`<dialog>`,
  text selection touching the host) are one predicate, `_holdInside`.
- A hold **marks the tick-retry** and leaves the signature unadvanced.
  There is no second retry path.
- Canon `renderRegion` still performs the **swap**, called with no `sig`
  and no `force`. Its guards re-evaluate as provably false (its
  `_isInteractive` is a subset of the seam's, its endpoint-based
  `selectionInside` a strict subset of the seam's widened test, its
  overlay selector identical), so it never defers, never populates
  `_pendingFlush`, and always swaps.
- `force` is gone from the seam's signature. It had no call sites;
  `markRegionStale` is the "rebuild next time, *through* the guards"
  verb.

Per-shape hold differences stay exactly as ADR-0004 sets them: a keyed
list holds per row and on removal, never at list level (deferring a
whole list on one focused row is the freeze #312 removed); the in-place
updaters hold on selection only, because an in-place text write cannot
detach a focused node. One *mechanism*, not one *policy*.

## Considered alternatives

- **Pre-empt every canon branch but let canon keep the sig gate.**
  Without owning the sig, the seam can't tell whether a render is owed
  before deciding to hold, so it must mark the retry on every held tick
  and regress #245.
- **Re-arm canon's listeners from the seam** (instant flush for all
  three holds). Duplicates ~30 lines of canon logic in the app copy —
  the "unmarked fork" failure mode of #199, reintroduced deliberately.
- **Own the swap too.** Makes `templates.js` a second full
  `renderRegion` beside the vendored one, re-exposes the advance-on-skip
  trap ADR-0004 names, needs a `raw-swap` gate suppression, and
  maximises what a future upstream fix must delete.
- **Patch `web/js/lib/render.js` in place.** Copy-verbatim vendoring
  (CLAUDE.md); a forked copy is the one state the drift gate treats as
  an error.

## Consequences

- A held region swap lands on the **next poll pass**, not the instant
  the hold clears — bounded by one tick: `interactionHeld()` keeps the
  pacer at its fast (~500 ms) cadence while any interaction is held
  (ADR-0013's poll is always running), and the retry flag defeats the
  304 short-circuit. Renders triggered outside the poll (view switch,
  `refresh()`, catalog load) defer safely too — they all re-enter
  through `renderAll`.
- **Do not re-add self-flush "for #42".** Canon's rationale is a quiet
  SSE stream stranding stale DOM; this app polls (ADR-0013) and retries
  through a flag, so that failure cannot occur here.
- Canon's deferral machinery (`_deferSwap`, `_flushRegion`,
  `_pendingFlush`, its `_regionSig`) is dead weight in the vendored
  copy — the accepted price of copy-verbatim; the seam is the only
  importer of `lib/render.js`, so a re-vendor re-wires nothing.
- The fix belongs upstream (filed against the Verktoykasse toolkit):
  consult `relatedTarget` in the `focusout` flush, and expose the hold
  as a predicate with an injectable deferral strategy. If it lands, this
  seam gets *thinner*, not rewritten.
