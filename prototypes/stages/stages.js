// =============================================================================
// TapScribe · Stages — operator-grade dashboard.
//
// Left spine, TWO groups:
//   GLOBAL (pinned, un-numbered, set apart by a divider — NOT the session
//   journey):  Taps · People · Settings
//   THIS SESSION (the numbered journey, with the session picker + New session):
//   1 Capture → 2 Recordings → 3 Transcript
//
// One dense, single-focus workspace at a time. Sharp + utilitarian: hairline
// separators, thin borders, monospace data, no pills/bubbles. window.gotoView
// accepts: taps, people, settings, capture, recordings, transcript.
//
// Real TapScribe features each get a home; the net-new ones (per-mic Person
// profiles, multi-person/diarized taps, primary+secondary language, the
// waveform cut preview) are mocked UI flagged as such inline.
// =============================================================================

import {
  LANGS, SPEAKERS, MODELS, selectedModel, LIVE_TAPS, LIVE_CAPTIONS,
  SESSIONS, STRIP_DEFAULTS, REP_WAV, TRANSCRIPT, computeRegions, helpers,
  speakerById, APP,
  GATE_DEFAULTS, GATE_KINDS, PERSONS, personById, TAP_PERSON_MAP,
  HALLUCINATION_RULES, PROMPTS, WAV_TRANSCRIPTS, TRANSCRIBE_JOB,
} from "../_shared/mock-data.js";

const { clock, clockH } = helpers;
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const flagOf = (code) => LANGS[code]?.flag || "";
const langName = (code) => LANGS[code]?.name || code;

// ----- live, mutable UI state ------------------------------------------------
const state = {
  view: "capture", // one of taps|people|settings|capture|recordings|transcript
  sessionId: SESSIONS.find((s) => s.current)?.id || SESSIONS[0].id,
  isFresh: false, // "New session" empty state overlaying the live one
  knobs: { ...STRIP_DEFAULTS }, // Recordings strip-silence live re-cut
  selectedWav: 0, // which WAV the Recordings hero waveform shows
  recSource: "stripped", // original | stripped source toggle (Recordings)
  // engines: Settings holds the DEFAULT; Transcript holds the per-session OVERRIDE
  engineDefault: { ...selectedModel },
  engineOverride: { ...selectedModel },
  gate: { ...GATE_DEFAULTS }, // speech-gate LiveConfig knobs (Taps)
  recordingArmed: APP.recordingEnabled, // global RECORDING_ENABLED toggle
  auditOpen: false,
  expandedTap: "room-oslo", // which Taps row shows its config strip (room → shows multi → Speaker A/B)
  // per-tap single|multi mode (rooms default multi)
  tapMode: Object.fromEntries(LIVE_TAPS.map((t) => [t.identity, speakerById(t.identity)?.isRoom ? "multi" : "single"])),
  // per-Person "transcribe as" quick switch (defaults to primary lang)
  transcribeAs: Object.fromEntries(PERSONS.map((p) => [p.id, p.primaryLang])),
};

// The numbered journey, in order. The GLOBAL group sits above and is excluded
// from both numbering and the progress fill.
const JOURNEY = ["capture", "recordings", "transcript"];
const GLOBAL = ["taps", "people", "settings"];
const isJourney = (v) => JOURNEY.includes(v);
const ALL_VIEWS = [...GLOBAL, ...JOURNEY];

// A synthetic, empty session for the "New session" button.
const FRESH_SESSION = {
  id: "__fresh__", label: "New session", folder: "recordings/(pending)",
  startedAt: new Date().toISOString(), durationS: 0, wavCount: 0,
  speakers: [], current: true, hasTranscript: false, langs: [], fresh: true,
};
function session() {
  if (state.isFresh) return FRESH_SESSION;
  return SESSIONS.find((s) => s.id === state.sessionId) || SESSIONS[0];
}

// =============================================================================
// NAV definitions — GLOBAL items + the numbered journey, each with a live chip.
// =============================================================================
function globalDefs() {
  const liveTaps = LIVE_TAPS.filter((t) => t.live).length;
  return [
    { id: "taps", ic: "🛰️", name: "Taps", live: liveTaps, chip: { tone: "mute", text: `${LIVE_TAPS.length} connected` } },
    { id: "people", ic: "👥", name: "People", chip: { tone: "mute", text: `${PERSONS.length} persons` } },
    { id: "settings", ic: "⚙️", name: "Settings", chip: { tone: "mute", text: `${APP.backend} · ${state.engineDefault.model}` } },
  ];
}
function journeyDefs() {
  const sess = session();
  const fresh = !!sess.fresh;
  const liveCount = sess.current ? LIVE_TAPS.filter((t) => t.live).length : 0;
  const wavs = wavModel();
  const needTune = wavs.filter((w) => w.needsTune).length;
  const suppressed = TRANSCRIPT.lines.filter((l) => l.suppressed).length;
  return [
    {
      id: "capture", n: 1, ic: "🎙️", name: "Capture",
      chip: fresh ? { tone: "mute", text: "no taps yet" }
        : sess.current ? (liveCount ? { tone: "live", text: `${liveCount} live` } : { tone: "mute", text: "idle" })
          : { tone: "good", text: `${sess.speakers.length} sources` },
      done: !fresh && !sess.current,
    },
    {
      id: "recordings", n: 2, ic: "🌊", name: "Recordings",
      chip: fresh ? { tone: "mute", text: "no WAVs" }
        : needTune ? { tone: "warn", text: `${needTune} to tune` } : { tone: "good", text: `${wavs.length} WAVs` },
      done: !fresh && sess.hasTranscript,
    },
    {
      id: "transcript", n: 3, ic: "📝", name: "Transcript",
      chip: fresh ? { tone: "mute", text: "nothing yet" }
        : sess.hasTranscript ? (suppressed ? { tone: "warn", text: `${suppressed} suppressed` } : { tone: "good", text: "merged" })
          : { tone: "mute", text: "not run" },
      done: !fresh && sess.hasTranscript,
    },
  ];
}

// Per-session WAV model: originals + stripped-region clips, synthesized from
// REP_WAV + strip-silence so "needs tuning" is real. Empty for fresh sessions.
function wavModel() {
  const sess = session();
  if (sess.fresh || sess.wavCount === 0) return [];
  const base = [
    { sp: "atle", t: "09:04:12" }, { sp: "mette", t: "09:05:48" },
    { sp: "room-oslo", t: "09:07:03" }, { sp: "atle", t: "09:09:21" },
    { sp: "james", t: "09:11:40" },
  ].slice(0, Math.min(5, Math.max(2, Math.round(sess.wavCount / 8))));
  return base.map((c, i) => {
    const dur = [48, 31, 62, 22, 18][i] ?? 30;
    const clips = computeRegions(REP_WAV.peaks, REP_WAV.durationS, STRIP_DEFAULTS).clips;
    const needsTune = i % 2 === 0;
    return { ...c, dur, clips: needsTune ? clips : 1, needsTune, idx: i };
  });
}

// =============================================================================
// SPINE
// =============================================================================
function navItem(d, { numbered = false, done = false } = {}) {
  const active = d.id === state.view;
  const lead = numbered
    ? `<span class="navitem__n">${done && !active ? "✓" : d.n}</span>`
    : `<span class="navitem__ic">${d.ic}</span>`;
  const right = d.live
    ? `<span class="navitem__live"><span class="dot"></span>${d.live}</span>`
    : "";
  const node = el(`
    <button class="navitem ${active ? "is-active" : ""} ${done ? "is-done" : ""}" data-view="${esc(d.id)}">
      ${lead}
      <span class="navitem__body">
        <span class="navitem__name">${esc(d.name)}</span>
        <span class="navitem__chip tone-${esc(d.chip.tone)}"><span class="dot"></span>${esc(d.chip.text)}</span>
      </span>
      ${right}
    </button>`);
  node.addEventListener("click", () => goView(d.id));
  return node;
}

function renderSpine() {
  const sess = session();

  const gnav = document.getElementById("globalNav");
  gnav.innerHTML = "";
  for (const d of globalDefs()) gnav.appendChild(navItem(d));

  document.getElementById("sessionLabel").textContent = sess.label || "(untitled session)";
  document.getElementById("sessionMeta").textContent = sess.fresh
    ? "fresh · 0 clips" : `${clockH(sess.durationS)} · ${sess.wavCount} clips`;
  document.getElementById("sessionLive").style.display = sess.current ? "" : "none";

  const jnav = document.getElementById("journeyNav");
  jnav.innerHTML = "";
  const defs = journeyDefs();
  for (const d of defs) jnav.appendChild(navItem(d, { numbered: true, done: d.done }));

  // journey progress fill — global views show no advance
  const idx = defs.findIndex((d) => d.id === state.view);
  const onJourney = idx >= 0;
  const fill = onJourney ? Math.round(((idx + 1) / defs.length) * 100) : 0;
  document.getElementById("journeyFill").style.width = `${fill}%`;
  document.getElementById("journeyCap").innerHTML = onJourney
    ? `<span>Stage ${idx + 1} of ${defs.length}</span><span>${fill}%</span>`
    : `<span>Global view</span><span>—</span>`;
}

