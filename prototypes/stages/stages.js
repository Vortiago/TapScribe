// =============================================================================
// TapScribe · Stages — the meeting's life as a guided journey.
// A slim ordered "spine" (Capture → Transcript → People) with live state on
// each stop; one dense, logically-grouped workspace at a time.
//
// Transcript is the merged-stage focus: a tight IRC-style merged transcript
// dominates, with recordings + waveform + strip-silence tuning folded in as a
// secondary side panel and the engine controls living in a compact header
// popover. One clear focus per screen, dense within, calm between.
// =============================================================================

import {
  LANGS, SPEAKERS, MODELS, selectedModel, LIVE_TAPS, LIVE_CAPTIONS,
  SESSIONS, STRIP_DEFAULTS, REP_WAV, TRANSCRIPT, computeRegions, helpers,
  speakerById, APP,
} from "../_shared/mock-data.js";

const { clock, clockH } = helpers;
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ----- live, mutable UI state ------------------------------------------------
const state = {
  stage: "capture",
  sessionId: SESSIONS.find((s) => s.current)?.id || SESSIONS[0].id,
  knobs: { ...STRIP_DEFAULTS },
  selectedClip: null,          // null → recordings panel collapsed to the clip list
  engine: { ...selectedModel },
  enginePopover: false,        // compact engine control opens as a popover
  auditOpen: false,            // collapsible filter audit under the transcript
  isFresh: false,              // "New session" empty state overlaying the live one
  // per-speaker "transcribe as" quick switch (defaults to primary lang)
  transcribeAs: Object.fromEntries(SPEAKERS.map((s) => [s.id, s.primaryLang])),
};

// A synthetic, empty session used by the "New session" button.
const FRESH_SESSION = {
  id: "__fresh__",
  label: "New session",
  folder: "recordings/(pending)",
  startedAt: new Date().toISOString(),
  durationS: 0,
  wavCount: 0,
  speakers: [],
  current: true,
  hasTranscript: false,
  langs: [],
  fresh: true,
};

function session() {
  if (state.isFresh) return FRESH_SESSION;
  return SESSIONS.find((s) => s.id === state.sessionId) || SESSIONS[0];
}

// =============================================================================
// Stage definitions — the ORDERED journey (Capture → Transcript → People).
// Each carries a live status chip derived from the current session/data, so the
// spine doubles as a readout.
// =============================================================================
function stageDefs() {
  const sess = session();
  const fresh = !!sess.fresh;
  const liveCount = sess.current ? LIVE_TAPS.filter((t) => t.live).length : 0;
  const recs = clipModel();
  const needTune = recs.filter((c) => c.needsTune).length;
  const suppressed = TRANSCRIPT.lines.filter((l) => l.suppressed).length;
  return [
    {
      id: "capture", n: 1, ic: "🎙️", name: "Capture",
      chip: fresh
        ? { tone: "mute", text: "no taps yet" }
        : sess.current
          ? (liveCount ? { tone: "live", text: `${liveCount} live` } : { tone: "mute", text: "idle" })
          : { tone: "good", text: `${sess.speakers.length} sources` },
      done: !fresh && !sess.current,
    },
    {
      id: "transcript", n: 2, ic: "📝", name: "Transcript",
      chip: fresh
        ? { tone: "mute", text: "nothing yet" }
        : sess.hasTranscript
          ? (suppressed ? { tone: "warn", text: `${suppressed} suppressed` } : { tone: "good", text: "reviewed" })
          : (needTune ? { tone: "warn", text: `${needTune} to tune` } : { tone: "mute", text: "not run" }),
      done: !fresh && sess.hasTranscript,
    },
    {
      id: "people", n: 3, ic: "👥", name: "People",
      chip: { tone: "mute", text: `${SPEAKERS.length} profiles` },
      done: false,
    },
  ];
}

// Recordings model: synthesize a small clip list for THIS session from REP_WAV +
// strip-silence so the "needs tuning" status is real (clips whose default cut
// produced >1 region are flagged as "tune"). Empty for a fresh session.
function clipModel() {
  const sess = session();
  if (sess.fresh || sess.wavCount === 0) return [];
  const names = [
    { sp: "atle", t: "09:04:12" }, { sp: "mette", t: "09:05:48" },
    { sp: "room-oslo", t: "09:07:03" }, { sp: "atle", t: "09:09:21" },
    { sp: "james", t: "09:11:40" },
  ].slice(0, Math.min(5, Math.max(2, Math.round(sess.wavCount / 8))));
  return names.map((c, i) => {
    const dur = [48, 31, 62, 22, 18][i] ?? 30;
    const clips = computeRegions(REP_WAV.peaks, REP_WAV.durationS, STRIP_DEFAULTS).clips;
    const needsTune = i % 2 === 0; // alternate flag for a realistic mix
    return { ...c, dur, clips: needsTune ? clips : 1, needsTune, idx: i };
  });
}

// =============================================================================
// SPINE
// =============================================================================
function renderSpine() {
  const sess = session();
  document.getElementById("sessionLabel").textContent = sess.label || "(untitled session)";
  document.getElementById("sessionMeta").textContent = sess.fresh
    ? "fresh · 0 clips"
    : `${clockH(sess.durationS)} · ${sess.wavCount} clips`;
  document.getElementById("sessionLive").style.display = sess.current ? "" : "none";

  const nav = document.getElementById("stagesNav");
  nav.innerHTML = "";
  const defs = stageDefs();
  for (const d of defs) {
    const active = d.id === state.stage;
    const node = el(`
      <button class="stage ${active ? "is-active" : ""} ${d.done ? "is-done" : ""}" data-stage="${d.id}">
        <span class="stage__rail"><span class="stage__num">${d.done && !active ? "✓" : d.n}</span></span>
        <span class="stage__body">
          <span class="stage__name"><span class="ic">${d.ic}</span>${d.name}</span>
          <span class="stage__chip tone-${d.chip.tone}"><span class="dot"></span>${esc(d.chip.text)}</span>
        </span>
      </button>`);
    node.addEventListener("click", () => goStage(d.id));
    nav.appendChild(node);
  }

  // journey progress fill: how far down the pipeline the session sits
  const idx = defs.findIndex((d) => d.id === state.stage);
  const fill = Math.round(((idx + 1) / defs.length) * 100);
  document.getElementById("journeyFill").style.width = `${fill}%`;
  document.getElementById("journeyCap").innerHTML =
    `<span>Stage ${idx + 1} of ${defs.length}</span><span>${fill}%</span>`;
}

