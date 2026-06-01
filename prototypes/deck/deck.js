// =============================================================================
// TapScribe — DECK prototype
// Consolidation: Console's density + Clarity's IA. Persistent top tabs,
// integrated diarization (a property of a tap, not a screen), unified
// Speakers directory (person + saved mic = one thing), IRC transcripts.
// All data from ../_shared/mock-data.js.
// =============================================================================
import {
  MOCK, LANGS, SPEAKERS, MODELS, selectedModel,
  LIVE_TAPS, LIVE_CAPTIONS, SESSIONS, STRIP_DEFAULTS, REP_WAV, TRANSCRIPT,
  computeRegions, helpers, speakerById,
} from "../_shared/mock-data.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const SPK_VAR = (spk) => `var(--spk${spk})`;
const SPK_HEX = (spk) => getComputedStyle(document.documentElement).getPropertyValue(`--spk${spk}`).trim() || "#38d6e0";
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const langChip = (code, extra = "") => {
  const l = helpers.lang(code);
  return `<span class="lang-chip ${extra}"><span class="fl">${l.flag}</span>${l.code.toUpperCase()}</span>`;
};
// initials for a transcript/feed line whose "speaker" may be "Oslo Room · Speaker B"
const lineInitials = (name) =>
  name.includes("Speaker A") ? "A" :
  name.includes("Speaker B") ? "B" :
  (SPEAKERS.find((s) => s.name === name)?.initials ?? name.slice(0, 2).toUpperCase());

// ---------------------------------------------------------------------------
// TOP BAR
// ---------------------------------------------------------------------------
function renderTopbar() {
  $("#appVer").textContent = "v" + MOCK.APP.version;
  const wrap = $("#topBackends");
  wrap.innerHTML = "";
  for (const b of MOCK.APP.backends) {
    const active = MOCK.APP.backend === b.kind;
    const chip = el("span", `bchip ${active ? "active" : ""} ${b.available ? "" : "disabled"}`,
      `<span class="bdot"></span>${b.label}${b.available ? "" : " ·off"}`);
    chip.title = b.available ? `${b.label} backend` : `${b.label} unavailable on this host`;
    chip.addEventListener("click", () => { if (!b.available) return; toast("Backend", b.label); });
    wrap.appendChild(chip);
  }
  $("#tabLiveBadge").textContent = String(LIVE_TAPS.filter((t) => t.live).length);
}

function startClock() {
  let t = SESSIONS.find((s) => s.current)?.durationS ?? 2880;
  const node = $("#clock");
  const tick = () => { node.textContent = helpers.clockH(t); t += 1; };
  tick();
  setInterval(tick, 1000);
}

// ---------------------------------------------------------------------------
// SPARKLINE (canvas)
// ---------------------------------------------------------------------------
function sparkline(levels, { w = 84, h = 20, color = "#38d6e0", active = true } = {}) {
  const dpr = 2;
  const c = el("canvas", "spark");
  c.width = w * dpr; c.height = h * dpr;
  c.style.width = w + "px"; c.style.height = h + "px";
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  const n = levels.length;
  const max = Math.max(0.06, ...levels);
  const x = (i) => (i / (n - 1)) * (w - 2) + 1;
  const y = (v) => h - 2 - (v / max) * (h - 4);

  ctx.strokeStyle = "rgba(255,255,255,0.05)"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, h - 1.5); ctx.lineTo(w, h - 1.5); ctx.stroke();
  if (!active) {
    ctx.strokeStyle = "rgba(73,83,94,0.8)";
    ctx.beginPath(); ctx.moveTo(1, h - 2); ctx.lineTo(w - 1, h - 2); ctx.stroke();
    return c;
  }
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "55"); grad.addColorStop(1, color + "00");
  ctx.beginPath(); ctx.moveTo(x(0), h);
  for (let i = 0; i < n; i++) ctx.lineTo(x(i), y(levels[i]));
  ctx.lineTo(x(n - 1), h); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < n; i++) (i ? ctx.lineTo : ctx.moveTo).call(ctx, x(i), y(levels[i]));
  ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.lineJoin = "round";
  ctx.shadowColor = color; ctx.shadowBlur = 4; ctx.stroke();
  ctx.shadowBlur = 6; ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(x(n - 1), y(levels[n - 1]), 1.8, 0, Math.PI * 2); ctx.fill();
  return c;
}

