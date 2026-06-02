// =============================================================================
// TapScribe — Glance → Focus
// A calm status HOME of five glanceable digest cards. Selecting one ZOOMS it
// into a single dense focused workspace; the rest recede to a status rail.
// Strictly one focus at a time + an always-present way back to the glance home.
// =============================================================================

import {
  MOCK,
  LANGS,
  SPEAKERS,
  MODELS,
  selectedModel,
  LIVE_TAPS,
  LIVE_CAPTIONS,
  SESSIONS,
  STRIP_DEFAULTS,
  REP_WAV,
  TRANSCRIPT,
  computeRegions,
  helpers,
  speakerById,
} from "../_shared/mock-data.js";

const { clock, pct } = helpers;
const app = document.getElementById("app");
const stage = document.getElementById("stage");
const backBtn = document.getElementById("backBtn");

// ---- tiny DOM helpers -------------------------------------------------------
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) =>
  String(s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );
const flagOf = (code) => (LANGS[code] || LANGS.auto).flag;
const langName = (code) => (LANGS[code] || LANGS.auto).name;

// per-speaker palette helpers (matches CSS --spk0..4)
const SPK_HEX = ["#f5a524", "#38bdf8", "#a78bfa", "#34d399", "#fb7185"];
const meterColor = (spk) => SPK_HEX[spk] ?? "#7787a0";

// ---- the five concerns (single source of truth) ----------------------------
const CONCERNS = [
  { id: "live", icon: "🎙", title: "Live" },
  { id: "sessions", icon: "🗂", title: "Sessions" },
  { id: "clips", icon: "✂️", title: "Tuning" },
  { id: "speakers", icon: "👥", title: "Speakers" },
  { id: "engine", icon: "⚙️", title: "Engine" },
];

// ---- live derived digests ---------------------------------------------------
const liveTaps = LIVE_TAPS.filter((t) => t.live);
const speakingTaps = liveTaps.filter((t) => t.gateOpen);
const recPaused = LIVE_TAPS.filter((t) => t.live && !t.record).length;
const roomTap = LIVE_TAPS.find((t) => speakerById(t.identity)?.isRoom);
const currentSession = SESSIONS.find((s) => s.current);
const noTxCount = SESSIONS.filter((s) => !s.hasTranscript).length;
const suppressedCount = TRANSCRIPT.lines.filter((l) => l.suppressed).length;
const lowConfCount = TRANSCRIPT.lines.filter((l) => l.lowConfidence).length;
const disabledBackends = MOCK.APP.backends.filter((b) => !b.available);

// strip-silence live state (mutated by the Tuning knobs)
const stripState = { ...STRIP_DEFAULTS };

// ---- sparkline / canvas drawing --------------------------------------------
function drawSpark(canvas, levels, color) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 220;
  const h = canvas.clientHeight || 30;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const n = levels.length;
  const step = w / (n - 1);
  // area fill
  ctx.beginPath();
  ctx.moveTo(0, h);
  levels.forEach((v, i) => ctx.lineTo(i * step, h - v * (h - 3) - 1.5));
  ctx.lineTo(w, h);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "55");
  grad.addColorStop(1, color + "00");
  ctx.fillStyle = grad;
  ctx.fill();
  // stroke
  ctx.beginPath();
  levels.forEach((v, i) => {
    const x = i * step;
    const y = h - v * (h - 3) - 1.5;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.lineJoin = "round";
  ctx.stroke();
}

// waveform + strip-silence cut regions, re-cut live
function drawWaveform(canvas, marquee) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 800;
  const h = canvas.clientHeight || 200;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const { peaks, durationS } = REP_WAV;
  const { regions } = computeRegions(peaks, durationS, stripState);
  const mid = h / 2;
  const pxPerS = w / durationS;

  // dropped (silence) base layer
  ctx.fillStyle = "#0b0f15";
  ctx.fillRect(0, 0, w, h);

  // shade kept (speech) regions
  regions.forEach((r) => {
    const x0 = r.startS * pxPerS;
    const x1 = r.endS * pxPerS;
    ctx.fillStyle = "#f5a5240e";
    ctx.fillRect(x0, 0, x1 - x0, h);
  });

  // helper: is sample index inside a kept region?
  const inRegion = (t) => regions.some((r) => t >= r.startS && t <= r.endS);

  // draw peaks: kept = amber, dropped = dim slate
  const n = peaks.length;
  const step = w / n;
  for (let i = 0; i < n; i++) {
    const t = (i / n) * durationS;
    const v = peaks[i];
    const ph = Math.max(0.6, v * (mid - 6));
    ctx.fillStyle = inRegion(t) ? "#f5a524" : "#33415540";
    const x = i * step;
    ctx.fillRect(x, mid - ph, Math.max(0.7, step * 0.8), ph * 2);
  }

  // cut boundary lines + region badges
  ctx.font = "10px ui-monospace, monospace";
  regions.forEach((r, idx) => {
    const x0 = r.startS * pxPerS;
    const x1 = r.endS * pxPerS;
    [x0, x1].forEach((x) => {
      ctx.strokeStyle = "#34d39988";
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    });
    ctx.setLineDash([]);
    // clip number tab
    ctx.fillStyle = "#34d399";
    ctx.fillRect(x0, 0, Math.min(22, x1 - x0), 14);
    ctx.fillStyle = "#0b0f15";
    ctx.fillText(`#${idx + 1}`, x0 + 3, 10);
  });

  // optional marquee selection (a visual "drag to re-cut" affordance)
  if (marquee) {
    const x0 = marquee.startS * pxPerS;
    const x1 = marquee.endS * pxPerS;
    ctx.fillStyle = "#60a5fa22";
    ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.strokeStyle = "#60a5fa";
    ctx.setLineDash([5, 3]);
    ctx.strokeRect(x0, 1, x1 - x0, h - 2);
    ctx.setLineDash([]);
  }

  // center baseline
  ctx.strokeStyle = "#1f2937";
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(w, mid);
  ctx.stroke();
}