// session menu
function renderSessionMenu() {
  const menu = document.getElementById("sessionMenu");
  menu.innerHTML = "";
  for (const s of SESSIONS) {
    const item = el(`
      <button class="smitem ${!state.isFresh && s.id === state.sessionId ? "is-current" : ""}">
        <span class="smitem__dot"></span>
        <span class="smitem__body">
          <span class="smitem__label">${esc(s.label || "(untitled)")}</span>
          <span class="smitem__meta">${esc(s.startedAt.slice(0, 10))} · ${clockH(s.durationS)} · ${s.wavCount} clips</span>
        </span>
        ${s.current ? '<span class="smitem__badge" style="color:#ffb3b3;border-color:#4a2626">live</span>' : (s.hasTranscript ? '<span class="smitem__badge">tx</span>' : '<span class="smitem__badge">raw</span>')}
      </button>`);
    item.addEventListener("click", () => {
      state.isFresh = false;
      state.sessionId = s.id;
      menu.hidden = true;
      // switching session re-seeds the journey at the most relevant stage
      state.stage = s.current ? "capture" : "transcript";
      state.selectedClip = null;
      render();
    });
    menu.appendChild(item);
  }
}

document.getElementById("sessionPick").addEventListener("click", () => {
  const m = document.getElementById("sessionMenu");
  m.hidden = !m.hidden;
});

// "New session" — drop into a fresh, empty journey at Capture.
document.getElementById("newSession").addEventListener("click", () => {
  state.isFresh = true;
  state.stage = "capture";
  state.selectedClip = null;
  state.auditOpen = false;
  document.getElementById("sessionMenu").hidden = true;
  render();
  window.scrollTo(0, 0);
});