// ---------------------------------------------------------------------------
// LIVE TAPS — diarization is a PROPERTY of a tap (Oslo Room expands inline)
// ---------------------------------------------------------------------------
function tapRow(t) {
  const sp = speakerById(t.identity);
  const room = sp?.isRoom ? sp : null;
  const tr = el("tr", t.level > 0 ? "" : "muted");
  if (room) tr.classList.add("has-voices");

  // speaker cell (+ "2 voices" marker for a diarized tap)
  const spkTd = el("td", "col-spk");
  const voices = room
    ? `<span class="voices-tag" title="This tap carries more than one voice — TapScribe separates them into Speaker A/B."><span class="vx-tw">▶</span>${room.diarizedInto.length} voices</span>`
    : "";
  spkTd.innerHTML = `<span class="spk-cell">
    <span class="spk-chip" style="background:${SPK_VAR(t.spk)}">${sp?.initials ?? "??"}</span>
    <span class="spk-name">${esc(t.name)}</span></span>${voices}`;
  tr.appendChild(spkTd);

  tr.appendChild(el("td", "dim", esc(sp?.mic.label ?? "—")));
  tr.appendChild(el("td", "col-lang", langChip(t.lang)));

  const levTd = el("td", "col-level");
  const cls = t.level <= 0.001 ? "idle" : (t.level >= 0.55 ? "warm" : "");
  levTd.innerHTML = `<span class="levbar ${cls}"><span style="width:${Math.round(t.level * 100)}%"></span></span><span class="lev-num">${t.level.toFixed(2)}</span>`;
  tr.appendChild(levTd);

  const sparkTd = el("td", "col-spark");
  sparkTd.appendChild(sparkline(t.levels, { color: t.level > 0.001 ? SPK_HEX(t.spk) : "#49535e", active: t.level > 0.001 }));
  tr.appendChild(sparkTd);

  const lagTd = el("td", "num col-lag");
  lagTd.textContent = t.level > 0.001 ? `${Math.round(t.lagS * 1000)} ms` : "—";
  tr.appendChild(lagTd);

  const gateTd = el("td", "col-gate");
  gateTd.innerHTML = `<span class="gate ${t.gateOpen ? "open" : "closed"}"><span class="gdot"></span>${t.gateOpen ? "open" : "closed"}</span>`;
  tr.appendChild(gateTd);

  const tgTd = el("td", "col-toggles");
  tgTd.innerHTML = `<span class="toggles">
    <span class="tg r ${t.record ? "on" : ""}" title="record ${t.record ? "on" : "off"}">R</span>
    <span class="tg l ${t.live ? "on" : ""}" title="live ${t.live ? "on" : "off"}">L</span></span>`;
  tr.appendChild(tgTd);
  return { tr, room };
}

function diarSubRow(t, d) {
  const tr = el("tr", "subrow");
  tr.dataset.parent = t.identity;
  tr.innerHTML = `
    <td class="col-spk"><span class="spk-cell">
      <span class="spk-chip" style="background:${SPK_VAR(d.spk)};width:17px;height:17px;font-size:8px">${d.label.split(" ")[1]}</span>
      <span class="spk-name">${esc(d.label)} <span class="sub">· diarized</span></span></span></td>
    <td class="dim">— same mic —</td>
    <td>${langChip(d.lang)}</td>
    <td colspan="3"><div class="levbar" style="width:120px"><span style="width:${d.talkPct}%;background:${SPK_VAR(d.spk)};box-shadow:0 0 8px ${SPK_VAR(d.spk)}"></span></div><span class="lev-num">${helpers.pct(d.talkPct)} talk</span></td>
    <td class="dim" style="font-size:10px">voice</td>`;
  return tr;
}

function renderTaps() {
  const body = $("#tapsBody");
  body.innerHTML = "";
  let activeCount = 0;
  for (const t of LIVE_TAPS) {
    if (t.live) activeCount++;
    const { tr, room } = tapRow(t);
    body.appendChild(tr);
    if (room) {
      const subs = room.diarizedInto.map((d) => diarSubRow(t, d));
      subs.forEach((s) => body.appendChild(s));
      // start expanded so the diarization is visible by default
      tr.classList.add("expanded");
      tr.style.cursor = "pointer";
      const toggle = () => {
        const open = tr.classList.toggle("expanded");
        subs.forEach((s) => { s.hidden = !open; });
      };
      tr.addEventListener("click", toggle);
    }
  }
  $("#tapsActive").textContent = `${activeCount} active`;
  $("#tabLiveBadge").textContent = String(activeCount);
}