// =============================================================================
// GLANCE HOME — five sparse digest cards
// =============================================================================
function renderHome() {
  const root = el("div", "glance-home zoom-in");

  // attention summary line
  const attnBits = [];
  if (recPaused) attnBits.push(`${recPaused} tap rec-paused`);
  if (noTxCount) attnBits.push(`${noTxCount} session needs transcript`);
  if (suppressedCount) attnBits.push(`${suppressedCount} suppressed line`);
  const lead = el(
    "div",
    "home-lead",
    `<h1>Good morning, Atle</h1>
     <span class="lead-sub">5 things to keep an eye on</span>
     <span class="attn">⚠ ${attnBits.join(" · ")}</span>`,
  );
  root.appendChild(lead);

  const grid = el("div", "card-grid");

  // --- LIVE card (tall, left) ---
  grid.appendChild(liveCard());
  // --- SESSIONS ---
  grid.appendChild(sessionsCard());
  // --- TUNING / clips ---
  grid.appendChild(clipsCard());
  // --- SPEAKERS ---
  grid.appendChild(speakersCard());
  // --- ENGINE ---
  grid.appendChild(engineCard());

  root.appendChild(grid);
  return root;
}

function cardShell(concern, attn) {
  const c = el("button", "card" + (attn ? " is-attn" : ""));
  c.type = "button";
  c.dataset.shot = concern.id;
  c.setAttribute("aria-label", `Open ${concern.title}`);
  c.addEventListener("click", () => focus(concern.id));
  const head = el("div", "card-head");
  head.appendChild(el("span", "card-ico", concern.icon));
  head.appendChild(el("span", "card-title", concern.title));
  if (attn) {
    head.appendChild(el("span", "card-attn-dot", `<span class="d"></span>${attn}`));
  } else {
    head.appendChild(el("span", "card-open", "›"));
  }
  c.appendChild(head);
  return c;
}

function liveCard() {
  const c = cardShell(CONCERNS[0], `${speakingTaps.length} speaking`);
  c.classList.add("live-card");

  const figs = el("div", "digest-figs");
  figs.innerHTML = `
    <div class="fig"><span class="n amber">${liveTaps.length}</span><span class="l">live taps</span></div>
    <div class="fig"><span class="n good">${speakingTaps.length}</span><span class="l">gate open</span></div>
    <div class="fig"><span class="n sm">${liveTaps.reduce((a, t) => a + (t.lagS || 0), 0).toFixed(1)}s</span><span class="l">total lag</span></div>`;
  c.appendChild(figs);

  // mini tap meters (digest, not full controls)
  const mini = el("div", "mini-taps");
  liveTaps.forEach((t) => {
    const row = el("div", "mini-tap");
    const isRoom = speakerById(t.identity)?.isRoom;
    row.innerHTML = `
      <span class="av sw${t.spk}">${speakerById(t.identity)?.initials || "?"}</span>
      <span class="nm">${esc(t.name.replace("Oslo Conference Room", "Oslo Room"))}</span>
      ${isRoom ? '<span class="tap-badge diar">diarized</span>' : ""}
      <span class="meter"><i style="width:${Math.round(t.level * 100)}%;background:${meterColor(t.spk)}"></i></span>
      <span class="gate ${t.gateOpen ? "open" : "shut"}">${t.gateOpen ? "OPEN" : "—"}</span>`;
    mini.appendChild(row);
  });
  c.appendChild(mini);

  // sparkline of the loudest live tap (fills the tall column) + caption preview
  const sparkWrap = el("div", "live-spark");
  sparkWrap.innerHTML = `<span class="l" style="font-size:11px;color:var(--ink-3)">${esc(liveTaps[0].name.split(" ")[0])} level · last 12s</span>`;
  const spark = el("canvas", "spark");
  spark.style.height = "54px";
  sparkWrap.appendChild(spark);
  c.appendChild(sparkWrap);
  const lastCap = LIVE_CAPTIONS[LIVE_CAPTIONS.length - 1];
  c.appendChild(
    el(
      "div",
      "digest-foot",
      `<span class="dot live"></span> latest · <span class="lang-tag">${flagOf(lastCap.lang)} ${esc(lastCap.text.slice(0, 40))}…</span>`,
    ),
  );
  // draw after attach
  requestAnimationFrame(() => drawSpark(spark, liveTaps[0].levels, SPK_HEX[0]));
  return c;
}

function sessionsCard() {
  const c = cardShell(CONCERNS[1], noTxCount ? `${noTxCount} no tx` : null);
  const figs = el("div", "digest-figs");
  const totalWav = SESSIONS.reduce((a, s) => a + s.wavCount, 0);
  figs.innerHTML = `
    <div class="fig"><span class="n">${SESSIONS.length}</span><span class="l">sessions</span></div>
    <div class="fig"><span class="n sm">${totalWav}</span><span class="l">recordings</span></div>`;
  c.appendChild(figs);
  // a couple of recent sessions as one-liners (digest, not the full list)
  const recent = el("div", "mini-taps");
  SESSIONS.slice(0, 3).forEach((s) => {
    recent.appendChild(
      el(
        "div",
        "mini-tap",
        `<span class="dot ${s.current ? "live" : s.hasTranscript ? "idle" : "warn"}"></span>
         <span class="nm">${esc(s.label || "Untitled")}</span>
         <span class="wav-clips" style="margin-left:auto">${clock(s.durationS)}</span>`,
      ),
    );
  });
  c.appendChild(recent);
  c.appendChild(
    el(
      "div",
      "digest-foot",
      `<span class="dot ${noTxCount ? "warn" : "live"}"></span> ${noTxCount ? noTxCount + " awaiting transcript" : "all transcribed"}`,
    ),
  );
  return c;
}

