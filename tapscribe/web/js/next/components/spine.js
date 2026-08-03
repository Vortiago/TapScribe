// @ts-check
// gate-allow: signal-listener — handlers ride nodes this component builds; replaced subtrees take their listeners with them, and the few persistent targets are wired exactly once per page.
// Stages spine — the slim left rail in two groups. GLOBAL (Taps · People ·
// Settings, pinned + un-numbered) and THIS SESSION (the numbered Capture →
// Recordings → Transcript → Summary journey, with the session picker + New
// session). Rebuilt every poll tick from /api/state; live status chips show
// real data where we have it.

import { tpl, pick, renderRegion } from "../../templates.js";
import { fmtSessionLabel, fmtDur } from "../../formatters.js";
import { GLOBAL_VIEWS, VIEWS, newestFirst } from "../shell.js";
import { editSessionLabel, pendingOr, sessionLabelFor } from "../session-labels.js";

// Renames typed in the Session Information card go through
// next/session-labels.js (#355), which owns the pending edit, the debounced PUT,
// the catch-up sweep AND the status cells it narrates into — shared with the
// Sessions view, which renames the same session through the same endpoint.

/**
 * @typedef {{ tone: "live"|"good"|"warn"|"mute", text: string }} Chip
 * @typedef {{
 *   id: import('../shell.js').ViewId, name: string, lead: string,
 *   chip: Chip, numbered?: boolean, done?: boolean,
 * }} NavDef
 */

/**
 * The People chip's count — the ADR-0009 registry (`j.people`, server-resolved
 * from every session's Roster + live identities), NOT the pre-ADR-0009
 * `session_meta.aliases` shadow join. Exported so it agrees, by construction,
 * with the People view's own count (`people.js`'s `people.length`) rather than
 * drifting from it. Factored out as a pure helper so it's unit-testable
 * without a DOM (see spine.test.js).
 * @param {import('../../types.js').AppState} j
 * @returns {number}
 */
export function peopleCount(j) {
  return (j.people || []).length;
}

/**
 * @param {import('../../types.js').AppState} j
 * @param {import('../../types.js').Session | null} sess
 * @returns {NavDef[]}
 */
function globalDefs(j, sess) {
  return [...VIEWS.entries()]
    .filter(([, e]) => e.group === "global")
    .map(([id, entry]) => ({
      id, name: entry.name, lead: entry.lead,
      chip: buildChip(id, j, sess),
    }));
}

/**
 * The four REAL session milestones, each reflecting its own deliverable —
 * shared by journeyDefs (per-stage ✓) and the progress fill so the bar and
 * the checkmarks can't disagree. Summary (#83/#84/#85/#86) is fully wired —
 * a generated-and-persisted summary ships as the session's `session_summary`
 * marker on /api/state — so it's a real milestone like the other three.
 * Exported so the derivation is unit-testable without a DOM (see
 * spine.test.js).
 * @param {import('../../types.js').Session | null} sess
 */
export function realMilestones(sess) {
  return {
    // Audio actually recorded — NOT the session being archived. The session
    // being recorded is "done capturing" the moment it has WAVs.
    captured: (sess?.wav_count || 0) > 0,
    // Silence-stripped clips exist — NOT a transcript existing. Its own
    // deliverable, so the chip goes green only once stripped.
    stripped: !!sess?.stripped,
    transcribed: !!sess?.session_transcript, // a merged transcript exists
    // `summarized_at` is null only on malformed on-disk JSON (see
    // SummaryMarker's docstring) — a present-but-unstamped marker is not yet
    // a real summary.
    summarized: !!sess?.session_summary?.summarized_at,
  };
}

/**
 * Dynamic chip text/tone per view — the live state (j.active, sess.wav_count)
 * VIEWS cannot carry. Each case derives what it needs from `j`/`sess`, so the
 * two callers need not agree on a positional argument order.
 * @param {import('../shell.js').ViewId} id
 * @param {import('../../types.js').AppState} j
 * @param {import('../../types.js').Session | null} sess
 * @returns {Chip}
 */
