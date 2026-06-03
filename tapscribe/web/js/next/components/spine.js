// @ts-check
// Stages spine — the slim left rail in two groups. GLOBAL (Taps · People ·
// Settings, pinned + un-numbered) and THIS SESSION (the numbered Capture →
// Recordings → Transcript → Summary journey, with the session picker + New
// session). Rebuilt every poll tick from /api/state; live status chips show
// real data where we have it.

import { tpl, pick } from "../../templates.js";
import { fmtSessionLabel } from "../../formatters.js";
import { GLOBAL_VIEWS, JOURNEY_VIEWS } from "../shell.js";

/**
 * @typedef {{ tone: "live"|"good"|"warn"|"mute", text: string }} Chip
 * @typedef {{
 *   id: import('../shell.js').ViewId, name: string, lead: string,
 *   chip: Chip, live?: number, numbered?: boolean, done?: boolean,
 * }} NavDef
 */

/**
 * @param {import('../../types.js').AppState} j
 * @param {import('../../types.js').Session | null} sess
 * @returns {NavDef[]}
 */
function globalDefs(j, sess) {
  const sessions = j.sessions || [];
  const peopleNames = new Set();
  for (const s of sessions) {
    for (const k of Object.keys((s.session_meta || {}).aliases || {})) peopleNames.add(k);
  }
  const liveTaps = (j.active || []).filter((a) => a.live !== false).length;
  return [
    {
      id: "taps", name: "Taps", lead: "🛰️",
      live: liveTaps || undefined,
      chip: liveTaps
        ? { tone: "live", text: `${liveTaps} live` }
        : { tone: "mute", text: `${(j.active || []).length} connected` },
    },
    {
      id: "people", name: "People", lead: "👥",
      chip: { tone: "mute", text: peopleNames.size ? `${peopleNames.size} named` : "registry" },
    },
    {
      id: "settings", name: "Settings", lead: "⚙️",
      chip: { tone: "mute", text: `${j.backend || "auto"}` },
    },
  ];
}

/**
 * @param {import('../../types.js').AppState} j
 * @param {import('../../types.js').Session | null} sess
 * @returns {NavDef[]}
 */
function journeyDefs(j, sess) {
  const isCurrent = !!sess?.is_current;
  const liveCount = isCurrent ? (j.active || []).filter((a) => a.live !== false).length : 0;
  const wavCount = sess?.wav_count || 0;
  const stripped = !!sess?.stripped;
  const tx = sess?.session_transcript || null;
  const suppressed = tx?.suppressed_count || 0;
  return [
    {
      id: "capture", name: "Capture", lead: "1", numbered: true,
      done: !!sess && !isCurrent,
      chip: !sess ? { tone: "mute", text: "no session" }
        : isCurrent ? (liveCount ? { tone: "live", text: `${liveCount} live` } : { tone: "mute", text: "idle" })
          : { tone: "good", text: "archived" },
    },
    {
      id: "recordings", name: "Recordings", lead: "2", numbered: true,
      done: !!tx,
      chip: !wavCount ? { tone: "mute", text: "no WAVs" }
        : stripped ? { tone: "good", text: `${wavCount} WAVs` }
          : { tone: "warn", text: `${wavCount} to strip` },
    },
    {
      id: "transcript", name: "Transcript", lead: "3", numbered: true,
      done: !!tx,
      chip: tx
        ? (suppressed ? { tone: "warn", text: `${suppressed} suppressed` } : { tone: "good", text: "merged" })
        : { tone: "mute", text: "not run" },
    },
    {
      // Summary is a preview of a future feature — no backend yet (mock UI), so
      // it never marks done and shows a mute "preview" chip.
      id: "summary", name: "Summary", lead: "4", numbered: true,
      done: false,
      chip: { tone: "mute", text: "preview" },
    },
  ];
}

/**
 * @param {NavDef} d
 * @param {import('../shell.js').ViewId} currentView
 * @param {(id: import('../shell.js').ViewId) => void} onSelect
 */