// =============================================================================
// WORKSPACE shell
// =============================================================================
// `actions` is raw trusted HTML built locally (ordinary action buttons — NOT
// forced next-step gates). Title/eyebrow are escaped; sub is trusted markup.
function header({ eyebrow, title, sub, actions }) {
  return `
    <div class="whead">
      <div class="whead__l">
        <div class="whead__eyebrow">${esc(eyebrow)}</div>
        <h1 class="whead__title">${esc(title)}</h1>
        <div class="whead__sub">${sub}</div>
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
  switch (state.stage) {
    case "capture": frag = viewCapture(); break;
    case "transcript": frag = viewTranscript(); break;
    case "people": frag = viewPeople(); break;
    default: frag = viewCapture();
  }
  root.appendChild(frag);
  // stage-specific post-render hooks
  if (state.stage === "transcript") afterTranscript();
}

function goStage(id) { state.stage = id; render(); window.scrollTo(0, 0); }

// =============================================================================
// IRC line builder — shared by the live captions feed and the merged transcript
// so both read as ONE tight stream: `[m:ss] Speaker: text`, speaker coloured,
// monospace, minimal gutters. Treatments (low-conf, suppressed, translation
// badge) ride on the same row.
// =============================================================================
function ircLine(ln, { inflight = false } = {}) {
  const cls = [
    "irc",
    ln.suppressed ? "is-sup" : "",
    ln.lowConfidence ? "is-low" : "",
    inflight ? "is-inflight" : "",
  ].filter(Boolean).join(" ");
  let badges = "";
  if (ln.translatedFrom) badges += `<span class="ircb tr">${esc(ln.translatedFrom)}→en</span>`;
  if (ln.lowConfidence) badges += `<span class="ircb low">${(ln.confidence ?? 0).toFixed(2)}</span>`;
  if (ln.suppressed) badges += `<span class="ircb sup">⨯ ${esc(ln.matchedRule || "rule")}</span>`;
  const cursor = inflight ? `<span class="irc__cursor">▍</span>` : "";
  return `
    <div class="${cls}">
      <span class="irc__t">${clock(ln.t)}</span>
      <span class="irc__who spk-ink-${ln.spk}">${esc(ln.speaker)}<span class="irc__lang flag">${LANGS[ln.lang]?.flag || ""}</span>:</span>
      <span class="irc__txt">${esc(ln.text)}${cursor}${badges}</span>
    </div>`;
}

// =============================================================================
// STAGE 1 — CAPTURE  (live taps + gate + rec/live + diarization + IRC captions)
// =============================================================================
function viewCapture() {
  const sess = session();
  if (sess.fresh) return viewCaptureFresh(sess);
  if (!sess.current) return viewCaptureArchived(sess);
  const live = LIVE_TAPS.filter((t) => t.live).length;
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 1 · Live",
    title: "Capture",
    sub: `${live} taps streaming into <b>${esc(sess.label)}</b> · recorder ${APP.recordingEnabled ? "<span style='color:var(--good)'>on</span>" : "<span class='muted'>paused</span>"} · backend <span class='mono'>${esc(APP.backend)}</span>`,
  });

  const grid = el(`<div class="grid cols-cap"></div>`);

  // ---- LEFT: live taps table (dense, with inline diarization) ----
  const tapsPanel = el(`
    <div class="panel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">🎙️</span>Live taps</div>
        <div class="panel__hint">level · lag · gate · rec/live</div>
      </div>
      <div class="panel__body flush"><div class="taps"></div></div>
    </div>`);
  const taps = tapsPanel.querySelector(".taps");
  for (const t of LIVE_TAPS) {
    const sp = speakerById(t.identity);
    const idle = !t.gateOpen && t.level < 0.02;
    const row = el(`
      <div class="tap ${idle ? "is-idle" : ""}">
        <span class="av spk-${t.spk}">${esc(sp?.initials || "??")}</span>
        <span class="tap__id">
          <span class="tap__name">${esc(t.name)}</span>
          <span class="tap__meta"><span class="flag">${LANGS[t.lang]?.flag || ""}</span>${esc(LANGS[t.lang]?.name || t.lang)} · ${esc(sp?.mic.label || "—")}</span>
        </span>
        <span class="meter">
          <span class="meter__bar"><span class="meter__fill spk-bar-${t.spk}" style="width:${Math.round(t.level * 100)}%"></span></span>
          <canvas class="meter__spark" width="160" height="36" data-spark='${JSON.stringify(t.levels)}' data-spk="${t.spk}"></canvas>
        </span>
        <span class="gate">
          <span class="gate__led ${t.gateOpen ? "open" : ""}"></span>
          <span class="gate__txt ${t.gateOpen ? "open" : ""}">${t.gateOpen ? "gate open" : "gate shut"}</span>
          <span class="lag ${t.lagS > 1.2 ? "hot" : ""}">${t.lagS ? t.lagS.toFixed(1) + "s lag" : ""}</span>
        </span>
        <span class="toggles">
          <span class="tg rec ${t.record ? "on" : ""}">● REC</span>
          <span class="tg live ${t.live ? "on" : ""}">LIVE</span>
        </span>
      </div>`);
    taps.appendChild(row);

    // diarization rendered INLINE as a property of this tap (room mic only)
    if (sp?.isRoom && sp.diarizedInto) {
      const diar = el(`
        <div class="diar">
          <div class="diar__head">↳ diarized into ${sp.diarizedInto.length} voices <span class="tag warn">multi-voice tap</span></div>
        </div>`);
      for (const d of sp.diarizedInto) {
        diar.appendChild(el(`
          <div class="diar__row">
            <span class="av sm spk-${d.spk}">${d.spk}</span>
            <span class="diar__name">${esc(d.label)} <span class="flag">${LANGS[d.lang]?.flag || ""}</span> <span class="dim mono" style="font-size:10px">${esc(d.lang)}</span></span>
            <span class="diar__split"><span class="diar__splitfill spk-bar-${d.spk}" style="width:${d.talkPct}%"></span></span>
            <span class="diar__pct">${d.talkPct}%</span>
          </div>`));
      }
      taps.appendChild(diar);
    }
  }
  grid.appendChild(tapsPanel);

  // ---- RIGHT: live captions feed — SAME tight IRC stream as the transcript ----
  const capsPanel = el(`
    <div class="panel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">💬</span>Live captions</div>
        <div class="panel__hint">speaker · language</div>
      </div>
      <div class="panel__body flush"><div class="irclog caps"></div></div>
    </div>`);
  const caps = capsPanel.querySelector(".irclog");
  for (const c of LIVE_CAPTIONS) caps.appendChild(el(ircLine(c, { inflight: c.inflight })));
  grid.appendChild(capsPanel);
  wrap.appendChild(grid);

  // ---- BOTTOM ROW: capture health summary + capture settings (grouped) ----
  wrap.appendChild(el(`<div class="spacer"></div>`));
  const bottom = el(`<div class="grid cols-cap"></div>`);

  // health summary — gates open, languages in play, lag health
  const openGates = LIVE_TAPS.filter((t) => t.gateOpen).length;
  const recOn = LIVE_TAPS.filter((t) => t.record).length;
  const langSet = [...new Set(LIVE_TAPS.map((t) => t.lang))];
  const maxLag = Math.max(...LIVE_TAPS.map((t) => t.lagS));
  bottom.appendChild(el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">📡</span>Capture health</div><div class="panel__hint">this session, right now</div></div>
      <div class="panel__body">
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px">
          <div class="profcell"><div class="profcell__k">Gates open</div><div class="profcell__v">${openGates}<span class="dim" style="font-size:11px"> / ${LIVE_TAPS.length}</span></div></div>
          <div class="profcell"><div class="profcell__k">Recording</div><div class="profcell__v">${recOn}<span class="dim" style="font-size:11px"> / ${LIVE_TAPS.length}</span></div></div>
          <div class="profcell"><div class="profcell__k">Max lag</div><div class="profcell__v">${maxLag.toFixed(1)}s</div></div>
          <div class="profcell"><div class="profcell__k">Languages</div><div class="profcell__v" style="font-size:16px">${langSet.map((c) => LANGS[c]?.flag || "").join(" ")}</div></div>
        </div>
      </div>
    </div>`));

  // capture settings — prompt / hotwords / hallucination rules / recording
  bottom.appendChild(el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">⚙️</span>Capture settings</div><div class="panel__hint">applies live</div></div>
      <div class="panel__body">
        <dl class="kv">
          <dt>Recording</dt><dd><span class="tag on">on</span></dd>
          <dt>Prompt</dt><dd>"Nordic Sync, quarterly review"</dd>
          <dt>Hotwords</dt><dd>Vortiago · Nordic · KPI</dd>
          <dt>Hallucination rules</dt><dd>3 active <span class="dim">(youtube-outro…)</span></dd>
        </dl>
      </div>
    </div>`));
  wrap.appendChild(bottom);

  // draw sparklines after mount
  queueMicrotask(() => drawSparks(wrap));
  return wrap;
}

// Fresh "New session" capture — nothing has streamed in yet.
function viewCaptureFresh(sess) {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 1 · New session",
    title: "Capture",
    sub: `<b>${esc(sess.label)}</b> is armed · waiting for the first tap to connect`,
  });
  wrap.appendChild(el(`
    <div class="panel"><div class="panel__body"><div class="empty">
      <div style="font-size:30px;margin-bottom:8px">🎙️</div>
      <div style="font-weight:600;color:var(--ink-2);margin-bottom:4px">No taps yet</div>
      <div>This session is recording-ready. As Bridges connect, each appears here with its level, lag, gate and rec/live state — room mics split into diarized voices inline.</div>
    </div></div></div>`));
  // a calm hint of what's next, without forcing the journey
  wrap.appendChild(el(`<div class="spacer"></div>`));
  const bottom = el(`<div class="grid cols-cap"></div>`);
  bottom.appendChild(el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">📡</span>Capture health</div><div class="panel__hint">nothing yet</div></div>
      <div class="panel__body">
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px">
          <div class="profcell"><div class="profcell__k">Gates open</div><div class="profcell__v dim">0 <span style="font-size:11px">/ 0</span></div></div>
          <div class="profcell"><div class="profcell__k">Recording</div><div class="profcell__v dim">0 <span style="font-size:11px">/ 0</span></div></div>
          <div class="profcell"><div class="profcell__k">Max lag</div><div class="profcell__v dim">—</div></div>
          <div class="profcell"><div class="profcell__k">Languages</div><div class="profcell__v dim">—</div></div>
        </div>
      </div>
    </div>`));
  bottom.appendChild(el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">💬</span>Live captions</div><div class="panel__hint">speaker · language</div></div>
      <div class="panel__body"><div class="empty" style="padding:24px 12px">No captions yet — they stream in as speech is transcribed.</div></div>
    </div>`));
  wrap.appendChild(bottom);
  return wrap;
}

// Capture for a PAST session: no live taps — show the sources that were
// captured (closed), so the stage still reflects the session's real state.
function viewCaptureArchived(sess) {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 1 · Archived",
    title: "Capture",
    sub: `<b>${esc(sess.label)}</b> finished · ${sess.speakers.length} sources captured · ${sess.wavCount} clips recorded`,
  });
  const panel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">🎙️</span>Captured sources</div><div class="panel__hint">closed · no longer live</div></div>
      <div class="panel__body flush"><div class="taps"></div></div>
    </div>`);
  const taps = panel.querySelector(".taps");
  for (const id of sess.speakers) {
    const sp = speakerById(id);
    if (!sp) continue;
    taps.appendChild(el(`
      <div class="tap is-idle">
        <span class="av spk-${sp.spk}">${esc(sp.initials)}</span>
        <span class="tap__id">
          <span class="tap__name">${esc(sp.name)}</span>
          <span class="tap__meta"><span class="flag">${LANGS[sp.primaryLang]?.flag || ""}</span>${esc(LANGS[sp.primaryLang]?.name || sp.primaryLang)} · ${esc(sp.mic.label)}</span>
        </span>
        <span class="muted mono" style="font-size:10px">recorded</span>
        <span></span>
        <span class="toggles"><span class="tg rec on">● REC</span></span>
      </div>`));
    if (sp.isRoom && sp.diarizedInto) {
      const diar = el(`<div class="diar"><div class="diar__head">↳ diarized into ${sp.diarizedInto.length} voices <span class="tag warn">multi-voice tap</span></div></div>`);
      for (const d of sp.diarizedInto) {
        diar.appendChild(el(`
          <div class="diar__row">
            <span class="av sm spk-${d.spk}">${d.spk}</span>
            <span class="diar__name">${esc(d.label)} <span class="flag">${LANGS[d.lang]?.flag || ""}</span></span>
            <span class="diar__split"><span class="diar__splitfill spk-bar-${d.spk}" style="width:${d.talkPct}%"></span></span>
            <span class="diar__pct">${d.talkPct}%</span>
          </div>`));
      }
      taps.appendChild(diar);
    }
  }
  wrap.appendChild(panel);
  return wrap;
}

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
      const bh = Math.max(2, v * (h - 4));
      ctx.fillStyle = v < 0.05 ? "#2a313c" : color;
      ctx.globalAlpha = v < 0.05 ? 1 : 0.85;
      ctx.fillRect(i * bw + 1, (h - bh) / 2, Math.max(1.5, bw - 2), bh);
    }
    ctx.globalAlpha = 1;
  });
}

