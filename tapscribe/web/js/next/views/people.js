// @ts-check
// Stages · People (GLOBAL · Registry). The canonical registry of HUMANS.
//
// REAL (the genuine TapScribe feature): the per-session participants strip.
// For the focused session we derive every speaker actually present — from the
// merged transcript's speakers[], the per-WAV speaker_name, and (for the
// CURRENT session) the live active[] identities — and render an editable
// DISPLAY NAME for each. Saving a name PUTs to the session's
// session_meta.aliases (PUT /api/session-meta/{session}, server merges the
// partial { aliases } payload), which renames that speaker in the merged
// transcript. This mirrors session-detail.js's alias editor / deriveSpeakerKeys
// against the same endpoint — no mock here.
//
// MOCK (rendered from inline fixtures, tagged "mock · not wired"): the global
// cross-session Person registry — each Person's name, primary/secondary
// language + the "transcribe as" quick-switch, the taps/diarized voices mapped
// to them, "seen in N sessions" — plus the Unidentified-voices card. These have
// no backend yet; the fixtures preview the prototype's intended shape.
//
// Built once for the page; `update(j, session)` re-renders the real
// participants strip each poll tick (signature-gated so an in-progress name
// edit isn't clobbered) and refreshes the session-dependent header. The mock
// registry is static, so it's built once and never re-rendered.

import { tpl, pick } from "../../templates.js";
import { putJson } from "../../api.js";
import { speakerIndex } from "../../speakers.js";
import { header, strong, inline } from "../shell.js";

// ---- Mock fixtures (NOT wired) ---------------------------------------------
// Inlined from the approved prototype (prototypes/_shared/mock-data.js). These
// drive ONLY the clearly-tagged mock Person registry below; the real
// per-session alias editor reads /api/state, never these.

/** @typedef {{ code: string, name: string, flag: string }} Lang */
/** @type {Record<string, Lang>} */
const LANGS = {
  nb: { code: "nb", name: "Norwegian", flag: "🇳🇴" },
  da: { code: "da", name: "Danish", flag: "🇩🇰" },
  en: { code: "en", name: "English", flag: "🇬🇧" },
};
/** @param {string} code */
const flagOf = (code) => LANGS[code]?.flag || "";
/** @param {string} code */
const langName = (code) => LANGS[code]?.name || code;

/** The input kinds a Tap can present (label + icon). */
/** @typedef {{ label: string, icon: string }} InputKind */
/** @type {Record<string, InputKind>} */
const INPUT_KINDS = {
  microphone: { label: "microphone", icon: "🎙️" },
  "line-in": { label: "line-in", icon: "🔌" },
  "stereo-mix": { label: "stereo-mix", icon: "🎚️" },
};
/** @param {string} kind @returns {InputKind} */
const inputKind = (kind) => INPUT_KINDS[kind] ?? { label: "microphone", icon: "🎙️" };

/** @typedef {{ voiceId: string, label: string, spk: number, lang: string, talkPct: number }} MockVoice */
/** @typedef {{ identity: string, name: string, input: string, live: boolean, voices: MockVoice[] | null }} MockTap */
/** @type {MockTap[]} */
const MOCK_TAPS = [
  { identity: "atle", name: "Atle Håvsø", input: "microphone", live: true, voices: null },
  { identity: "mette", name: "Mette Sørensen", input: "microphone", live: true, voices: null },
  { identity: "james", name: "James Park", input: "line-in", live: false, voices: null },
  {
    identity: "room-oslo", name: "Oslo room (Jabra Speak 710)", input: "stereo-mix", live: true,
    voices: [
      { voiceId: "room-oslo#A", label: "Speaker A", spk: 3, lang: "nb", talkPct: 58 },
      { voiceId: "room-oslo#B", label: "Speaker B", spk: 4, lang: "en", talkPct: 42 },
    ],
  },
  {
    identity: "demo-clip", name: "Vortiago launch clip", input: "stereo-mix", live: true,
    voices: [
      { voiceId: "demo-clip#A", label: "Speaker A", spk: 1, lang: "en", talkPct: 71 },
      { voiceId: "demo-clip#B", label: "Speaker B", spk: 3, lang: "en", talkPct: 29 },
    ],
  },
];

/** @typedef {{ id: string, name: string, initials: string, spk: number, primaryLang: string, secondaryLang: string | null, sessionsSeen: number, note: string }} MockPerson */
/** @type {MockPerson[]} */
const MOCK_PEOPLE = [
  { id: "atle", name: "Atle Håvsø", initials: "AH", spk: 0, primaryLang: "nb", secondaryLang: "en", sessionsSeen: 14, note: "Host. Norwegian, switches to English for guests." },
  { id: "mette", name: "Mette Sørensen", initials: "MS", spk: 1, primaryLang: "da", secondaryLang: "en", sessionsSeen: 9, note: "Danish, comfortable in English." },
  { id: "henrik", name: "Henrik Lie", initials: "HL", spk: 3, primaryLang: "nb", secondaryLang: "en", sessionsSeen: 4, note: "Often joins from the Oslo room mic — diarized as a room voice." },
  { id: "james", name: "James Park", initials: "JP", spk: 4, primaryLang: "en", secondaryLang: null, sessionsSeen: 2, note: "Guest. English only." },
];