// ---------------------------------------------------------------------------
// NOW SPEAKING
// ---------------------------------------------------------------------------
function renderNow() {
  const lane = $("#nowLane");
  lane.innerHTML = "";
  const speaking = LIVE_TAPS.filter((t) => t.level > 0.001 && t.buffer);
  $("#nowCount").textContent = `${speaking.length} talking`;
  if (!speaking.length) { lane.innerHTML = `<div class="now-empty">— silence — no open gates</div>`; return; }
  for (const t of speaking) {
    const sp = speakerById(t.identity);
    const label = t.diarized ? `${t.name} · ${t.diarized}` : t.name;
    const card = el("div", "now-card");
    card.style.borderLeftColor = SPK_VAR(t.spk);
    const bars = [9, 13, 6, 11].map((hh) =>
      `<i style="height:${hh}px;background:${SPK_VAR(t.spk)};box-shadow:0 0 6px ${SPK_VAR(t.spk)}"></i>`).join("");
    card.innerHTML = `
      <div class="now-top">
        <span class="spk-chip" style="background:${SPK_VAR(t.spk)}">${sp?.initials ?? "??"}</span>
        <span class="now-name">${esc(label)}</span>
        ${langChip(t.lang)}
        <span class="now-eq">${bars}</span>
      </div>
      <div class="now-buf"><span class="lbl">IN-FLIGHT HYPOTHESIS</span>${esc(t.buffer)}<span class="caret"> ▋</span></div>`;
    lane.appendChild(card);
  }
}

// ---------------------------------------------------------------------------
// LIVE FEED (IRC single-line)
// ---------------------------------------------------------------------------
function renderFeed() {
  const body = $("#feedBody");
  body.innerHTML = "";
  for (const c of LIVE_CAPTIONS) {
    const l = helpers.lang(c.lang);
    const line = el("div", `fl-line ${c.inflight ? "inflight" : ""}`);
    line.innerHTML = `<span class="ts">[${helpers.clock(c.t)}]</span> <span class="who" style="color:${SPK_VAR(c.spk)}">${esc(c.speaker)}</span> <span class="fg" title="${l.name}">·${l.flag}·</span> <span class="txt">${esc(c.text)}</span>`;
    body.appendChild(line);
  }
  body.scrollTop = body.scrollHeight;
}

// ---------------------------------------------------------------------------
// SESSIONS list + detail
// ---------------------------------------------------------------------------
let selectedSession = SESSIONS.find((s) => s.current) || SESSIONS[0];

function renderSessions() {
  const body = $("#sessionsBody");
  body.innerHTML = "";
  for (const s of SESSIONS) {
    const tr = el("tr", `clickable ${s.current ? "current" : ""} ${s.id === selectedSession.id ? "selected" : ""}`);
    tr.dataset.session = s.id;
    const labelHtml = s.label ? `<span class="spk-name">${esc(s.label)}</span>` : `<span class="dim">Untitled</span>`;
    const cur = s.current ? ` <span class="badge badge-ok">LIVE</span>` : "";
    const d = new Date(s.startedAt);
    const started = `${d.toISOString().slice(5, 10).replace("-", "/")} ${d.toISOString().slice(11, 16)}`;
    const langs = s.langs.map((c) => langChip(c)).join(" ");
    const tx = s.hasTranscript ? `<span class="badge badge-ok">✓</span>` : `<span class="badge">queued</span>`;
    tr.innerHTML = `
      <td>${labelHtml}${cur}</td>
      <td class="col-when mono">${started}</td>
      <td class="num">${helpers.clock(s.durationS)}</td>
      <td class="num">${s.wavCount}</td>
      <td><span class="lang-row">${langs}</span></td>
      <td class="col-tx">${tx}</td>`;
    tr.addEventListener("click", () => { selectSession(s.id); });
    body.appendChild(tr);
  }
}