function clipsCard() {
  const c = cardShell(CONCERNS[2], null);
  const { clips, speechS, totalS, regions } = computeRegions(REP_WAV.peaks, REP_WAV.durationS, STRIP_DEFAULTS);
  const figs = el("div", "digest-figs");
  figs.innerHTML = `
    <div class="fig"><span class="n">${clips}</span><span class="l">clips cut</span></div>
    <div class="fig"><span class="n sm good">${pct((speechS / totalS) * 100)}</span><span class="l">speech kept</span></div>`;
  c.appendChild(figs);
  // mini cut-preview strip (a thumbnail of the focus view's waveform)
  const strip = el("canvas", "spark");
  strip.style.height = "26px";
  c.appendChild(strip);
  requestAnimationFrame(() => drawMiniCuts(strip, regions));
  c.appendChild(
    el(
      "div",
      "digest-foot",
      `<span class="dot live"></span> gap ${STRIP_DEFAULTS.minSilenceMs}ms · pad ${STRIP_DEFAULTS.padMs}ms · floor ${STRIP_DEFAULTS.speechFloorDb}dB`,
    ),
  );
  return c;
}

// tiny region strip for the Tuning digest card
function drawMiniCuts(canvas, regions) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 220;
  const h = canvas.clientHeight || 26;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const dur = REP_WAV.durationS;
  ctx.fillStyle = "#1f2937";
  ctx.fillRect(0, h / 2 - 1, w, 2);
  regions.forEach((r, i) => {
    const x0 = (r.startS / dur) * w;
    const x1 = (r.endS / dur) * w;
    ctx.fillStyle = "#f5a524";
    ctx.fillRect(x0, h / 2 - 6, x1 - x0, 12);
    ctx.fillStyle = "#0e1218";
    ctx.font = "8px ui-monospace, monospace";
    ctx.fillText(`${i + 1}`, x0 + 2, h / 2 + 3);
  });
}

function speakersCard() {
  const c = cardShell(CONCERNS[3], null);
  const figs = el("div", "digest-figs");
  const rooms = SPEAKERS.filter((s) => s.isRoom).length;
  figs.innerHTML = `
    <div class="fig"><span class="n">${SPEAKERS.length}</span><span class="l">profiles</span></div>
    <div class="fig"><span class="n sm">${rooms}</span><span class="l">room · diarized</span></div>`;
  c.appendChild(figs);
  // mic + language digest chips
  c.appendChild(
    el(
      "div",
      "chip-row",
      SPEAKERS.map(
        (s) =>
          `<span class="chip"><span class="dot" style="display:inline-block;width:7px;height:7px;background:${meterColor(s.spk)}"></span>${esc(s.mic.label)} · ${flagOf(s.primaryLang)}${s.secondaryLang ? "/" + flagOf(s.secondaryLang) : ""}</span>`,
      ).join(""),
    ),
  );
  const foot = el("div", "digest-foot");
  const avs = el("div", "avatar-stack");
  SPEAKERS.forEach((s) => avs.appendChild(el("span", `av sw${s.spk}`, s.initials)));
  foot.appendChild(avs);
  foot.appendChild(el("span", "", "per-mic profiles · reused across sessions"));
  c.appendChild(foot);
  return c;
}

function engineCard() {
  const c = cardShell(CONCERNS[4], disabledBackends.length ? "cuda off" : null);
  const figs = el("div", "digest-figs");
  figs.innerHTML = `
    <div class="fig"><span class="n sm amber">${selectedModel.family}</span><span class="l">family</span></div>
    <div class="fig"><span class="n sm">${MOCK.APP.backend}</span><span class="l">backend</span></div>`;
  c.appendChild(figs);
  c.appendChild(
    el(
      "div",
      "chip-row",
      `<span class="chip good">${esc(selectedModel.model)}</span>
       <span class="chip">${flagOf(selectedModel.sourceLang)}→${flagOf(selectedModel.targetLang)} translate</span>
       ${disabledBackends.map((b) => `<span class="chip off">${b.label}</span>`).join("")}`,
    ),
  );
  return c;
}

// =============================================================================
// FOCUS MODE — [ rail | workspace ]
// =============================================================================
function renderFocus(id) {
  const concern = CONCERNS.find((c) => c.id === id);
  const wrap = el("div", "focus-wrap zoom-in");

  // --- status rail (the receded glance) ---
  const rail = el("div", "rail");
  const home = el("button", "rail-btn rail-home");
  home.type = "button";
  home.title = "Back to Glance";
  home.innerHTML = "◈";
  home.addEventListener("click", goHome);
  rail.appendChild(home);
  rail.appendChild(el("div", "rail-div", ""));
  CONCERNS.forEach((cn) => {
    const b = el("button", "rail-btn" + (cn.id === id ? " active" : ""));
    b.type = "button";
    b.title = cn.title;
    b.dataset.rail = cn.id;
    b.innerHTML = cn.icon;
    // peripheral status pip
    if (cn.id === "live") b.innerHTML += '<span class="pip live"></span>';
    if (cn.id === "sessions" && noTxCount) b.innerHTML += '<span class="pip attn"></span>';
    if (cn.id === "engine" && disabledBackends.length)
      b.innerHTML += '<span class="pip attn"></span>';
    b.addEventListener("click", () => focus(cn.id));
    rail.appendChild(b);
  });
  wrap.appendChild(rail);

  // --- workspace ---
  const ws = el("div", "workspace");
  const head = el("div", "ws-head");
  head.innerHTML = `<span class="ws-ico">${concern.icon}</span>
    <div><div class="ws-title">${concern.title}</div><div class="ws-sub" id="wsSub"></div></div>
    <div class="ws-head-right" id="wsHeadRight"></div>`;
  ws.appendChild(head);
  const body = el("div", "ws-body");
  body.id = "wsBody";
  ws.appendChild(body);
  wrap.appendChild(ws);

  // fill workspace per concern
  ({
    live: fillLive,
    sessions: fillSessions,
    clips: fillClips,
    speakers: fillSpeakers,
    engine: fillEngine,
  })[id](body, head);

  return wrap;
}