function buildChip(id, j, sess) {
  switch (id) {
    case "taps": {
      const liveTaps = (j.active || []).filter((a) => a.live !== false).length;
      return liveTaps
        ? { tone: "live", text: `${liveTaps} live` }
        : { tone: "mute", text: `${(j.active || []).length} connected` };
    }
    case "sessions": {
      const sessCount = (j.sessions || []).length;
      return { tone: "mute", text: sessCount ? `${sessCount} session${sessCount === 1 ? "" : "s"}` : "none yet" };
    }
    case "people": {
      const nPeople = peopleCount(j);
      return { tone: "mute", text: nPeople ? `${nPeople} ${nPeople === 1 ? "person" : "people"}` : "registry" };
    }
    case "settings":
      return { tone: "mute", text: `${j.backend || "auto"}` };
    case "capture": {
      const isCurrent = !!sess?.is_current;
      const liveCount = isCurrent ? (j.active || []).filter((a) => a.live !== false).length : 0;
      const captured = (sess?.wav_count || 0) > 0;
      if (!sess) return { tone: "mute", text: "no session" };
      if (isCurrent) return liveCount ? { tone: "live", text: `${liveCount} live` } : captured ? { tone: "good", text: "captured" } : { tone: "mute", text: "idle" };
      return captured ? { tone: "good", text: `${sess.wav_count} WAVs` } : { tone: "mute", text: "no audio" };
    }
    case "recordings": {
      const wavCount = sess?.wav_count || 0;
      if (!wavCount) return { tone: "mute", text: "no WAVs" };
      if (sess?.stripped) return { tone: "good", text: `${wavCount} stripped` };
      return { tone: "warn", text: `${wavCount} to strip` };
    }
    case "transcript": {
      const tx = sess?.session_transcript || null;
      const suppressed = tx?.suppressed_count || 0;
      if (tx) return suppressed ? { tone: "warn", text: `${suppressed} suppressed` } : { tone: "good", text: "merged" };
      return { tone: "mute", text: "not run" };
    }
    case "summary": {
      const { summarized } = realMilestones(sess);
      return summarized ? { tone: "good", text: "summarized" } : { tone: "mute", text: "not run" };
    }
    default:
      // A VIEWS entry with no case here throws on the next render rather than
      // rendering a chipless nav item.
      throw new Error(`buildChip: unknown view id "${id}" — add a case or update VIEWS`);
  }
}

/**
 * @param {import('../../types.js').AppState} j
 * @param {import('../../types.js').Session | null} sess
 * @returns {NavDef[]}
 */
function journeyDefs(j, sess) {
  const { captured, stripped, transcribed, summarized } = realMilestones(sess);
  return [...VIEWS.entries()]
    .filter(([, e]) => e.group === "journey")
    .map(([id, entry]) => {
      const chip = buildChip(id, j, sess);
      /** @type {NavDef} */
      const base = { id, name: entry.name, lead: entry.lead, numbered: true, chip };
      // Map done states from realMilestones onto the journey entries
      // that correspond to milestone stages.
      if (id === "capture") base.done = captured;
      if (id === "recordings") base.done = stripped;
      if (id === "transcript") base.done = transcribed;
      if (id === "summary") base.done = summarized;
      return base;
    });
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

  btn.addEventListener("click", () => onSelect(d.id));
  return node;
}

/**
 * Build the Session Information card (spine foot): editable name + compact
 * stats for the focused session. Wires the name input to a debounced rename
 * with an optimistic overlay; renderRegion keeps the input alive while it's
 * focused so a poll tick can't wipe an in-progress edit.
 * @param {import('../../types.js').Session} session
 * @param {(s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta} metaFor
 */
