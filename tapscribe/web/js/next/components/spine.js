// @ts-check
// Stages spine — the slim left rail in two groups. GLOBAL (Taps · People ·
// Settings, pinned + un-numbered) and THIS SESSION (the numbered Capture →
// Recordings → Transcript → Summary journey, with the session picker + New
// session). Rebuilt every poll tick from /api/state; live status chips show
// real data where we have it.

import { tpl, pick, renderRegion } from "../../templates.js";
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
 * The three REAL session milestones, each reflecting its own deliverable —
 * shared by journeyDefs (per-stage ✓) and the progress fill so the bar and
 * the checkmarks can't disagree. Summary is a mock stage with no backend and
 * is deliberately NOT a milestone (it can never be "done").
 * @param {import('../../types.js').Session | null} sess
 */
function realMilestones(sess) {
  return {
    captured: (sess?.wav_count || 0) > 0,   // audio actually recorded
    stripped: !!sess?.stripped,             // silence-stripped clips exist
    transcribed: !!sess?.session_transcript, // a merged transcript exists
  };
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
  const tx = sess?.session_transcript || null;
  const suppressed = tx?.suppressed_count || 0;
  const { captured, stripped, transcribed } = realMilestones(sess);
  return [
    {
      // Done once audio has actually been captured — NOT once the session is
      // archived. The session you're actively recording is "done capturing"
      // the moment it has WAVs; its chip then shows live/idle for liveness.
      id: "capture", name: "Capture", lead: "1", numbered: true,
      done: captured,
      chip: !sess ? { tone: "mute", text: "no session" }
        : isCurrent ? (liveCount ? { tone: "live", text: `${liveCount} live` } : captured ? { tone: "good", text: "captured" } : { tone: "mute", text: "idle" })
          : captured ? { tone: "good", text: `${wavCount} WAVs` } : { tone: "mute", text: "no audio" },
    },
    {
      // Done when silence has been stripped (its own deliverable) — NOT when a
      // transcript exists. Chip tone matches: green only once stripped.
      id: "recordings", name: "Recordings", lead: "2", numbered: true,
      done: stripped,
      chip: !wavCount ? { tone: "mute", text: "no WAVs" }
        : stripped ? { tone: "good", text: `${wavCount} stripped` }
          : { tone: "warn", text: `${wavCount} to strip` },
    },
    {
      id: "transcript", name: "Transcript", lead: "3", numbered: true,
      done: transcribed,
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
  const { currentView, session, metaFor, onSelectView, onSelectSession, onNewSession } = ctx;

  // Build the whole spine fragment. Invoked by renderRegion only when it
  // actually swaps, so a skipped tick (operator interacting with a control
  // inside the spine) never builds.
  const buildFrag = () => {
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
    // blur() on pick so the per-tick render no longer sees this <select>
    // focused (renderRegion skips while it is) and rebuilds the spine for the
    // newly-selected session.
    pickSel.addEventListener("change", () => { if (pickSel.value) { pickSel.blur(); onSelectSession(pickSel.value); } });

    const newBtn = /** @type {HTMLButtonElement} */ (pick(frag, "newSession"));
    newBtn.addEventListener("click", onNewSession);

    // THIS SESSION journey
    const jnav = pick(frag, "journeyNav");
    const jdefs = journeyDefs(j, session);
    for (const d of jdefs) jnav.appendChild(navItem(d, currentView, onSelectView));

    // Progress fill — driven by how many of the session's REAL milestones
    // (captured → stripped → transcribed) are actually done, NOT by which tab
    // is selected. Summary is a mock stage with no backend, so it's excluded:
    // an empty session reads 0% and a transcribed one reads 100% of real work.
    const ms = realMilestones(session);
    const realStages = 3; // captured, stripped, transcribed
    const reached = (ms.captured ? 1 : 0) + (ms.stripped ? 1 : 0) + (ms.transcribed ? 1 : 0);
    const fillPct = Math.round((reached / realStages) * 100);
    /** @type {HTMLElement} */ (pick(frag, "journeyFill")).style.width = `${fillPct}%`;
    pick(frag, "journeyCap").textContent = session
      ? `${reached}/${realStages} stages · ${fillPct}%`
      : (GLOBAL_VIEWS.includes(currentView) ? "Global view" : "no session");

    return frag;
  };

  // renderRegion supersedes the old hand-rolled focus guard: the spine rebuilds
  // on every /api/state poll (~2×/s), but renderRegion skips the swap while any
  // control inside the spine (today the session <select>; tomorrow any input/
  // textarea) holds focus, so the native dropdown / caret survives the tick.
  // Always-fresh otherwise (no sig), except during interaction.
  renderRegion(host, buildFrag, {});
}

// Re-export for callers that need the group membership of the active view.
export { GLOBAL_VIEWS, JOURNEY_VIEWS };