function renderSessionMenu() {
  const menu = document.getElementById("sessionMenu");
  menu.innerHTML = "";
  for (const s of SESSIONS) {
    const badge = s.current
      ? '<span class="smitem__badge" style="color:#ffb3b3;border-color:#4a2626">live</span>'
      : (s.hasTranscript ? '<span class="smitem__badge">tx</span>' : '<span class="smitem__badge">raw</span>');
    const item = el(`
      <button class="smitem ${!state.isFresh && s.id === state.sessionId ? "is-current" : ""}">
        <span class="smitem__dot"></span>
        <span class="smitem__body">
          <span class="smitem__label">${esc(s.label || "(untitled)")}</span>
          <span class="smitem__meta">${esc(s.startedAt.slice(0, 10))} · ${clockH(s.durationS)} · ${s.wavCount} clips</span>
        </span>
        ${badge}
      </button>`);
    item.addEventListener("click", () => {
      state.isFresh = false; state.sessionId = s.id; menu.hidden = true;
      state.view = s.current ? "capture" : "transcript";
      state.selectedWav = 0;
      render();
    });
    menu.appendChild(item);
  }
  // session actions surfaced from the picker: absorb / prune-empty / delete
  const actions = el(`
    <div class="smactions">
      <button class="smact" data-act="absorb" title="Absorb another session's WAVs into this one">↳ absorb</button>
      <button class="smact" data-act="prune" title="Prune empty sessions">⌫ prune empty</button>
      <button class="smact danger" data-act="delete" title="Delete this session">✕ delete</button>
    </div>`);
  actions.querySelectorAll(".smact").forEach((b) => b.addEventListener("click", () => pulse(b)));
  menu.appendChild(actions);
}

document.getElementById("sessionPick").addEventListener("click", () => {
  const m = document.getElementById("sessionMenu");
  m.hidden = !m.hidden;
});
document.getElementById("newSession").addEventListener("click", () => {
  state.isFresh = true; state.view = "capture"; state.selectedWav = 0; state.auditOpen = false;
  document.getElementById("sessionMenu").hidden = true;
  render(); window.scrollTo(0, 0);
});

// =============================================================================
// WORKSPACE shell
// =============================================================================
// `actions` and `sub` are raw trusted markup built locally; the eyebrow/title
// are escaped. Any dynamic string inside `sub`/`actions` is run through esc() at
// its build site.
function header({ eyebrow, title, sub, actions }) {
  return `
    <div class="whead">
      <div class="whead__l">
        <div class="whead__eyebrow">${esc(eyebrow)}</div>
        <h1 class="whead__title">${esc(title)}</h1>
        <div class="whead__sub">${sub || ""}</div>
      </div>
      <div class="whead__r">${actions || ""}</div>
    </div>`;
}

function render() {
  renderSpine();
  renderSessionMenu();
  const root = document.getElementById("workInner");
  root.innerHTML = "";
  let frag;
  switch (state.view) {
    case "taps": frag = viewTaps(); break;
    case "people": frag = viewPeople(); break;
    case "settings": frag = viewSettings(); break;
    case "capture": frag = viewCapture(); break;
    case "recordings": frag = viewRecordings(); break;
    case "transcript": frag = viewTranscript(); break;
    default: frag = viewCapture();
  }
  root.appendChild(frag);
  if (state.view === "recordings") afterRecordings();
  if (state.view === "transcript") afterTranscript();
}

function goView(id) {
  if (!ALL_VIEWS.includes(id)) return;
  state.view = id; render(); window.scrollTo(0, 0);
}

function pulse(btn) {
  if (!btn) return;
  btn.classList.add("is-pulse");
  setTimeout(() => btn.classList.remove("is-pulse"), 450);
}

// =============================================================================
// Shared: IRC line builder (live captions + merged transcript share one stream)
// =============================================================================
function ircLine(ln, { inflight = false, restorable = false } = {}) {
  const cls = ["irc", ln.suppressed ? "is-sup" : "", ln.lowConfidence ? "is-low" : "", inflight ? "is-inflight" : ""].filter(Boolean).join(" ");
  let badges = "";
  if (ln.identity) badges += `<span class="ircb id">${esc(ln.identity)}</span>`;
  if (ln.translatedFrom) badges += `<span class="ircb tr">${esc(ln.translatedFrom)}→${esc(ln.targetLang || "en")}</span>`;
  if (ln.lowConfidence) badges += `<span class="ircb low">${esc((ln.confidence ?? 0).toFixed(2))}</span>`;
  if (ln.suppressed) {
    badges += `<span class="ircb sup">⨯ ${esc(ln.matchedRule || "rule")}</span>`;
    if (restorable) badges += `<span class="ircb restore" data-restore="1">restore</span>`;
  }
  const cursor = inflight ? `<span class="irc__cursor">▍</span>` : "";
  return `
    <div class="${cls}">
      <span class="irc__t">${esc(clock(ln.t))}</span>
      <span class="irc__who spk-ink-${ln.spk}">${esc(ln.speaker)}<span class="irc__lang flag">${flagOf(ln.lang)}</span>:</span>
      <span class="irc__txt">${esc(ln.text)}${cursor}${badges}</span>
    </div>`;
}

// Shared sparkline draw (Taps level history)
function drawSparks(scope) {
  scope.querySelectorAll("canvas[data-spark]").forEach((cv) => {
    const levels = JSON.parse(cv.dataset.spark);
    const ctx = cv.getContext("2d");
    const w = cv.width, h = cv.height, n = levels.length;
    const spk = cv.dataset.spk;
    const color = getComputedStyle(document.documentElement).getPropertyValue(`--spk${spk}`).trim() || "#4aa3ff";
    ctx.clearRect(0, 0, w, h);
    const bw = w / n;
    for (let i = 0; i < n; i++) {
      const v = Math.max(0.02, levels[i]);
      const bh = Math.max(2, v * (h - 3));
      ctx.fillStyle = v < 0.05 ? "#262d38" : color;
      ctx.globalAlpha = v < 0.05 ? 1 : 0.85;
      ctx.fillRect(i * bw + 1, (h - bh) / 2, Math.max(1.5, bw - 2), bh);
    }
    ctx.globalAlpha = 1;
  });
}

// =============================================================================
// Shared: ENGINE controls — backend chips + model-by-family + Canary langs.
// Rendered VISIBLY in Settings (the default) and Transcript (the per-session
// override). `eng` is the live object to mutate; `ns` namespaces element ids.
// =============================================================================
// model families that only run as a batch job (not live)
const BATCH_ONLY = new Set(["voxtral", "parakeet", "canary"]);

function engineControls(eng, ns) {
  const wrap = el(`<div class="eng"></div>`);

  const beChips = APP.backends.map((b) =>
    `<button class="chip ${b.kind === eng.backend ? "is-sel" : ""}" data-be="${esc(b.kind)}" ${b.available ? "" : "disabled"}>${esc(b.label)}${b.available ? "" : '<span class="chip__x">n/a</span>'}</button>`
  ).join("");
  wrap.appendChild(el(`<div class="eng-row"><span class="eng-cap">Backend</span><div class="chips" data-grp="be">${beChips}</div></div>`));

  const fam = el(`<div class="eng-row"><span class="eng-cap">Model · grouped by family</span><div class="famgrid"></div></div>`);
  const fg = fam.querySelector(".famgrid");
  for (const f of MODELS) {
    const tag = BATCH_ONLY.has(f.family) ? "batch only" : "live + batch";
    const block = el(`<div class="fam"><div class="fam__head"><span>${esc(f.family)}</span><span class="fam__tag">${esc(tag)}</span></div><div class="fam__models"></div></div>`);
    const fm = block.querySelector(".fam__models");
    for (const m of f.models) {
      const seld = eng.family === f.family && eng.model === m.id;
      fm.appendChild(el(`
        <button class="model ${seld ? "is-sel" : ""}" data-family="${esc(f.family)}" data-model="${esc(m.id)}">
          <span class="model__l"><span class="model__name">${esc(m.display)}</span><span class="model__desc">${esc(m.desc)}</span></span>
          <span class="model__dot"></span>
        </button>`));
    }
    fg.appendChild(block);
  }
  wrap.appendChild(fam);

  if (eng.family === "canary") {
    const opts = (sel) => ["nb", "da", "en", "sv", "de", "fr"].map((c) =>
      `<option value="${esc(c)}" ${c === sel ? "selected" : ""}>${flagOf(c)} ${esc(langName(c))}</option>`).join("");
    wrap.appendChild(el(`
      <div class="eng-row">
        <span class="eng-cap">Canary translation</span>
        <div class="selrow">
          <div class="selfield"><label>source_lang</label><select id="${ns}Src">${opts(eng.sourceLang)}</select></div>
          <div style="padding-bottom:6px;color:var(--ink-4)">→</div>
          <div class="selfield"><label>target_lang</label><select id="${ns}Tgt">${opts(eng.targetLang)}</select></div>
        </div>
        <div class="translate-note">🌐 transcribe <b id="${ns}TrS">${esc(langName(eng.sourceLang))}</b> → <b id="${ns}TrT">${esc(langName(eng.targetLang))}</b></div>
      </div>`));
  }
  return wrap;
}

