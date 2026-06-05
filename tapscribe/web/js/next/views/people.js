// @ts-check
// Stages · People (GLOBAL · Registry). Two REAL panels, both derived purely
// from /api/state's per-session session_meta.aliases — no mock data:
//
//   1. "In this session · names" — the per-session participants strip (the
//      genuine, working feature). For the focused session we derive every
//      speaker actually present — from the merged transcript's speakers[], the
//      per-WAV speaker_name, and (for the CURRENT session) the live active[]
//      identities — and render an editable DISPLAY NAME for each. Saving a name
//      PUTs to the session's session_meta.aliases (PUT
//      /api/session-meta/{session}, server merges the partial { aliases }
//      payload), which renames that speaker in the merged transcript. Mirrors
//      session-detail.js's alias editor / deriveSpeakerKeys against the same
//      endpoint. A debounced save + optimistic local overlay keeps an
//      in-progress edit from being clobbered by the poll.
//
//   2. "People · across all sessions" — the names the operator has assigned
//      anywhere, aggregated from EVERY session's aliases into one row per
//      distinct name: avatar (initials), the name, a real "N session(s)"
//      count, and the identity code(s) mapped to that name. A real "people
//      you've recorded & named" registry — no languages, voice-mapping, input
//      kinds, or fixtures.
//
// Built once for the page; `update(j, session)` re-renders the participants
// strip each poll tick (signature-gated so an in-progress name edit isn't
// clobbered) and rebuilds the registry from every session's aliases.

import { tpl, pick } from "../../templates.js";
import { putJson } from "../../api.js";
import { speakerIndex } from "../../speakers.js";
import { header, strong, inline } from "../shell.js";

/** spk palette index → the avatar class suffix next.css `.av.spk-N` paints. */
/** @param {number} spk */
const spkClass = (spk) => `spk-${((spk % 5) + 5) % 5}`;

/** First two letters of a name, upper-cased, for an avatar chip. */
/** @param {string} s */
const initials = (s) => (s || "?").trim().slice(0, 2).toUpperCase() || "?";

/**
 * Recover the speaker slug from a recorder filename — the JS mirror of
 * `parse_wav_speaker_slug` (tapscribe/text.py). Recorder names follow
 * `<iso>_<speaker_slug>_<ident>_<uuid8>.wav`, so the slug is the middle chunk
 * between the leading timestamp and the trailing `<ident>_<uuid8>`. Returns ""
 * for anything that isn't a real recorded name (e.g. an active stream whose
 * record flag is off carries `filename = "(record off)"`).
 * @param {string} filename
 */
function speakerSlugFromFilename(filename) {
  const base = (filename || "").replace(/\.[^.]*$/, "");
  const parts = base.split("_");
  if (parts.length < 4) return "";
  return parts.slice(1, -2).join("_");
}

/**
 * Speaker identities actually present in `s`, mirroring session-detail's
 * deriveSpeakerKeys: the merged transcript's speakers[] + per-WAV
 * speaker_name. For the CURRENT session we also fold in live active[] streams
 * so a recording-but-not-yet-transcribed session still lists who's talking —
 * but ONE row per human, not two. The recorded key is the speaker slug
 * (`speaker_name` = parse_wav_speaker_slug(filename), e.g. "Atle_Havso"),
 * which is also the key the alias editor saves under; a live stream's
 * `identity` ("atle") is a DIFFERENT token for the same person. We bridge them
 * via the active stream's `filename`, which is the canonical recorder name, so
 * the slug we parse from it is exactly the recorded `speaker_name`. A live
 * stream that maps onto a recorded speaker just flips that existing row live
 * (preferring the canonical recorded key so naming still writes the right
 * alias); only a live identity with no recorded counterpart adds its own row.
 * @param {import('../../types.js').AppState} j
 * @param {import('../../types.js').Session} s
 * @returns {{ id: string, live: boolean }[]}
 */
