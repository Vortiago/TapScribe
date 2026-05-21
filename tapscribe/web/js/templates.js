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
// Cast to HTMLElement because every template author knows the slot exists
// and uses HTML-specific properties (.dataset, .hidden, .title) on it.
// Subtype-specific props (.value, .checked, .disabled) still need an
// explicit cast at the call site.
/**
 * @param {ParentNode} frag
 * @param {string} name
 * @returns {HTMLElement}
 */
export const pick = (frag, name) => /** @type {HTMLElement} */ (frag.querySelector(`[data-slot="${name}"]`));

// Replace `host`'s children with the rendered fragment. Avoids the
// `innerHTML = ""` flicker by swapping once.
/**
 * @param {Element} host
 * @param {Node} frag
 */
export function mount(host, frag) {
  host.replaceChildren(frag);
}