function buildSessInfo(session, metaFor) {
  const sid = session.session;
  // (The optimistic overlay is dropped by render()'s every-session catch-up
  // sweep, not here — a buildSessInfo-local drop only ever saw the FOCUSED
  // session and stranded every other session's overlay.)
  const card = tpl("tpl-next-sessinfo");
  const nameInput = /** @type {HTMLInputElement} */ (pick(card, "name"));
  const statusEl = pick(card, "status");
  // Stamp the cell with the session it reports on, so a settling save can
  // re-resolve the LIVE one rather than writing into this (by then possibly
  // detached) node — see session-labels.js's statusCellsFor.
  statusEl.dataset.statusSid = sid;

  nameInput.value = pendingOr(sid, metaFor(session).label ?? "");
  nameInput.placeholder = fmtSessionLabel(sid) || "name this session";
  nameInput.addEventListener("input", () => {
    editSessionLabel(sid, nameInput.value);
  });

  const live = !!session.is_current;
  statusEl.textContent = live ? "● live" : "idle";
  statusEl.classList.add(live ? "is-live" : "is-idle");

  // total_duration_s is precomputed server-side — /api/state no longer ships
  // the per-WAV files[] array (a huge session re-shipped it every poll).
  const totalDur = session.total_duration_s || 0;
  const wn = session.wav_count || 0;
  const tx = session.session_transcript ? "tx ✓" : "no tx";
  pick(card, "stats").textContent = `${wn} WAV${wn === 1 ? "" : "s"} · ${fmtDur(totalDur)} · ${tx}`;

  const idEl = pick(card, "id");
  idEl.textContent = sid;
  idEl.title = sid;
  return card;
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
    const sessions = [...(j.sessions || [])].sort(newestFirst);
    if (!sessions.length) {
      pickSel.add(new Option("no sessions yet", "", true, true));
      pickSel.disabled = true;
    } else {
      for (const s of sessions) {
        const meta = metaFor(s);
        const label = pendingOr(s.session, meta.label) || fmtSessionLabel(s.session) || s.session;
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
    // (captured → stripped → transcribed → summarized) are actually done, NOT
    // by which tab is selected: an empty session reads 0% and a fully
    // summarized one reads 100% of real work.
    const ms = realMilestones(session);
    const realStages = 4; // captured, stripped, transcribed, summarized
    const reached =
      (ms.captured ? 1 : 0) + (ms.stripped ? 1 : 0) + (ms.transcribed ? 1 : 0) + (ms.summarized ? 1 : 0);
    const fillPct = Math.round((reached / realStages) * 100);
    /** @type {HTMLElement} */ (pick(frag, "journeyFill")).style.width = `${fillPct}%`;
    pick(frag, "journeyCap").textContent = session
      ? `${reached}/${realStages} stages · ${fillPct}%`
      : (GLOBAL_VIEWS.includes(currentView) ? "Global view" : "no session");

    // Session Information card (foot) — editable name + stats, or a muted
    // placeholder when no session is focused.
    const sessInfoHost = pick(frag, "sessInfo");
    if (session) {
      sessInfoHost.appendChild(buildSessInfo(session, metaFor));
    } else {
      sessInfoHost.classList.add("is-empty");
      sessInfoHost.textContent = "no session — pick or start one above";
    }

    return frag;
  };

  // Signature of everything the spine displays. Without it the spine rebuilt on
  // EVERY poll (~2×/s) — ~30 nodes + ~11 listeners/tick of collectable garbage
  // that the operator's tab accumulated between GCs until it OOMed. The sig
  // gates renderRegion so it only rebuilds on a real change. It must include
  // every value buildFrag reads — a miss leaves a stale spine. The focused
  // session's growing WAV duration is deliberately EXCLUDED (it changes every
  // tick during recording); wav_count covers "a new utterance landed", and the
  // sessInfo duration stat refreshes on that rebuild.
  const sessions = j.sessions || [];
  const active = j.active || [];
  const tx = session?.session_transcript || null;
  const sig = [
    currentView,
    j.backend || "",
    active.filter((a) => a.live !== false).length,
    active.length,
    sessions.length,
    // The People chip's count reads the ADR-0009 registry directly (not a
    // per-session walk), so ONE scalar covers it here.
    peopleCount(j),
    // The label term is the shared shell.js sig helper bound to the spine's
    // rename overlay — its doc carries the metaFor-equivalence rationale
    // (value-identical to what buildFrag paints, no throwaway EffectiveMeta
    // per session per tick); buildFrag itself keeps metaFor and only runs
    // past the gate.
    sessions.map((s) => `${s.session}~${sessionLabelFor(s)}~${s.is_current ? 1 : 0}~${s.session_transcript ? 1 : 0}`).join(","),
    session
      ? `${session.session}~${session.wav_count || 0}~${tx ? 1 : 0}~${tx?.suppressed_count || 0}~${session.stripped ? 1 : 0}~${session.is_current ? 1 : 0}~${session.session_summary?.summarized_at || ""}`
      : "",
  ].join("§");

  // renderRegion skips the swap while any control inside the spine holds focus
  // (so the native dropdown / caret survives a tick) AND when the sig is
  // unchanged — the latter is what kills the idle churn.
  renderRegion(host, buildFrag, { sig });
}