// tap/voice key → Person id (null = Unidentified). Keys are a single-person
// tap's identity or a diarized voiceId.
/** @type {Record<string, string | null>} */
const MOCK_MAP = {
  atle: "atle", mette: "mette", james: "james",
  "room-oslo#A": "henrik", "room-oslo#B": null,
  "demo-clip#A": "mette", "demo-clip#B": null,
};

/** spk palette index → the avatar/ink class suffix the next.css `.spk-N` uses. */
/** @param {number} spk */
const spkClass = (spk) => `spk-${((spk % 5) + 5) % 5}`;

/**
 * Build the mapped-taps/voices rows for one mock Person.
 * @returns {{ single: MockTap[], voices: { tap: MockTap, voice: MockVoice }[] }}
 * @param {string} personId
 */
function mappingsForPerson(personId) {
  /** @type {MockTap[]} */
  const single = [];
  /** @type {{ tap: MockTap, voice: MockVoice }[]} */
  const voices = [];
  for (const t of MOCK_TAPS) {
    if (t.voices && t.voices.length) {
      for (const v of t.voices) if (MOCK_MAP[v.voiceId] === personId) voices.push({ tap: t, voice: v });
    } else if (MOCK_MAP[t.identity] === personId) {
      single.push(t);
    }
  }
  return { single, voices };
}

/** Diarized voices across all mock taps not yet mapped to any Person. */
function unidentifiedVoices() {
  /** @type {{ tap: MockTap, voice: MockVoice }[]} */
  const out = [];
  for (const t of MOCK_TAPS) {
    if (!t.voices) continue;
    for (const v of t.voices) if (!MOCK_MAP[v.voiceId]) out.push({ tap: t, voice: v });
  }
  return out;
}

/** Small input badge (icon + kind), matching the Taps view's `.tag.inp`. */
/** @param {HTMLElement} el @param {string} kind */
function paintInputBadge(el, kind) {
  const k = inputKind(kind);
  el.classList.add(`inp--${kind}`);
  el.textContent = `${k.icon} ${k.label}`;
}

// ---- Mock registry (built once) --------------------------------------------

/** @param {MockPerson} p */
function mockPersonCard(p) {
  const node = tpl("tpl-next-person");
  const av = pick(node, "av");
  av.textContent = p.initials;
  av.classList.add(spkClass(p.spk));
  const nameEl = pick(node, "name");
  nameEl.textContent = p.name;
  nameEl.classList.add(`ink-${spkClass(p.spk)}`);
  pick(node, "note").textContent = p.note;
  pick(node, "seen").textContent = String(p.sessionsSeen);

  // LEFT col: primary/secondary language + the (disabled) "transcribe as" switch.
  const langpair = pick(node, "langpair");
  langpair.appendChild(langPill("1st", p.primaryLang, true));
  if (p.secondaryLang) {
    const sep = document.createElement("span");
    sep.className = "langsep";
    sep.textContent = "·";
    langpair.appendChild(sep);
    langpair.appendChild(langPill("2nd", p.secondaryLang, false));
  } else {
    const none = document.createElement("span");
    none.className = "dim langnone";
    none.textContent = "· no secondary";
    langpair.appendChild(none);
  }
  const qs = pick(node, "quickswitch");
  const opts = [p.primaryLang, p.secondaryLang, "en"].filter(
    /** @param {string | null} v */ (v, i, a) => !!v && a.indexOf(v) === i);
  opts.forEach((code, i) => {
    if (!code) return;
    const b = tpl("tpl-next-qbtn");
    if (i === 0) /** @type {HTMLElement} */ (b.firstElementChild)?.classList.add("is-active");
    pick(b, "flag").textContent = flagOf(code);
    pick(b, "name").textContent = langName(code);
    qs.appendChild(b);
  });

  // RIGHT col: the taps/diarized voices mapped to this Person.
  const maps = pick(node, "maps");
  const { single, voices } = mappingsForPerson(p.id);
  for (const t of single) {
    maps.appendChild(idRow({
      avText: "", avSpk: null, code: t.identity, input: t.input,
      tail: { text: t.live ? "live" : "idle", cls: t.live ? "is-live" : "is-idle" },
    }));
  }
  for (const { tap, voice } of voices) {
    maps.appendChild(idRow({
      avText: voice.label.replace("Speaker ", ""), avSpk: voice.spk,
      code: `${tap.identity} · ${voice.label}`, input: tap.input, tail: null,
    }));
  }
  if (!single.length && !voices.length) {
    const empty = document.createElement("div");
    empty.className = "dim idrow-empty";
    empty.textContent = "no taps mapped yet";
    maps.appendChild(empty);
  }
  return node;
}