function deriveParticipants(j, s) {
  /** @type {Map<string, boolean>} */
  const seen = new Map(); // canonical key → live (active right now)
  const add = /** @param {string} id @param {boolean} live */ (id, live) => {
    if (!id) return;
    seen.set(id, (seen.get(id) ?? false) || live);
  };
  const t = s.session_transcript;
  if (t && Array.isArray(t.speakers)) for (const sp of t.speakers) add(sp, false);
  for (const f of (s.files || [])) if (f.speaker_name) add(f.speaker_name, false);
  // The recorded speaker keys we already have — a live stream that resolves to
  // one of these must NOT add a second (identity-keyed) row for the same human.
  const recordedKeys = new Set(seen.keys());
  if (s.is_current) {
    for (const a of (j.active || [])) {
      const live = a.live !== false;
      // Prefer the recorded slug parsed from this stream's filename: when it
      // matches a recorded speaker we flip that canonical row live instead of
      // emitting a duplicate. Otherwise (no recording yet / record off) fall
      // back to the identity, which becomes this person's single row.
      const slug = speakerSlugFromFilename(a.filename);
      add(slug && recordedKeys.has(slug) ? slug : (slug || a.identity), live);
    }
  }
  return [...seen.entries()].map(([id, live]) => ({ id, live })).sort((a, b) => a.id.localeCompare(b.id));
}

/** @typedef {{ name: string, sessions: Set<string>, ids: Set<string> }} NamedPerson */

/**
 * Aggregate the display names the operator has assigned across ALL sessions.
 * Build a map of distinct NAME → { session ids it appears in, identity codes
 * mapped to it }, then sort by session-count desc, then name. Derived purely
 * from each session's session_meta.aliases (identity → display name).
 * @param {import('../../types.js').Session[]} sessions
 * @returns {NamedPerson[]}
 */
function aggregatePeople(sessions) {
  /** @type {Map<string, NamedPerson>} */
  const byName = new Map();
  for (const s of sessions) {
    const aliases = s.session_meta?.aliases || {};
    for (const [identity, raw] of Object.entries(aliases)) {
      const name = (raw || "").trim();
      if (!name) continue;
      let p = byName.get(name);
      if (!p) { p = { name, sessions: new Set(), ids: new Set() }; byName.set(name, p); }
      p.sessions.add(s.session);
      p.ids.add(identity);
    }
  }
  return [...byName.values()].sort(
    (a, b) => (b.sessions.size - a.sessions.size) || a.name.localeCompare(b.name),
  );
}

