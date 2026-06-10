// @ts-check
// Tiny template loader.
//
// Components live in `tapscribe/web/components/*.html` as one or more
// `<template id="tpl-…">` blocks. `loadTemplates(…urls)` fetches each file,
// inlines the `<template>` nodes into the document so `document.getElementById`
// can find them, and is safe to call multiple times for the same URL.
//
// Components then do:
//
//   const node = tpl("tpl-live-row");
//   node.querySelector("[data-slot=lbl]").textContent = "model";
//   container.appendChild(node);

/** @type {Set<string>} */
const fetched = new Set();

/** @param {...string} urls */
export async function loadTemplates(...urls) {
  const fresh = urls.filter((u) => !fetched.has(u));
  const texts = await Promise.all(
    fresh.map((u) => fetch(u).then((r) => {
      if (!r.ok) throw new Error(`template fetch ${u}: ${r.status}`);
      return r.text();
    })),
  );
  fresh.forEach((u) => fetched.add(u));
  const holder = document.createElement("div");
  holder.hidden = true;
  holder.innerHTML = texts.join("\n");
  document.body.append(...holder.children);
}

// Clone a `<template id>` and return its DocumentFragment.
/**
 * @param {string} id
 * @returns {DocumentFragment}
 */
export const tpl = (id) => {
  const t = /** @type {HTMLTemplateElement | null} */ (document.getElementById(id));
  if (!t) throw new Error(`template not loaded: ${id}`);
  return /** @type {DocumentFragment} */ (t.content.cloneNode(true));
};

// Fill text slots: `{ slot: value }` sets textContent on `[data-slot=slot]`.
// `null`/`undefined` values are skipped so a single object can describe a
// partial update. Returns the frag for chaining.
/**
 * @template {ParentNode & Node} T
 * @param {T} frag
 * @param {Record<string, unknown>} slots
 * @returns {T}
 */
export function slot(frag, slots) {
  for (const [k, v] of Object.entries(slots)) {
    if (v == null) continue;
    for (const el of frag.querySelectorAll(`[data-slot="${k}"]`)) {
      el.textContent = String(v);
    }
  }
  return frag;
}

// Convenience: `pick(frag, "name")` → first `[data-slot=name]` element.
// Throws if the slot isn't present: template authors are expected to keep
// `data-slot=…` markers in sync with their `pick()` calls, so a missing
// slot is a programmer bug, not a runtime condition to handle. The throw
// surfaces it at the call site (where the developer can see which slot
// they typo'd) instead of as a "Cannot read properties of null" three
// frames deeper.
/**
 * @param {ParentNode} frag
 * @param {string} name
 * @returns {HTMLElement}
 */
export const pick = (frag, name) => {
  const el = /** @type {HTMLElement | null} */ (frag.querySelector(`[data-slot="${name}"]`));
  if (!el) throw new Error(`template slot not found: data-slot="${name}"`);
  return el;
};

// Replace `host`'s children with the rendered fragment. Avoids the
// `innerHTML = ""` flicker by swapping once.
/**
 * @param {Element} host
 * @param {Node} frag
 */
export function mount(host, frag) {
  host.replaceChildren(frag);
}

/** Per-host last signature, for the optional sig-gate. */
/** @type {WeakMap<Element, string>} */
const _regionSig = new WeakMap();

// Dev/test only. Re-runs `build` into a detached probe and compares to what the
// region currently shows; a mismatch means `sig` is missing a dependency the
// render reads, so the region would silently go stale. Records to
// globalThis.__TAPSCRIBE_SIG_DRIFT (a throw would be swallowed by the dashboard's
// event-handler try/catch, so we don't rely on it) and console.errors loudly.
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

/** @param {Element} el — true for controls that hold live interaction state. */
function _isInteractive(el) {
  const tag = el.tagName;
  return (
    tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA" ||
    /** @type {HTMLElement} */ (el).isContentEditable === true
  );
}

/**
 * True while the operator has a non-collapsed text selection that starts or
 * ends inside `host`. Rebuilding (or rewriting textContent of) a node the
 * selection touches destroys the selection — mid-copy, that reads as the UI
 * "flashing away" the operator's marked text. The same interaction-state
 * rule as the focus guard, for selections. Exported for per-second updaters
 * that write text in place rather than going through renderRegion (the live
 * log dialog is the canonical case).
 * @param {Element} host
 */
export function selectionInside(host) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  return (
    (!!sel.anchorNode && host.contains(sel.anchorNode)) ||
    (!!sel.focusNode && host.contains(sel.focusNode))
  );
}

/**
 * Render `build()`'s output into `host` WITHOUT clobbering live interaction.
 * The dashboard re-renders every poll; replacing a node that holds an open
 * <select>, a focused input, or a mid-edit textarea would snap it shut — and
 * replacing a node the operator is select-copying text from would dissolve
 * the selection. So:
 *   - skip the swap while the operator is interacting with a control INSIDE
 *     `host` (the active element is a select/input/textarea/contenteditable);
 *   - skip the swap while a text selection starts or ends inside `host`;
 *   - optionally skip when a caller-supplied `sig` is unchanged (perf);
 *   - otherwise replaceChildren(build()).
 * `build` is only invoked when we actually swap, so a skipped tick is cheap.
 * Use this instead of raw `host.replaceChildren(...)` for any region that is
 * re-rendered on the poll tick and may contain a control. `force:true` swaps
 * unconditionally.
 *
 * @param {Element} host
 * @param {() => Node} build
 * @param {{ sig?: string, force?: boolean }} [opts]
 */
export function renderRegion(host, build, opts = {}) {
  if (!opts.force) {
    const active = document.activeElement;
    if (active && active !== document.body && host.contains(active) && _isInteractive(active)) return;
    if (selectionInside(host)) return;
    if (opts.sig != null && _regionSig.get(host) === opts.sig) {
      if (globalThis.__TAPSCRIBE_SIG_AUDIT) _auditSigCoversOutput(host, build, opts.sig);
      return;
    }
  }
  if (opts.sig != null) _regionSig.set(host, opts.sig);
  host.replaceChildren(build());
}

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

/**
 * Invalidate `host`'s remembered render signature so the NEXT `renderRegion`
 * call re-renders even if its `sig` is unchanged. This is the "mark stale"
 * companion to `renderRegion`'s perf gate: a mutation (a fresh summary landing,
 * a session switch, a lazy body resolving) makes the on-screen content stale
 * without changing the sig the caller computes, so the caller calls this to
 * force one more render.
 *
 * Deliberately NOT `force:true`: forcing would bypass the focus/selection
 * guards and could clobber an open control or a mid-copy selection. Marking
 * stale instead lets the held-back render land on the first tick AFTER the
 * interaction clears — preserving the interaction hold (ADR-0004).
 *
 * @param {Element} host
 */
export function markRegionStale(host) {
  _regionSig.delete(host);
}