// wire an engine block in place; on family/backend change, re-render just it.
function wireEngine(container, eng, ns, rebuild) {
  container.querySelectorAll('[data-grp="be"] .chip').forEach((b) => {
    if (b.disabled) return;
    b.addEventListener("click", () => { eng.backend = b.dataset.be; rebuild(); });
  });
  container.querySelectorAll("[data-model]").forEach((m) => {
    m.addEventListener("click", () => {
      const changedFam = eng.family !== m.dataset.family;
      eng.family = m.dataset.family; eng.model = m.dataset.model;
      if (changedFam) rebuild(); else {
        m.closest(".famgrid").querySelectorAll(".model").forEach((x) => x.classList.toggle("is-sel", x === m));
        const lbl = document.getElementById(`${ns}Lbl`);
        if (lbl) lbl.textContent = eng.model;
      }
    });
  });
  const src = document.getElementById(`${ns}Src`), tgt = document.getElementById(`${ns}Tgt`);
  if (src) src.addEventListener("change", () => { eng.sourceLang = src.value; document.getElementById(`${ns}TrS`).textContent = langName(src.value); });
  if (tgt) tgt.addEventListener("change", () => { eng.targetLang = tgt.value; document.getElementById(`${ns}TrT`).textContent = langName(tgt.value); });
}

// =============================================================================
// GLOBAL · TAPS — live ingress. Connected + incoming taps, global recording
// arm, the speech-gate (LiveConfig) knobs, and per-tap person-map + single/multi.
// =============================================================================
const INCOMING_TAP = { identity: "__incoming__", name: "lenovo-x1 · meeting-room-2", device: "Poly Sync 20", lang: "auto", incoming: true };

function viewTaps() {
  const wrap = el(`<div></div>`);
  const live = LIVE_TAPS.filter((t) => t.live).length;
  wrap.innerHTML = header({
    eyebrow: "Global · Ingress",
    title: "Taps",
    sub: `${LIVE_TAPS.length} connected · <span style="color:var(--good)">${esc(String(live))} live</span> · 1 incoming · settings persist across sessions`,
  });

  // global recording arm/pause (RECORDING_ENABLED)
  const arm = el(`
    <div class="armbar">
      <span class="armbar__l">
        <span class="armbar__lab">Recording</span>
        <span class="armbar__hint">RECORDING_ENABLED — global arm for every tap</span>
      </span>
      <span class="switch" id="armSwitch">
        <button class="switch__opt ${state.recordingArmed ? "is-on" : ""}" data-arm="1">● armed</button>
        <button class="switch__opt ${state.recordingArmed ? "" : "is-on warn"}" data-arm="0">⏸ paused</button>
      </span>
    </div>`);
  arm.querySelectorAll("[data-arm]").forEach((b) => b.addEventListener("click", () => { state.recordingArmed = b.dataset.arm === "1"; render(); }));
  wrap.appendChild(arm);

  // connected taps table (+ incoming). Each row can expand into a config strip.
  const panel = el(`
    <div class="panel panel--primary">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">🛰️</span>Taps</div>
        <div class="panel__hint">click a row to map its Person &amp; set single / multi</div>
      </div>
      <div class="panel__body flush">
        <table class="tbl">
          <thead><tr>
            <th style="width:30px"></th><th>Identity · device</th><th>Person</th>
            <th style="width:140px">Level</th><th style="width:88px">Spark</th>
            <th style="width:84px">Gate · lag</th><th class="r" style="width:118px">Rec / live</th>
          </tr></thead>
          <tbody id="tapBody"></tbody>
        </table>
      </div>
    </div>`);
  const body = panel.querySelector("#tapBody");
  for (const t of LIVE_TAPS) tapRows(body, t);
  tapRows(body, INCOMING_TAP);
  wrap.appendChild(panel);

  // speech-gate settings (real LiveConfig knobs that gate every tap)
  wrap.appendChild(gatePanel());

  queueMicrotask(() => {
    drawSparks(wrap);
    wireGate(wrap);
  });
  return wrap;
}

function tapRows(tbody, t) {
  if (t.incoming) {
    const r = el(`
      <tr class="taprow is-incoming">
        <td><span class="navitem__ic" style="font-size:13px">📡</span></td>
        <td><span class="tapname"><span class="tapname__n">${esc(t.name)} <span class="tag warn">incoming</span></span><span class="tapname__d">${esc(t.device)}</span></span></td>
        <td><span class="muted" style="font-size:10.5px">handshaking…</span></td>
        <td colspan="2"><span class="muted" style="font-size:10.5px">negotiating gate &amp; identity</span></td>
        <td><span class="muted mono" style="font-size:9.5px">—</span></td>
        <td class="r"><span class="tgs"><span class="tg rec">● REC</span><span class="tg live">LIVE</span></span></td>
      </tr>`);
    tbody.appendChild(r);
    return;
  }

  const sp = speakerById(t.identity);
  const person = personById(TAP_PERSON_MAP[t.identity] || t.identity);
  const idle = !t.gateOpen && t.level < 0.02;
  const expanded = state.expandedTap === t.identity;
  const mode = state.tapMode[t.identity] || (sp?.isRoom ? "multi" : "single");

  const main = el(`
    <tr class="taprow ${idle ? "is-idle" : ""}" data-tap="${esc(t.identity)}" style="cursor:pointer">
      <td><span class="av spk-${t.spk}">${esc(sp?.initials || "??")}</span></td>
      <td><span class="tapname"><span class="tapname__n spk-ink-${t.spk}">${esc(t.name)}</span><span class="tapname__d">${esc(sp?.mic.label || "—")} · ${esc(t.identity)}</span></span></td>
      <td><span class="pick"><span class="av sm spk-${person?.spk ?? t.spk}">${esc(person?.initials || "?")}</span>${esc(person?.name?.split(" ")[0] || t.identity)}<span class="pick__chev">⌄</span></span></td>
      <td><span class="meter"><span class="meter__bar"><span class="meter__fill spk-bar-${t.spk}" style="width:${Math.round(t.level * 100)}%"></span></span><span class="meter__val">${esc(t.level.toFixed(2))}</span></span></td>
      <td><canvas class="spark" width="184" height="44" data-spark='${esc(JSON.stringify(t.levels))}' data-spk="${t.spk}"></canvas></td>
      <td><span class="gate"><span class="gate__led ${t.gateOpen ? "open" : ""}"></span><span class="gate__txt ${t.gateOpen ? "open" : ""}">${t.gateOpen ? "open" : "shut"}</span></span><div class="lag ${t.lagS > 1.2 ? "hot" : ""}" style="margin-top:3px">${t.lagS ? esc(t.lagS.toFixed(1)) + "s" : "—"}</div></td>
      <td class="r"><span class="tgs"><span class="tg rec ${t.record ? "on" : ""}">● REC</span><span class="tg live ${t.live ? "on" : ""}">LIVE</span></span></td>
    </tr>`);
  main.addEventListener("click", () => { state.expandedTap = expanded ? null : t.identity; render(); });
  tbody.appendChild(main);

  if (!expanded) return;

  // ---- config strip: in-flight buffer, person map, language, single/multi ----
  const buffer = t.buffer
    ? `<span class="mono" style="color:var(--ink-2)">${esc(t.buffer)}<span class="irc__cursor">▍</span></span>`
    : `<span class="muted" style="font-size:10.5px">— gate shut, no in-flight audio —</span>`;
  const cfg = el(`
    <tr class="tapcfg"><td colspan="7"><div class="tapcfg__in">
      <span class="cfgblk"><span class="cfgblk__k">Person</span>
        <span class="pick"><span class="av sm spk-${person?.spk ?? t.spk}">${esc(person?.initials || "?")}</span>${esc(person?.name || t.identity)}<span class="pick__chev">⌄</span></span>
        <a class="act act--sm act--ghost" data-go="people" style="text-decoration:none">→ People</a>
      </span>
      <span class="cfgsep"></span>
      <span class="cfgblk"><span class="cfgblk__k">Language</span>
        <span class="pick"><span class="flag">${flagOf(t.lang)}</span>${esc(langName(t.lang))} <span class="muted">(from Person)</span><span class="pick__chev">⌄</span></span></span>
      <span class="cfgsep"></span>
      <span class="cfgblk"><span class="cfgblk__k">Mode</span>
        <span class="seg" data-seg="${esc(t.identity)}">
          <button class="seg__opt ${mode === "single" ? "is-on" : ""}" data-mode="single">👤 single</button>
          <button class="seg__opt multi ${mode === "multi" ? "is-on" : ""}" data-mode="multi">👥 multi</button>
        </span>
        <span class="muted" style="font-size:10px">${mode === "multi" ? "diarized → speakers" : "one speaker"}</span></span>
    </div></td></tr>`);
  cfg.querySelector(".seg")?.querySelectorAll(".seg__opt").forEach((b) => {
    b.addEventListener("click", (e) => { e.stopPropagation(); state.tapMode[t.identity] = b.dataset.mode; render(); });
  });
  cfg.querySelector('[data-go="people"]')?.addEventListener("click", (e) => { e.stopPropagation(); goView("people"); });
  tbody.appendChild(cfg);

  // resulting diarized speakers for a MULTI tap (Speaker A/B + lang + share)
  if (mode === "multi") {
    const voices = sp?.diarizedInto || [];
    const sub = el(`<tr class="subrows"><td colspan="7"><div class="subcap">↳ ${esc(String(voices.length))} diarized speakers <span class="tag info">mock UI</span></div></td></tr>`);
    const cell = sub.querySelector("td");
    for (const d of voices) {
      cell.appendChild(el(`
        <div class="subrow">
          <span class="av sm spk-${d.spk}">${esc(d.label.replace("Speaker ", ""))}</span>
          <span class="subrow__name">${esc(d.label)} <span class="flag">${flagOf(d.lang)}</span></span>
          <span class="subrow__bar"><span class="subrow__fill spk-bar-${d.spk}" style="width:${d.talkPct}%"></span></span>
          <span class="subrow__pct">${d.talkPct}%</span>
        </div>`));
    }
    tbody.appendChild(sub);
  }
}

