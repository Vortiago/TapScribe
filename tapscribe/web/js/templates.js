// @ts-check
// TapScribe's template/render seam over the vendored vanilla-web canon.
//
// The canon lives in ./lib/ as provenance-stamped, copy-verbatim files
// (templates.js — the .html seam + view-lifecycle helpers; render.js —
// interaction-safe re-rendering; chrome.js — page-chrome wiring). Never edit
// those in place; re-copy from the toolkit to update (see CLAUDE.md →
// "Frontend toolkit vendoring + gates"). This file re-exports them and layers
// the TapScribe-only pieces on top:
//
//   - renderList — the KEYED-LIST dual of renderRegion (rows updated in place,
//     never swapped). Canon reconcileList is not re-exported, so this is the
//     only door to it.
//   - the ADR-0004 interaction hold, as ONE mechanism for every render shape:
//     a held render marks the tick-retry flag (markDeferredRender /
//     consumeDeferredRender) and re-derives on the next poll pass. The region
//     gate below, every renderList deferral, and the bespoke gates behind the
//     IN-PLACE updaters (active-taps.js, live-feed.js, the live-log dialog)
//     plus sessions.js's cold search-mode swap all land that way. ADR-0016 has
//     the why; the short version is that only a REGION could ever self-flush (a
//     keyed list re-derives from live state rather than replaying a captured
//     build, and an in-place updater has no build closure to replay at all), so
//     a second mechanism bought one shape a sub-tick head start at the price of
//     two independent hold registries per host.
//   - the region gate itself: this seam owns the per-host render signature AND
//     all three interaction holds; canon renderRegion performs the swap. The
//     sig is read BEFORE the holds — see `renderRegion` for why that ordering
//     is what makes one mechanism affordable (#245).
//   - interactionHeld() — the document-wide hold predicate the poll pacer
//     uses to keep the /api/state cadence fast while the operator works.
//   - the dev/test-only sig-drift audit (__TAPSCRIBE_SIG_AUDIT).
//   - renderMarkdown — the safe, textContent-only markdown subset for LLM
//     summaries.

export { loadTemplates, tpl, slot, pick, mount, loadCSS, every, withPending } from "./lib/templates.js";
export { withTransition } from "./lib/render.js";
export { wireTheme, wireErrorBar } from "./lib/chrome.js";

// Canon `reconcileList` is deliberately NOT re-exported: `renderList` below is
// the only door to it, so a keyed list cannot be added un-held (the same
// gated-by-construction stance ADR-0008 takes for the tap-bearer scheme). This
// module is the sole importer of ./lib/render.js, so the raw reconcile is
// genuinely unreachable from a view rather than merely discouraged. If a
// user-initiated reorder ever needs the ungated call, re-export it then — with
// a note saying why it isn't a polled render.
// Canon `markRegionStale` is not imported either: this seam passes NO `sig` to
// canon renderRegion (it owns the gate), so canon's own per-host sig is never
// written and forgetting it would be a no-op on an empty map.
import {
  renderRegion as canonRenderRegion,
  selectionInside as canonSelectionInside,
  reconcileList as canonReconcileList,
} from "./lib/render.js";

// A per-tick render can be DEFERRED (skipped without advancing its signature
// gate) to protect operator interaction state — ADR-0004 "Interaction hold".
// main.js's tick() short-circuits its whole renderAll pass when /api/state's
// fetchState() returns the identical cached object (a 304), since there is
// nothing new to show — but a fetch going quiet does NOT mean an interaction
// hold has cleared. Without this flag, a render a BESPOKE gate held back while
// a selection was live would never get retried once the server stopped
// changing, stranding it even after the operator released the selection.
// EVERY hold in this module marks it — the region gate, every renderList
// deferral, and the in-place gates (via deferIfSelectionInside below). That is
// the whole retry mechanism; there is no second one (ADR-0016). main.js consumes
// it right before a retry so a render that lands this pass doesn't force another
// retry next tick.
let _deferredRender = false;

/** Mark that a render was skipped to protect operator interaction state and
 * must be retried once it ends. */
export function markDeferredRender() {
  _deferredRender = true;
}

/** Read and clear the deferred-render flag in one step, so a caller can tell
 * whether a retry is owed without racing a render that sets it again. */
export function consumeDeferredRender() {
  const had = _deferredRender;
  _deferredRender = false;
  return had;
}