// ---- LIVE workspace ---------------------------------------------------------
function fillLive(body, head) {
  head.querySelector("#wsSub").textContent = `${liveTaps.length} active taps · session “${currentSession.label}”`;
  head.querySelector("#wsHeadRight").innerHTML =
    `<span class="rec-pill" style="position:static"><span class="rec-dot"></span> recording</span>`;
  body.classList.add("no-scroll");

  const cols = el("div", "cols c-2 fill-h");
  const leftCol = el("div", "col-stack");

  // -- left: taps with full controls (level/lag/gate/rec-live + diarization) --
  const tapsBlock = el("div", "block");
  tapsBlock.appendChild(
    blockHead("Taps", "level · lag · gate · rec / live", `<span class="lang-tag">${liveTaps.length} streaming</span>`),
  );
  const tapsBody = el("div", "block-body flush");
  LIVE_TAPS.forEach((t) => {
    const sp = speakerById(t.identity);
    const row = el("div", "tap-row" + (t.live ? "" : " muted"));
    const isRoom = sp?.isRoom;
    row.innerHTML = `
      <span class="tap-av sw${t.spk}">${sp?.initials || "?"}</span>
      <div class="tap-main">
        <div class="tap-name">
          ${esc(t.name.replace("Oslo Conference Room", "Oslo Room"))}
          ${isRoom ? '<span class="tap-badge diar">⑂ diarized → A / B</span>' : ""}
          <span class="lang-tag">${flagOf(t.lang)} ${langName(t.lang)}</span>
        </div>
        <div class="tap-meta">
          <span class="bar"><i style="width:${Math.round(t.level * 100)}%;background:${meterColor(t.spk)}"></i></span>
          <span>lvl <b>${t.level.toFixed(2)}</b></span>
          <span>lag <b>${t.lagS.toFixed(1)}s</b></span>
          <span class="gate ${t.gateOpen ? "open" : "shut"}">${t.gateOpen ? "GATE OPEN" : "GATE SHUT"}</span>
        </div>
      </div>
      <div class="tap-ctrls">
        <button class="toggle rec ${t.record ? "on" : ""}" type="button">REC</button>
        <button class="toggle live ${t.live ? "on" : ""}" type="button">LIVE</button>
      </div>`;
    // wire toggles (visual only)
    const [recBtn, liveBtn] = row.querySelectorAll(".toggle");
    recBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      recBtn.classList.toggle("on");
    });
    liveBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      liveBtn.classList.toggle("on");
    });
    tapsBody.appendChild(row);

    // diarization fan-out: a PROPERTY of the room tap, inline
    if (isRoom && sp.diarizedInto) {
      const fan = el("div", "diar-fan");
      sp.diarizedInto.forEach((d) => {
        fan.appendChild(
          el(
            "div",
            "diar-child",
            `<span class="av sw${d.spk}">${d.label.split(" ")[1]}</span>
             <span><b>${d.label}</b> · ${flagOf(d.lang)} ${langName(d.lang)}</span>
             <span class="talk">${d.talkPct}% talk</span>`,
          ),
        );
      });
      const wrapRow = el("div", "tap-row");
      wrapRow.style.paddingTop = "0";
      wrapRow.appendChild(el("span", "", ""));
      wrapRow.appendChild(fan);
      tapsBody.appendChild(wrapRow);
    }
  });
  tapsBlock.appendChild(tapsBody);
  leftCol.appendChild(tapsBlock);

  // -- left/secondary: live language mix + gate activity (fills column) --
  const mixBlock = el("div", "block grow");
  mixBlock.appendChild(blockHead("This session, live", "language mix · gate activity", ""));
  const mixBody = el("div", "block-body");
  // language mix bar from captions so far
  const byLang = {};
  LIVE_CAPTIONS.forEach((c) => (byLang[c.lang] = (byLang[c.lang] || 0) + 1));
  const totalCaps = LIVE_CAPTIONS.length;
  mixBody.appendChild(el("div", "fig", `<span class="l" style="margin-bottom:6px">languages transcribed</span>`));
  const langBar = el("div", "speak-bar");
  const langColor = { nb: "#f5a524", da: "#38bdf8", en: "#34d399" };
  Object.entries(byLang).forEach(([code, n]) => {
    langBar.innerHTML += `<span title="${langName(code)} ${n}" style="width:${(n / totalCaps) * 100}%;background:${langColor[code] || "#7787a0"}"></span>`;
  });
  mixBody.appendChild(langBar);
  const leg = el("div", "speak-legend");
  Object.entries(byLang).forEach(([code, n]) => {
    leg.innerHTML += `<span class="it"><span class="sq" style="background:${langColor[code] || "#7787a0"}"></span>${flagOf(code)} ${langName(code)} · ${n}</span>`;
  });
  mixBody.appendChild(leg);
  // small live stats row
  mixBody.appendChild(
    el(
      "div",
      "cut-stats",
      `<div class="fig"><span class="n sm good">${speakingTaps.length}/${liveTaps.length}</span><span class="l">gates open</span></div>
       <div class="fig"><span class="n sm amber">${recPaused}</span><span class="l">rec paused</span></div>
       <div class="fig"><span class="n sm">${roomTap ? "A+B" : "—"}</span><span class="l">room voices</span></div>`,
    ),
  );
  mixBody.querySelector(".cut-stats").style.marginTop = "14px";
  mixBlock.appendChild(mixBody);
  leftCol.appendChild(mixBlock);
  cols.appendChild(leftCol);

  // -- right: live captions (line-oriented, tagged by speaker + language) --
  const capBlock = el("div", "block");
  capBlock.appendChild(blockHead("Live captions", "timestamp · speaker · text", `<span class="dot live"></span>`));
  const capBody = el("div", "block-body flush cap-list");
  LIVE_CAPTIONS.forEach((c) => {
    const line = el("div", "cap-line" + (c.inflight ? " inflight" : ""));
    line.innerHTML = `
      <span class="cap-t">${clock(c.t)}</span>
      <span class="cap-spk ink${c.spk}"><span class="flag">${flagOf(c.lang)}</span>${esc(c.speaker.replace("Oslo Conference Room", "Oslo Room"))}</span>
      <span class="cap-text">${esc(c.text)}</span>`;
    capBody.appendChild(line);
  });
  capBlock.appendChild(capBody);
  cols.appendChild(capBlock);

  body.appendChild(cols);
}

