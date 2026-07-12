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
//   - the ADR-0004 interaction-hold flag for the BESPOKE per-tick gates that
//     can't route through renderRegion (recordings.js / transcript.js WAV
//     lists, active-taps.js, live-feed.js): markDeferredRender /
//     consumeDeferredRender / deferIfSelectionInside. Canon renderRegion
//     needs none of this — a swap it defers flushes ITSELF the instant the
//     interaction clears (one-shot listener per host), tick or no tick.
//   - interactionHeld() — the document-wide hold predicate the poll pacer
//     uses to keep the /api/state cadence fast while the operator works.
//   - the dev/test-only sig-drift audit (__TAPSCRIBE_SIG_AUDIT), wrapped
//     around canon renderRegion.
//   - renderMarkdown — the safe, textContent-only markdown subset for LLM
//     summaries.

export { loadTemplates, tpl, slot, pick, mount, loadCSS, every, withPending } from "./lib/templates.js";
export { reconcileList, withTransition, selectionInside } from "./lib/render.js";
export { wireTheme, wireErrorBar } from "./lib/chrome.js";

import {
  renderRegion as canonRenderRegion,
  markRegionStale as canonMarkRegionStale,
  selectionInside,
} from "./lib/render.js";

// A per-tick render can be DEFERRED (skipped without advancing its signature
// gate) to protect operator interaction state — ADR-0004 "Interaction hold".
// main.js's tick() short-circuits its whole renderAll pass when /api/state's
// fetchState() returns the identical cached object (a 304), since there is
// nothing new to show — but a fetch going quiet does NOT mean an interaction
// hold has cleared. Without this flag, a render a BESPOKE gate held back while
// a selection was live would never get retried once the server stopped
// changing, stranding it even after the operator released the selection.
// Only the bespoke gates mark this (via deferIfSelectionInside below); canon
// renderRegion deferrals flush themselves the instant the hold clears and
// never need the tick-retry. main.js consumes it right before a retry so a
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

/** @param {Element} el — true for controls that hold live interaction state. */
function _isInteractive(el) {
  const tag = el.tagName;
  return (
    tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA" ||
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
 * the shape every per-tick gate that can't route through renderRegion needs
 * (recordings.js/transcript.js's WAV lists, active-taps.js, live-feed.js), so
 * a future bespoke gate has one call to make, not two to remember. Returns
 * true when the render must be held back.
 * @param {Element} host
 */
export function deferIfSelectionInside(host) {
  if (!selectionInside(host)) return false;
  markDeferredRender();
  return true;
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
  const active = document.activeElement;
  if (active && active !== document.body && host.contains(active) && _isInteractive(active)) return true;
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
 * Canon renderRegion (lib/render.js) plus the dev/test-only sig-drift audit:
 * when __TAPSCRIBE_SIG_AUDIT is set and a call is about to sig-skip (same sig,
 * no interaction hold inside the host), the build is probed against the live
 * DOM and any divergence is recorded to __TAPSCRIBE_SIG_DRIFT. Semantics are
 * otherwise the canon's — including the instant deferred-flush: a swap held
 * back by focus / an open overlay / a selection lands the moment that clears,
 * not on the next poll tick.
 * @param {Element} host
 * @param {() => Node} build
 * @param {{ sig?: string, force?: boolean }} [opts]
 */
export function renderRegion(host, build, opts = {}) {
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