function gatePanel() {
  const g = state.gate;
  const panel = el(`
    <div class="panel" id="gatePanel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">🚪</span>Speech gate · LiveConfig</div>
        <div class="panel__hint">governs how every tap is gated</div>
      </div>
      <div class="panel__body">
        <div class="eng-row" style="margin-bottom:13px">
          <span class="eng-cap">gate_kind</span>
          <div class="chips" id="gateKind">
            ${GATE_KINDS.map((k) => `<button class="chip ${k.kind === g.gate_kind ? "is-sel" : ""}" data-kind="${esc(k.kind)}" ${k.available ? "" : "disabled"}>${esc(k.label)}${k.available ? "" : '<span class="chip__x">n/a</span>'}</button>`).join("")}
          </div>
        </div>
        <div class="knobgrid">
          ${gateKnob("gate_speech_threshold", "gate_speech_threshold", g.gate_speech_threshold, 0, 1, 0.01, "")}
          ${gateKnob("gate_hangover_ms", "gate_hangover_ms", g.gate_hangover_ms, 0, 10000, 50, "ms")}
          ${gateKnob("gate_pre_roll_ms", "gate_pre_roll_ms", g.gate_pre_roll_ms, 0, 5000, 50, "ms")}
          ${gateKnob("gate_min_speech_ms", "gate_min_speech_ms", g.gate_min_speech_ms, 0, 5000, 50, "ms")}
        </div>
        <div class="checkrow" style="margin-top:13px">
          <span class="checkbox ${g.confidence_validation ? "on" : ""}" id="gateConf">${g.confidence_validation ? "✓" : ""}</span>
          <span><b style="color:var(--ink)">confidence_validation</b> — drop low-confidence hypotheses before they reach the merge</span>
        </div>
      </div>
    </div>`);
  return panel;
}
function gateKnob(key, label, val, min, max, step, unit) {
  return `
    <div class="kfield">
      <div class="kfield__top"><span class="kfield__k">${esc(label)}</span><span class="kfield__v" id="gv_${esc(key)}">${esc(String(val))}${esc(unit ? " " + unit : "")}</span></div>
      <input type="range" min="${min}" max="${max}" step="${step}" value="${val}" data-key="${esc(key)}" data-unit="${esc(unit)}">
      <div class="kfield__rng"><span>${esc(String(min))}</span><span>${esc(String(max))}</span></div>
    </div>`;
}
function wireGate(scope) {
  scope.querySelectorAll("#gateKind .chip").forEach((b) => {
    if (b.disabled) return;
    b.addEventListener("click", () => { state.gate.gate_kind = b.dataset.kind; render(); });
  });
  scope.querySelectorAll('#gatePanel input[type="range"]').forEach((inp) => {
    inp.addEventListener("input", () => {
      const k = inp.dataset.key;
      state.gate[k] = Number(inp.value);
      document.getElementById(`gv_${k}`).textContent = `${inp.value}${inp.dataset.unit ? " " + inp.dataset.unit : ""}`;
    });
  });
  const conf = scope.querySelector("#gateConf");
  if (conf) conf.addEventListener("click", () => { state.gate.confidence_validation = !state.gate.confidence_validation; render(); });
}

// =============================================================================
// GLOBAL · PEOPLE — canonical Persons registry. Per-Person: name, primary +
// secondary language (+ "transcribe as" quick switch), MULTIPLE per-microphone
// profiles (gate threshold + noise floor each), the taps/identities mapped here,
// "seen in N sessions". Plus per-session participation.
// =============================================================================
function viewPeople() {
  const sess = session();
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Global · Registry",
    title: "People",
    sub: `${PERSONS.length} canonical persons · per-<b>microphone</b> profiles reused across every session`,
  });

  wrap.appendChild(el(`
    <div class="note"><span class="ic">👥</span><div>A <b>Person</b> can own several <b>microphone profiles</b> (each with its own gate + noise floor) and have multiple taps mapped to it. Per-mic profiles &amp; dual language are <b>mock UI</b>; the per-session alias is the real backend piece.</div></div>`));

  // per-session participation strip
  if (!sess.fresh && sess.speakers.length) {
    const chips = sess.speakers.map((id) => {
      const p = personById(id) || speakerById(id);
      return `<span class="langpill"><span class="av sm spk-${p?.spk ?? 0}">${esc(p?.initials || "?")}</span>${esc(p?.name || id)}</span>`;
    }).join("");
    wrap.appendChild(el(`
      <div class="panel" style="margin-bottom:11px"><div class="panel__head"><div class="panel__title"><span class="ic">📍</span>In this session · ${esc(sess.label || "(untitled)")}</div><div class="panel__hint">${esc(String(sess.speakers.length))} participants</div></div>
      <div class="panel__body"><div class="langpair" style="margin:0">${chips}</div></div></div>`));
  }

  for (const p of PERSONS) wrap.appendChild(personCard(p));
  return wrap;
}

function personCard(p) {
  const card = el(`
    <div class="person ${p.isRoom ? "is-room" : ""}">
      <div class="person__head">
        <span class="av lg spk-${p.spk}">${esc(p.initials)}</span>
        <span class="person__id">
          <span class="person__name spk-ink-${p.spk}">${esc(p.name)} ${p.isRoom ? '<span class="tag warn">room · multi</span>' : ""}</span>
          <span class="person__note">${esc(p.note)}</span>
        </span>
        <span class="person__seen">seen in<br><b>${esc(String(p.sessionsSeen))}</b> sessions</span>
      </div>
      <div class="person__cols">
        <div class="person__col" data-col="mics"></div>
        <div class="person__col" data-col="lang"></div>
      </div>
    </div>`);

  // LEFT col: microphone profiles (a Person can have several) + identities
  const mics = card.querySelector('[data-col="mics"]');
  mics.appendChild(el(`<div class="subhead">🎚️ Microphone profiles <span class="tag info">multi-mic · mock</span></div>`));
  for (const m of p.mics) {
    mics.appendChild(el(`
      <div class="microw">
        <span class="microw__l"><span class="microw__lab"><b>${esc(m.label)}</b> ${m.primary ? '<span class="tag" style="font-size:8px">primary</span>' : ""}</span></span>
        <span class="micval"><span class="micval__k">gate </span>${esc(m.gateThreshold.toFixed(2))}</span>
        <span class="micval"><span class="micval__k">floor </span>${esc(String(m.noiseFloorDb))} dB</span>
      </div>`));
  }
  mics.appendChild(el(`<div class="subhead" style="margin-top:11px">🔗 Mapped taps / identities</div>`));
  for (const id of p.identities) {
    const t = LIVE_TAPS.find((x) => x.identity === id);
    mics.appendChild(el(`
      <div class="idrow">
        <span class="idrow__code">${esc(id)}</span>
        ${t ? `<span class="tag ${t.live ? "live" : "off"}">${t.live ? "live" : "idle"}</span>` : '<span class="tag off">saved</span>'}
        <span class="muted" style="margin-left:auto;font-size:10px">${esc(speakerById(id)?.mic.label || "—")}</span>
      </div>`));
  }
  mics.appendChild(el(`<button class="act act--sm act--ghost" data-map="1" style="margin-top:9px">+ map another tap / mic</button>`));
  mics.querySelector("[data-map]")?.addEventListener("click", (e) => pulse(e.currentTarget));

  // RIGHT col: language pair + quick switch (+ diarization for rooms)
  const lang = card.querySelector('[data-col="lang"]');
  lang.appendChild(el(`<div class="subhead">🗣️ Language <span class="tag info">primary + secondary · mock</span></div>`));
  const secondary = p.secondaryLang
    ? `<span class="langsep">·</span><span class="langpill"><span class="langpill__role">2nd</span><span class="flag">${flagOf(p.secondaryLang)}</span>${esc(langName(p.secondaryLang))}</span>`
    : `<span class="langsep">·</span><span class="muted" style="font-size:10px">no secondary</span>`;
  lang.appendChild(el(`
    <div class="langpair">
      <span class="langpill primary"><span class="langpill__role">1st</span><span class="flag">${flagOf(p.primaryLang)}</span>${esc(langName(p.primaryLang))}</span>
      ${secondary}
    </div>`));
  const opts = [p.primaryLang, p.secondaryLang, "en"].filter((v, i, a) => v && a.indexOf(v) === i);
  const qs = el(`<div class="quickswitch"><span class="quickswitch__lab">transcribe as →</span></div>`);
  for (const code of opts) {
    const btn = el(`<button class="qbtn ${state.transcribeAs[p.id] === code ? "is-active" : ""}" data-lang="${esc(code)}"><span class="flag">${flagOf(code)}</span>${esc(langName(code))}</button>`);
    btn.addEventListener("click", () => {
      state.transcribeAs[p.id] = code;
      qs.querySelectorAll(".qbtn").forEach((b) => b.classList.toggle("is-active", b.dataset.lang === code));
    });
    qs.appendChild(btn);
  }
  lang.appendChild(qs);

  if (p.isRoom && p.diarizedInto) {
    lang.appendChild(el(`<div class="subhead" style="margin-top:11px">👥 Diarizes into</div>`));
    for (const d of p.diarizedInto) {
      lang.appendChild(el(`
        <div class="idrow">
          <span class="av sm spk-${d.spk}">${esc(d.label.replace("Speaker ", ""))}</span>
          <span>${esc(d.label)}</span>
          <span class="tag"><span class="flag">${flagOf(d.lang)}</span>${esc(langName(d.lang))}</span>
          <span class="mono dim" style="margin-left:auto;font-size:10px">${d.talkPct}%</span>
        </div>`));
    }
  }
  return card;
}