// ---- SESSIONS workspace -----------------------------------------------------
let activeSession = currentSession.id;
function fillSessions(body, head) {
  head.querySelector("#wsSub").textContent = `${SESSIONS.length} sessions · ${SESSIONS.reduce((a, s) => a + s.wavCount, 0)} recordings`;

  const cols = el("div", "cols c-list-detail");

  // list
  const listBlock = el("div", "block");
  listBlock.appendChild(blockHead("All sessions", "", ""));
  const list = el("div", "block-body flush");
  SESSIONS.forEach((s) => {
    const b = el("button", "sess-item" + (s.id === activeSession ? " active" : ""));
    b.type = "button";
    b.dataset.sess = s.id;
    const avs = s.speakers
      .map((id) => `<span class="av sw${speakerById(id).spk}">${speakerById(id).initials}</span>`)
      .join("");
    b.innerHTML = `
      <div class="sess-top">
        <span class="sess-label">${esc(s.label || "Untitled session")}</span>
        ${s.current ? '<span class="sess-cur">LIVE</span>' : ""}
      </div>
      <div class="sess-meta"><span>${clock(s.durationS)}</span><span>${s.wavCount} wav</span><span>${s.langs.map(flagOf).join("")}</span></div>
      <div class="sess-flags">
        <span class="avatar-stack">${avs}</span>
        ${s.hasTranscript ? '<span class="chip good" style="font-size:10px">transcript</span>' : '<span class="chip amber" style="font-size:10px">no tx</span>'}
      </div>`;
    b.addEventListener("click", () => {
      activeSession = s.id;
      list.querySelectorAll(".sess-item").forEach((x) => x.classList.toggle("active", x.dataset.sess === s.id));
      renderSessionDetail(detailWrap, s);
    });
    list.appendChild(b);
  });
  listBlock.appendChild(list);
  cols.appendChild(listBlock);

  // detail
  const detailWrap = el("div", "");
  cols.appendChild(detailWrap);
  renderSessionDetail(detailWrap, SESSIONS.find((s) => s.id === activeSession));

  body.appendChild(cols);
}

function renderSessionDetail(wrap, s) {
  wrap.innerHTML = "";

  // summary + speaking-time
  const sum = el("div", "block");
  sum.appendChild(
    blockHead(
      esc(s.label || "Untitled session"),
      `${s.folder}`,
      s.current ? '<span class="sess-cur">LIVE</span>' : `<span class="lang-tag">${clock(s.durationS)}</span>`,
    ),
  );
  const sumBody = el("div", "block-body");
  if (s.hasTranscript) {
    const bar = el("div", "speak-bar");
    TRANSCRIPT.speakingTime.forEach((st) => {
      bar.innerHTML += `<span title="${esc(st.speaker)} ${st.pct}%" style="width:${st.pct}%;background:${meterColor(st.spk)}"></span>`;
    });
    sumBody.appendChild(el("div", "fig", `<span class="l" style="margin-bottom:6px">speaking time</span>`));
    sumBody.appendChild(bar);
    const leg = el("div", "speak-legend");
    TRANSCRIPT.speakingTime.forEach((st) => {
      leg.innerHTML += `<span class="it"><span class="sq" style="background:${meterColor(st.spk)}"></span>${esc(st.speaker.replace("Oslo Room · ", ""))} ${st.pct}%</span>`;
    });
    sumBody.appendChild(leg);
  } else {
    sumBody.innerHTML = `<div class="muted-note">No merged transcript yet — ${s.wavCount} recordings captured. Run the engine to transcribe.</div>`;
  }
  sum.appendChild(sumBody);
  wrap.appendChild(sum);

  if (!s.hasTranscript) {
    // still show the per-WAV listing so clips are reachable
    wrap.appendChild(wavListBlock(s));
    return;
  }

  // dense merged transcript
  const txBlock = el("div", "block");
  txBlock.appendChild(
    blockHead(
      "Merged transcript",
      `${TRANSCRIPT.model} · ${TRANSCRIPT.backend}`,
      `<span class="tx-tag tr">${lowConfCount} low-conf</span><span class="tx-tag sup">${suppressedCount} suppressed</span>`,
    ),
  );
  const txBody = el("div", "block-body flush tx");
  TRANSCRIPT.lines.forEach((l) => {
    let cls = "tx-line";
    if (l.lowConfidence) cls += " low";
    if (l.suppressed) cls += " sup";
    const tags = [];
    if (l.lowConfidence) tags.push(`<span class="tx-tag low">⚠ ${Math.round((l.confidence || 0) * 100)}%</span>`);
    if (l.translatedFrom)
      tags.push(
        `<span class="tx-tag tr">↳ ${l.translatedFrom}→${selectedModel.targetLang} translated</span>`,
      );
    if (l.suppressed) tags.push(`<span class="tx-tag sup">⊘ ${l.matchedRule}</span>`);
    const line = el("div", cls);
    line.innerHTML = `
      <span class="tx-t">${clock(l.t)}</span>
      <span class="tx-spk ink${l.spk}">${flagOf(l.lang)} ${esc(l.speaker.replace("Oslo Room · ", ""))}</span>
      <span class="tx-text">${esc(l.text)}${tags.join("")}</span>`;
    txBody.appendChild(line);
  });
  txBlock.appendChild(txBody);
  wrap.appendChild(txBlock);

  // per-WAV / clip listing
  wrap.appendChild(wavListBlock(s));
}