function selectSession(id) {
  selectedSession = SESSIONS.find((s) => s.id === id) || selectedSession;
  $$("#sessionsBody tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.session === selectedSession.id));
  renderDetail();
}

function renderDetail() {
  const s = selectedSession;
  $("#detailLabel").textContent = (s.label || "Untitled") + (s.current ? "  · LIVE" : "");
  renderTranscript(s);
  renderRecordings(s);
  renderEngine(s);
}

// --- session sub-tabs ---
let currentSub = "transcript";
function setSubtab(name) {
  if (!["transcript", "recordings", "engine"].includes(name)) name = "transcript";
  currentSub = name;
  for (const k of ["transcript", "recordings", "engine"]) {
    const v = $(`#sub-${k}`); if (v) v.hidden = k !== name;
  }
  $$("#subtabs .subtab").forEach((b) => b.classList.toggle("active", b.dataset.sub === name));
  if (name === "recordings") requestAnimationFrame(redrawWave);
}

// ---------------------------------------------------------------------------
// SUB: TRANSCRIPT (IRC merged transcript)
// ---------------------------------------------------------------------------
function renderTranscript(s) {
  $("#txHeadMeta").textContent = `${TRANSCRIPT.model} · ${TRANSCRIPT.backend} · ${helpers.clock(TRANSCRIPT.durationS)}`;
  const tb = $("#txTranslate");
  if (TRANSCRIPT.translated) { tb.hidden = false; tb.textContent = "translated nb→en"; } else tb.hidden = true;
  $("#txLineCount").textContent = `${TRANSCRIPT.lines.length} lines`;

  // speaking-time bar + legend
  $("#txSpkBar").innerHTML = TRANSCRIPT.speakingTime.map((st) =>
    `<span style="width:${st.pct}%;background:${SPK_VAR(st.spk)}" title="${esc(st.speaker)} ${st.pct}%"></span>`).join("");
  $("#txSpkLegend").innerHTML = TRANSCRIPT.speakingTime.map((st) =>
    `<span><span class="sw" style="background:${SPK_VAR(st.spk)}"></span>${esc(st.speaker)}<span class="pct">${st.pct}%</span></span>`).join("");

  // IRC lines: [HH:MM:SS] Speaker: text   (mono, pre-wrap container)
  const lines = $("#txLines");
  lines.innerHTML = "";
  for (const ln of TRANSCRIPT.lines) {
    const l = helpers.lang(ln.lang);
    const div = document.createElement("div");
    const ts = `<span class="ts">[${helpers.clockH(ln.t)}]</span>`;
    const who = `<span class="who" style="color:${SPK_VAR(ln.spk)}">${esc(ln.speaker)}</span>`;
    let body;
    if (ln.suppressed) {
      body = `<span class="seg suppressed" title="suppressed by rule: ${esc(ln.matchedRule)}">${l.flag} ${esc(ln.text)}</span>`;
    } else if (ln.lowConfidence) {
      const pct = ln.confidence != null ? Math.round(ln.confidence * 100) + "%" : "";
      body = `<span class="seg lowconf" title="low confidence">${l.flag} ${esc(ln.text)}<span class="conf-chip">${pct}</span></span>`;
    } else if (ln.translatedFrom) {
      body = `<span class="seg xlate">${l.flag} ${esc(ln.text)}</span><span class="tx-badge-inl">${ln.translatedFrom}→en</span>`;
    } else {
      body = `<span>${l.flag} ${esc(ln.text)}</span>`;
    }
    // single line (pre-wrap): no internal newlines
    div.innerHTML = `${ts} ${who}: ${body}`;
    lines.appendChild(div);
  }

  renderAudit();
}

function renderAudit() {
  const root = $("#txAudit");
  const suppressed = TRANSCRIPT.lines.filter((l) => l.suppressed);
  if (!suppressed.length) { root.innerHTML = ""; return; }
  root.innerHTML = `
    <button class="audit-toggle" id="auditToggle"><span class="atw">▶</span>${suppressed.length} suppressed line${suppressed.length > 1 ? "s" : ""} — audit</button>
    <div id="auditBody" hidden>
      <table class="audit-tbl">
        <thead><tr><th>time</th><th>speaker</th><th>text</th><th>matched rule</th><th>from</th></tr></thead>
        <tbody>${suppressed.map((l) => `
          <tr>
            <td class="muted tnum">${helpers.clockH(l.t)}</td>
            <td>${esc(l.speaker)}</td>
            <td class="wrap"><code>${esc(l.text)}</code></td>
            <td class="muted"><code>${esc(l.matchedRule)}</code></td>
            <td class="muted">${helpers.lang(l.lang).flag} ${l.lang}</td>
          </tr>`).join("")}</tbody>
      </table>
    </div>`;
  const btn = $("#auditToggle"), bodyEl = $("#auditBody");
  btn.addEventListener("click", () => { const open = btn.classList.toggle("open"); bodyEl.hidden = !open; });
}

// ---------------------------------------------------------------------------
// SUB: RECORDINGS (waveform + strip-silence live re-cut + per-WAV list)
// ---------------------------------------------------------------------------
const stripState = { ...STRIP_DEFAULTS };
let waveCanvas = null;

function drawWave(canvas, regions, h) {
  const dpr = 2;
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth || 560;
  canvas.width = cssW * dpr; canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssW, h);
  const peaks = REP_WAV.peaks, dur = REP_WAV.durationS, mid = h / 2;
  const xOf = (s) => (s / dur) * cssW;

  ctx.fillStyle = "rgba(255,92,108,0.05)"; ctx.fillRect(0, 0, cssW, h);
  for (const r of regions) {
    const x0 = xOf(r.startS), x1 = xOf(r.endS);
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, "rgba(61,220,132,0.16)"); g.addColorStop(0.5, "rgba(61,220,132,0.05)"); g.addColorStop(1, "rgba(61,220,132,0.16)");
    ctx.fillStyle = g; ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.strokeStyle = "rgba(61,220,132,0.85)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x0 + 0.5, 0); ctx.lineTo(x0 + 0.5, h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x1 - 0.5, 0); ctx.lineTo(x1 - 0.5, h); ctx.stroke();
  }
  const floorAmp = Math.pow(10, stripState.speechFloorDb / 20);
  const fy = (mid - floorAmp * (mid - 2));
  ctx.strokeStyle = "rgba(245,185,72,0.5)"; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, fy); ctx.lineTo(cssW, fy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, h - fy); ctx.lineTo(cssW, h - fy); ctx.stroke();
  ctx.setLineDash([]);

  const inRegion = (s) => regions.some((r) => s >= r.startS && s <= r.endS);
  const n = peaks.length, barW = Math.max(1, cssW / n);
  for (let i = 0; i < n; i++) {
    const s = (i / n) * dur, v = peaks[i];
    const ph = Math.max(0.6, v * (mid - 2)), x = (i / n) * cssW;
    ctx.fillStyle = inRegion(s) ? "rgba(61,220,132,0.92)" : "rgba(110,123,135,0.4)";
    if (inRegion(s) && v > floorAmp) { ctx.shadowColor = "rgba(61,220,132,0.6)"; ctx.shadowBlur = 3; } else ctx.shadowBlur = 0;
    ctx.fillRect(x, mid - ph, Math.max(0.7, barW - 0.3), ph * 2);
  }
  ctx.shadowBlur = 0;
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(cssW, mid); ctx.stroke();
}