// =============================================================================
// STAGE 2 — TRANSCRIPT  (merged stage: IRC transcript PRIMARY; recordings +
// waveform + strip-silence folded in as a secondary side panel; engine controls
// in a compact header popover)
// =============================================================================
function viewTranscript() {
  const sess = session();
  const wrap = el(`<div></div>`);

  if (sess.fresh || !sess.hasTranscript) return viewTranscriptEmpty(sess, wrap);

  const tx = TRANSCRIPT;
  const suppressed = tx.lines.filter((l) => l.suppressed).length;
  const low = tx.lines.filter((l) => l.lowConfidence).length;
  wrap.innerHTML = header({
    eyebrow: "Stage 2 · Transcript",
    title: "Transcript",
    sub: `merged · <span class='mono'>${esc(tx.model)}</span> on <span class='mono'>${esc(tx.backend)}</span> · ${tx.translated ? "<span style='color:#8fd0ff'>contains translations</span>" : "no translation"}`,
    actions: `
      <button class="act" id="enginePop">⚙️ Engine <span class="act__val mono">${esc(state.engine.model)}</span> <span class="act__chev">⌄</span></button>
      <button class="act act--primary" id="rerunBtn">↻ Re-run transcript</button>`,
  });

  // engine popover (compact control, NOT a co-equal panel)
  wrap.appendChild(buildEnginePopover());

  // ---- PRIMARY: dense IRC merged transcript dominates the canvas ----
  const main = el(`<div class="grid cols-tx"></div>`);

  const stBar = tx.speakingTime.map((s) =>
    `<span class="sptiny spk-ink-${s.spk}" style="flex:${s.pct}" title="${esc(s.speaker)} ${s.pct}%"><span class="sptiny__bar spk-bar-${s.spk}"></span><span class="sptiny__lab">${esc(s.speaker.replace("Oslo Room · ", ""))} ${s.pct}%</span></span>`
  ).join("");

  const txPanel = el(`
    <div class="panel panel--primary">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">📝</span>Merged transcript</div>
        <div class="panel__hint">${tx.lines.length} lines · ${low} low-conf · ${suppressed} suppressed</div>
      </div>
      <div class="sptbar" title="speaking time">${stBar}</div>
      <div class="panel__body flush"><div class="irclog tx"></div></div>
      <div class="audit" id="audit"></div>
    </div>`);
  const txBody = txPanel.querySelector(".irclog");
  for (const ln of tx.lines) txBody.appendChild(el(ircLine(ln)));

  // collapsible filter audit, folded at the bottom of the transcript itself
  const flagged = tx.lines.filter((l) => l.suppressed || l.lowConfidence);
  const audit = txPanel.querySelector(".audit");
  audit.appendChild(el(`
    <button class="audit__toggle" id="auditToggle">
      <span>🛡️ Filter audit <span class="dim">· ${flagged.length} flagged (${suppressed} suppressed, ${low} low-conf)</span></span>
      <span class="audit__chev">${state.auditOpen ? "⌃" : "⌄"}</span>
    </button>`));
  if (state.auditOpen) {
    const body = el(`<div class="audit__body"></div>`);
    for (const l of flagged) {
      const kind = l.suppressed ? `suppressed · ${esc(l.matchedRule)}` : `low confidence ${(l.confidence ?? 0).toFixed(2)}`;
      const tone = l.suppressed ? "sup" : "low";
      body.appendChild(el(`
        <div class="audit__item">
          <div class="row-between" style="margin-bottom:3px">
            <span class="mono dim" style="font-size:10px">${clock(l.t)} · ${esc(l.speaker)}</span>
            <span class="ircb ${tone}">${kind}</span>
          </div>
          <div style="font-size:11.5px;color:var(--ink-3);font-style:italic">"${esc(l.text)}"</div>
        </div>`));
    }
    body.appendChild(el(`<div class="muted" style="font-size:10.5px;padding-top:8px">Suppressed lines stay out of the merge but are logged here so a wrong filter can be audited and restored.</div>`));
    audit.appendChild(body);
  }
  main.appendChild(txPanel);

  // ---- SECONDARY: "Recordings & tuning" side panel (contextual disclosure) ----
  main.appendChild(buildRecordingsPanel());

  wrap.appendChild(main);
  return wrap;
}

