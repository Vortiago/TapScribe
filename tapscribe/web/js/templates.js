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

const fetched = new Set();

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
export const tpl = (id) => {
  const t = document.getElementById(id);
  if (!t) throw new Error(`template not loaded: ${id}`);
  return t.content.cloneNode(true);
};

// Fill text slots: `{ slot: value }` sets textContent on `[data-slot=slot]`.
// `null`/`undefined` values are skipped so a single object can describe a
// partial update. Returns the frag for chaining.
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
export const pick = (frag, name) => frag.querySelector(`[data-slot="${name}"]`);

// Replace `host`'s children with the rendered fragment. Avoids the
// `innerHTML = ""` flicker by swapping once.
export function mount(host, frag) {
  host.replaceChildren(frag);
}
