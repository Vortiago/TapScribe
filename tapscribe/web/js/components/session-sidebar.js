// Grouped session sidebar — filterable list grouped by date bucket
// (Today / Yesterday / This week / Older).

import { tpl, mount, slot, pick } from "../templates.js";
import { fmtSessionLabel } from "../formatters.js";

// Bucket sessions by date relative to "now".
function groupSessions(sessions) {
  const now = Date.now();
  const groups = { Today: [], Yesterday: [], "This week": [], Older: [] };
  for (const s of sessions) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T/.exec(s.session);
    const day = m ? new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00`).getTime() : null;
    if (!day) { groups.Older.push(s); continue; }
    const diff = Math.floor((now - day) / 86400000);
    if (diff <= 0) groups.Today.push(s);
    else if (diff === 1) groups.Yesterday.push(s);
    else if (diff < 7) groups["This week"].push(s);
    else groups.Older.push(s);
  }
  return Object.entries(groups).filter(([, items]) => items.length > 0);
}

export function render(sessions, {
  listEl, selectedId, filter, metaFor, onSelect, onDelete,
}) {
  const q = filter.trim().toLowerCase();
  const filtered = q
    ? sessions.filter((s) => {
        const meta = metaFor(s);
        return s.session.toLowerCase().includes(q)
          || (meta.label || "").toLowerCase().includes(q);
      })
    : sessions;
  const groups = groupSessions(filtered);

  if (!groups.length) {
    mount(listEl, slot(tpl("tpl-sess-empty"), { msg: "no matches" }));
    return;
  }

  const out = document.createDocumentFragment();
  for (const [gname, items] of groups) {
    out.appendChild(slot(tpl("tpl-sess-group-hd"), { name: gname, count: `· ${items.length}` }));
    for (const s of items) {
      const meta = metaFor(s);
      const node = tpl("tpl-sess-item");
      const row = node.firstElementChild;
      row.classList.toggle("active", s.session === selectedId);
      row.classList.toggle("current", !!s.is_current);
      row.dataset.sessId = s.session;
      row.title = s.session;

      const primary = pick(row, "primary");
      if (meta.label) {
        primary.textContent = meta.label;
      } else {
        primary.appendChild(slot(tpl("tpl-sess-item-unnamed"), { label: fmtSessionLabel(s.session) }));
      }
      pick(row, "folder").textContent = s.session;
      pick(row, "counter").textContent =
        `${s.wav_count || 0}w${s.stripped ? " · ✂" : ""}${s.session_transcript ? " · tx" : ""}`;
      const del = pick(row, "del");
      // The current session is never deletable — hide its delete button.
      if (!s.is_current) {
        del.hidden = false;
        del.dataset.delSess = s.session;
      }
      out.appendChild(node);
    }
  }
  mount(listEl, out);

  for (const row of listEl.querySelectorAll(".sess-item")) {
    row.addEventListener("click", (e) => {
      // Delete-button clicks bubble to the row — bail so the delete handler
      // (which runs first via stopPropagation) owns that event.
      if (e.target.closest("[data-del-sess]")) return;
      onSelect(row.dataset.sessId);
    });
  }
  for (const del of listEl.querySelectorAll("[data-del-sess]")) {
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      e.preventDefault();
      await onDelete(del.dataset.delSess);
    });
  }
}