function wavListBlock(s) {
  const block = el("div", "block");
  block.appendChild(blockHead("Recordings", `${s.wavCount} WAV utterances · strip-silence clips`, ""));
  const body = el("div", "block-body flush");
  // synthesise a small representative listing from REP_WAV + a few siblings
  const base = REP_WAV.name.replace(/_atle_atle_[a-f0-9]+\.wav$/, "");
  const sample = [
    { name: REP_WAV.name, spk: 0, dur: 48, clips: computeRegions(REP_WAV.peaks, REP_WAV.durationS, STRIP_DEFAULTS).clips },
    { name: `${base}_mette_mette_5f6e7d8c.wav`, spk: 1, dur: 31, clips: 3 },
    { name: `${base}_room-oslo_A_9a8b7c6d.wav`, spk: 3, dur: 22, clips: 2 },
    { name: `${base}_james_james_1f2e3d4c.wav`, spk: 4, dur: 12, clips: 1 },
  ];
  sample.forEach((w) => {
    const row = el("div", "wav-row");
    row.innerHTML = `
      <span class="wav-name"><span class="dot" style="display:inline-block;background:${meterColor(w.spk)};width:7px;height:7px;margin-right:7px"></span>${esc(w.name)}</span>
      <span class="wav-clips">${clock(w.dur)}</span>
      <span class="wav-clips">${w.clips} clip${w.clips > 1 ? "s" : ""}</span>`;
    body.appendChild(row);
  });
  block.appendChild(body);
  // link to tuning
  const foot = el("div", "block-body");
  foot.style.borderTop = "1px solid var(--line-soft)";
  const goTune = el("button", "be-chip", "✂️ Open in Tuning to re-cut →");
  goTune.type = "button";
  goTune.addEventListener("click", () => focus("clips"));
  foot.appendChild(goTune);
  block.appendChild(foot);
  return block;
}

// ---- CLIPS / TUNING workspace (waveform + live re-cut) ----------------------
function fillClips(body, head) {
  head.querySelector("#wsSub").textContent = `${REP_WAV.name}`;
  head.querySelector("#wsHeadRight").innerHTML = `<span class="lang-tag">strip-silence · live re-cut</span>`;

  // waveform
  const waveBlock = el("div", "block");
  waveBlock.appendChild(
    blockHead("Waveform & cut points", "amber = kept speech · dashed = clip boundary", `<span id="cutBadge" class="chip good"></span>`),
  );
  const waveBody = el("div", "block-body");
  const ww = el("div", "wave-wrap");
  const canvas = el("canvas", "wave-canvas");
  canvas.id = "waveCanvas";
  ww.appendChild(canvas);
  const scale = el("div", "wave-scale");
  scale.innerHTML = `<span>0:00</span><span>0:12</span><span>0:24</span><span>0:36</span><span>0:48</span>`;
  ww.appendChild(scale);
  waveBody.appendChild(ww);
  waveBody.appendChild(
    el(
      "div",
      "muted-note",
      "Drag across the waveform to preview an audition region — the cut points (dashed) recompute live as you move the knobs below.",
    ),
  );
  waveBlock.appendChild(waveBody);
  body.appendChild(waveBlock);

  // knobs + live stats
  const cols = el("div", "cols c-2");
  cols.style.marginTop = "16px";

  const knobBlock = el("div", "block");
  knobBlock.appendChild(blockHead("Strip-silence knobs", "re-cuts live", ""));
  const knobBody = el("div", "block-body");
  const knobs = el("div", "knobs");
  knobs.appendChild(knobUI("Silence gap", "minSilenceMs", 100, 4000, 50, "ms", "Merge clips separated by less than this."));
  knobs.appendChild(knobUI("Edge pad", "padMs", 0, 600, 25, "ms", "Padding kept around each speech region."));
  knobs.appendChild(knobUI("Speech floor", "speechFloorDb", -60, -20, 1, "dB", "Below this is treated as silence."));
  knobBody.appendChild(knobs);
  knobBlock.appendChild(knobBody);
  cols.appendChild(knobBlock);

  const statBlock = el("div", "block");
  statBlock.appendChild(blockHead("Result", "from current knobs", ""));
  const statBody = el("div", "block-body");
  statBody.id = "cutStats";
  statBlock.appendChild(statBody);
  cols.appendChild(statBlock);

  body.appendChild(cols);

  // draw + wire
  let marquee = null;
  const redraw = () => {
    drawWaveform(canvas, marquee);
    const { clips, speechS, totalS } = computeRegions(REP_WAV.peaks, REP_WAV.durationS, stripState);
    document.getElementById("cutBadge").textContent = `${clips} clips`;
    document.getElementById("cutStats").innerHTML = `
      <div class="cut-stats">
        <div class="fig"><span class="n amber">${clips}</span><span class="l">clips</span></div>
        <div class="fig"><span class="n good">${speechS.toFixed(1)}s</span><span class="l">speech kept</span></div>
        <div class="fig"><span class="n sm">${(totalS - speechS).toFixed(1)}s</span><span class="l">silence dropped</span></div>
        <div class="fig"><span class="n sm">${pct((speechS / totalS) * 100)}</span><span class="l">kept</span></div>
      </div>`;
  };
  // marquee drag
  let dragStart = null;
  const sToX = (clientX) => {
    const r = canvas.getBoundingClientRect();
    return Math.min(REP_WAV.durationS, Math.max(0, ((clientX - r.left) / r.width) * REP_WAV.durationS));
  };
  canvas.addEventListener("mousedown", (e) => {
    dragStart = sToX(e.clientX);
    marquee = { startS: dragStart, endS: dragStart };
  });
  window.addEventListener("mousemove", (e) => {
    if (dragStart == null) return;
    const cur = sToX(e.clientX);
    marquee = { startS: Math.min(dragStart, cur), endS: Math.max(dragStart, cur) };
    redraw();
  });
  window.addEventListener("mouseup", () => {
    dragStart = null;
  });

  // expose redraw to knob handlers
  body._redraw = redraw;
  requestAnimationFrame(redraw);
}

