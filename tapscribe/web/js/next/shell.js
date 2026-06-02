// @ts-check
// Stages-only shared helpers. New file under js/next/ so the existing shared
// modules stay untouched — see the Phase-1 brief. Pure DOM + template glue.

import { tpl, pick } from "../templates.js";

/** The Stages views. GLOBAL group is pinned + un-numbered; the THIS SESSION
 * group is the numbered Capture → Recordings → Transcript → Summary journey. */
/** @typedef {"capture"|"transcript"|"summary"|"settings"|"taps"|"recordings"|"people"} ViewId */

/** @type {ViewId[]} */
export const GLOBAL_VIEWS = ["taps", "people", "settings"];
/** @type {ViewId[]} */
export const JOURNEY_VIEWS = ["capture", "recordings", "transcript", "summary"];
/** @type {ViewId[]} */
export const ALL_VIEWS = [...GLOBAL_VIEWS, ...JOURNEY_VIEWS];

/**
 * Build the shared stage header into a view's `[data-slot=head]` host.
 * `sub`/`actions` accept either a string (→ textContent) or a Node to append,
 * so callers never inject HTML strings (XSS-safe, like the rest of the code).
 * @param {Element} host
 * @param {{ eyebrow: string, title: string, sub?: string | Node, actions?: Node }} opts
 */
export function header(host, { eyebrow, title, sub, actions }) {
  const frag = tpl("tpl-next-head");
  pick(frag, "eyebrow").textContent = eyebrow;
  pick(frag, "title").textContent = title;
  const subEl = pick(frag, "sub");
  if (sub instanceof Node) subEl.appendChild(sub);
  else if (sub != null) subEl.textContent = sub;
  if (actions) pick(frag, "actions").appendChild(actions);
  host.replaceChildren(frag);
}

/**
 * A tiny inline <b>text</b> span — used to emphasise a value inside an
 * otherwise plain header sub-line without resorting to innerHTML.
 * @param {string} text
 */
export function strong(text) {
  const b = document.createElement("b");
  b.textContent = text;
  return b;
}

/**
 * Compose several inline nodes/strings into one fragment for a header sub.
 * @param {...(string | Node)} parts
 */
export function inline(...parts) {
  const frag = document.createDocumentFragment();
  for (const p of parts) frag.append(p);
  return frag;
}

/**
 * Build the "Coming in a later phase" placeholder for a stubbed view.
 * @param {Element} root
 * @param {{ eyebrow: string, title: string, sub?: string, icon: string, heading: string, detail: string }} opts
 */
export function placeholderView(root, { eyebrow, title, sub, icon, heading, detail }) {
  const headHost = document.createElement("div");
  const body = tpl("tpl-next-placeholder");
  pick(body, "icon").textContent = icon;
  pick(body, "heading").textContent = heading;
  pick(body, "detail").textContent = detail;
  root.replaceChildren(headHost, body);
  header(headHost, { eyebrow, title, sub });
}

/**
 * Resolve the session the Stages UI is focused on: the explicitly-selected
 * one, else the current (recording) session, else the newest on disk.
 * @param {import('../types.js').Session[]} sessions
 * @param {string | null} selectedId
 * @returns {import('../types.js').Session | null}
 */
export function resolveSession(sessions, selectedId) {
  if (!sessions.length) return null;
  if (selectedId) {
    const hit = sessions.find((s) => s.session === selectedId);
    if (hit) return hit;
  }
  const current = sessions.find((s) => s.is_current);
  return current ?? sessions[0] ?? null;
}