function redrawWave() {
  if (!waveCanvas) return;
  const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, stripState);
  drawWave(waveCanvas, r.regions, 116);
  $("#stripClips").textContent = r.clips;
  $("#stripSpeech").textContent = r.speechS;
  $("#stripTotal").textContent = r.totalS;
  renderWavList(r.regions);
}

function renderRecordings(s) {
  $("#stripFile").textContent = "/ " + REP_WAV.name;
  $("#waveMid").textContent = helpers.clock(REP_WAV.durationS / 2);
  $("#waveEnd").textContent = helpers.clock(REP_WAV.durationS);
  $("#stripNote").innerHTML = `Re-cut runs live on <span class="mono" style="color:var(--ink-2)">${esc(REP_WAV.name)}</span>. Raise <b style="color:var(--cyan)">gap</b> to merge adjacent clips; lower <b style="color:var(--cyan)">floor</b> to keep quiet speech (down to 1 clip).`;

  // knobs
  const knobs = $("#stripKnobs");
  knobs.innerHTML = `
    <div class="knob"><label>gap <span class="unit">ms</span></label><input type="range" min="100" max="4000" step="50" value="${stripState.minSilenceMs}" data-knob="minSilenceMs"><span class="kval" data-kv="minSilenceMs">${stripState.minSilenceMs} ms</span></div>
    <div class="knob"><label>pad <span class="unit">ms</span></label><input type="range" min="0" max="600" step="20" value="${stripState.padMs}" data-knob="padMs"><span class="kval" data-kv="padMs">${stripState.padMs} ms</span></div>
    <div class="knob"><label>floor <span class="unit">dBFS</span></label><input type="range" min="-60" max="-25" step="1" value="${stripState.speechFloorDb}" data-knob="speechFloorDb"><span class="kval" data-kv="speechFloorDb">${stripState.speechFloorDb} dB</span></div>`;
  $$("[data-knob]", knobs).forEach((inp) => {
    inp.addEventListener("input", () => {
      const k = inp.dataset.knob;
      stripState[k] = +inp.value;
      const kv = $(`[data-kv="${k}"]`, knobs);
      if (kv) kv.textContent = k === "speechFloorDb" ? `${stripState[k]} dB` : `${stripState[k]} ms`;
      redrawWave();
    });
  });
  waveCanvas = $("#wavecanvas");
  requestAnimationFrame(redrawWave);
}