function knobUI(label, key, min, max, step, unit, hint) {
  const k = el("div", "knob");
  const fmt = (v) => (unit === "dB" ? `${v} ${unit}` : `${v} ${unit}`);
  k.innerHTML = `
    <div class="knob-top"><span class="knob-label">${label}</span><span class="knob-val" data-val>${fmt(stripState[key])}</span></div>
    <input type="range" min="${min}" max="${max}" step="${step}" value="${stripState[key]}" />
    <div class="knob-hint">${hint}</div>`;
  const input = k.querySelector("input");
  const valEl = k.querySelector("[data-val]");
  input.addEventListener("input", () => {
    stripState[key] = Number(input.value);
    valEl.textContent = fmt(stripState[key]);
    // find the workspace body's redraw
    const body = document.getElementById("wsBody");
    if (body && body._redraw) body._redraw();
  });
  return k;
}

// ---- SPEAKERS workspace -----------------------------------------------------
function fillSpeakers(body, head) {
  head.querySelector("#wsSub").textContent = `${SPEAKERS.length} profiles · per-microphone · reused across sessions`;

  const grid = el("div", "spk-grid");
  SPEAKERS.forEach((s) => {
    const card = el("div", "spk-card" + (s.isRoom ? " room" : ""));
    card.innerHTML = `
      <div class="spk-head">
        <span class="spk-av sw${s.spk}">${s.initials}</span>
        <div>
          <div class="spk-name">${esc(s.name)}</div>
          <div class="spk-role">${s.isRoom ? "Shared room mic" : s.note.split(".")[0]}</div>
        </div>
        <span class="mic-tag">🎤 ${esc(s.mic.label)}</span>
      </div>
      <div class="spk-note">${esc(s.note)}</div>
      <div class="profile-grid">
        <div><div class="k">Gate threshold</div><div class="v">${s.gateThreshold.toFixed(2)}</div></div>
        <div><div class="k">Noise floor</div><div class="v">${s.noiseFloorDb} dB</div></div>
        <div><div class="k">Mic profile</div><div class="v" style="font-size:11px">${esc(s.mic.id)}</div></div>
        <div><div class="k">Sessions seen</div><div class="v">${s.sessionsSeen} <span class="reuse-tag" style="margin:0">reused</span></div></div>
      </div>`;

    // primary + secondary language + quick switch
    const langRow = el("div", "lang-pri-sec");
    langRow.innerHTML = `
      <span class="pl">Primary <b>${flagOf(s.primaryLang)} ${langName(s.primaryLang)}</b></span>
      <span class="pl">Secondary <b>${s.secondaryLang ? flagOf(s.secondaryLang) + " " + langName(s.secondaryLang) : "—"}</b></span>`;
    card.appendChild(langRow);

    const sw = el("div", "lang-switch-row");
    sw.appendChild(el("span", "lbl", "Transcribe this as:"));
    const seg = el("div", "langseg");
    ["en", "nb", "da"].forEach((code) => {
      const b = el("button", code === s.primaryLang ? "sel" : "", `${flagOf(code)} ${code.toUpperCase()}`);
      b.type = "button";
      b.addEventListener("click", () => {
        seg.querySelectorAll("button").forEach((x) => x.classList.remove("sel"));
        b.classList.add("sel");
      });
      seg.appendChild(b);
    });
    sw.appendChild(seg);
    card.appendChild(sw);

    // diarization (a property of the room speaker)
    if (s.isRoom && s.diarizedInto) {
      const dl = el("div", "diar-list");
      dl.appendChild(el("div", "fam-name", `⑂ Diarized into ${s.diarizedInto.length} voices`));
      s.diarizedInto.forEach((d) => {
        dl.appendChild(
          el(
            "div",
            "diar-child",
            `<span class="av sw${d.spk}">${d.label.split(" ")[1]}</span>
             <span><b>${d.label}</b> · ${flagOf(d.lang)} ${langName(d.lang)}</span>
             <span class="talk">${d.talkPct}% talk</span>`,
          ),
        );
      });
      card.appendChild(dl);
    }

    grid.appendChild(card);
  });
  body.appendChild(grid);
}