// =============================================================================
// GLOBAL · SETTINGS — defaults/config. Default engine (visible), global prompt /
// live-prompt / hotwords (real config files), hallucination rules (real format).
// =============================================================================
function viewSettings() {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Global · Defaults",
    title: "Settings",
    sub: `default engine &amp; prompts applied to every session unless a session overrides them`,
  });

  const grid = el(`<div class="grid cols-tx"></div>`);

  // LEFT: default engine (this is the DEFAULT; Transcript holds the override)
  const engPanel = el(`
    <div class="panel panel--primary">
      <div class="panel__head"><div class="panel__title"><span class="ic">🧠</span>Default engine</div><div class="panel__hint">backend · model · translation defaults</div></div>
      <div class="panel__body" id="engDefaultBody"></div>
    </div>`);
  const mountDefault = () => {
    const body = engPanel.querySelector("#engDefaultBody");
    body.innerHTML = "";
    const ctl = engineControls(state.engineDefault, "set");
    body.appendChild(ctl);
    wireEngine(body, state.engineDefault, "set", mountDefault);
  };
  mountDefault();
  grid.appendChild(engPanel);

  // RIGHT: prompts + hotwords + hallucination rules
  const aside = el(`<div class="aside"></div>`);
  aside.appendChild(el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">✍️</span>Prompts &amp; hotwords</div><div class="panel__hint">/api/config/{key}</div></div>
      <div class="panel__body">
        <div class="field" style="margin-bottom:10px"><label>prompt (global)</label><textarea class="ta" rows="3">${esc(PROMPTS.prompt)}</textarea></div>
        <div class="field" style="margin-bottom:10px"><label>live-prompt (separate)</label><textarea class="ta" rows="2">${esc(PROMPTS.livePrompt)}</textarea></div>
        <div class="field"><label>hotwords</label><textarea class="ta" rows="3">${esc(PROMPTS.hotwords)}</textarea></div>
      </div>
    </div>`));

  const rulesPanel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">🛡️</span>Hallucination rules</div><div class="panel__hint">${esc(String(HALLUCINATION_RULES.length))} rules</div></div>
      <div class="panel__body flush"><div class="rules"></div></div>
    </div>`);
  const rules = rulesPanel.querySelector(".rules");
  for (const r of HALLUCINATION_RULES) {
    rules.appendChild(el(`
      <div class="rule">
        <span class="rule__kind ${esc(r.kind)}">${esc(r.kind)}</span>
        <span class="rule__txt">${esc(r.display)}</span>
        <span class="rule__note">${esc(r.note)}</span>
      </div>`));
  }
  rules.appendChild(el(`<div style="padding:7px 11px;font-size:9.5px;color:var(--ink-4);font-family:var(--mono)">plain = substring · exact: = whole-line · re: = regex</div>`));
  aside.appendChild(rulesPanel);
  grid.appendChild(aside);
  wrap.appendChild(grid);
  return wrap;
}

// =============================================================================
// SESSION · 1 CAPTURE — live. IRC captions, capture health, live channel
// control, read-only "taps feeding this session", recording + prompt override.
// =============================================================================
function viewCapture() {
  const sess = session();
  if (sess.fresh) return viewCaptureFresh(sess);
  if (!sess.current) return viewCaptureArchived(sess);

  const wrap = el(`<div></div>`);
  const live = LIVE_TAPS.filter((t) => t.live).length;
  wrap.innerHTML = header({
    eyebrow: "Stage 1 · Live",
    title: "Capture",
    sub: `${esc(String(live))} taps into <b>${esc(sess.label)}</b> · recorder ${state.recordingArmed ? "<span style='color:var(--good)'>armed</span>" : "<span class='muted'>paused</span>"} · live model <span class='mono'>nb-whisper-medium</span>`,
    actions: `<button class="act" data-go="taps">🛰️ Taps <span class="act__val">ingress</span></button>`,
  });

  const grid = el(`<div class="grid cols-cap"></div>`);

  // LEFT: live captions IRC feed (primary focus)
  const capsPanel = el(`
    <div class="panel panel--primary">
      <div class="panel__head"><div class="panel__title"><span class="ic">💬</span>Live captions</div><div class="panel__hint">[m:ss] speaker · language</div></div>
      <div class="panel__body flush"><div class="irclog caps"></div></div>
    </div>`);
  const caps = capsPanel.querySelector(".irclog");
  for (const c of LIVE_CAPTIONS) caps.appendChild(el(ircLine(c, { inflight: c.inflight })));
  grid.appendChild(capsPanel);

  // RIGHT aside: live channel control + read-only taps-feeding + health
  const aside = el(`<div class="aside"></div>`);
  aside.appendChild(liveChannelPanel());
  aside.appendChild(feedPanel());
  grid.appendChild(aside);
  wrap.appendChild(grid);

  // bottom: capture health + per-session recording/prompt override
  wrap.appendChild(el(`<div class="spacer"></div>`));
  const bottom = el(`<div class="grid cols-cap"></div>`);
  bottom.appendChild(healthPanel());
  bottom.appendChild(captureOverridePanel());
  wrap.appendChild(bottom);

  queueMicrotask(() => wrap.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => goView(b.dataset.go))));
  return wrap;
}

function liveChannelPanel() {
  // live channel control: state, model+language, start/stop, log peek
  const panel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">📡</span>Live channel</div>
        <span class="chanstate"><span class="chanstate__led running"></span>running</span></div>
      <div class="panel__body">
        <dl class="kv" style="margin-bottom:10px">
          <dt>model</dt><dd>nb-whisper-medium</dd>
          <dt>language</dt><dd>${flagOf("nb")} nb (per-tap)</dd>
          <dt>backend</dt><dd>${esc(APP.backend)}</dd>
        </dl>
        <div class="row-between" style="margin-bottom:9px">
          <span class="switch">
            <button class="switch__opt is-on" data-chan="run">▶ running</button>
            <button class="switch__opt" data-chan="stop">■ stop</button>
          </span>
          <span class="muted" style="font-size:9.5px">live transcribe</span>
        </div>
        <div class="logpeek"><span class="t">09:11:38</span> tap atle gate=open lvl=0.62
<span class="t">09:11:39</span> room-oslo → Speaker B (en)
<span class="t">09:11:40</span> <span class="ok">flushed</span> 1.6s · nb-whisper-medium
<span class="t">09:11:41</span> mette gate=shut (idle 2.1s)</div>
      </div>
    </div>`);
  panel.querySelectorAll("[data-chan]").forEach((b) => b.addEventListener("click", () => {
    panel.querySelectorAll("[data-chan]").forEach((x) => x.classList.toggle("is-on", x === b));
    const led = panel.querySelector(".chanstate__led");
    const txt = panel.querySelector(".chanstate");
    const running = b.dataset.chan === "run";
    led.className = `chanstate__led ${running ? "running" : "stopped"}`;
    txt.lastChild.textContent = running ? "running" : "stopped";
  }));
  return panel;
}

function feedPanel() {
  // read-only reference: taps feeding THIS session (configure → Taps)
  const panel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">🛰️</span>Taps feeding this session</div>
        <button class="act act--sm act--ghost" data-go="taps">configure →</button></div>
      <div class="panel__body flush"><table class="tbl"><tbody id="feedBody"></tbody></table></div>
    </div>`);
  const body = panel.querySelector("#feedBody");
  for (const t of LIVE_TAPS) {
    const sp = speakerById(t.identity);
    const idle = !t.gateOpen && t.level < 0.02;
    body.appendChild(el(`
      <tr class="${idle ? "is-idle" : ""}" style="${idle ? "opacity:.6" : ""}">
        <td style="width:24px"><span class="av sm spk-${t.spk}">${esc(sp?.initials || "??")}</span></td>
        <td><span class="spk-ink-${t.spk}" style="font-weight:600">${esc(sp?.name?.split(" ")[0] || t.name)}</span> ${sp?.isRoom ? '<span class="tag info" style="font-size:8px">multi</span>' : ""}</td>
        <td style="width:80px"><span class="meter"><span class="meter__bar"><span class="meter__fill spk-bar-${t.spk}" style="width:${Math.round(t.level * 100)}%"></span></span></span></td>
        <td class="r" style="width:54px"><span class="tg rec ${t.record ? "on" : ""}">${t.record ? "● REC" : "off"}</span></td>
      </tr>`));
    if (sp?.isRoom && sp.diarizedInto) {
      for (const d of sp.diarizedInto) {
        body.appendChild(el(`
          <tr style="opacity:.85"><td></td>
            <td colspan="3" style="padding-left:6px"><span class="av sm spk-${d.spk}">${esc(d.label.replace("Speaker ", ""))}</span> <span class="mono" style="font-size:10px;color:var(--ink-3)">${esc(d.label)} ${flagOf(d.lang)} ${d.talkPct}%</span></td>
          </tr>`));
      }
    }
  }
  panel.querySelector("[data-go]").addEventListener("click", () => goView("taps"));
  return panel;
}