/** @param {"1st"|"2nd"} role @param {string} code @param {boolean} primary */
function langPill(role, code, primary) {
  const node = tpl("tpl-next-langpill");
  const root = /** @type {HTMLElement} */ (node.firstElementChild);
  if (primary) root.classList.add("primary");
  pick(node, "role").textContent = role;
  pick(node, "flag").textContent = flagOf(code);
  pick(node, "name").textContent = langName(code);
  return node;
}

/**
 * @param {{ avText: string, avSpk: number | null, code: string, input: string, tail: { text: string, cls: string } | null }} o
 */
function idRow(o) {
  const node = tpl("tpl-next-idrow");
  const av = pick(node, "av");
  if (o.avSpk == null) { av.classList.add("unid"); av.textContent = o.avText || "·"; }
  else { av.classList.add(spkClass(o.avSpk)); av.textContent = o.avText; }
  pick(node, "code").textContent = o.code;
  paintInputBadge(pick(node, "input"), o.input);
  const tail = pick(node, "tail");
  if (o.tail) { tail.textContent = o.tail.text; tail.classList.add("tag", o.tail.cls); }
  else tail.remove();
  return node;
}

function unidentifiedCard() {
  const node = tpl("tpl-next-person-unid");
  const col = pick(node, "unid");
  const unid = unidentifiedVoices();
  for (const { tap, voice } of unid) {
    const row = tpl("tpl-next-idrow");
    const av = pick(row, "av");
    av.classList.add(spkClass(voice.spk));
    av.textContent = voice.label.replace("Speaker ", "");
    pick(row, "code").textContent = `${tap.identity} · ${voice.label}`;
    paintInputBadge(pick(row, "input"), tap.input);
    const tail = pick(row, "tail");
    const lang = document.createElement("span");
    lang.className = "tag";
    lang.textContent = `${flagOf(voice.lang)} ${langName(voice.lang)}`;
    const map = document.createElement("button");
    map.className = "act act--sm";
    map.type = "button";
    map.disabled = true;
    map.textContent = "→ map to a Person";
    tail.replaceWith(lang, map);
  }
  if (!unid.length) {
    const empty = document.createElement("div");
    empty.className = "dim idrow-empty";
    empty.textContent = "all voices identified";
    col.appendChild(empty);
  }
  return node;
}

// ---- Real per-session participants (alias editor) --------------------------

/**
 * Speaker identities actually present in `s`, mirroring session-detail's
 * deriveSpeakerKeys: the merged transcript's speakers[] + per-WAV
 * speaker_name. For the CURRENT session we also fold in the live active[]
 * identities so a recording-but-not-yet-transcribed session still lists who's
 * talking. Each carries where it was seen for the source badge.
 * @param {import('../../types.js').AppState} j
 * @param {import('../../types.js').Session} s
 * @returns {{ id: string, live: boolean }[]}
 */
function deriveParticipants(j, s) {
  /** @type {Map<string, boolean>} */
  const seen = new Map(); // id → live (active in the current session right now)
  const add = /** @param {string} id @param {boolean} live */ (id, live) => {
    if (!id) return;
    seen.set(id, (seen.get(id) ?? false) || live);
  };
  const t = s.session_transcript;
  if (t && Array.isArray(t.speakers)) for (const sp of t.speakers) add(sp, false);
  for (const f of (s.files || [])) if (f.speaker_name) add(f.speaker_name, false);
  if (s.is_current) for (const a of (j.active || [])) add(a.identity, a.live !== false);
  return [...seen.entries()].map(([id, live]) => ({ id, live })).sort((a, b) => a.id.localeCompare(b.id));
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
  const peopleHost = pick(frag, "people");

  // Build the MOCK registry once — it's static.
  for (const p of MOCK_PEOPLE) peopleHost.appendChild(mockPersonCard(p));
  peopleHost.appendChild(unidentifiedCard());

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
    av.textContent = (aliases[p.id] || p.id).slice(0, 2).toUpperCase();
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
      av.textContent = (input.value || p.id).slice(0, 2).toUpperCase();
      persist(s.session);
    });
    return node;
  };

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    header(headHost, {
      eyebrow: "Global · Registry",
      title: "People",
      sub: sess
        ? inline("name the speakers in ", strong(sess.session_meta?.label || sess.session), " · registry + languages are mock")
        : "canonical humans · pick a session to name its speakers",
    });

    const parts = sess ? deriveParticipants(j, sess) : [];
    const aliases = sess ? aliasesFor(sess) : {};

    // Signature gate — rebuild the participants list only when the focused
    // session, its participant set, or their saved names actually change. Skips
    // while a name <input> is focused so an in-progress edit isn't wiped.
    const sig = [
      sess?.session || "",
      parts.map((p) => `${p.id}:${p.live ? 1 : 0}:${aliases[p.id] || ""}`).join("|"),
    ].join("§");
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
        ? "No speakers yet — once taps record (or you transcribe) this session, each identity appears here to name."
        : "Pick a session from the spine to name the speakers in it.";
      partList.replaceChildren(empty);
      return;
    }

    const list = document.createDocumentFragment();
    for (const p of parts) list.appendChild(partRow(sess, p, aliases));
    partList.replaceChildren(list);
  };

  return { node: frag, update };
}