/**
 * @param {{ afterMutate: () => void }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { afterMutate } = ctx;
  const frag = tpl("tpl-next-view-people");

  const headHost = pick(frag, "head");
  const partHint = pick(frag, "partHint");
  const partList = pick(frag, "partList");
  const regHint = pick(frag, "regHint");
  const peopleHost = pick(frag, "people");

  // ---- Real alias editor state ----------------------------------------------
  /** Optimistic local alias overlay, per session id, so a save + re-poll round
   * trip doesn't clear the field the operator just typed. */
  /** @type {Map<string, Record<string, string>>} */
  const localAliases = new Map();
  /** Debounce timers per session id (debounced PUT, like the classic editor). */
  /** @type {Map<string, ReturnType<typeof setTimeout>>} */
  const saveTimers = new Map();
  let lastSig = " "; // sentinel so the first update always renders the list

  /** Effective aliases for a session = server meta merged with the local overlay. */
  /** @param {import('../../types.js').Session} s */
  const aliasesFor = (s) => ({ ...(s.session_meta?.aliases || {}), ...(localAliases.get(s.session) || {}) });

  /** Debounced PUT /api/session-meta/{session} with the merged { aliases } map. */
  /** @param {string} sid */
  const persist = (sid) => {
    clearTimeout(saveTimers.get(sid));
    saveTimers.set(sid, setTimeout(async () => {
      saveTimers.delete(sid);
      const aliases = localAliases.get(sid);
      if (!aliases) return;
      const statusEls = partList.querySelectorAll('[data-status-sess]');
      for (const el of statusEls) if (el instanceof HTMLElement && el.dataset.statusSess === sid) el.textContent = "saving…";
      try {
        await putJson(`/api/session-meta/${encodeURIComponent(sid)}`, { aliases });
        for (const el of statusEls) {
          if (el instanceof HTMLElement && el.dataset.statusSess === sid && el.textContent === "saving…") {
            el.textContent = "saved";
            setTimeout(() => { if (el.textContent === "saved") el.textContent = ""; }, 1400);
          }
        }
      } catch (e) {
        for (const el of statusEls) {
          if (el instanceof HTMLElement && el.dataset.statusSess === sid) el.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
        }
      } finally {
        afterMutate();
      }
    }, 600));
  };

  /**
   * @param {import('../../types.js').Session} s
   * @param {{ id: string, live: boolean }} p
   * @param {Record<string, string>} aliases
   */
  const partRow = (s, p, aliases) => {
    const node = tpl("tpl-next-partrow");
    const av = pick(node, "av");
    av.classList.add(spkClass(speakerIndex(p.id)));
    av.textContent = initials(aliases[p.id] || p.id);
    const code = pick(node, "code");
    code.textContent = p.id;
    code.title = p.id;
    const src = pick(node, "src");
    src.textContent = p.live ? "● live" : "recorded";
    src.classList.add(p.live ? "is-live" : "is-recorded");
    const input = /** @type {HTMLInputElement} */ (pick(node, "name"));
    input.value = aliases[p.id] || "";
    input.placeholder = p.id.replace(/[_-]+/g, " ");
    const status = pick(node, "status");
    status.dataset.statusSess = s.session;
    input.addEventListener("input", () => {
      const cur = { ...(localAliases.get(s.session) || s.session_meta?.aliases || {}) };
      if (input.value) cur[p.id] = input.value;
      else delete cur[p.id];
      localAliases.set(s.session, cur);
      // keep the avatar initials in step with the typed name
      av.textContent = initials(input.value || p.id);
      persist(s.session);
    });
    return node;
  };

  /** @param {NamedPerson} p */
  const pregRow = (p) => {
    const node = tpl("tpl-next-pregrow");
    const av = pick(node, "av");
    av.classList.add(spkClass(speakerIndex(p.name)));
    av.textContent = initials(p.name);
    pick(node, "name").textContent = p.name;
    const n = p.sessions.size;
    pick(node, "count").textContent = `${n} session${n === 1 ? "" : "s"}`;
    pick(node, "ids").textContent = [...p.ids].sort().join(" · ");
    return node;
  };

  /** Rebuild the across-sessions registry from every session's aliases. */
  /** @param {import('../../types.js').Session[]} sessions */
  const renderRegistry = (sessions) => {
    const people = aggregatePeople(sessions);
    regHint.textContent = `${people.length} named`;
    if (!people.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No one named yet — name speakers above.";
      peopleHost.replaceChildren(empty);
      return;
    }
    const list = document.createDocumentFragment();
    for (const p of people) list.appendChild(pregRow(p));
    peopleHost.replaceChildren(list);
  };

  /** Signature of the registry input so it only rebuilds when names change. */
  /** @param {import('../../types.js').Session[]} sessions */
  const registrySig = (sessions) => sessions
    .map((s) => `${s.session}=${JSON.stringify(s.session_meta?.aliases || {})}`)
    .join("§");
  let lastRegSig = " "; // sentinel so the first update always renders

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    header(headHost, {
      eyebrow: "Global · Registry",
      title: "People",
      sub: sess
        ? inline("name the speakers in ", strong(sess.session_meta?.label || sess.session))
        : "pick a session to name its speakers",
    });

    // ---- Panel 2: across-sessions registry (cheap signature gate) ----
    const sessions = j.sessions || [];
    const regSig = registrySig(sessions);
    if (regSig !== lastRegSig) {
      lastRegSig = regSig;
      renderRegistry(sessions);
    }

    // ---- Panel 1: this session's participants (alias editor) ----
    const parts = sess ? deriveParticipants(j, sess) : [];
    const aliases = sess ? aliasesFor(sess) : {};

    // Signature gate — rebuild the participants list only when the focused
    // session, its participant set, or their saved names actually change. Skips
    // while a name <input> is focused so an in-progress edit isn't wiped.
    const sig = [
      sess?.session || "",
      parts.map((p) => `${p.id}:${p.live ? 1 : 0}:${aliases[p.id] || ""}`).join("|"),
    ].join("§");
    // Bespoke focus+signature guard for the alias editor (battle-tested,
    // mirrors session-detail.js). NEW /next per-tick regions should render via
    // renderRegion (templates.js) rather than hand-rolling this.
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    const editing = focused instanceof HTMLInputElement && partList.contains(focused);
    if (sig === lastSig || editing) {
      // Still refresh the small header count on the skip path (cheap, no DOM
      // churn in the list itself).
      partHint.textContent = sess ? `${parts.length} speaker${parts.length === 1 ? "" : "s"}` : "no session";
      return;
    }
    lastSig = sig;

    partHint.textContent = sess ? `${parts.length} speaker${parts.length === 1 ? "" : "s"}` : "no session";

    if (!sess || !parts.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = sess
        ? "No speakers yet — record or transcribe this session to name its identities."
        : "Pick a session from the spine to name its speakers.";
      partList.replaceChildren(empty);
      return;
    }

    const list = document.createDocumentFragment();
    for (const p of parts) list.appendChild(partRow(sess, p, aliases));
    partList.replaceChildren(list);
  };

  return { node: frag, update };
}