function healthPanel() {
  const openGates = LIVE_TAPS.filter((t) => t.gateOpen).length;
  const recOn = LIVE_TAPS.filter((t) => t.record).length;
  const langSet = [...new Set(LIVE_TAPS.map((t) => t.lang))];
  const maxLag = Math.max(...LIVE_TAPS.map((t) => t.lagS));
  return el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">📊</span>Capture health</div><div class="panel__hint">right now</div></div>
      <div class="panel__body">
        <div class="statgrid c4">
          <div class="statcell"><div class="statcell__k">Gates open</div><div class="statcell__v">${openGates}<span class="dim"> / ${LIVE_TAPS.length}</span></div></div>
          <div class="statcell"><div class="statcell__k">Recording</div><div class="statcell__v">${recOn}<span class="dim"> / ${LIVE_TAPS.length}</span></div></div>
          <div class="statcell"><div class="statcell__k">Max lag</div><div class="statcell__v">${esc(maxLag.toFixed(1))}s</div></div>
          <div class="statcell"><div class="statcell__k">Languages</div><div class="statcell__v" style="font-size:14px">${esc(langSet.map(flagOf).join(" "))}</div></div>
        </div>
      </div>
    </div>`);
}

function captureOverridePanel() {
  // per-session recording toggle + prompt/hotwords override (falls back global)
  return el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">⚙️</span>Session overrides</div><div class="panel__hint">else inherits Settings</div></div>
      <div class="panel__body">
        <div class="row-between" style="margin-bottom:10px">
          <span style="font-size:11.5px;color:var(--ink-2)">Recording (this session)</span>
          <span class="tag ${state.recordingArmed ? "on" : "off"}">${state.recordingArmed ? "on" : "paused"}</span>
        </div>
        <div class="field" style="margin-bottom:9px"><label>prompt override <span class="dim">(blank → global)</span></label><textarea class="ta" rows="2" placeholder="${esc(PROMPTS.prompt)}"></textarea></div>
        <div class="field"><label>hotwords override</label><textarea class="ta" rows="2" placeholder="inherits: Vortiago, Nordic, KPI…"></textarea></div>
      </div>
    </div>`);
}

function viewCaptureFresh(sess) {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 1 · New session",
    title: "Capture",
    sub: `<b>${esc(sess.label)}</b> is armed · waiting for the first tap to connect`,
  });
  wrap.appendChild(el(`
    <div class="panel"><div class="panel__body"><div class="empty">
      <div class="empty__ic">🎙️</div>
      <div class="empty__h">No taps yet</div>
      <div>This session is recording-ready. As Bridges connect, they appear here with level, lag, gate and rec/live state — configure each in <b>Taps</b>.</div>
    </div></div></div>`));
  wrap.appendChild(el(`<div class="spacer"></div>`));
  const bottom = el(`<div class="grid cols-cap"></div>`);
  bottom.appendChild(el(`
    <div class="panel"><div class="panel__head"><div class="panel__title"><span class="ic">📊</span>Capture health</div><div class="panel__hint">nothing yet</div></div>
      <div class="panel__body"><div class="statgrid c4">
        <div class="statcell"><div class="statcell__k">Gates open</div><div class="statcell__v dim">0 / 0</div></div>
        <div class="statcell"><div class="statcell__k">Recording</div><div class="statcell__v dim">0 / 0</div></div>
        <div class="statcell"><div class="statcell__k">Max lag</div><div class="statcell__v dim">—</div></div>
        <div class="statcell"><div class="statcell__k">Languages</div><div class="statcell__v dim">—</div></div>
      </div></div></div>`));
  bottom.appendChild(el(`
    <div class="panel"><div class="panel__head"><div class="panel__title"><span class="ic">💬</span>Live captions</div><div class="panel__hint">—</div></div>
      <div class="panel__body"><div class="empty" style="padding:20px 12px">Captions stream in as speech is transcribed.</div></div></div>`));
  wrap.appendChild(bottom);
  return wrap;
}

function viewCaptureArchived(sess) {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 1 · Archived",
    title: "Capture",
    sub: `<b>${esc(sess.label)}</b> finished · ${esc(String(sess.speakers.length))} sources · ${esc(String(sess.wavCount))} clips`,
  });
  const panel = el(`
    <div class="panel"><div class="panel__head"><div class="panel__title"><span class="ic">🎙️</span>Captured sources</div><div class="panel__hint">closed · no longer live</div></div>
      <div class="panel__body flush"><table class="tbl"><tbody id="archBody"></tbody></table></div></div>`);
  const body = panel.querySelector("#archBody");
  for (const id of sess.speakers) {
    const sp = speakerById(id);
    if (!sp) continue;
    body.appendChild(el(`
      <tr>
        <td style="width:24px"><span class="av sm spk-${sp.spk}">${esc(sp.initials)}</span></td>
        <td><span style="font-weight:600">${esc(sp.name)}</span> ${sp.isRoom ? '<span class="tag info" style="font-size:8px">multi</span>' : ""}</td>
        <td><span class="flag">${flagOf(sp.primaryLang)}</span> <span class="mono" style="font-size:10px;color:var(--ink-3)">${esc(sp.mic.label)}</span></td>
        <td class="r"><span class="tag on">recorded</span></td>
      </tr>`));
  }
  wrap.appendChild(panel);
  return wrap;
}

// =============================================================================
// SESSION · 2 RECORDINGS — WIDE. Hero waveform with strip-silence live re-cut,
// per-WAV list (originals + stripped clips), original/stripped toggle, transcribe
// actions (one WAV / range + force) with job progress, per-WAV transcript cache.
// =============================================================================
function viewRecordings() {
  const sess = session();
  if (sess.fresh || sess.wavCount === 0) return viewRecordingsEmpty(sess);

  const wavs = wavModel();
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 2 · Recordings",
    title: "Recordings",
    sub: `${esc(String(wavs.length))} WAVs in <b>${esc(sess.label)}</b> · strip silence, then transcribe`,
    actions: `<span class="srcsw" id="srcSw">
        <button class="srcsw__opt ${state.recSource === "original" ? "is-on" : ""}" data-src="original">original</button>
        <button class="srcsw__opt ${state.recSource === "stripped" ? "is-on" : ""}" data-src="stripped">stripped</button>
      </span>`,
  });

  // HERO: wide waveform + live re-cut knobs (full width)
  wrap.appendChild(heroWaveform(wavs[state.selectedWav] || wavs[0]));

  // below: per-WAV list (left) + transcribe + cache (right)
  wrap.appendChild(el(`<div class="spacer"></div>`));
  const grid = el(`<div class="grid cols-tx"></div>`);
  grid.appendChild(wavListPanel(wavs));
  const aside = el(`<div class="aside"></div>`);
  aside.appendChild(transcribePanel(wavs));
  aside.appendChild(cachePanel(wavs[state.selectedWav] || wavs[0]));
  grid.appendChild(aside);
  wrap.appendChild(grid);
  return wrap;
}

function heroWaveform(sel) {
  const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
  const k = state.knobs;
  const keptPct = Math.round((r.speechS / REP_WAV.durationS) * 100);
  const panel = el(`
    <div class="panel panel--primary wavehero">
      <div class="wave-top">
        <span class="wave-top__name">🌊 …${esc(sel.t.replace(/:/g, ""))}_${esc(sel.sp)}.wav · ${esc(clock(REP_WAV.durationS))} · <span class="dim">${esc(state.recSource)}</span></span>
        <div class="wave-stats">
          <div class="wstat"><span class="wstat__v accent" id="sClips">${r.clips}</span><span class="wstat__k">clips</span></div>
          <div class="wstat"><span class="wstat__v good" id="sSpeech">${esc(String(r.speechS))}s</span><span class="wstat__k">speech_seconds</span></div>
          <div class="wstat"><span class="wstat__v" id="sIn">${esc(String(REP_WAV.durationS))}s</span><span class="wstat__k">in_seconds</span></div>
          <div class="wstat"><span class="wstat__v" id="sKept">${keptPct}%</span><span class="wstat__k">kept</span></div>
        </div>
      </div>
      <div class="wavecanvas-wrap"><canvas id="waveCanvas" width="2400" height="360"></canvas></div>
      <div class="wave-axis"><span>0:00</span><span>${esc(clock(REP_WAV.durationS / 4))}</span><span>${esc(clock(REP_WAV.durationS / 2))}</span><span>${esc(clock((REP_WAV.durationS * 3) / 4))}</span><span>${esc(clock(REP_WAV.durationS))}</span></div>
      <div class="wave-legend">
        <span><span class="sw" style="background:linear-gradient(180deg,#f5a623,#b87a12)"></span>kept</span>
        <span><span class="sw" style="background:#262d38"></span>dropped</span>
        <span class="dim">┊ cut marker</span>
        <span class="dim">— speech_floor_db</span>
        <span class="tag info" style="margin-left:auto">live re-cut · mock</span>
      </div>
      <div class="knobbar">
        ${recKnob("minSilenceMs", "min_silence_ms", k.minSilenceMs, 100, 600000, 100, "ms")}
        ${recKnob("padMs", "pad_ms", k.padMs, 0, 5000, 50, "ms")}
        ${recKnob("speechFloorDb", "speech_floor_db", k.speechFloorDb, -120, 0, 1, "dB")}
        <div class="knobbar__act">
          <button class="act act--sm act--primary" id="stripBtn">✂ strip</button>
          <button class="act act--sm act--ghost" id="clearBtn">clear</button>
        </div>
      </div>
    </div>`);
  return panel;
}
function recKnob(key, label, val, min, max, step, unit) {
  return `
    <div class="kfield">
      <div class="kfield__top"><span class="kfield__k">${esc(label)}</span><span class="kfield__v" id="rv_${esc(key)}">${esc(String(val))} ${esc(unit)}</span></div>
      <input type="range" min="${min}" max="${max}" step="${step}" value="${val}" data-key="${esc(key)}" data-unit="${esc(unit)}">
      <div class="kfield__rng"><span>${esc(String(min))}</span><span>${esc(String(max))}</span></div>
    </div>`;
}