// Empty transcript — either a fresh session or a recorded-but-not-transcribed
// one. Keeps the recordings side panel so the clips are still reachable.
function viewTranscriptEmpty(sess, wrap) {
  const clips = clipModel();
  wrap.innerHTML = header({
    eyebrow: "Stage 2 · Transcript",
    title: "Transcript",
    sub: sess.fresh
      ? `<b>${esc(sess.label)}</b> — nothing recorded yet`
      : `<b>${esc(sess.label)}</b> · ${clips.length} clips recorded, not transcribed yet`,
    actions: sess.fresh ? "" : `
      <button class="act" id="enginePop">⚙️ Engine <span class="act__val mono">${esc(state.engine.model)}</span> <span class="act__chev">⌄</span></button>
      <button class="act act--primary" id="rerunBtn">▶ Transcribe ${clips.length} clips</button>`,
  });
  if (!sess.fresh) wrap.appendChild(buildEnginePopover());

  const main = el(`<div class="grid cols-tx"></div>`);
  main.appendChild(el(`
    <div class="panel panel--primary"><div class="panel__body"><div class="empty">
      <div style="font-size:30px;margin-bottom:8px">📝</div>
      <div style="font-weight:600;color:var(--ink-2);margin-bottom:4px">${sess.fresh ? "Nothing to transcribe yet" : "Not transcribed yet"}</div>
      <div>${sess.fresh
        ? "Once taps record into this session, tune their recordings on the right, then run the engine to produce the merged transcript."
        : `Tune the ${clips.length} clips on the right if needed, then run the engine to produce the merged transcript.`}</div>
    </div></div></div>`));
  main.appendChild(buildRecordingsPanel());
  wrap.appendChild(main);
  return wrap;
}

// ---- SECONDARY side panel: recordings list → reveal waveform + knobs ----
// Collapsed = just the clip list. Select a clip → waveform with live re-cut
// markers + the strip-silence knobs appear (contextual disclosure, so the
// transcript stays the dominant focus).
function buildRecordingsPanel() {
  const clips = clipModel();
  const aside = el(`<div class="aside"></div>`);

  const needTune = clips.filter((c) => c.needsTune).length;
  const listPanel = el(`
    <div class="panel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">📁</span>Recordings &amp; tuning</div>
        <div class="panel__hint">${clips.length} WAV${needTune ? ` · ${needTune} to tune` : ""}</div>
      </div>
      <div class="panel__body flush"><div class="cliplist"></div></div>
    </div>`);
  const list = listPanel.querySelector(".cliplist");
  if (!clips.length) {
    list.appendChild(el(`<div class="empty" style="padding:22px 12px">No recordings yet.</div>`));
  }
  clips.forEach((c, i) => {
    const sp = speakerById(c.sp);
    const node = el(`
      <button class="clip ${i === state.selectedClip ? "is-sel" : ""}" data-clip="${i}">
        <span class="clip__l">
          <span class="clip__name">…${c.t.replace(/:/g, "")}_${esc(c.sp)}.wav</span>
          <span class="clip__sub"><span class="av sm spk-${sp?.spk ?? 0}">${esc(sp?.initials || "?")}</span>${esc(sp?.name?.split(" ")[0] || c.sp)} · ${c.clips} clip${c.clips !== 1 ? "s" : ""}</span>
        </span>
        <span style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
          <span class="clip__dur">${clock(c.dur)}</span>
          <span class="clip__flag ${c.needsTune ? "tune" : "ok"}">${c.needsTune ? "tune" : "ok"}</span>
        </span>
      </button>`);
    node.addEventListener("click", () => { state.selectedClip = (state.selectedClip === i ? null : i); render(); });
    list.appendChild(node);
  });
  aside.appendChild(listPanel);

  // contextual disclosure: only when a clip is selected do the waveform + knobs
  // appear — the marquee strip-silence live re-cut, kept secondary to the tx.
  if (state.selectedClip != null && clips[state.selectedClip]) {
    aside.appendChild(buildTuningPanel(clips[state.selectedClip]));
  } else if (clips.length) {
    aside.appendChild(el(`
      <div class="panel panel--ghost">
        <div class="panel__body"><div class="tunehint">
          <span class="tunehint__ic">🌊</span>
          <div>Select a clip to open its <b>waveform</b> and the <b>strip-silence</b> knobs — cuts re-compute live as you drag.</div>
        </div></div>
      </div>`));
  }
  return aside;
}