function renderWavList(repRegions) {
  const root = $("#wavList");
  const wavs = [
    { name: REP_WAV.name, spk: 0, regions: repRegions, durationS: REP_WAV.durationS },
    { name: "2026-05-28T09-11-40Z_mette_mette_e5f6a7b8.wav", spk: 1, regions: [{ startS: 0.4, endS: 14.2 }, { startS: 18.0, endS: 31.6 }], durationS: 34 },
    { name: "2026-05-28T09-18-02Z_room-oslo_speakerB_c9d0e1f2.wav", spk: 4, regions: [{ startS: 1.1, endS: 22.8 }], durationS: 24 },
  ];
  root.innerHTML = "";
  for (const w of wavs) {
    const speech = w.regions.reduce((a, r) => a + (r.endS - r.startS), 0);
    const row = el("div", "wav-row");
    const clips = w.regions.map((r, i) => {
      const dur = r.endS - r.startS;
      const widthPct = Math.min(100, (dur / w.durationS) * 100);
      return `<div class="clip"><span class="ci">${i + 1}</span><span class="cmini" style="width:${Math.max(14, widthPct)}%"></span><span class="crange mono">${helpers.clock(r.startS)}–${helpers.clock(r.endS)}</span><span class="cdur">${dur.toFixed(1)}s</span></div>`;
    }).join("");
    row.innerHTML = `
      <div class="wav-name"><span class="spk-chip" style="background:${SPK_VAR(w.spk)};width:15px;height:15px;font-size:7px">${w.spk}</span>
        <span class="nm mono">${esc(w.name.slice(11))}</span>
        <span class="tag-clips">${w.regions.length} clip${w.regions.length > 1 ? "s" : ""}</span></div>
      <div class="wav-orig">original ${w.durationS}s → stripped to ${speech.toFixed(1)}s speech</div>
      <div class="clip-list">${clips}</div>`;
    root.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// SUB: ENGINE (backend chips + model-by-family + Canary src/tgt)
// ---------------------------------------------------------------------------
function renderEngine() {
  const root = $("#enginePanel");
  const sm = { ...selectedModel };
  const backendChips = MOCK.APP.backends.map((b) => {
    const active = sm.backend === b.kind;
    return `<span class="bchip ${active ? "active" : ""} ${b.available ? "" : "disabled"}" data-backend="${b.kind}"><span class="bdot"></span>${b.label}${b.available ? "" : " ·off"}</span>`;
  }).join("");
  const optGroups = MODELS.map((fam) => {
    const opts = fam.models.map((m) => `<option value="${m.id}" ${m.id === sm.model ? "selected" : ""}>${m.display} — ${m.desc}</option>`).join("");
    return `<optgroup label="${fam.family}">${opts}</optgroup>`;
  }).join("");
  const langOpts = (sel) => Object.values(LANGS).map((l) => `<option value="${l.code}" ${l.code === sel ? "selected" : ""}>${l.flag} ${l.name}</option>`).join("");

  root.innerHTML = `
    <div class="fld">
      <label>Backend</label>
      <div class="seg-backends" id="engineBackends">${backendChips}</div>
      <span class="hint">cuda unavailable on this host — mlx active</span>
    </div>
    <div class="fld">
      <label>Model · by family</label>
      <select id="modelSelect">${optGroups}</select>
      <div class="model-desc" id="modelDesc"></div>
    </div>
    <div class="fld" id="canaryFields">
      <label>Canary · translation</label>
      <div class="canary-row">
        <div><div class="sub-label">source</div><select id="srcLang">${langOpts(sm.sourceLang)}</select></div>
        <div class="canary-arrow">→</div>
        <div><div class="sub-label">target</div><select id="tgtLang">${langOpts(sm.targetLang)}</select></div>
      </div>
      <div style="margin-top:9px"><div class="sub-label">hotwords</div><input type="text" placeholder="Acme, Vortiago…" id="hotwords"></div>
    </div>`;

  const descFor = (id) => { for (const fam of MODELS) { const m = fam.models.find((x) => x.id === id); if (m) return { fam: fam.family, m }; } return null; };
  const updateDesc = () => {
    const d = descFor($("#modelSelect").value);
    const langs = d.m.langs.map((c) => langChip(c)).join(" ");
    $("#modelDesc").innerHTML = `<b style="color:var(--ink-2)">${d.fam}</b> · ${d.m.desc}<div style="margin-top:5px">${langs}</div>`;
    $("#canaryFields").style.display = d.fam === "canary" ? "" : "none";
  };
  $("#modelSelect").addEventListener("change", () => { updateDesc(); toast("Model", $("#modelSelect").value); });
  $$("#engineBackends .bchip").forEach((c) => c.addEventListener("click", () => {
    if (c.classList.contains("disabled")) return;
    $$("#engineBackends .bchip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active"); toast("Backend", c.dataset.backend);
  }));
  $("#srcLang").addEventListener("change", () => toast("Source", helpers.lang($("#srcLang").value).name));
  $("#tgtLang").addEventListener("change", () => toast("Target", helpers.lang($("#tgtLang").value).name));
  updateDesc();
}

// ---------------------------------------------------------------------------
// SPEAKERS — unified directory (person + saved mic = ONE thing).
// Oslo Room is a diarized SOURCE: splits inline into Speaker A/B.
// ---------------------------------------------------------------------------
const XAS_LANGS = ["en", "nb", "da"];
function speakerRow(s) {
  const tr = el("tr", s.isRoom ? "room-row" : "");
  const gatePct = Math.round(s.gateThreshold * 100);
  const segBtns = XAS_LANGS.map((c) =>
    `<button data-as="${c}" class="${c === s.primaryLang ? "on" : ""}">${helpers.lang(c).flag} ${c.toUpperCase()}</button>`).join("");
  const secLang = s.secondaryLang ? langChip(s.secondaryLang, "sec") : `<span class="dim" style="font-size:10px">none</span>`;
  const splitNote = s.isRoom ? `<span class="split-note" title="This mic carries more than one voice — TapScribe splits it into Speaker A/B.">⚠ splits into ${s.diarizedInto.map((d) => d.label).join(" / ")}</span>` : "";
  tr.innerHTML = `
    <td>
      <span class="spk-cell"><span class="spk-chip" style="background:${SPK_VAR(s.spk)}">${s.initials}</span>
      <span class="spk-name">${esc(s.name)}</span></span>${s.isRoom ? ` <span class="badge badge-warn">room</span>` : ""}${splitNote}
      <div class="note-cell">${esc(s.note)}</div>
    </td>
    <td><span class="mic-cell"><span class="mic-glyph">▣</span>${esc(s.mic.label)}</span><span class="mic-id">${esc(s.mic.id)}</span></td>
    <td><span class="lang-row" style="margin-bottom:4px">${langChip(s.primaryLang, "pri")} ${secLang}</span><div class="xas"><span class="xl-lbl">AS</span><span class="seg" data-spk="${s.id}">${segBtns}</span></div></td>
    <td class="num">${s.gateThreshold.toFixed(2)}<span class="gate-mini"><span style="width:${gatePct}%"></span></span></td>
    <td class="num">${s.noiseFloorDb} dB</td>
    <td class="num"><span class="seen">${s.sessionsSeen}×</span></td>`;
  return tr;
}
function diarSpeakerSub(d) {
  const tr = el("tr", "diar-sub");
  tr.innerHTML = `
    <td><span class="spk-cell"><span class="spk-chip" style="background:${SPK_VAR(d.spk)};width:17px;height:17px;font-size:8px">${d.label.split(" ")[1]}</span>
      <span class="spk-name">${esc(d.label)} <span class="sub">· diarized voice</span></span></span></td>
    <td class="dim">— shared room mic —</td>
    <td>${langChip(d.lang)} <span class="dim" style="font-size:10px">${helpers.pct(d.talkPct)} of talk</span></td>
    <td class="num dim">—</td><td class="num dim">—</td><td class="num dim">—</td>`;
  return tr;
}
function renderSpeakers() {
  const body = $("#spkBody");
  body.innerHTML = "";
  for (const s of SPEAKERS) {
    body.appendChild(speakerRow(s));
    if (s.isRoom) s.diarizedInto.forEach((d) => body.appendChild(diarSpeakerSub(d)));
  }
  $$("#spkBody .seg").forEach((seg) => {
    seg.addEventListener("click", (e) => {
      const btn = e.target.closest("button"); if (!btn) return;
      $$("button", seg).forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      const sp = speakerById(seg.dataset.spk);
      toast("Transcribe as", `${sp.name} → ${helpers.lang(btn.dataset.as).name}`);
    });
  });
}

// ---------------------------------------------------------------------------
// SETTINGS (prompt / hotwords / hallucinations + recording toggle + catalog)
// ---------------------------------------------------------------------------
function renderSettings() {
  const form = $("#settingsForm");
  form.innerHTML = `
    <div class="fld"><label>Global prompt</label>
      <textarea id="setPrompt" spellcheck="false">Quarterly business review. Nordic team. Speakers: Atle (host), Mette, James. Keep proper nouns and product names verbatim.</textarea>
      <span class="hint">prepended to every transcription as context</span></div>
    <div class="fld"><label>Hotwords</label>
      <textarea id="setHot" spellcheck="false">Acme, Vortiago, TapScribe, Nordic Sync, Q3</textarea>
      <span class="hint">boost recognition of these terms</span></div>
    <div class="fld"><label>Hallucination filters</label>
      <textarea id="setHall" spellcheck="false">youtube-outro: "thank you for watching, please subscribe"
silence-filler: "[mm-hmm]"</textarea>
      <span class="hint">lines matching a rule are suppressed (struck-through in the transcript, listed in the audit)</span></div>
    <div class="set-toggle-row">
      <span class="switch ${MOCK.APP.recordingEnabled ? "on" : ""}" id="recSwitch" role="switch" aria-checked="${MOCK.APP.recordingEnabled}"></span>
      <div><div style="color:var(--ink)">Recording</div><div class="hint">arm new /tap streams · ${MOCK.APP.recordingEnabled ? "currently armed" : "paused"}</div></div>
    </div>`;
  const sw = $("#recSwitch");
  sw.addEventListener("click", () => {
    const on = sw.classList.toggle("on");
    sw.setAttribute("aria-checked", String(on));
    $(".hint", sw.parentElement.querySelector("div"))?.replaceChildren(document.createTextNode(`arm new /tap streams · ${on ? "currently armed" : "paused"}`));
    toast("Recording", on ? "armed" : "paused");
  });

  // side: backends + model catalog grouped by family
  const side = $("#settingsSide");
  const backends = MOCK.APP.backends.map((b) =>
    `<span class="bchip ${MOCK.APP.backend === b.kind ? "active" : ""} ${b.available ? "" : "disabled"}"><span class="bdot"></span>${b.label}${b.available ? "" : " ·off"}</span>`).join("");
  const catalog = MODELS.map((fam) => `
    <div class="cat-fam">${fam.family}</div>
    ${fam.models.map((m) => {
      const sel = m.id === selectedModel.model;
      return `<div class="cat-row ${sel ? "sel" : ""}"><span class="cm">${m.display}</span><span class="cd">${m.desc}</span>${sel ? `<span class="cstar">★ active</span>` : ""}</div>`;
    }).join("")}`).join("");
  side.innerHTML = `
    <div class="fld"><label>Available backends</label><div class="seg-backends">${backends}</div>
      <span class="hint">resolved: <b style="color:var(--green)">${MOCK.APP.backend}</b> · cuda offline</span></div>
    <div class="fld"><label>Model catalog · by family</label><div class="catalog-list">${catalog}</div></div>`;
}

// ---------------------------------------------------------------------------
// TOP-LEVEL VIEW SWITCHER
// ---------------------------------------------------------------------------
const VIEWS = ["live", "sessions", "speakers", "settings"];
let currentView = "live";
function setView(name) {
  if (!VIEWS.includes(name)) name = "live";
  currentView = name;
  for (const v of VIEWS) { const node = $(`#view-${v}`); if (node) node.hidden = v !== name; }
  $$("#tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  if (name === "sessions") requestAnimationFrame(() => { if (currentSub === "recordings") redrawWave(); });
}

// ---------------------------------------------------------------------------
// TOAST
// ---------------------------------------------------------------------------
let toastTimer = null;
function toast(k, msg) {
  const t = $("#toast");
  t.innerHTML = `<span class="tk">${esc(k)}</span><span>${esc(msg)}</span>`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

// ---------------------------------------------------------------------------
// WIRING + NAV HOOKS for screenshots
// ---------------------------------------------------------------------------
function wire() {
  $$("#tabs .tab").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $$("#subtabs .subtab").forEach((b) => b.addEventListener("click", () => setSubtab(b.dataset.sub)));
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    if (["1", "2", "3", "4"].includes(e.key)) setView(VIEWS[+e.key - 1]);
  });
}

// window.gotoView('live'|'sessions'|'speakers'|'settings') and
// window.gotoView('sessions/recordings') / window.setSubtab('transcript'|…)
window.gotoView = (name) => {
  if (typeof name === "string" && name.includes("/")) {
    const [v, sub] = name.split("/");
    setView(v);
    setSubtab(sub);
    return;
  }
  setView(name);
};
window.setSubtab = (name) => { setView("sessions"); setSubtab(name); };

// ---------------------------------------------------------------------------
// BOOT
// ---------------------------------------------------------------------------
function boot() {
  renderTopbar();
  startClock();
  renderTaps();
  renderNow();
  renderFeed();
  renderSessions();
  renderDetail();
  setSubtab("transcript");
  renderSpeakers();
  renderSettings();
  wire();
  setView("live");
  $("#app").setAttribute("aria-busy", "false");

  let rt;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => redrawWave(), 120); });
}
boot();