function wavListPanel(wavs) {
  const panel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">📁</span>WAVs &amp; stripped clips</div><div class="panel__hint">${esc(String(wavs.length))} originals</div></div>
      <div class="panel__body flush" id="wavList"></div>
    </div>`);
  const list = panel.querySelector("#wavList");
  wavs.forEach((w, i) => {
    const sp = speakerById(w.sp);
    const sel = i === state.selectedWav;
    const node = el(`
      <button class="wavbtn ${sel ? "is-sel" : ""}" data-wav="${i}">
        <span class="wavbtn__l">
          <span class="wavbtn__n">…${esc(w.t.replace(/:/g, ""))}_${esc(w.sp)}.wav</span>
          <span class="wavbtn__sub"><span class="av sm spk-${sp?.spk ?? 0}">${esc(sp?.initials || "?")}</span>${esc(sp?.name?.split(" ")[0] || w.sp)} · original</span>
        </span>
        <span class="wavbtn__r"><span class="wavbtn__dur">${esc(clock(w.dur))}</span><span class="tag ${w.needsTune ? "warn" : "on"}">${w.needsTune ? "tune" : "ok"}</span></span>
      </button>`);
    node.addEventListener("click", () => { state.selectedWav = i; render(); });
    list.appendChild(node);
    // stripped region clips beneath the selected original (mock split result)
    if (sel && state.recSource === "stripped") {
      const regions = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs).regions;
      regions.forEach((rg, ci) => {
        list.appendChild(el(`
          <div class="wavbtn is-clip" style="cursor:default">
            <span class="wavbtn__l"><span class="wavbtn__n">↳ ${esc(w.t.replace(/:/g, ""))}_${esc(w.sp)}.part${ci + 1}.wav</span>
            <span class="wavbtn__sub">stripped region · ${esc(clock(rg.startS))}–${esc(clock(rg.endS))}</span></span>
            <span class="wavbtn__r"><span class="wavbtn__dur">${esc(clock(rg.endS - rg.startS))}</span></span>
          </div>`));
      });
    }
  });
  return panel;
}

function transcribePanel(wavs) {
  const job = TRANSCRIBE_JOB;
  const pct = Math.round((job.current / job.total) * 100);
  const sel = wavs[state.selectedWav] || wavs[0];
  const panel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">▶</span>Transcribe</div><div class="panel__hint">one job per session</div></div>
      <div class="panel__body flush">
        <div class="jobbar">
          <div class="jobbar__top"><span>Job running</span><span class="jobbar__pct">${job.current} / ${job.total}</span></div>
          <div class="progress"><div class="progress__fill" style="width:${pct}%"></div></div>
          <div class="jobbar__wav" style="margin-top:5px">current: ${esc(job.wav)}</div>
        </div>
        <div style="padding:10px 13px;border-top:1px solid var(--line)">
          <div class="row-between" style="margin-bottom:9px">
            <span style="font-size:11px;color:var(--ink-2)">Selected WAV</span>
            <button class="act act--sm" id="txOne">transcribe …${esc(sel.t.replace(/:/g, ""))}_${esc(sel.sp)}</button>
          </div>
          <div class="field" style="margin-bottom:8px"><label>session range</label>
            <div class="rangeform">
              <div class="field"><label>from</label><input type="text" value="09:04" size="5"></div>
              <div class="field"><label>to</label><input type="text" value="09:48" size="5"></div>
              <label class="checkrow" style="font-size:10.5px"><span class="checkbox" id="forceBox"></span>force</label>
            </div>
          </div>
          <button class="act act--sm act--primary" id="txRange" style="width:100%;justify-content:center">▶ transcribe range</button>
        </div>
      </div>
    </div>`);
  panel.querySelector("#forceBox")?.addEventListener("click", (e) => {
    const b = e.currentTarget;
    const on = b.classList.toggle("on");
    b.textContent = on ? "✓" : "";
  });
  panel.querySelectorAll("#txOne, #txRange").forEach((b) => b.addEventListener("click", () => pulse(b)));
  return panel;
}

function cachePanel(sel) {
  const panel = el(`
    <div class="panel cache">
      <div class="panel__head"><div class="panel__title"><span class="ic">🗂️</span>Transcript cache</div><div class="panel__hint">…${esc(sel.t.replace(/:/g, ""))}_${esc(sel.sp)}.wav</div></div>
      <div class="panel__body flush">
        <table class="tbl"><thead><tr><th>backend · model</th><th>source</th><th class="r">words</th><th class="r">avg_logprob</th><th class="r">primary</th></tr></thead><tbody id="cacheBody"></tbody></table>
      </div>
    </div>`);
  const body = panel.querySelector("#cacheBody");
  WAV_TRANSCRIPTS.forEach((c, i) => {
    const row = el(`
      <tr>
        <td><span class="mono">${esc(c.backend)} · ${esc(c.model)}</span></td>
        <td><span class="tag ${c.source === "stripped" ? "on" : "off"}">${esc(c.source)}</span></td>
        <td class="num">${esc(String(c.words))}</td>
        <td class="num">${esc(c.avgLogprob.toFixed(2))}</td>
        <td class="r"><button class="pickprimary ${c.primary ? "is-primary" : ""}" data-cache="${i}">${c.primary ? "● primary" : "set"}</button></td>
      </tr>`);
    body.appendChild(row);
  });
  // pick primary (mock: toggle the visual primary)
  body.querySelectorAll("[data-cache]").forEach((b) => b.addEventListener("click", () => {
    body.querySelectorAll(".pickprimary").forEach((x) => { x.classList.remove("is-primary"); x.textContent = "set"; });
    b.classList.add("is-primary"); b.textContent = "● primary";
  }));
  return panel;
}

function viewRecordingsEmpty(sess) {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 2 · Recordings",
    title: "Recordings",
    sub: `<b>${esc(sess.label)}</b> — no WAVs recorded yet`,
  });
  wrap.appendChild(el(`
    <div class="panel"><div class="panel__body"><div class="empty">
      <div class="empty__ic">🌊</div>
      <div class="empty__h">No recordings yet</div>
      <div>Once taps record into this session, each WAV appears here. Strip silence with the live waveform knobs, then transcribe.</div>
    </div></div></div>`));
  return wrap;
}

function afterRecordings() {
  const sess = session();
  if (sess.fresh || sess.wavCount === 0) return;
  drawWaveform();
  // source toggle
  document.querySelectorAll("#srcSw [data-src]").forEach((b) => b.addEventListener("click", () => { state.recSource = b.dataset.src; render(); }));
  // strip/clear
  const strip = document.getElementById("stripBtn");
  if (strip) strip.addEventListener("click", () => pulse(strip));
  const clear = document.getElementById("clearBtn");
  if (clear) clear.addEventListener("click", () => { state.knobs = { ...STRIP_DEFAULTS }; render(); });
  // live re-cut knobs
  document.querySelectorAll(".knobbar input[type=range]").forEach((inp) => {
    inp.addEventListener("input", () => {
      state.knobs[inp.dataset.key] = Number(inp.value);
      document.getElementById(`rv_${inp.dataset.key}`).textContent = `${inp.value} ${inp.dataset.unit}`;
      const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
      document.getElementById("sClips").textContent = r.clips;
      document.getElementById("sSpeech").textContent = `${r.speechS}s`;
      document.getElementById("sKept").textContent = `${Math.round((r.speechS / REP_WAV.durationS) * 100)}%`;
      drawWaveform();
    });
  });
}