function buildTuningPanel(sel) {
  const regions = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
  const k = state.knobs;
  const panel = el(`
    <div class="panel" id="tunePanel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">🌊</span>${esc(sel.t)} · strip-silence</div>
        <button class="panel__x" id="tuneClose" title="close">✕</button>
      </div>
      <div class="wavewrap">
        <div class="wave-meta">
          <span class="wave-meta__name">${esc(speakerById(sel.sp)?.name || sel.sp)} · ${clock(REP_WAV.durationS)}</span>
          <div class="wave-stats">
            <div class="wstat"><span class="wstat__v accent" id="statClips">${regions.clips}</span><span class="wstat__k">clips</span></div>
            <div class="wstat"><span class="wstat__v" id="statSpeech">${regions.speechS}s</span><span class="wstat__k">speech</span></div>
            <div class="wstat"><span class="wstat__v" id="statTrim">${(REP_WAV.durationS - regions.speechS).toFixed(1)}s</span><span class="wstat__k">trimmed</span></div>
          </div>
        </div>
        <div class="wavecanvas-wrap"><canvas id="waveCanvas" width="1280" height="280"></canvas></div>
        <div class="wave-axis"><span>0:00</span><span>${clock(REP_WAV.durationS / 2)}</span><span>${clock(REP_WAV.durationS)}</span></div>
        <div class="wave-legend">
          <span><span class="sw" style="background:linear-gradient(180deg,#f5a623,#b87a12)"></span>kept</span>
          <span><span class="sw" style="background:#2a313c"></span>dropped</span>
          <span class="dim">│ cut</span>
        </div>
        <div class="knobs">
          <div class="knob">
            <div class="knob__top"><span class="knob__label">Min silence gap</span><span class="knob__val" id="vGap">${k.minSilenceMs} ms</span></div>
            <input type="range" id="kGap" min="150" max="4000" step="50" value="${k.minSilenceMs}">
          </div>
          <div class="knob">
            <div class="knob__top"><span class="knob__label">Speech floor</span><span class="knob__val" id="vFloor">${k.speechFloorDb} dB</span></div>
            <input type="range" id="kFloor" min="-60" max="-25" step="1" value="${k.speechFloorDb}">
          </div>
          <div class="knob">
            <div class="knob__top"><span class="knob__label">Edge pad</span><span class="knob__val" id="vPad">${k.padMs} ms</span></div>
            <input type="range" id="kPad" min="0" max="500" step="25" value="${k.padMs}">
          </div>
          <div class="recount">
            <span>⤷ cuts into <b id="reCount">${regions.clips}</b> clip${regions.clips !== 1 ? "s" : ""}, keeping <b id="reSpeech">${regions.speechS}s</b> of ${REP_WAV.durationS}s</span>
            <div class="recount__act">
              <button class="act act--sm" id="recutBtn">re-cut</button>
              <button class="act act--sm act--ghost" id="resetBtn">reset</button>
            </div>
          </div>
        </div>
      </div>
    </div>`);
  return panel;
}

function afterTranscript() {
  // engine popover toggle
  const pop = document.getElementById("enginePop");
  if (pop) pop.addEventListener("click", () => { state.enginePopover = !state.enginePopover; syncPopover(); });
  // ordinary actions (no-op visual feedback — these are real-action affordances)
  const rerun = document.getElementById("rerunBtn");
  if (rerun) rerun.addEventListener("click", () => pulse(rerun));
  // audit toggle
  const at = document.getElementById("auditToggle");
  if (at) at.addEventListener("click", () => { state.auditOpen = !state.auditOpen; render(); });
  syncPopover();
  wireEnginePopover();
  // tuning panel (only present when a clip is selected)
  if (state.selectedClip != null) wireTuning();
}

function syncPopover() {
  const pop = document.getElementById("enginePopover");
  if (pop) pop.classList.toggle("is-open", state.enginePopover);
  const chev = document.querySelector("#enginePop .act__chev");
  if (chev) chev.textContent = state.enginePopover ? "⌃" : "⌄";
}

function pulse(btn) {
  btn.classList.add("is-pulse");
  setTimeout(() => btn.classList.remove("is-pulse"), 450);
}

function wireTuning() {
  drawWaveform();
  const close = document.getElementById("tuneClose");
  if (close) close.addEventListener("click", () => { state.selectedClip = null; render(); });
  const recut = document.getElementById("recutBtn");
  if (recut) recut.addEventListener("click", () => pulse(recut));
  const reset = document.getElementById("resetBtn");
  if (reset) reset.addEventListener("click", () => { state.knobs = { ...STRIP_DEFAULTS }; render(); });
  const wire = (id, vId, fmt, key) => {
    const inp = document.getElementById(id);
    if (!inp) return;
    inp.addEventListener("input", () => {
      state.knobs[key] = Number(inp.value);
      document.getElementById(vId).textContent = fmt(Number(inp.value));
      const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
      document.getElementById("reCount").textContent = r.clips;
      document.getElementById("reSpeech").textContent = `${r.speechS}s`;
      document.getElementById("statClips").textContent = r.clips;
      document.getElementById("statSpeech").textContent = `${r.speechS}s`;
      document.getElementById("statTrim").textContent = `${(REP_WAV.durationS - r.speechS).toFixed(1)}s`;
      drawWaveform();
    });
  };
  wire("kGap", "vGap", (v) => `${v} ms`, "minSilenceMs");
  wire("kFloor", "vFloor", (v) => `${v} dB`, "speechFloorDb");
  wire("kPad", "vPad", (v) => `${v} ms`, "padMs");
}

