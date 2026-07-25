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
//   - the ADR-0004 interaction-hold flag for the per-tick gates that render
//     neither a region nor a keyed list — the IN-PLACE updaters (active-taps.js,
//     live-feed.js, the live-log dialog) plus sessions.js's cold search-mode
//     swap: markDeferredRender / consumeDeferredRender / deferIfSelectionInside.
//     A renderRegion swap needs none of this — it flushes ITSELF the instant
//     the interaction clears (one-shot listener per host), tick or no tick;
//     the exceptions are the straddling-selection case below, which has no
//     listener of its own, and every renderList deferral (its rows come from
//     live state, so it re-derives on the next tick rather than replaying).
//   - the two interaction-hold guards the canon gets WRONG, pre-empted here
//     rather than patched in the vendored file: the focus hold (canon flushes
//     onto the incoming focus — see the "Focus hold" block) and the widened
//     `selectionInside` (canon only tests the selection's endpoints).
//   - interactionHeld() — the document-wide hold predicate the poll pacer
//     uses to keep the /api/state cadence fast while the operator works.
//   - the dev/test-only sig-drift audit (__TAPSCRIBE_SIG_AUDIT), wrapped
//     around canon renderRegion.
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
import {
  renderRegion as canonRenderRegion,
  markRegionStale as canonMarkRegionStale,
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
// The in-place gates mark this (via deferIfSelectionInside below), and so does
// every renderList deferral; canon renderRegion deferrals flush themselves the
// instant the hold clears and never need the tick-retry. main.js consumes it right before a retry so a
// render that lands this pass doesn't force another retry next tick.
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
 * Endpoint containment stays the fast path; `intersectsNode` covers the
 * straddle. Every app consumer imports the predicate from this module
 * (active-taps.js / live-feed.js / sessions.js's search branch via
 * deferIfSelectionInside, the live-log dialog directly, and every keyed list via
 * renderList), so widening it here covers all of them at once.
 * @param {Element} host
 */
export function selectionInside(host) {
  if (canonSelectionInside(host)) return true;
  return _selectionStraddles(host);
}

/** The DELTA the canon guard can't see: a range that contains `host` outright
 * (neither endpoint inside it). Split out so renderRegion can hold for exactly
 * that case without re-testing what the canon already handles.
 * @param {Element} host */
function _selectionStraddles(host) {
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
 * @param {Element} el
 */
function _isInteractive(el) {
  const tag = el.tagName;
  return (
    tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA" ||
    /** @type {HTMLElement} */ (el).dataset?.cfgKey != null ||
    /** @type {HTMLElement} */ (el).isContentEditable === true
  );
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
  if (active && active !== document.body && _isInteractive(active)) return true;
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

// ── Focus hold (seam-owned, pre-empts the canon's) ──────────────────────────
//
// Canon renderRegion defers a swap while a control inside the host is focused
// and arms a ONE-SHOT `focusout` listener to flush it. That listener re-enters
// renderRegion — but during `focusout` `document.activeElement` is <body>, so
// the re-checked guard sees NO hold and the swap lands ON TOP of the INCOMING
// focus. Verified in headless Chromium: two inputs in one host, focus the
// first, defer, Tab to the second → the host is rebuilt and focus is lost
// entirely. It bites hardest on live-channel.js's body, which holds the model
// <select>, the language input, the gate-kind <select>, four number inputs and
// the init-prompt textarea in ONE host: an operator tabbing between gate knobs
// mid-transition loses un-Applied edits.
//
// `focusout`'s `relatedTarget` IS populated (unlike activeElement) and names
// the element RECEIVING focus, so it answers the question the flush actually
// needs to ask. The canon can't be patched here (it's a copy-verbatim vendored
// file — CLAUDE.md), so the seam takes the focus branch over entirely and
// leaves the canon its overlay/selection/sig branches: when we delegate below,
// no control inside the host is focused, so the canon's focus branch is never
// reached. Drop this block once the fix lands upstream in vanilla-web.

/** @typedef {{ build: () => Node, sig: string | undefined, controller: AbortController }} HeldSwap */
/** One entry per host with a swap held back by focus — latest-wins, exactly
 * like the canon's `_pendingFlush`: a repeat skip on an already-held host
 * replaces `build`/`sig` in place and keeps the SAME controller, so one
 * listener is armed per host, never appended. @type {WeakMap<Element, HeldSwap>} */
const _focusHeld = new WeakMap();

/** @param {Element} host — true while a control INSIDE `host` holds focus. */
function _focusedInside(host) {
  const active = document.activeElement;
  return !!active && active !== document.body && host.contains(active) && _isInteractive(active);
}

/** Stash the latest skipped build for `host` and, if nothing is armed yet,
 * attach the listener that flushes it once focus leaves the host FOR REAL.
 * Not `once`, so focus moving BETWEEN controls inside the host re-arms for
 * free rather than needing a second registration.
 * @param {Element} host @param {() => Node} build @param {string | undefined} sig */
function _holdForFocus(host, build, sig) {
  const held = _focusHeld.get(host);
  if (held) { held.build = build; held.sig = sig; return; }
  const controller = new AbortController();
  _focusHeld.set(host, { build, sig, controller });
  host.addEventListener("focusout", (e) => {
    const to = /** @type {Node | null} */ (/** @type {FocusEvent} */ (e).relatedTarget);
    if (to && host.contains(to)) return; // focus moved to a sibling control INSIDE the host — still held
    const pending = _focusHeld.get(host);
    if (!pending) return;
    _focusHeld.delete(host);
    controller.abort();
    // Re-enter through the full guard set: another interaction (an overlay, a
    // selection) may have started meanwhile, in which case this just re-defers.
    // A DETACHED host drops its held swap — nothing may render into DOM that
    // left the document, and the entry must not pin its build closure.
    if (host.isConnected) renderRegion(host, pending.build, { sig: pending.sig });
  }, { signal: controller.signal }); // gate-allow: signal-listener — armed only while a swap is held; the same controller detaches it on flush
}

/** Drop `host`'s held swap — a swap happening NOW (or a canon-side deferral
 * that supersedes it) makes the held one moot. @param {Element} host */
function _releaseFocusHold(host) {
  const held = _focusHeld.get(host);
  if (!held) return;
  _focusHeld.delete(host);
  held.controller.abort();
}

// ── Sig-drift audit (dev/test only) ─────────────────────────────────────────

/** App-side mirror of the canon's per-host sig, used ONLY to decide when the
 * audit should probe. Advanced only when no interaction hold is live inside
 * the host at call time, so a canon-DEFERRED swap (sig not yet rendered)
 * can't be mistaken for a sig-gated skip. @type {WeakMap<Element, string>} */
const _auditSig = new WeakMap();

/** Mirror of the canon guards' predicates, for the audit gate only: a hold
 * means the canon deferred (or would defer) rather than sig-skipped.
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
  if (probe.innerHTML === host.innerHTML) return;
  (globalThis.__TAPSCRIBE_SIG_DRIFT ||= []).push({ sig, expected: probe.innerHTML, actual: host.innerHTML });
  console.error(
    `renderRegion sig drift: output changed but sig ${JSON.stringify(sig)} did not — the build ` +
      `closure reads a value missing from its sig, so this region will silently go stale. Add that ` +
      `value to the sig, OR render the derived bit in place (a sibling toggled per-tick) instead of ` +
      `through a sig-gated region.`,
  );
}

/**
 * Canon renderRegion (lib/render.js) plus the two guards the seam owns and the
 * dev/test-only sig-drift audit.
 *
 * The guards run BEFORE the canon and, when they hold, the canon is never
 * called (so it never records `sig` — a skip must not advance the gate,
 * ADR-0004):
 *   - a focused control inside `host` (see the "Focus hold" block above — the
 *     canon flushes such a swap onto the INCOMING focus);
 *   - a selection that STRADDLES `host` without either endpoint inside it,
 *     which the canon's own selection guard can't see. That one defers through
 *     the bespoke tick-retry flag rather than a listener: it's rare, and the
 *     canon already self-flushes every case where an endpoint IS inside.
 *
 * Everything else is the canon's — the overlay guard, the sig gate, the swap,
 * and the instant deferred-flush (a swap held back lands the moment the hold
 * clears, not on the next poll tick).
 *
 * The audit: when __TAPSCRIBE_SIG_AUDIT is set and a call is about to sig-skip
 * (same sig, no interaction hold inside the host), the build is probed against
 * the live DOM and any divergence is recorded to __TAPSCRIBE_SIG_DRIFT.
 * @param {Element} host
 * @param {() => Node} build
 * @param {{ sig?: string, force?: boolean }} [opts]
 */
export function renderRegion(host, build, opts = {}) {
  if (!opts.force) {
    if (_focusedInside(host)) {
      _holdForFocus(host, build, opts.sig);
      return;
    }
    if (_selectionStraddles(host)) {
      markDeferredRender();
      return;
    }
  }
  // This call reaches the canon: whatever it does with the build (swap, or
  // defer on an overlay / an endpoint-inside selection) supersedes a held one.
  _releaseFocusHold(host);
  if (globalThis.__TAPSCRIBE_SIG_AUDIT && !opts.force && opts.sig != null &&
      _auditSig.get(host) === opts.sig && !_holdInside(host)) {
    _auditSigCoversOutput(host, build, opts.sig);
  }
  canonRenderRegion(host, build, opts);
  if (opts.sig != null && (opts.force || !_holdInside(host))) _auditSig.set(host, opts.sig);
}

/**
 * Canon markRegionStale (lib/render.js) — invalidate `host`'s remembered
 * render signature so the NEXT renderRegion call re-renders even if its `sig`
 * is unchanged, WITHOUT bypassing the interaction guards the way `force:true`
 * would (ADR-0004). Also resets the audit mirror so the next sig-skip isn't
 * mis-probed.
 * @param {Element} host
 */
export function markRegionStale(host) {
  canonMarkRegionStale(host);
  _auditSig.delete(host);
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

/** Per-row remembered item signature and reconcile key, stamped by the seam on
 * every node it creates. Kept off the DOM rather than in `data-` attributes so a
 * row's own markup (and anything asserting on it) stays untouched.
 * @type {WeakMap<Element, string>} */
const _itemSig = new WeakMap();
/** @type {WeakMap<Element, string>} */
const _rowKey = new WeakMap();

/** Resolve `renderList`'s `items`, which may be a thunk so a caller can keep an
 * O(rows) build behind the gates.
 * @template T @param {T[] | (() => T[])} items @returns {T[]} */
function _resolveItems(items) {
  return typeof items === "function" ? items() : items;
}

/** The DIRECT CHILD of `host` containing the focused interactive control, or
 * null. Rows are host children, so this is "which row is the operator in".
 * Defers the PREDICATE to `_focusedInside` rather than re-testing it, so a future
 * widening of what counts as interactive reaches the removal hold too, not just
 * renderRegion's hold and the per-row hold.
 * @param {Element} host @returns {Element | null} */
function _focusedRow(host) {
  if (!_focusedInside(host)) return null;
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
function _auditListSigCoversRows(host, items, opts, sig) {
  const expected = items.map((it) => `${opts.key(it)}§${opts.itemSig ? opts.itemSig(it) : ""}`);
  const actual = [...host.children].map((n) => `${_rowKey.get(n) ?? "?"}§${_itemSig.get(n) ?? ""}`);
  globalThis.__TAPSCRIBE_SIG_PROBES = (globalThis.__TAPSCRIBE_SIG_PROBES || 0) + 1;
  if (expected.length === actual.length && expected.every((s, i) => s === actual[i])) return;
  // Joined, not arrays: __TAPSCRIBE_SIG_DRIFT is one record shape shared with
  // renderRegion's probe (types.d.ts), and one row per line reads fine.
  (globalThis.__TAPSCRIBE_SIG_DRIFT ||= []).push({
    sig,
    expected: expected.join("\n"),
    actual: actual.join("\n"),
  });
  console.error(
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
  globalThis.__TAPSCRIBE_SIG_PROBES = (globalThis.__TAPSCRIBE_SIG_PROBES || 0) + 1;
  const probe = opts.create(item);
  if (opts.update) opts.update(probe, item);
  if (probe.innerHTML === node.innerHTML) return;
  (globalThis.__TAPSCRIBE_SIG_DRIFT ||= []).push({ sig, expected: probe.innerHTML, actual: node.innerHTML });
  console.error(
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
 *  3. A row holding a focused control whose key is absent from `items` → defer
 *     the whole render. Canon `reconcileList` removes every key not present,
 *     which would take the focused control with it — and CONTEXT.md defines the
 *     interaction hold as never destroying interaction state, so removal is the
 *     case it most has to cover.
 *  4. Reconcile. Per row: a focused control inside the row holds that row's
 *     `update` (defer, and do NOT stamp its `itemSig`); otherwise an unchanged
 *     `itemSig` skips it; otherwise update and stamp. The hold is deliberately
 *     COARSER than a per-control guard — one focused control freezes its whole
 *     row for a tick — which is consistent with renderRegion holding a whole
 *     region, and means a control added to a row later is covered for free.
 *  5. Advance `sig` only if no row was held.
 *
 * A held render lands via the tick-retry (`markDeferredRender` →
 * `consumeDeferredRender` in next/main.js), NOT the self-flush renderRegion uses.
 * The honest trade-off: renderRegion captures a `build` CLOSURE, which re-reads
 * view state whenever it is finally invoked, so flushing it on focusout is safe
 * by construction. A list is driven by `items` — materialized data, and even as a
 * thunk it closes over one tick's locals — so a flush would need the view to
 * re-derive anyway. Going through the tick does that with no second mechanism,
 * at a cost of one poll interval; `interactionHeld()` keeps the pacer fast while
 * the hold lasts, so that interval is the fast one.
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
 * }} opts — `auditRows: false` opts THIS list's rows out of the dev-only row
 *   probe; say why at the call site.
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
  const list = _resolveItems(items);

  // One key per item per call, shared by the removal scan and the create path.
  // Canon recomputes keys internally (its own map isn't exported), but this at
  // least stops US walking the list twice while a rename input holds focus —
  // which is every tick, for as long as the operator is typing.
  /** @type {Map<T, string>} */
  const keyed = new Map();
  /** @param {T} item */
  const keyFor = (item) => {
    let k = keyed.get(item);
    if (k === undefined) keyed.set(item, (k = key(item)));
    return k;
  };

  // Removal hold (rule 3). Only costs anything when a row actually holds focus.
  const focused = _focusedRow(host);
  if (focused) {
    const k = _rowKey.get(focused);
    if (k !== undefined && !list.some((it) => keyFor(it) === k)) {
      markDeferredRender();
      return false;
    }
  }

  let held = false;
  canonReconcileList(
    host,
    list,
    keyFor,
    (item) => {
      const node = create(item);
      _rowKey.set(node, keyFor(item));
      if (update) update(node, item);
      if (itemSig) _itemSig.set(node, itemSig(item));
      return node;
    },
    (node, item) => {
      if (_focusedInside(node)) {
        // This row's update is held. Marking the flag is what earns it a retry
        // at all: main.js skips the whole render pass on a 304, and the poll
        // goes quiet exactly while the operator types.
        held = true;
        markDeferredRender();
        return;
      }
      /** @type {string | undefined} */
      let stamp;
      if (itemSig) {
        stamp = itemSig(item);
        if (_itemSig.get(node) === stamp) {
          if (globalThis.__TAPSCRIBE_SIG_AUDIT && opts.auditRows !== false) {
            _auditItemSigCoversRow(node, item, opts, stamp);
          }
          return;
        }
      }
      if (update) update(node, item);
      // Stamp AFTER the write, never before: an `update` that throws must leave
      // the row's gate UNADVANCED so the next tick retries it. Stamping first
      // reads as tidier — one exit instead of two — but it strands the row
      // forever, because every later tick recomputes the same signature and
      // skips. That is the ADR-0004 trap this seam exists to remove, and the
      // throw is real: fillRow reaches slots via `pick`, which throws when one
      // is missing, and exceptions on the poll path are swallowed.
      if (stamp !== undefined) _itemSig.set(node, stamp);
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