function drawWaveform() {
  const cv = document.getElementById("waveCanvas");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const peaks = REP_WAV.peaks, n = peaks.length, dur = REP_WAV.durationS;
  const { regions } = computeRegions(peaks, dur, state.knobs);
  const inRegion = (t) => regions.some((rg) => t >= rg.startS && t <= rg.endS);
  const mid = H / 2;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#f5a623";

  ctx.fillStyle = "rgba(245,166,35,0.06)";
  for (const rg of regions) {
    const x0 = (rg.startS / dur) * W, x1 = (rg.endS / dur) * W;
    ctx.fillRect(x0, 0, x1 - x0, H);
  }
  const bw = W / n;
  for (let i = 0; i < n; i++) {
    const t = (i / n) * dur, v = peaks[i];
    const bh = Math.max(1.2, v * (H * 0.92));
    if (inRegion(t)) {
      const g = ctx.createLinearGradient(0, mid - bh / 2, 0, mid + bh / 2);
      g.addColorStop(0, accent); g.addColorStop(1, "#b87a12");
      ctx.fillStyle = g;
    } else { ctx.fillStyle = "#262d38"; }
    ctx.fillRect(i * bw, mid - bh / 2, Math.max(1, bw * 0.78), bh);
  }
  ctx.strokeStyle = "rgba(245,166,35,0.85)"; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
  for (const rg of regions) {
    for (const tt of [rg.startS, rg.endS]) {
      const x = (tt / dur) * W;
      ctx.beginPath(); ctx.moveTo(x, 6); ctx.lineTo(x, H - 6); ctx.stroke();
    }
  }
  ctx.setLineDash([]);
  const floorAmp = Math.pow(10, state.knobs.speechFloorDb / 20);
  const fy1 = mid - floorAmp * (H * 0.92) / 2, fy2 = mid + floorAmp * (H * 0.92) / 2;
  ctx.strokeStyle = "rgba(255,93,93,0.4)"; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(0, fy1); ctx.lineTo(W, fy1); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, fy2); ctx.lineTo(W, fy2); ctx.stroke();
  ctx.setLineDash([]);
}

// =============================================================================
// SESSION · 3 TRANSCRIPT — merged result. Tight IRC transcript (speaker +
// identity, language, confidence, matched_rule with restore, translation badge),
// speaking-time bar, models/backends used, + a VISIBLE engine override.
// =============================================================================
function viewTranscript() {
  const sess = session();
  if (sess.fresh || !sess.hasTranscript) return viewTranscriptEmpty(sess);

  const tx = TRANSCRIPT;
  const suppressed = tx.lines.filter((l) => l.suppressed).length;
  const low = tx.lines.filter((l) => l.lowConfidence).length;
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 3 · Merged",
    title: "Transcript",
    sub: `models_used <span class='mono'>${esc(tx.model)}</span> · backends_used <span class='mono'>${esc(tx.backend)}</span> · ${tx.translated ? "<span style='color:var(--info)'>contains translations</span>" : "no translation"}`,
    actions: `<button class="act act--primary" id="rerunBtn">↻ re-run with override</button>`,
  });

  const grid = el(`<div class="grid cols-tx"></div>`);

  // PRIMARY: IRC merged transcript
  const stBar = tx.speakingTime.map((s) =>
    `<span class="sptiny spk-ink-${s.spk}" style="flex:${s.pct}" title="${esc(s.speaker)} ${s.pct}%"><span class="sptiny__bar spk-bar-${s.spk}"></span><span class="sptiny__lab">${esc(s.speaker.replace("Oslo Room · ", ""))} ${s.pct}%</span></span>`
  ).join("");
  const txPanel = el(`
    <div class="panel panel--primary">
      <div class="panel__head"><div class="panel__title"><span class="ic">📝</span>Merged transcript</div>
        <div class="panel__hint">${esc(String(tx.lines.length))} lines · ${esc(String(low))} low-conf · ${esc(String(suppressed))} suppressed</div></div>
      <div class="sptbar" title="speaking time">${stBar}</div>
      <div class="panel__body flush"><div class="irclog tx" id="txBody"></div></div>
      <div class="audit" id="audit"></div>
    </div>`);
  const txBody = txPanel.querySelector("#txBody");
  // attach identity to lines so the IRC badge shows speaker + identity
  for (const ln of tx.lines) {
    const idForSpeaker = SPEAKERS.find((s) => s.spk === ln.spk)?.id || null;
    txBody.appendChild(el(ircLine({ ...ln, identity: idForSpeaker }, { restorable: true })));
  }
  // filter audit (suppressed/low-confidence; restore affordance)
  const flagged = tx.lines.filter((l) => l.suppressed || l.lowConfidence);
  const audit = txPanel.querySelector("#audit");
  audit.appendChild(el(`
    <button class="audit__toggle" id="auditToggle">
      <span>🛡️ Filter audit <span class="dim">· ${esc(String(flagged.length))} flagged (${esc(String(suppressed))} suppressed, ${esc(String(low))} low-conf)</span></span>
      <span class="audit__chev">${state.auditOpen ? "⌃" : "⌄"}</span>
    </button>`));
  if (state.auditOpen) {
    const abody = el(`<div class="audit__body"></div>`);
    for (const l of flagged) {
      const kind = l.suppressed ? `suppressed · ${l.matchedRule}` : `low confidence ${(l.confidence ?? 0).toFixed(2)}`;
      const tone = l.suppressed ? "sup" : "low";
      abody.appendChild(el(`
        <div class="audit__item">
          <div class="row-between" style="margin-bottom:3px">
            <span class="mono dim" style="font-size:10px">${esc(clock(l.t))} · ${esc(l.speaker)}</span>
            <span class="ircb ${tone}">${esc(kind)}</span>
          </div>
          <div style="font-size:11px;color:var(--ink-3);font-style:italic">"${esc(l.text)}"</div>
        </div>`));
    }
    abody.appendChild(el(`<div class="muted" style="font-size:10px;padding-top:7px">Suppressed lines stay out of the merge but are logged here — a wrong filter can be restored from the line.</div>`));
    audit.appendChild(abody);
  }
  grid.appendChild(txPanel);

  // SECONDARY: VISIBLE engine override (the per-session override of Settings)
  const aside = el(`<div class="aside"></div>`);
  const ovPanel = el(`
    <div class="panel panel--accent">
      <div class="panel__head"><div class="panel__title"><span class="ic">🧠</span>Engine · session override</div><div class="panel__hint">overrides Settings default</div></div>
      <div class="panel__body" id="engOverBody"></div>
    </div>`);
  const mountOver = () => {
    const body = ovPanel.querySelector("#engOverBody");
    body.innerHTML = "";
    body.appendChild(el(`<div class="muted" style="font-size:10px;margin-bottom:9px">default: <span class="mono">${esc(state.engineDefault.backend)} · ${esc(state.engineDefault.model)}</span> — change below to override for this session only</div>`));
    const ctl = engineControls(state.engineOverride, "ov");
    body.appendChild(ctl);
    wireEngine(body, state.engineOverride, "ov", mountOver);
  };
  mountOver();
  aside.appendChild(ovPanel);
  grid.appendChild(aside);
  wrap.appendChild(grid);
  return wrap;
}

function viewTranscriptEmpty(sess) {
  const wrap = el(`<div></div>`);
  const wavs = wavModel();
  wrap.innerHTML = header({
    eyebrow: "Stage 3 · Merged",
    title: "Transcript",
    sub: sess.fresh ? `<b>${esc(sess.label)}</b> — nothing recorded yet`
      : `<b>${esc(sess.label)}</b> · ${esc(String(wavs.length))} WAVs recorded, not transcribed yet`,
    actions: sess.fresh ? "" : `<button class="act" data-go="recordings">🌊 Recordings <span class="act__val">transcribe</span></button>`,
  });
  wrap.appendChild(el(`
    <div class="panel"><div class="panel__body"><div class="empty">
      <div class="empty__ic">📝</div>
      <div class="empty__h">${sess.fresh ? "Nothing to transcribe yet" : "Not transcribed yet"}</div>
      <div>${sess.fresh
        ? "Once taps record into this session, strip silence in <b>Recordings</b>, then run the engine to produce the merged transcript."
        : "Strip + transcribe the WAVs in <b>Recordings</b> to produce the merged transcript here."}</div>
    </div></div></div>`));
  queueMicrotask(() => wrap.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => goView(b.dataset.go))));
  return wrap;
}

function afterTranscript() {
  const rerun = document.getElementById("rerunBtn");
  if (rerun) rerun.addEventListener("click", () => pulse(rerun));
  const at = document.getElementById("auditToggle");
  if (at) at.addEventListener("click", () => { state.auditOpen = !state.auditOpen; render(); });
  // restore a suppressed line (mock: visual un-strike)
  document.querySelectorAll("#txBody [data-restore]").forEach((b) => b.addEventListener("click", () => {
    const row = b.closest(".irc");
    if (row) { row.classList.remove("is-sup"); b.remove(); }
  }));
}

// =============================================================================
// boot + screenshot hooks
// =============================================================================
render();

window.gotoView = (name) => { state.isFresh = false; goView(name); };
window.stagesGo = window.gotoView;
window.stagesPickSession = (id) => { if (SESSIONS.some((s) => s.id === id)) { state.isFresh = false; state.sessionId = id; render(); } };
window.stagesNewSession = () => { document.getElementById("newSession").click(); };
window.stagesSelectWav = (i) => { state.view = "recordings"; state.selectedWav = i; render(); };
window.stagesSetKnob = (key, val) => { if (key in state.knobs) { state.knobs[key] = val; render(); } };
window.stagesSetTap = (identity, mode) => { if (identity in state.tapMode) { state.tapMode[identity] = mode; state.expandedTap = identity; render(); } };
// Set the per-session engine override (Transcript) to show it diverging from
// the Settings default — used by the 08-transcript-engine screenshot.
window.stagesSetOverride = (family, model, backend) => {
  const fam = MODELS.find((f) => f.family === family);
  const m = fam?.models.find((x) => x.id === model);
  if (!m) return;
  state.engineOverride.family = family;
  state.engineOverride.model = model;
  if (backend && APP.backends.some((b) => b.kind === backend && b.available)) state.engineOverride.backend = backend;
  state.view = "transcript";
  render();
};
