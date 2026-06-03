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

/** @param {Element} el — true for controls that hold live interaction state. */
function _isInteractive(el) {
  const tag = el.tagName;
  return (
    tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA" ||
    /** @type {HTMLElement} */ (el).isContentEditable === true
  );
}

/**
 * Render `build()`'s output into `host` WITHOUT clobbering live interaction.
 * The dashboard re-renders every poll; replacing a node that holds an open
 * <select>, a focused input, or a mid-edit textarea would snap it shut. So:
 *   - skip the swap while the operator is interacting with a control INSIDE
 *     `host` (the active element is a select/input/textarea/contenteditable);
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
    if (opts.sig != null && _regionSig.get(host) === opts.sig) return;
  }
  if (opts.sig != null) _regionSig.set(host, opts.sig);
  host.replaceChildren(build());
}