/**
 * Canon `selectionInside`, WIDENED. The canon tests only the selection's
 * anchor/focus nodes, so a range that starts BEFORE `host` and ends AFTER it —
 * ⌘A over a panel, a drag from the header past the last card — reports false
 * and the region is rebuilt out of the MIDDLE of the operator's selection
 * mid-copy: exactly the bug ADR-0004 names, just approached from outside.
 * `intersectsNode` covers that. Canon's endpoint test still runs FIRST — it is
 * the cheap path, and it is the only one that answers at all where `Range` is
 * missing (the node tests' fake documents), though everything it catches the
 * widened test would catch too. Every app
 * consumer imports the predicate from this module (active-taps.js /
 * live-feed.js / sessions.js's search branch via deferIfSelectionInside, the
 * live-log dialog directly, every keyed list via renderList, and the region gate
 * via `_holdInside`), so widening it here covers all of them at once.
 * @param {Element} host
 */
export function selectionInside(host) {
  if (canonSelectionInside(host)) return true;
  return _selectionIntersects(host);
}

/** True when ANY live range intersects `host` — which INCLUDES a selection
 * wholly inside it, not only one straddling it. That is deliberate but easy to
 * misread, and the comment here used to claim the opposite ("the delta canon
 * can't see: a range that contains `host` outright"): `Range.intersectsNode`
 * compares boundary points, so a range nested inside `host` intersects it too,
 * making this a strict SUPERSET of canon's endpoint test rather than a disjoint
 * delta. Two consequences worth stating, because the old wording hid both:
 * canon's selection branch is unreachable from this seam, and a caller cannot
 * use this to ask "straddling only".
 * @param {Element} host */
function _selectionIntersects(host) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  for (let i = 0; i < sel.rangeCount; i++) {
    const range = sel.getRangeAt(i);
    // Feature-guarded: the node tests' fake documents have no Range API.
    if (typeof range?.intersectsNode === "function" && range.intersectsNode(host)) return true;
  }
  return false;
}

/**
 * True for elements that hold live interaction state: the form controls, plus
 * a focused `[data-cfg-key]` SAVE BUTTON. A button is not a control canon
 * renderRegion would ever hold for — but while a config editor's putJson is in
 * flight focus sits on its save button, and swapping the region then detaches
 * the status span the awaiting save writes to. Folding it in HERE (rather than
 * leaving each region to remember a guard of its own before its renderRegion
 * call — the shape that made `[data-cfg-key]` a hold two callers applied and
 * every future one would forget) means the seam holds the swap automatically,
 * and `interactionHeld()` reports a mid-save button to the poll pacer too.
 *
 * The EDITABLE-STATE half of the pair below: what a WRITE could clobber.
 * @param {Element} el
 */
