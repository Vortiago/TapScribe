---
status: accepted (amends ADR-0004)
date: 2026-07-26
---

# One hold registry, one retry: the seam owns the region gate

ADR-0004 established the **Interaction hold** and named `renderRegion`
as the seam that applies it. This amends *how* that seam is built. The
decision in 0004 stands unchanged; nothing here weakens "defer, never
destroy" or "never advance the gate on a skip".

The dashboard vendors the vanilla-web canon copy-verbatim under
`web/js/lib/` and wraps it in `web/js/templates.js`. Canon
`renderRegion` applies the same three holds and then **self-flushes**: a
deferred swap arms a per-host listener (`focusout`, the overlay's
`toggle`/`close` plus a MutationObserver, `selectionchange`) and lands
the captured build the instant the hold clears, with no next tick
required (canon's #42).

Two things made that mechanism a poor fit here, and neither was visible
in what the code claimed about itself.

**Canon's focus flush lands on the incoming focus.** Its `focusout`
listener re-enters `renderRegion`, where `document.activeElement` is
`<body>` mid-`focusout`, so every guard passes and the swap detaches
whatever was about to receive focus. Verified in headless Chromium
against canon directly: two inputs in one host, focus the first, defer a
render, Tab to the second — the host is rebuilt, an identity stamp on
its children is lost, and focus ends on `<body>`. `focusout`'s
`relatedTarget` *is* populated and names the incoming element, which is
the question the flush needs to ask. The seam had already taken the
focus branch over for this reason.

**Only a region can self-flush at all.** The other three shapes
structurally cannot: a keyed list (`renderList`) is driven by
materialized `items` and must re-derive from live state rather than
replay a captured build; the in-place updaters (`active-taps.js`,
`live-feed.js`, the live-log dialog) have no build closure to replay;
and a selection *straddling* a host has no event of its own to listen
for. All three already landed their held renders through the tick-retry
flag (`markDeferredRender` → `consumeDeferredRender` in `next/main.js`).

So the app was running two retry mechanisms, and the split did not fall
where the comments said it did. `_selectionStraddles` was documented as
covering only the straddle case canon misses, but it tests
`Range.intersectsNode(host)`, which is true for a selection *wholly
inside* the host as well — a strict superset of canon's endpoint test.
Canon's selection branch was therefore already unreachable from the
seam, and *no* selection deferral self-flushed. CLAUDE.md's "a deferred
swap flushes ITSELF the instant the hold clears" described a behaviour
only the focus path still had.

That left the two registries able to diverge on one host, which is a
real (if latent) hazard rather than an aesthetic one. Canon's
`_pendingFlush` could still be armed through its overlay branch. Once
armed it would freeze at that tick's build while the seam's focus hold
absorbed every newer tick; when the overlay closed, canon's flush would
re-enter canon, hit canon's buggy focus branch, re-arm on `focusout`,
and land the stale build on the next focus move — destroying focus and
showing superseded content. If the poll then went 304-quiet, nothing had
marked the tick-retry and the seam's held (fresh) build was stranded
waiting on a `focusout` that could no longer come. No `renderRegion`
host contains a popover or `<dialog>` today, so this is unreachable as
shipped; the first one added arms it.

## Decision

**One hold registry and one retry mechanism.** `web/js/templates.js`
owns the region gate outright:

- The **per-host render signature** lives in the seam, and is read
  **before** the holds. This ordering is the load-bearing part: a caret
  parked in a region with nothing changing server-side must not mark the
  tick-retry every tick, which would defeat `main.js`'s 304
  short-circuit and re-run `renderAll` forever (#245). `renderList`
  already ordered its gates this way; now both do.
- **All three holds** (focused editable control, open popover/`<dialog>`,
  text selection touching the host) are evaluated by one predicate,
  `_holdInside`.
- A hold **marks the tick-retry** and leaves the signature unadvanced.
  There is no second retry path.
- Canon `renderRegion` still performs the **swap**, called with no `sig`
  and no `force`. Its guards re-evaluate as provably false (its
  `_isInteractive` is a subset of the seam's, its endpoint-based
  `selectionInside` a subset of the widened one, its overlay selector
  identical), so it never defers, never populates `_pendingFlush`, and
  always swaps.
- `force` is gone from the seam's signature. It had no call sites;
  `markRegionStale` is the "rebuild next time, *through* the guards"
  verb.

The per-shape hold differences stay exactly as ADR-0004 set them: a
keyed list holds per row and on removal, never at list level (deferring
a whole list on one focused row is the freeze #312 removed), and the
in-place updaters hold on selection only, because an in-place text write
cannot detach a focused node. One *mechanism*, not one *policy*.

## Considered alternatives

**Pre-empt every canon branch but let canon keep the sig gate.**
Rejected: without owning the sig, the seam cannot tell whether a render
is owed before deciding to hold, so it must mark the retry on every held
tick and regress #245. The sig ownership is forced by the retry
consolidation, not chosen alongside it.

**Have the seam re-arm canon's listeners itself** (keeping instant
flush for all three holds). Rejected: it duplicates ~30 lines of canon
logic — listener arming, the MutationObserver for an overlay removed
without `close`, the abort bookkeeping — in the app copy. That is the
"unmarked fork" failure mode issue #199 was filed about, reintroduced
deliberately.

**Own the swap too** (`replaceChildren` in the seam). Rejected: it makes
`templates.js` a second full `renderRegion` implementation beside the
vendored one, re-exposes the advance-on-skip trap ADR-0004 names, needs
a `raw-swap` gate suppression, and maximises what has to be deleted if
the upstream fix lands. Delegating the swap keeps the vendoring real.

**Patch `web/js/lib/render.js` in place.** Rejected: copy-verbatim
vendoring (CLAUDE.md → "Frontend toolkit vendoring + gates"); a forked
copy is the one state the drift gate treats as an error.

## Consequences

- A held region swap now lands on the **next poll pass** rather than the
  instant the hold clears. Bounded by one tick: `interactionHeld()`
  keeps the pacer at its fast (~500 ms) cadence while any interaction is
  held (ADR-0013's poll is always running), and the retry flag defeats
  the 304 short-circuit. Renders triggered outside the poll (view
  switch, `refresh()`, catalog load) defer safely for the same reason —
  they all re-enter through `renderAll`.
- **Do not re-add self-flush "for #42".** Canon's rationale is a quiet
  SSE stream stranding stale DOM. This app polls (ADR-0013) and retries
  through a flag, so the failure #42 prevents cannot occur here.
- Canon's `renderRegion` deferral machinery (`_deferSwap`,
  `_flushRegion`, `_pendingFlush`, its `_regionSig`) is dead weight in
  the vendored copy. That is the accepted price of copy-verbatim; a
  re-vendor refreshes it and nothing re-wires it, because the seam is
  the only importer of `lib/render.js`.
- The fix belongs upstream: canon's `focusout` flush should consult
  `relatedTarget`, and the hold should be exposed as a predicate with an
  injectable deferral strategy, so a polled consumer needn't own a
  second copy of the decision. Filed against the Verktoykasse toolkit.
  If it lands, this seam gets *thinner*, not rewritten.