function navItem(d, currentView, onSelect) {
  const active = d.id === currentView;
  const node = tpl("tpl-next-navitem");
  const btn = /** @type {HTMLButtonElement} */ (node.firstElementChild);
  btn.classList.toggle("is-active", active);
  if (d.numbered) btn.classList.add("is-numbered");
  if (d.done) btn.classList.add("is-done");

  const lead = pick(node, "lead");
  if (d.numbered) {
    lead.classList.add("navitem__n");
    lead.textContent = d.done && !active ? "✓" : d.lead;
  } else {
    lead.classList.add("navitem__ic");
    lead.textContent = d.lead;
  }
  pick(node, "name").textContent = d.name;

  const chip = pick(node, "chip");
  chip.textContent = d.chip.text;
  const chipWrap = /** @type {HTMLElement} */ (chip.closest(".navitem__chip"));
  chipWrap.classList.add(`tone-${d.chip.tone}`);

  if (d.live) {
    const live = pick(node, "live");
    live.hidden = false;
    pick(node, "liveCount").textContent = String(d.live);
  }

  btn.addEventListener("click", () => onSelect(d.id));
  return node;
}

/**
 * @param {Element} host
 * @param {import('../../types.js').AppState} j
 * @param {{
 *   currentView: import('../shell.js').ViewId,
 *   session: import('../../types.js').Session | null,
 *   metaFor: (s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta,
 *   onSelectView: (id: import('../shell.js').ViewId) => void,
 *   onSelectSession: (id: string) => void,
 *   onNewSession: () => void,
 * }} ctx
 */
export function render(host, j, ctx) {
  // The spine rebuilds on every /api/state poll (~2×/s). If the operator has
  // the session <select> open, replacing its node would snap the native
  // dropdown shut on every tick — so skip the rebuild while that select is
  // focused. The change handler blur()s on pick (so the post-selection rebuild
  // still runs), and once focus leaves the select, ticks rebuild normally.
  const focused = document.activeElement;
  if (focused instanceof HTMLSelectElement && host.contains(focused)) return;
  const { currentView, session, metaFor, onSelectView, onSelectSession, onNewSession } = ctx;
  const frag = tpl("tpl-next-spine");

  // GLOBAL group
  const gnav = pick(frag, "globalNav");
  for (const d of globalDefs(j, session)) gnav.appendChild(navItem(d, currentView, onSelectView));

  // Session picker — real sessions from /api/state, newest first.
  const pickSel = /** @type {HTMLSelectElement} */ (pick(frag, "sessionPick"));
  const sessions = [...(j.sessions || [])].sort((a, b) => (a.session < b.session ? 1 : -1));
  if (!sessions.length) {
    pickSel.add(new Option("no sessions yet", "", true, true));
    pickSel.disabled = true;
  } else {
    for (const s of sessions) {
      const meta = metaFor(s);
      const label = meta.label || fmtSessionLabel(s.session) || s.session;
      const tag = s.is_current ? " ● live" : s.session_transcript ? " · tx" : "";
      const opt = new Option(`${label}${tag}`, s.session, false, s.session === session?.session);
      pickSel.add(opt);
    }
  }
  // blur() on pick so the per-tick render above no longer sees this <select>
  // focused and rebuilds the spine for the newly-selected session.
  pickSel.addEventListener("change", () => { if (pickSel.value) { pickSel.blur(); onSelectSession(pickSel.value); } });

  const newBtn = /** @type {HTMLButtonElement} */ (pick(frag, "newSession"));
  newBtn.addEventListener("click", onNewSession);

  // THIS SESSION journey
  const jnav = pick(frag, "journeyNav");
  const jdefs = journeyDefs(j, session);
  for (const d of jdefs) jnav.appendChild(navItem(d, currentView, onSelectView));

  // Progress fill — journey views advance it; global views show "—".
  const idx = jdefs.findIndex((d) => d.id === currentView);
  const onJourney = idx >= 0;
  const fillPct = onJourney ? Math.round(((idx + 1) / jdefs.length) * 100) : 0;
  /** @type {HTMLElement} */ (pick(frag, "journeyFill")).style.width = `${fillPct}%`;
  pick(frag, "journeyCap").textContent = onJourney
    ? `Stage ${idx + 1} of ${jdefs.length} · ${fillPct}%`
    : (GLOBAL_VIEWS.includes(currentView) ? "Global view" : "Session journey");

  host.replaceChildren(frag);
}

// Re-export for callers that need the group membership of the active view.
export { GLOBAL_VIEWS, JOURNEY_VIEWS };
