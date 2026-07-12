// @ts-check
// gate-allow: signal-listener — handlers ride nodes this component builds; replaced subtrees take their listeners with them, and the few persistent targets are wired exactly once per page.
// Stages spine — the slim left rail in two groups. GLOBAL (Taps · People ·
// Settings, pinned + un-numbered) and THIS SESSION (the numbered Capture →
// Recordings → Transcript → Summary journey, with the session picker + New
// session). Rebuilt every poll tick from /api/state; live status chips show
// real data where we have it.

import { tpl, pick, renderRegion } from "../../templates.js";
import { putJson } from "../../api.js";
import { fmtSessionLabel, fmtDur } from "../../formatters.js";
import { GLOBAL_VIEWS } from "../shell.js";

// Optimistic rename overlay (sid → edited label) so a rename typed in the
// Session Information card shows instantly in the name field AND the session
// picker, without waiting for the next /api/state poll. Cleared per session once
// the server's meta catches up (see buildSessInfo).
/** @type {Map<string, string>} */
const localLabels = new Map();
/** @type {Map<string, ReturnType<typeof setTimeout>>} */
const labelSaveTimers = new Map();

/**
 * Debounced PUT /api/session-meta/{sid} {label}. The server merges partial meta,
 * so aliases/prompt/hotwords are preserved. Mirrors sessions.js's rename save.
 * @param {string} sid @param {HTMLElement} statusEl
 */
function persistLabel(sid, statusEl) {
  clearTimeout(labelSaveTimers.get(sid));
  labelSaveTimers.set(sid, setTimeout(async () => {
    labelSaveTimers.delete(sid);
    const label = localLabels.get(sid);
    if (label == null) return;
    statusEl.textContent = "saving…";
    try {
      await putJson(`/api/session-meta/${encodeURIComponent(sid)}`, { label });
      if (statusEl.textContent === "saving…") {
        statusEl.textContent = "saved";
        setTimeout(() => { if (statusEl.textContent === "saved") statusEl.textContent = ""; }, 1400);
      }
    } catch (e) {
      statusEl.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
    }
  }, 600));
}

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
  const sessions = j.sessions || [];
  const liveTaps = (j.active || []).filter((a) => a.live !== false).length;
  const sessCount = sessions.length;
  const nPeople = peopleCount(j);
  return [
    {
      id: "taps", name: "Taps", lead: "🛰️",
      chip: liveTaps
        ? { tone: "live", text: `${liveTaps} live` }
        : { tone: "mute", text: `${(j.active || []).length} connected` },
    },
    {
      // The scannable all-sessions list (the spine <select> doesn't scale).
      id: "sessions", name: "Sessions", lead: "🗂️",
      chip: { tone: "mute", text: sessCount ? `${sessCount} session${sessCount === 1 ? "" : "s"}` : "none yet" },
    },
    {
      id: "people", name: "People", lead: "👥",
      chip: { tone: "mute", text: nPeople ? `${nPeople} ${nPeople === 1 ? "person" : "people"}` : "registry" },
    },
    {
      id: "settings", name: "Settings", lead: "⚙️",
      chip: { tone: "mute", text: `${j.backend || "auto"}` },
    },
  ];
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
    captured: (sess?.wav_count || 0) > 0,   // audio actually recorded
    stripped: !!sess?.stripped,             // silence-stripped clips exist
    transcribed: !!sess?.session_transcript, // a merged transcript exists
    // `summarized_at` is null only on malformed on-disk JSON (see
    // SummaryMarker's docstring) — a present-but-unstamped marker is not yet
    // a real summary.
    summarized: !!sess?.session_summary?.summarized_at,
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
  const { captured, stripped, transcribed, summarized } = realMilestones(sess);
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
      // Summary is fully wired (Local #86 / Command #82 / API #85 sources,
      // server-side persistence #83, saved config #84) — done once a summary
      // has actually been generated for this session (the `session_summary`
      // marker), matching Transcript's "not run" → "merged" shape.
      id: "summary", name: "Summary", lead: "4", numbered: true,
      done: summarized,
      chip: summarized ? { tone: "good", text: "summarized" } : { tone: "mute", text: "not run" },
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
  // Drop the optimistic overlay once the server's meta reflects the save.
  if (localLabels.has(sid) && metaFor(session).label === localLabels.get(sid)) {
    localLabels.delete(sid);
  }
  const card = tpl("tpl-next-sessinfo");
  const nameInput = /** @type {HTMLInputElement} */ (pick(card, "name"));
  const statusEl = pick(card, "status");

  nameInput.value = localLabels.get(sid) ?? metaFor(session).label ?? "";
  nameInput.placeholder = fmtSessionLabel(sid) || "name this session";
  nameInput.addEventListener("input", () => {
    localLabels.set(sid, nameInput.value);
    persistLabel(sid, statusEl);
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
    const sessions = [...(j.sessions || [])].sort((a, b) => (a.session < b.session ? 1 : -1));
    if (!sessions.length) {
      pickSel.add(new Option("no sessions yet", "", true, true));
      pickSel.disabled = true;
    } else {
      for (const s of sessions) {
        const meta = metaFor(s);
        const label = (localLabels.get(s.session) ?? meta.label) || fmtSessionLabel(s.session) || s.session;
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
    sessions.map((s) => `${s.session}~${(localLabels.get(s.session) ?? metaFor(s).label) || ""}~${s.is_current ? 1 : 0}~${s.session_transcript ? 1 : 0}`).join(","),
    session
      ? `${session.session}~${session.wav_count || 0}~${tx ? 1 : 0}~${tx?.suppressed_count || 0}~${session.stripped ? 1 : 0}~${session.is_current ? 1 : 0}~${session.session_summary?.summarized_at || ""}`
      : "",
  ].join("§");

  // renderRegion skips the swap while any control inside the spine holds focus
  // (so the native dropdown / caret survives a tick) AND when the sig is
  // unchanged — the latter is what kills the idle churn.
  renderRegion(host, buildFrag, { sig });
}