function drawWaveform() {
  const cv = document.getElementById("waveCanvas");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const peaks = REP_WAV.peaks, n = peaks.length, dur = REP_WAV.durationS;
  const { regions } = computeRegions(peaks, dur, state.knobs);
  const inRegion = (t) => regions.some((r) => t >= r.startS && t <= r.endS);
  const mid = H / 2;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#f5a623";

  // shade kept regions faintly behind the bars
  ctx.fillStyle = "rgba(245,166,35,0.07)";
  for (const r of regions) {
    const x0 = (r.startS / dur) * W, x1 = (r.endS / dur) * W;
    ctx.fillRect(x0, 0, x1 - x0, H);
  }

  // bars
  const bw = W / n;
  for (let i = 0; i < n; i++) {
    const t = (i / n) * dur;
    const v = peaks[i];
    const bh = Math.max(1.2, v * (H * 0.92));
    const kept = inRegion(t);
    if (kept) {
      const g = ctx.createLinearGradient(0, mid - bh / 2, 0, mid + bh / 2);
      g.addColorStop(0, accent); g.addColorStop(1, "#b87a12");
      ctx.fillStyle = g;
    } else {
      ctx.fillStyle = "#2a313c";
    }
    ctx.fillRect(i * bw, mid - bh / 2, Math.max(1, bw * 0.78), bh);
  }

  // cut-point markers at each region edge
  ctx.strokeStyle = "rgba(245,166,35,0.85)";
  ctx.lineWidth = 2;
  ctx.setLineDash([5, 4]);
  for (const r of regions) {
    for (const tt of [r.startS, r.endS]) {
      const x = (tt / dur) * W;
      ctx.beginPath(); ctx.moveTo(x, 6); ctx.lineTo(x, H - 6); ctx.stroke();
    }
  }
  ctx.setLineDash([]);
  // floor line
  const floorAmp = Math.pow(10, state.knobs.speechFloorDb / 20);
  const fy1 = mid - floorAmp * (H * 0.92) / 2, fy2 = mid + floorAmp * (H * 0.92) / 2;
  ctx.strokeStyle = "rgba(255,93,93,0.45)"; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
  ctx.beginPath(); ctx.moveTo(0, fy1); ctx.lineTo(W, fy1); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, fy2); ctx.lineTo(W, fy2); ctx.stroke();
  ctx.setLineDash([]);
}

// ---- compact engine popover (model-by-family, backend chips, Canary langs) ----
function buildEnginePopover() {
  const e = state.engine;
  const pop = el(`<div class="popover" id="enginePopover"><div class="popover__inner"></div></div>`);
  const inner = pop.querySelector(".popover__inner");

  // backend chips (cuda disabled)
  const beChips = APP.backends.map((b) =>
    `<button class="chip ${b.kind === e.backend ? "is-sel" : ""}" data-backend="${b.kind}" ${b.available ? "" : "disabled"}>${esc(b.label)}${b.available ? "" : '<span class="chip__x">n/a</span>'}</button>`
  ).join("");
  inner.appendChild(el(`<div class="eng-row"><span class="eng-cap">Backend</span><div class="chips">${beChips}</div></div>`));

  // model by family
  const fam = el(`<div class="eng-row"><span class="eng-cap">Model · by family</span><div class="famgrid"></div></div>`);
  const fg = fam.querySelector(".famgrid");
  for (const f of MODELS) {
    const block = el(`<div class="fam"><div class="fam__head">${esc(f.family)}</div><div class="fam__models"></div></div>`);
    const fm = block.querySelector(".fam__models");
    for (const m of f.models) {
      const seld = e.family === f.family && e.model === m.id;
      fm.appendChild(el(`
        <button class="model ${seld ? "is-sel" : ""}" data-family="${esc(f.family)}" data-model="${esc(m.id)}">
          <span class="model__l"><span class="model__name">${esc(m.display)}</span><span class="model__desc">${esc(m.desc)}</span></span>
          <span class="model__dot"></span>
        </button>`));
    }
    fg.appendChild(block);
  }
  inner.appendChild(fam);

  // Canary source/target selects (only when canary selected)
  if (e.family === "canary") {
    const langOpts = (selCode) => ["nb", "da", "en", "sv", "de", "fr"].map((c) =>
      `<option value="${c}" ${c === selCode ? "selected" : ""}>${LANGS[c].flag} ${LANGS[c].name}</option>`).join("");
    inner.appendChild(el(`
      <div class="eng-row">
        <span class="eng-cap">Canary translation</span>
        <div class="selrow">
          <div class="selfield"><label>Source</label><select id="srcLang">${langOpts(e.sourceLang)}</select></div>
          <div style="align-self:flex-end;padding-bottom:9px;color:var(--ink-4)">→</div>
          <div class="selfield"><label>Target</label><select id="tgtLang">${langOpts(e.targetLang)}</select></div>
        </div>
        <div class="translate-note">🌐 <b id="trSrc">${LANGS[e.sourceLang].name}</b> → <b id="trTgt">${LANGS[e.targetLang].name}</b> during transcription.</div>
      </div>`));
  }
  return pop;
}

function wireEnginePopover() {
  const pop = document.getElementById("enginePopover");
  if (!pop) return;
  pop.querySelectorAll("[data-backend]").forEach((b) => {
    if (b.disabled) return;
    b.addEventListener("click", () => { state.engine.backend = b.dataset.backend; rerenderPopover(); });
  });
  pop.querySelectorAll("[data-model]").forEach((m) => {
    m.addEventListener("click", () => {
      state.engine.family = m.dataset.family;
      state.engine.model = m.dataset.model;
      rerenderPopover();
      const lbl = document.querySelector("#enginePop .act__val");
      if (lbl) lbl.textContent = state.engine.model;
    });
  });
  const src = document.getElementById("srcLang"), tgt = document.getElementById("tgtLang");
  if (src) src.addEventListener("change", () => { state.engine.sourceLang = src.value; document.getElementById("trSrc").textContent = LANGS[src.value].name; });
  if (tgt) tgt.addEventListener("change", () => { state.engine.targetLang = tgt.value; document.getElementById("trTgt").textContent = LANGS[tgt.value].name; });
}