function _isInteractive(el) {
  const tag = el.tagName;
  if (tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA") return true;
  const html = /** @type {HTMLElement} */ (el);
  return html.dataset?.cfgKey != null || html.isContentEditable === true;
}

/**
 * The wider FOCUSABLE predicate: anything a keyboard can land on, including the
 * scripted controls the dashboard builds (`role="button"`, any non-negative
 * `tabindex`). `tabindex="-1"` is excluded — programmatically focusable, not
 * keyboard-reachable.
 *
 * TWO PREDICATES, one distinction: removing a node destroys focus on anything
 * focusable, so the REMOVAL hold uses this one; an in-place write only threatens
 * editable state, so the WRITE holds (renderRegion's, renderList's per-row) use
 * `_isInteractive`. `interactionHeld()` is the odd one out and takes the WIDE
 * predicate on purpose — its own doc says why (the pacer must not back off while
 * ANY render is owed, including one the removal hold is sitting on).
 * Conflating them breaks click-to-select
 * (a `role="button"` row handle holds its own row's update); omitting this one
 * dumps a keyboard operator to the document root when a re-key rebuilds their row.
 * @param {Element} el
 */
function _isFocusable(el) {
  if (_isInteractive(el)) return true;
  if (el.getAttribute?.("role") === "button") return true;
  const tabindex = el.getAttribute?.("tabindex");
  return tabindex != null && tabindex !== "-1";
}

/**
 * True while the operator is holding ANY live interaction anywhere in the
 * document — an interactive control (select/input/textarea/contenteditable) is
 * focused, or a non-collapsed text selection is held. The document-wide sibling
 * of renderRegion's per-host focus/selection guard, exported for the poll pacer
 * (`next/main.js`): the /api/state backoff must NOT engage while an interaction
 * is held, or a render a bespoke gate held back could land a whole backoff
 * interval after release instead of on the next tick (ADR-0004). Keeps "what
 * counts as an interaction" defined once, here with the hold.
 */
export function interactionHeld() {
  const active = document.activeElement;
  // `_isFocusable`, the WIDER predicate: the pacer must not back off while any
  // render is owed, and the removal hold fires on focusable-not-editable controls
  // (a row's `role="button"` handle). Reporting false there let the poll back off
  // to 2s with a held render outstanding — the delay ADR-0004's pacer clause
  // exists to prevent.
  if (active && active !== document.body && _isFocusable(active)) return true;
  const sel = document.getSelection();
  return !!sel && !sel.isCollapsed && sel.rangeCount > 0;
}

/**
 * `selectionInside(host)` plus marking the deferred-render flag in one step —
 * the shape every per-tick gate needs that renders neither a region nor a keyed
 * list (the in-place updaters in active-taps.js / live-feed.js, sessions.js's
 * cold search-mode swap), so a new one has one call to make, not two to
 * remember. `renderList` calls it for keyed lists, so a keyed list never needs
 * to. Returns true when the render must be held back.
 * @param {Element} host
 */
export function deferIfSelectionInside(host) {
  if (!selectionInside(host)) return false;
  markDeferredRender();
  return true;
}

/**
 * `deferIfSelectionInside`'s complete sibling: hold when a control inside `host`
 * is focused OR a selection is inside it, marking the tick-retry either way.
 *
 * `deferIfSelectionInside` covers HALF the interaction hold. That is enough for
 * an in-place text updater, whose writes threaten a selection but not a focused
 * control. It is NOT enough for a RAW SWAP, which detaches everything under the
 * host including whatever holds focus — and the dashboard has one: sessions.js's
 * cold search-mode swap. That path guarded selection only, so a focused rename
 * input was destroyed mid-keystroke whenever the filtered set emptied.
 *
 * Reach for this at any per-tick gate that replaces a host's children outright;
 * reach for `deferIfSelectionInside` only where the write cannot detach a
 * focused node.
 * @param {Element} host
 */
export function deferIfInteractionInside(host) {
  if (!_focusedInside(host, _isFocusable) && !selectionInside(host)) return false;
  markDeferredRender();
  return true;
}

/** True while a control INSIDE `host` holds focus. `isMatch` selects WHICH holds
 * this answers for: the default editable-state test gates WRITES (renderRegion's
 * hold, renderList's per-row hold); `_isFocusable` gates REMOVAL. One containment
 * rule, two predicates.
 * @param {Element} host @param {(el: Element) => boolean} [isMatch] */
function _focusedInside(host, isMatch = _isInteractive) {
  const active = document.activeElement;
  return !!active && active !== document.body && host.contains(active) && isMatch(active);
}

// ── The region gate (seam-owned) ────────────────────────────────────────────

/** Per-host remembered REGION signature — the gate `renderRegion` reads and
 * advances. It lives HERE, not in canon, for the ordering reason spelled out on
 * `renderRegion`: the seam must be able to answer "is a render even owed?"
 * BEFORE it decides whether to hold, and canon checks its sig only AFTER its own
 * guards. Advanced solely when the swap actually happened — ADR-0004: a skip
 * must never advance a gate. @type {WeakMap<Element, string>} */
const _regionSig = new WeakMap();

/** The region's FULL interaction hold: every state a whole-region swap would
 * destroy. One predicate, three terms, matching ADR-0004's three named bugs —
 * a focused control (the dropdown that snaps shut), an open popover/`<dialog>`
 * (destroyed mid-use), a text selection touching the host (dissolved mid-copy).
 *
 * Uses the EDITABLE predicate, not the focusable one: a region swap is a write,
 * and `_isFocusable` is the removal hold's (see `_isInteractive`'s doc).
 * @param {Element} host */
function _holdInside(host) {
  if (_focusedInside(host)) return true;
  if (host.querySelector(":popover-open, dialog[open]")) return true;
  return selectionInside(host);
}

// Re-runs `build` into a detached probe and compares to what the region
// currently shows; a mismatch means `sig` is missing a dependency the render
// reads, so the region would silently go stale. Records to
// globalThis.__TAPSCRIBE_SIG_DRIFT (a throw would be swallowed by the
// dashboard's event-handler try/catch, so we don't rely on it) and
// console.errors loudly.
/**
 * @param {Element} host
 * @param {() => Node} build
 * @param {string} sig
 */
function _auditSigCoversOutput(host, build, sig) {
  const probe = /** @type {Element} */ (document.createElement(host.tagName || "div"));
  probe.replaceChildren(build());
  _recordDrift(
    "region", sig, probe.innerHTML, host.innerHTML,
    `renderRegion sig drift: output changed but sig ${JSON.stringify(sig)} did not — the build ` +
      `closure reads a value missing from its sig, so this region will silently go stale. Add that ` +
      `value to the sig, OR render the derived bit in place (a sibling toggled per-tick) instead of ` +
      `through a sig-gated region.`,
  );
}

/**
 * Render a **region** — a host swapped WHOLE — under the interaction hold.
 * `renderList` is the keyed dual; CONTEXT.md → "Region · keyed list" has the
 * distinction. This seam owns the gate and the holds; canon renderRegion
 * (lib/render.js) performs the swap.
 *
 * The rules, in the order they run, because THE ORDER IS THE DESIGN:
 *  1. `sig` unchanged → return. Nothing is owed, so nothing is held and no
 *     retry is marked. This MUST come first: an operator's caret parked in a
 *     region with nothing changing server-side would otherwise mark the retry
 *     flag on every tick, defeating main.js's 304 short-circuit and re-running
 *     the whole renderAll pass forever (#245). `renderList` orders its own gates
 *     the same way and for the same reason.
 *  2. An interaction hold inside `host` (`_holdInside`) → defer: mark the
 *     tick-retry and leave `sig` UNADVANCED, so the next poll pass re-derives
 *     and re-offers the render. ADR-0004's "defer, never destroy"; ADR-0016 for
 *     why the tick is the only retry mechanism.
 *  3. Swap, then advance `sig`.
 *
 * Canon is handed NO `sig` and no `force`, so it never gates and never records a
 * signature of its own. Its three guards re-evaluate as provably false here —
 * its `_isInteractive` is a subset of this module's, its endpoint-based
 * `selectionInside` a subset of the widened one, its overlay selector identical
 * — so it never defers, never populates its `_pendingFlush`, and always swaps.
 * That is what keeps ONE hold registry in the app: two of them, on the same
 * host, is how a canon-side deferral could flush a build that went stale while
 * this seam absorbed the newer ticks (ADR-0016 records the sequence).
 *
 * There is no `force`: it would bypass the guards, which is never what a caller
 * wants (`markRegionStale` is the "rebuild next time, THROUGH the guards" verb).
 *
 * The audit: when __TAPSCRIBE_SIG_AUDIT is set and a call is about to sig-skip
 * (same sig, no interaction hold inside the host), the build is probed against
 * the live DOM and any divergence is recorded to __TAPSCRIBE_SIG_DRIFT.
 * @param {Element} host
 * @param {() => Node} build
 * @param {{ sig?: string }} [opts]
 */
export function renderRegion(host, build, opts = {}) {
  const { sig } = opts;
  if (sig !== undefined && _regionSig.get(host) === sig) {
    if (globalThis.__TAPSCRIBE_SIG_AUDIT && !_holdInside(host)) {
      _auditSigCoversOutput(host, build, sig);
    }
    return;
  }
  if (_holdInside(host)) {
    markDeferredRender();
    return;
  }
  canonRenderRegion(host, build);
  if (sig !== undefined) _regionSig.set(host, sig);
}

/**
 * Invalidate `host`'s remembered render signature so the NEXT `renderRegion`
 * call re-renders even though its `sig` is unchanged — for the out-of-band
 * changes a sig cannot see (a lazy body landed, a mutate just changed what
 * `build()` would produce). The rebuild still arrives THROUGH the interaction
 * hold, which is the whole point: it is the "defer, don't force" reset, not an
 * escape hatch (ADR-0004).
 *
 * The keyed-list twin is `markListStale`.
 * @param {Element} host
 */
export function markRegionStale(host) {
  _regionSig.delete(host);
}

// ── Keyed lists (the reconcile dual of renderRegion) ────────────────────────
//
// `renderRegion` holds a region that is SWAPPED whole. A **keyed list** is the
// other shape: rows keyed and updated in place, never swapped (canon
// `reconcileList`). Both need the interaction hold, and before this seam existed
// all three keyed lists — the Recordings WAV list, the Transcript per-WAV
// picker, and the Sessions rows — hand-rolled it, each with its own copy of the
// same rules and its own `gate-allow` suppression. `sessions.js` got one of them
// wrong: it advanced a row's signature ABOVE the focus guards, which stranded
// the skipped update forever (the next tick recomputed the identical sig and
// early-returned), so a focused row kept a stale label and a later keystroke
// persisted the stale value back over an external rename.
//
// TWO OPTIONAL GATES, because a call site uses whichever change detector it
// actually has:
//   - `sig` — a list-level stamp. Pays only when a CHEAP aggregate already
//     exists: the WAV lists have `files_sig` (computed server-side, one string
//     in /api/state), so a quiet tick skips the reconcile walk entirely.
//   - `itemSig` — a per-row stamp. What you use when no aggregate exists:
//     `sessions.js`'s rows each change independently (label, bytes, tx status,
//     progress) and no server digest covers them, so BUILDING a list-level sig
//     would itself be the O(rows) walk the gate is meant to skip.
// Neither is required; a call site may use one, both, or neither.

/** Per-host remembered LIST signature. Advanced ONLY when the reconcile ran and
 * no row was held — ADR-0004: a skip must never advance a gate.
 * @type {WeakMap<Element, string>} */
const _listSig = new WeakMap();

/** Everything the seam remembers about one row, in ONE record with ONE write
 * point (`_remember`): the reconcile `key`, the last `sig` its `update` ran for,
 * and the `item` it was last rendered from (what the removal hold re-inserts).
 * Three parallel WeakMaps written at four sites under three rules let the item
 * fall behind the sig; one record makes that impossible. Kept off the DOM rather
 * than in `data-` attributes so a row's markup, and anything asserting on it,
 * stays untouched.
 * @typedef {{ key: string, sig: string | undefined, item: any }} RowState
 * @type {WeakMap<Element, RowState>} */
const _rowState = new WeakMap();

/** @param {Element} node @param {string} key @param {any} item @param {string | undefined} sig */
function _remember(node, key, item, sig) {
  _rowState.set(node, { key, item, sig });
}

/** Rows currently retained by a removal hold, so one `focusout` is armed per row
 * rather than one per tick. @type {WeakSet<Element>} */
const _removalHeld = new WeakSet();

/** A retained row drops out the moment focus leaves it: invalidate the host's
 * list signature and ask for a retry, so the NEXT tick reconciles it away.
 *
 * It only INVALIDATES — nothing captured is replayed, and the tick re-derives
 * from live state. Without it the hold would have to keep `sig` unadvanced to
 * guarantee a later reconcile, which costs a full O(rows) rebuild every tick for
 * as long as the focus lasts.
 * @param {Element} row @param {Element} host */
function _armRemovalFlush(row, host) {
  if (_removalHeld.has(row)) return;
  _removalHeld.add(row);
  const controller = new AbortController();
  row.addEventListener("focusout", (e) => {
    const to = /** @type {Node | null} */ (/** @type {FocusEvent} */ (e).relatedTarget);
    if (to && row.contains(to)) return; // moved to a sibling control inside the row
    _removalHeld.delete(row);
    controller.abort();
    markListStale(host);
    markDeferredRender();
  }, { signal: controller.signal }); // gate-allow: signal-listener — armed only while the row is retained; the same controller detaches it on flush
}

/** Resolve `renderList`'s `items`, which may be a thunk so a caller can keep an
 * O(rows) build behind the gates.
 * @template T @param {T[] | (() => T[])} items @returns {T[]} */
function _resolveItems(items) {
  return typeof items === "function" ? items() : items;
}

/** The DIRECT CHILD of `host` containing the focused control, or null. Rows are
 * host children, so this is "which row is the operator in". Passes `_isFocusable`
 * to the ONE containment helper rather than restating it: this feeds the REMOVAL
 * hold, and removal destroys focus on anything focusable, not just on things with
 * a value to lose. Keeping the containment rule in one place is why the predicate
 * is a parameter — the write holds and the removal hold must not drift apart.
 * @param {Element} host @returns {Element | null} */
function _focusedRow(host) {
  if (!_focusedInside(host, _isFocusable)) return null;
  let row = /** @type {Element} */ (document.activeElement);
  while (row.parentElement && row.parentElement !== host) row = row.parentElement;
  return row.parentElement === host ? row : null;
}

/**
 * List-level drift probe. On a `sig` SKIP the rows on screen must already be
 * what `items` would produce, so compare row COUNT plus each row's remembered
 * key and item signature IN ORDER — no detached list build needed. A mismatch
 * means `sig` is missing a value the list reads, so the whole list silently goes
 * stale. Catches the case a row probe cannot: a term missing from `sig` itself.
 * @param {Element} host @param {any[]} items @param {any} opts @param {string} sig
 */
/** The dev-only audit's one write point: count the probe by KIND and record any
 * divergence. Three probes were each spelling this out, which is why adding the
 * census touched six lines. Per-kind counts (not one total) because a test can
 * only claim "no drift" for a kind whose probe actually RAN, and a single total
 * was satisfied by whichever probe happened to fire.
 * @param {"region" | "list" | "row"} kind @param {string} sig
 * @param {string} expected @param {string} actual @param {string} message */
function _recordDrift(kind, sig, expected, actual, message) {
  const census = (globalThis.__TAPSCRIBE_SIG_PROBES ||= { region: 0, list: 0, row: 0 });
  census[kind]++;
  if (expected === actual) return;
  (globalThis.__TAPSCRIBE_SIG_DRIFT ||= []).push({ sig, expected, actual });
  console.error(message);
}

/**
 * List-level drift probe. On a `sig` SKIP the rows on screen must already be
 * what `items` would produce, so compare row COUNT plus each row's remembered
 * key and item signature IN ORDER — no detached list build needed. A mismatch
 * means `sig` is missing a value the list reads, so the whole list silently goes
 * stale. Catches the case a row probe cannot: a term missing from `sig` itself.
 * @param {Element} host @param {any[]} items @param {any} opts @param {string} sig
 */
function _auditListSigCoversRows(host, items, opts, sig) {
  const expected = items.map((it) => `${opts.key(it)}§${opts.itemSig ? opts.itemSig(it) : ""}`);
  const actual = [...host.children].map((n) => {
    const st = _rowState.get(n);
    return `${st ? st.key : "?"}§${st && st.sig !== undefined ? st.sig : ""}`;
  });
  // Joined, not arrays: __TAPSCRIBE_SIG_DRIFT is one record shape shared with
  // renderRegion's probe (types.d.ts), and one row per line reads fine.
  _recordDrift(
    "list", sig, expected.join("\n"), actual.join("\n"),
    `renderList sig drift: the row set changed but sig ${JSON.stringify(sig)} did not — the list ` +
      `reads a value missing from its sig, so it will silently go stale. Add that value to the sig.`,
  );
}

/**
 * Row-level drift probe: rebuild the row from scratch and diff it against the
 * live one, so an `update` that writes a value missing from `itemSig` is caught.
 *
 * ON by default; a caller OPTS OUT with `auditRows: false` and a reason, the same
 * shape as the gate-allow suppressions — so a new keyed list is audited unless
 * its author says why it can't be, rather than unaudited unless they know the
 * flag exists. It is only SOUND for rows nothing mutates out of band: the
 * Recordings WAV rows are `<details>` whose bodies lazy-load a transcript on
 * expand, so a fresh probe legitimately differs from an expanded row and would
 * report drift that isn't there. Like renderRegion's probe, this re-runs
 * `create`, so a `create` with side effects pays them once per audited skip —
 * dev/test only.
 * @param {Element} node @param {any} item @param {any} opts @param {string} sig
 */
function _auditItemSigCoversRow(node, item, opts, sig) {
  const probe = opts.create(item);
  if (opts.update) opts.update(probe, item);
  // outerHTML on BOTH sides: every adapter's `update` writes the row ROOT's own
  // class (is-sel, is-focused), which innerHTML excludes — so an innerHTML diff
  // could never catch the drift this probe exists for, and recording innerHTML
  // while comparing outerHTML reported two byte-identical strings for exactly the
  // drift the comparison had just caught.
  _recordDrift(
    "row", sig, probe.outerHTML, node.outerHTML,
    `renderList item sig drift: row output changed but itemSig ${JSON.stringify(sig)} did not — ` +
      `update() reads a value missing from itemSig, so this row will silently go stale.`,
  );
}

/**
 * Render a **keyed list** into `host` under the interaction hold. The keyed dual
 * of `renderRegion`: rows are created once, matched by key, and updated in
 * place; the host's children belong to this seam alone, so a placeholder must be
 * a SIBLING of `host`, never swapped into it (see recordings.html / views.html /
 * sessions.html).
 *
 * The rules, in order — every one of them a rule a call site used to re-derive:
 *  1. `sig` unchanged → return without touching the DOM.
 *  2. A text selection inside or straddling `host` → defer (mark the tick-retry
 *     flag) and leave `sig` UNADVANCED, so the held render keeps showing the
 *     previous state intact instead of half-applying.
 *  3. A row holding a focused control whose key is absent from `items` is
 *     RETAINED: its item is spliced back at its current position, so the row (and
 *     the focus in it) survives while every other row still updates. Canon
 *     `reconcileList` removes every key not present, and CONTEXT.md defines the
 *     interaction hold as never destroying interaction state, so removal is the
 *     case it most has to cover. The retained row arms a one-shot `focusout` that
 *     invalidates the list, so it drops the moment focus leaves. Note the
 *     rendered row set may therefore EXCEED `items` by that one row while the
 *     hold lasts — a caller narrating the list (a count, a placeholder) is
 *     describing `items`, not necessarily what is on screen.
 *  4. Reconcile. Per row: a focused control inside the row holds that row's
 *     `update` (defer, and do NOT stamp its `itemSig`); otherwise an unchanged
 *     `itemSig` skips it; otherwise update and stamp. The hold is deliberately
 *     COARSER than a per-control guard — one focused control freezes its whole
 *     row for a tick — which is consistent with renderRegion holding a whole
 *     region, and means a control added to a row later is covered for free.
 *  5. Advance `sig` only if no row was held. A retained row (rule 3) does NOT
 *     count as held: everything the caller asked for was rendered, and the
 *     outstanding removal is owned by that row's focusout, so blocking the sig
 *     would re-run a full O(rows) reconcile every tick until focus left.
 *
 * A held render lands via the tick-retry (`markDeferredRender` →
 * `consumeDeferredRender` in next/main.js) — the same one mechanism `renderRegion`
 * uses, and the reason lists are the shape that settled it: a list is driven by
 * materialized `items`, so the view must re-derive on the next pass no matter
 * what, and a captured replay would only serve stale rows sooner (ADR-0016).
 * (A retained row's focusout, rule 3, only INVALIDATES — it replays nothing.)
 *
 * `create` builds a row's shell and `update` fills it; the seam runs `update` on
 * a freshly created row too, so a `create` need not call its own filler.
 *
 * @template T
 * @param {Element} host
 * @param {T[] | (() => T[])} items — pass a THUNK when building the list is
 *   itself O(rows) (flattening a file listing into row models, mapping wrappers).
 *   Rule 1 skips before the thunk is called, so a quiet tick costs nothing; an
 *   eagerly-built array is paid on every tick whether or not it gets used, which
 *   is the render-signature-hygiene footgun in a new place. Only the (dev-only)
 *   audit resolves it on the skip path. A plain array is fine when it is already
 *   in hand.
 * @param {{
 *   key: (item: T) => string,
 *   create: (item: T) => Element,
 *   update?: (node: Element, item: T) => void,
 *   itemSig?: (item: T) => string,
 *   sig?: string,
 *   auditRows?: boolean,
 * }} opts — `auditRows: false` opts out of the row probe; say why at the call site.
 * @returns {boolean} whether the rows were reconciled on this call — false for
 *   every skip and every deferral, so a caller can gate sibling work (a
 *   placeholder's visibility) on the render having actually happened.
 */
export function renderList(host, items, opts) {
  const { key, create, update, itemSig, sig } = opts;

  if (sig !== undefined && _listSig.get(host) === sig) {
    if (globalThis.__TAPSCRIBE_SIG_AUDIT && !_holdInside(host)) {
      _auditListSigCoversRows(host, _resolveItems(items), opts, sig);
    }
    return false;
  }

  if (deferIfSelectionInside(host)) return false;

  // Past the gates — NOW build the list (see the `items` param docs: a thunk is
  // how a caller avoids paying O(rows) for a tick that skips).
  let list = _resolveItems(items);
  // True once any row's render was held back, so rule 5 leaves `sig` unadvanced.
  let held = false;

  // Canon calls `keyOf` exactly once per item, so memoising is only worth it when
  // something ELSE also walks the keys — i.e. the removal scan below, which needs
  // a focused row. On every other tick a memo would replace N cheap calls with N
  // misses + N calls + N inserts, so `keyFor` stays `key` itself until the scan
  // proves it is needed.
  /** @type {Map<T, string> | null} */
  let keyed = null;
  /** @type {(item: T) => string} */
  let keyFor = key;

  // Removal hold (rule 3). Only costs anything when a row actually holds focus.
  //
  // Splice the held row back in at its current DOM position rather than skipping
  // the whole render: an unreleased focus would otherwise freeze the list
  // forever. Rule 4's hold is one row; this one must be too.
  //
  // It does NOT block `sig`. Everything the caller asked for was rendered; only
  // the removal is outstanding, and that waits on focus. Blocking the sig instead
  // re-ran the full thunk + O(rows) reconcile every tick for as long as focus
  // stayed. The row's focusout invalidates the list, so the removal lands on
  // release and costs nothing until then.
  const focused = _focusedRow(host);
  if (focused) {
    // The memo is not (only) an optimisation: `keyed.set(state.item, state.key)`
    // below PINS the retained row under the key it was reconciled with, and that
    // pin is load-bearing. `key` may close over per-tick view state — sessions.js
    // folds `archived.length > 1` into it — so re-deriving would hand canon a
    // DIFFERENT key, which fails to match the existing node and creates a
    // replacement, destroying the very focus the hold exists to protect. Never
    // remove this as dead weight.
    keyed = new Map();
    keyFor = (item) => {
      let k = /** @type {Map<T, string>} */ (keyed).get(item);
      if (k === undefined) /** @type {Map<T, string>} */ (keyed).set(item, (k = key(item)));
      return k;
    };
    const state = _rowState.get(focused);
    if (state && !list.some((it) => keyFor(it) === state.key)) {
      // Its current DOM position, so re-inserting doesn't make the row jump.
      let at = 0;
      for (let n = focused.previousElementSibling; n; n = n.previousElementSibling) at++;
      list = list.slice();
      list.splice(at, 0, state.item);
      /** @type {Map<T, string>} */ (keyed).set(state.item, state.key);
      _armRemovalFlush(focused, host);
    }
  }

  canonReconcileList(
    host,
    list,
    keyFor,
    (item) => {
      const node = create(item);
      if (update) update(node, item);
      _remember(node, keyFor(item), item, itemSig ? itemSig(item) : undefined);
      return node;
    },
    // Order is load-bearing twice over: the sig check comes FIRST, so a row with
    // nothing to write never marks a retry (an idle caret otherwise defeats
    // main.js's 304 short-circuit for as long as it sits there — #245); and the
    // row's record is written only AFTER `update` returns, so a throwing `update`
    // leaves the gate unadvanced and the next tick retries (`pick` throws on a
    // missing slot, and poll-path exceptions are swallowed).
    (node, item) => {
      const state = _rowState.get(node);
      /** @type {string | undefined} */
      let stamp;
      if (itemSig) {
        stamp = itemSig(item);
        if (state && state.sig === stamp) {
          if (globalThis.__TAPSCRIBE_SIG_AUDIT && opts.auditRows !== false) {
            _auditItemSigCoversRow(node, item, opts, stamp);
          }
          return;
        }
      }
      // `focused &&` first: when no row holds focus at all, _focusedRow already
      // told us so, and this would otherwise re-ask the document once per row.
      if (focused && _focusedInside(node)) {
        // This row HAS a pending write and holds focus. Marking the flag is what
        // earns it a retry at all: main.js skips the whole render pass on a 304,
        // and the poll goes quiet exactly while the operator types.
        held = true;
        markDeferredRender();
        return;
      }
      if (update) update(node, item);
      _remember(node, state ? state.key : keyFor(item), item, stamp);
    },
  );

  if (sig !== undefined && !held) _listSig.set(host, sig);
  return true;
}

/**
 * Invalidate `host`'s remembered LIST signature so the next `renderList`
 * reconciles even if its `sig` is unchanged — the keyed-list twin of
 * `markRegionStale`, and the "defer, don't force" reset: it does NOT bypass the
 * interaction guards the way a force flag would. Reach for it after a mutate
 * whose effect the sig might not name.
 * @param {Element} host
 */
export function markListStale(host) {
  _listSig.delete(host);
}

// ── Markdown (LLM summaries) ────────────────────────────────────────────────

/**
 * Inline markdown spans → nodes: `` `code` ``, `**bold**`, `*italic*`.
 * Everything is appended as text nodes or textContent — never parsed as HTML —
 * so markup in the source text stays literal. No nesting (bold inside italic
 * etc.); LLM summaries don't need it and flat spans keep this auditable.
 * @param {string} text
 * @returns {DocumentFragment}
 */
function _inlineMd(text) {
  const frag = document.createDocumentFragment();
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)/g;
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    const el = document.createElement(m[1] ? "code" : m[2] ? "strong" : "em");
    const span = m[1] || m[2] || m[3] || "";
    el.textContent = m[2] ? span.slice(2, -2) : span.slice(1, -1);
    frag.append(el);
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

/**
 * Render LLM-emitted markdown into a DocumentFragment, safely. Summaries come
 * back from an external model (the Command source pipes an untrusted
 * transcript through a CLI tool), so the text is untrusted: every node here is
 * built via createElement/textContent — NEVER innerHTML — which makes script
 * injection impossible by construction (`<img onerror=…>` in the summary
 * renders as those literal characters). Deliberately a small subset, not a
 * markdown engine: `#`–`######` headings, `-`/`*` bullets, `1.` ordered items,
 * fenced code blocks, paragraphs, plus the `_inlineMd` spans. Anything else
 * stays literal text.
 * @param {string} text
 * @returns {DocumentFragment}
 */
export function renderMarkdown(text) {
  const root = document.createDocumentFragment();
  /** @type {HTMLElement | null} — the open <ul>/<ol>, so adjacent items share one list. */
  let list = null;
  /** @type {string[]} — accumulated paragraph lines (single newlines join, like markdown). */
  let para = [];
  /** @type {string[] | null} — lines inside an open ``` fence (verbatim, no inline spans). */
  let fence = null;

  const flushPara = () => {
    if (!para.length) return;
    const p = document.createElement("p");
    p.append(_inlineMd(para.join(" ")));
    root.append(p);
    para = [];
  };
  const flushFence = () => {
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = (fence || []).join("\n");
    pre.append(code);
    root.append(pre);
    fence = null;
  };

  for (const raw of String(text ?? "").split(/\r?\n/)) {
    if (fence) {
      if (raw.trim().startsWith("```")) flushFence();
      else fence.push(raw);
      continue;
    }
    const t = raw.trim();
    if (t.startsWith("```")) {
      flushPara();
      list = null;
      fence = [];
      continue;
    }
    if (!t) {
      flushPara();
      list = null;
      continue;
    }
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara();
      list = null;
      const el = document.createElement(`h${(h[1] || "#").length}`);
      el.append(_inlineMd(h[2] || ""));
      root.append(el);
      continue;
    }
    const item = t.match(/^[-*]\s+(.*)$/) || t.match(/^\d+[.)]\s+(.*)$/);
    if (item) {
      flushPara();
      const want = /^[-*]/.test(t) ? "UL" : "OL";
      if (!list || list.tagName !== want) {
        list = document.createElement(want === "UL" ? "ul" : "ol");
        root.append(list);
      }
      const li = document.createElement("li");
      li.append(_inlineMd(item[1] || ""));
      list.append(li);
      continue;
    }
    list = null;
    para.push(t);
  }
  flushPara();
  if (fence) flushFence(); // unterminated fence — still show what we got
  return root;
}
