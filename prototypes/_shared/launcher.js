// Floating cross-prototype launcher. Include ONE line on any prototype page:
//   <script type="module" src="../_shared/launcher.js"></script>
// It injects the vanilla-components tokens (idempotent — harmless if already
// linked) and drops a fixed bottom-right button + menu listing every prototype
// from registry.js, so you can hop between them without retyping URLs.

import { el } from "./dom.js";
import { VC, warmAll } from "./vc.js";
import { PROTOTYPES, currentPrototypeId } from "./registry.js";

const here = new URL("./", import.meta.url); // .../prototypes/_shared/
const root = new URL("../", import.meta.url); // .../prototypes/  (gallery root)

// Ensure the design tokens + tone mixin exist (stages/ doesn't link them).
// They only add :root custom props in @layer tokens, so they never restyle the
// host's own elements (unlayered host rules win any name clash).
for (const [id, rel] of [["pl-tokens", "vc/tokens.css"], ["pl-tones", "vc/tones.css"]]) {
  if (!document.getElementById(id)) {
    const link = document.createElement("link");
    link.id = id;
    link.rel = "stylesheet";
    link.href = new URL(rel, here).href;
    document.head.appendChild(link);
  }
}

const STYLE = `
.pl-root { position: fixed; right: 14px; bottom: 14px; z-index: 2147483600; }
.pl-root .button { box-shadow: 0 6px 20px rgba(0,0,0,.28); }
`;
const style = document.createElement("style");
style.textContent = STYLE;
document.head.appendChild(style);

await warmAll();

const currentId = currentPrototypeId();
const currentTitle = PROTOTYPES.find((p) => p.id === currentId)?.title;

// Build the menu item list: gallery, then each prototype as a muted header
// (disabled item) followed by its links.
const items = [{ id: new URL("./", root).href, label: "All prototypes", icon: "▦" }, "separator"];
for (const p of PROTOTYPES) {
  items.push({ id: `__h_${p.id}`, label: p.title, disabled: true });
  for (const lnk of p.links) items.push({ id: new URL(lnk.href, root).href, label: lnk.label });
  items.push("separator");
}
items.pop(); // trailing separator

const trigger = VC.button({ label: currentTitle ? `◧ ${currentTitle}` : "◧ Prototypes", size: "sm" });
const host = el("div", { class: "pl-root" }, trigger.el);
document.body.appendChild(host);

VC.menu(trigger.el, {
  items,
  align: "end",
  onSelect: (id) => { if (!id.startsWith("__h_")) location.assign(id); },
});
