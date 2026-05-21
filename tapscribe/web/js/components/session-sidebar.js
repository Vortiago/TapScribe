// @ts-check
// Grouped session sidebar — filterable list grouped by date bucket
// (Today / Yesterday / This week / Older).

import { tpl, mount, slot, pick } from "../templates.js";
import { fmtSessionLabel } from "../formatters.js";

// Bucket sessions by date relative to "now". BUCKET_ORDER fixes the display
// order independently of Map.groupBy's first-seen insertion order.
const BUCKET_ORDER = /** @type {const} */ (["Today", "Yesterday", "This week", "Older"]);

/**
 * @param {string} session
 * @param {number} now
 * @returns {typeof BUCKET_ORDER[number]}
 */
function bucketOf(session, now) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T/.exec(session);
  if (!m) return "Older";
  const day = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00`).getTime();
  const diff = Math.floor((now - day) / 86400000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return "This week";
  return "Older";
}

/**
 * @param {import('../types.js').Session[]} sessions
 * @returns {[string, import('../types.js').Session[]][]}
 */
function groupSessions(sessions) {
  const now = Date.now();
  const byBucket = Map.groupBy(sessions, (s) => bucketOf(s.session, now));
  return BUCKET_ORDER.flatMap((name) => {
    const items = byBucket.get(name);
    return items ? [/** @type {[string, import('../types.js').Session[]]} */ ([name, items])] : [];
  });
}

/**
 * @param {import('../types.js').Session[]} sessions
 * @param {import('../types.js').SessionSidebarCtx} ctx
 */
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
      const row = /** @type {HTMLElement} */ (node.firstElementChild);
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

  for (const row of /** @type {NodeListOf<HTMLElement>} */ (listEl.querySelectorAll(".sess-item"))) {
    row.addEventListener("click", (e) => {
      // Delete-button clicks bubble to the row — bail so the delete handler
      // (which runs first via stopPropagation) owns that event.
      if (/** @type {Element | null} */ (e.target)?.closest("[data-del-sess]")) return;
      onSelect(row.dataset.sessId || "");
    });
  }
  for (const del of /** @type {NodeListOf<HTMLElement>} */ (listEl.querySelectorAll("[data-del-sess]"))) {
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      e.preventDefault();
      await onDelete(del.dataset.delSess || "");
    });
  }
}