function rerenderPopover() {
  const old = document.getElementById("enginePopover");
  if (!old) return;
  const fresh = buildEnginePopover();
  fresh.classList.toggle("is-open", state.enginePopover);
  old.replaceWith(fresh);
  wireEnginePopover();
}

// =============================================================================
// STAGE 3 — PEOPLE  (cross-session per-mic profiles + dual language + switch)
// =============================================================================
function viewPeople() {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 3 · People",
    title: "People",
    sub: `${SPEAKERS.length} profiles · settings keyed by <b>microphone</b>, reused across every session`,
  });

  const grid = el(`<div class="people"></div>`);
  for (const sp of SPEAKERS) {
    const card = el(`
      <div class="person ${sp.isRoom ? "is-room" : ""}">
        <div class="person__head">
          <span class="av spk-${sp.spk}">${esc(sp.initials)}</span>
          <span class="person__id">
            <span class="person__name spk-ink-${sp.spk}">${esc(sp.name)} ${sp.isRoom ? '<span class="tag warn">room</span>' : ""}</span>
            <span class="person__note">${esc(sp.note)}</span>
          </span>
          <span class="person__seen"><b>${sp.sessionsSeen}</b>sessions</span>
        </div>
        <div class="person__body"></div>
      </div>`);
    const body = card.querySelector(".person__body");

    // mic line (the profile key) + reused badge
    body.appendChild(el(`
      <div class="micline">
        <span class="ic">🎚️</span>
        <span><span class="micline__label">${esc(sp.mic.label)}</span> <span class="dim">· ${esc(sp.mic.id)}</span></span>
        <span class="micline__reuse">profile reused</span>
      </div>`));

    // gate + noise floor (the per-mic profile values)
    body.appendChild(el(`
      <div class="profgrid">
        <div class="profcell"><div class="profcell__k">Gate threshold</div><div class="profcell__v">${sp.gateThreshold.toFixed(2)}</div></div>
        <div class="profcell"><div class="profcell__k">Noise floor</div><div class="profcell__v">${sp.noiseFloorDb} dB</div></div>
      </div>`));

    // language block: primary + secondary + quick "transcribe as" switch
    const langBlk = el(`<div class="langblk"><div class="langblk__cap">Language</div></div>`);
    const secondary = sp.secondaryLang
      ? `<span class="langpair__arrow">·</span><span class="langpill"><span class="langpill__role">2nd</span><span class="flag">${LANGS[sp.secondaryLang].flag}</span>${LANGS[sp.secondaryLang].name}</span>`
      : `<span class="langpair__arrow">·</span><span class="muted" style="font-size:10.5px">no secondary</span>`;
    langBlk.appendChild(el(`
      <div class="langpair">
        <span class="langpill primary"><span class="langpill__role">1st</span><span class="flag">${LANGS[sp.primaryLang].flag}</span>${LANGS[sp.primaryLang].name}</span>
        ${secondary}
      </div>`));

    // quick switch: transcribe this speaker as <lang>
    const opts = [sp.primaryLang, sp.secondaryLang, "en"].filter((v, i, a) => v && a.indexOf(v) === i);
    const qs = el(`<div class="quickswitch"><span class="quickswitch__lab">transcribe as →</span></div>`);
    for (const code of opts) {
      const btn = el(`<button class="qbtn ${state.transcribeAs[sp.id] === code ? "is-active" : ""}" data-sp="${sp.id}" data-lang="${code}"><span class="flag">${LANGS[code].flag}</span>${LANGS[code].name}</button>`);
      btn.addEventListener("click", () => {
        state.transcribeAs[sp.id] = code;
        qs.querySelectorAll(".qbtn").forEach((b) => b.classList.toggle("is-active", b.dataset.lang === code));
      });
      qs.appendChild(btn);
    }
    langBlk.appendChild(qs);
    body.appendChild(langBlk);

    // diarization shown as a property of the room profile (mirrors capture)
    if (sp.isRoom && sp.diarizedInto) {
      const dm = el(`<div class="diar-mini"><div class="diar-mini__cap">↳ diarizes into ${sp.diarizedInto.length} voices</div></div>`);
      for (const d of sp.diarizedInto) {
        dm.appendChild(el(`
          <div class="diar-mini__row">
            <span class="av sm spk-${d.spk}">${d.spk}</span>
            <span>${esc(d.label)}</span>
            <span class="tag"><span class="flag">${LANGS[d.lang].flag}</span>${esc(LANGS[d.lang].name)}</span>
            <span class="mono dim" style="font-size:10px">${d.talkPct}%</span>
          </div>`));
      }
      body.appendChild(dm);
    }
    grid.appendChild(card);
  }
  wrap.appendChild(grid);
  return wrap;
}

// =============================================================================
// boot + screenshot hooks
// =============================================================================
render();

// deterministic hooks for the screenshotter
window.gotoView = (name) => {
  const map = { capture: "capture", transcript: "transcript", people: "people" };
  if (map[name]) { state.isFresh = false; goStage(map[name]); }
};
window.stagesGo = window.gotoView;
window.stagesPickSession = (id) => { if (SESSIONS.some((s) => s.id === id)) { state.isFresh = false; state.sessionId = id; render(); } };
window.stagesNewSession = () => { document.getElementById("newSession").click(); };
window.stagesSelectClip = (i) => { state.stage = "transcript"; state.selectedClip = i; render(); };
window.stagesSetKnob = (key, val) => { if (key in state.knobs) { state.knobs[key] = val; render(); } };