// ---- ENGINE workspace -------------------------------------------------------
let selFamily = selectedModel.family;
let selModelId = selectedModel.model;
function fillEngine(body, head) {
  head.querySelector("#wsSub").textContent = `${MOCK.APP.backend} · ${selModelId}`;

  const cols = el("div", "cols c-2");

  // left: backends + model-by-family
  const left = el("div", "");

  // backends
  const beBlock = el("div", "block");
  beBlock.appendChild(blockHead("Backend", "cuda unavailable on this host", ""));
  const beBody = el("div", "block-body");
  const chips = el("div", "backend-chips");
  MOCK.APP.backends.forEach((b) => {
    const chip = el(
      "button",
      "be-chip" + (b.kind === MOCK.APP.backend ? " sel" : "") + (b.available ? "" : " disabled"),
      `${b.label}<span class="be-state">${b.available ? (b.kind === MOCK.APP.backend ? "ACTIVE" : "ready") : "unavailable"}</span>`,
    );
    chip.type = "button";
    if (b.available) {
      chip.addEventListener("click", () => {
        chips.querySelectorAll(".be-chip").forEach((x) => x.classList.remove("sel"));
        chip.classList.add("sel");
      });
    }
    chips.appendChild(chip);
  });
  beBody.appendChild(chips);
  beBlock.appendChild(beBody);
  left.appendChild(beBlock);

  // model picker by family
  const modBlock = el("div", "block");
  modBlock.appendChild(blockHead("Model", "grouped by family", ""));
  const modBody = el("div", "block-body");
  MODELS.forEach((fam) => {
    const g = el("div", "fam-group");
    g.appendChild(el("div", "fam-name", esc(fam.family)));
    fam.models.forEach((m) => {
      const isSel = m.id === selModelId;
      const row = el("div", "model-row" + (isSel ? " sel" : ""));
      const translates = fam.family === "canary";
      row.innerHTML = `
        <span class="model-radio"></span>
        <div>
          <div class="model-name">${esc(m.display)}</div>
          <div class="model-desc">${esc(m.desc)}</div>
        </div>
        <div class="model-langs">
          ${m.langs.map((c) => `<span class="lang-tag">${flagOf(c)}</span>`).join("")}
          ${translates ? '<span class="tr-pill">↻ translate</span>' : ""}
        </div>`;
      row.addEventListener("click", () => {
        selFamily = fam.family;
        selModelId = m.id;
        modBody.querySelectorAll(".model-row").forEach((x) => x.classList.remove("sel"));
        row.classList.add("sel");
        // toggle canary translate box
        renderCanaryBox(canaryWrap);
        head.querySelector("#wsSub").textContent = `${MOCK.APP.backend} · ${selModelId}`;
      });
      g.appendChild(row);

      // canary translate box appears right under canary
      if (translates) {
        const canaryWrap = el("div", "");
        canaryWrap.id = "canaryWrap";
        g.appendChild(canaryWrap);
        renderCanaryBox(canaryWrap);
      }
    });
    modBody.appendChild(g);
  });
  modBlock.appendChild(modBody);
  left.appendChild(modBlock);
  cols.appendChild(left);

  // right: settings (prompt, hotwords, hallucination rules, recording)
  const right = el("div", "");

  const setBlock = el("div", "block");
  setBlock.appendChild(blockHead("Settings", "prompt · hotwords · recording", ""));
  const setBody = el("div", "block-body");
  const setGrid = el("div", "set-grid");
  setGrid.innerHTML = `
    <div class="set-field">
      <label>Recording</label>
      <div class="backend-chips">
        <button class="be-chip sel" type="button">On<span class="be-state">writing WAVs</span></button>
        <button class="be-chip" type="button">Off<span class="be-state">live only</span></button>
      </div>
    </div>
    <div class="set-field">
      <label>Initial prompt</label>
      <textarea>Nordic Sync standup. Speakers: Atle, Mette, James. Topics: quarterly numbers, dashboard.</textarea>
      <span class="hint">Biases the decoder toward in-domain vocabulary.</span>
    </div>
    <div class="set-field">
      <label>Hotwords</label>
      <input value="TapScribe, Vortiago, Nordic segment, kvartalstall" />
      <span class="hint">Comma-separated; boosted during decoding.</span>
    </div>`;
  setBody.appendChild(setGrid);
  setBlock.appendChild(setBody);
  right.appendChild(setBlock);

  // hallucination rules
  const halBlock = el("div", "block");
  halBlock.appendChild(blockHead("Hallucination rules", `${suppressedCount} line suppressed this session`, ""));
  const halBody = el("div", "block-body");
  [
    { rule: "youtube-outro", pat: "thank you for watching", hits: 1 },
    { rule: "subscribe-cta", pat: "please subscribe", hits: 1 },
    { rule: "silence-ghost", pat: "[music] / [applause]", hits: 0 },
  ].forEach((r) => {
    halBody.appendChild(
      el(
        "div",
        "rule-row",
        `<span class="dot ${r.hits ? "warn" : "idle"}"></span><b>${r.rule}</b> <code>${esc(r.pat)}</code><span class="hits">${r.hits} hit${r.hits === 1 ? "" : "s"}</span>`,
      ),
    );
  });
  halBody.appendChild(
    el("div", "muted-note", `Suppressed lines stay in the transcript audit (struck through) — never silently deleted.`),
  );
  halBlock.appendChild(halBody);
  right.appendChild(halBlock);

  cols.appendChild(right);
  body.appendChild(cols);
}

function renderCanaryBox(wrap) {
  wrap.innerHTML = "";
  if (selFamily !== "canary") return;
  const canary = MODELS.find((f) => f.family === "canary").models[0];
  const box = el("div", "canary-box");
  box.appendChild(el("div", "ct-title", "↻ Translation — source → target"));
  const row = el("div", "ct-row");
  const mkSelect = (input, selected) => {
    const opts = ["nb", "da", "en", "sv", "de", "fr"]
      .map((c) => `<option value="${c}" ${c === selected ? "selected" : ""}>${flagOf(c)} ${langName(c)}</option>`)
      .join("");
    return `<div class="ct-field"><label>${input.label}</label><select>${opts}</select></div>`;
  };
  const src = canary.inputs.find((i) => i.name === "source_lang");
  const tgt = canary.inputs.find((i) => i.name === "target_lang");
  const hot = canary.inputs.find((i) => i.name === "hotwords");
  row.innerHTML = `
    ${mkSelect(src, selectedModel.sourceLang)}
    <span class="ct-arrow">→</span>
    ${mkSelect(tgt, selectedModel.targetLang)}
    <div class="ct-field" style="flex:1"><label>${hot.label}</label><input placeholder="${hot.placeholder}" /></div>`;
  box.appendChild(row);
  box.appendChild(el("div", "muted-note", `Canary transcribes ${langName(selectedModel.sourceLang)} and emits ${langName(selectedModel.targetLang)} — translated lines get a ↳ badge in the transcript.`));
  wrap.appendChild(box);
}

// ---- shared block header ----------------------------------------------------
function blockHead(title, sub, right) {
  const h = el("div", "block-head");
  h.innerHTML = `<h3>${title}</h3>${sub ? `<span class="sub">${sub}</span>` : ""}<span class="right">${right || ""}</span>`;
  return h;
}

// =============================================================================
// router
// =============================================================================
function goHome() {
  app.dataset.mode = "glance";
  app.dataset.focus = "";
  backBtn.hidden = true;
  stage.innerHTML = "";
  stage.appendChild(renderHome());
  stage.scrollTop = 0;
}

function focus(id) {
  if (!CONCERNS.some((c) => c.id === id)) return;
  app.dataset.mode = "focus";
  app.dataset.focus = id;
  backBtn.hidden = false;
  stage.innerHTML = "";
  stage.appendChild(renderFocus(id));
}

backBtn.addEventListener("click", goHome);

// ---- public hooks for the screenshotter -------------------------------------
window.gotoView = (name) => {
  if (!name || name === "home" || name === "glance") return goHome();
  focus(name);
};
window.glanceHome = goHome;
window.glanceFocus = focus;

// ---- live wall clock (cosmetic) ---------------------------------------------
const wc = document.getElementById("wallClock");
function tickClock() {
  const d = new Date();
  wc.textContent = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
tickClock();

// boot
goHome();
