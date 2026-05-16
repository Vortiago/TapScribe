// Active taps panel — one row per live audio source the recorder is
// currently receiving bytes from.

import { tpl, mount, pick } from "../templates.js";
import { speakerIndex } from "../speakers.js";
import { fmtBytes, fmtDur, truncMid } from "../formatters.js";

export function render(j, { countEl, badgeEl, bodyEl }) {
  const list = j.active || [];
  countEl.textContent = String(list.length);

  if (!list.length) {
    mount(badgeEl, tpl("tpl-active-badge-idle"));
    mount(bodyEl, tpl("tpl-active-empty"));
    return;
  }
  mount(badgeEl, tpl("tpl-active-badge-capturing"));

  const frag = document.createDocumentFragment();
  for (const a of list) {
    const dur = (a.bytes_received || 0) / 32000;
    // Settings default to true when the server didn't include them (older
    // payload, or first-ever sighting of this identity).
    const recOn = a.record !== false;
    const liveOn = a.live !== false;
    const ident = a.identity || "";
    const filename = a.filename || "";

    const node = tpl("tpl-stream-row");
    const row = node.querySelector(".stream-row");

    const marker = pick(row, "spkMarker");
    marker.dataset.spk = speakerIndex(a.name || ident);

    pick(row, "name").textContent = a.name || "<anon>";
    const identEl = pick(row, "ident");
    identEl.title = filename;
    identEl.textContent = `${ident} · ${truncMid(filename, 30)}`;

    pick(row, "size").textContent = fmtBytes(a.bytes_received || 0);
    pick(row, "dur").textContent = `~${fmtDur(dur)}`;

    for (const btn of row.querySelectorAll(".tap-toggle")) {
      const which = btn.dataset.toggle;
      const on = which === "record" ? recOn : liveOn;
      btn.dataset.identity = ident;
      btn.dataset.state = on ? "1" : "0";
      btn.classList.toggle("on", on);
    }
    frag.appendChild(node);
  }
  bodyEl.replaceChildren(frag);
}
