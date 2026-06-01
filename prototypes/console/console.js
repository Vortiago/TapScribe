// =============================================================================
// TapScribe — CONSOLE prototype
// Live-ops terminal board + ⌘K command palette. All data from ../_shared.
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
const langChip = (code, extra = "") => {
  const l = helpers.lang(code);
  return `<span class="lang-chip ${extra}"><span class="fl">${l.flag}</span>${l.code.toUpperCase()}</span>`;
};
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------------------------------------------------------------------
// TOP BAR
// ---------------------------------------------------------------------------
function renderTopbar() {
  $("#appVer").textContent = "v" + MOCK.APP.version;

  const wrap = $("#topBackends");
  wrap.innerHTML = "";
  for (const b of MOCK.APP.backends) {
    const active = MOCK.APP.backend === b.kind;
    const chip = el(
      "span",
      `bchip ${active ? "active" : ""} ${b.available ? "" : "disabled"}`,
      `<span class="bdot"></span>${b.label}${b.available ? "" : " ·off"}`,
    );
    chip.title = b.available ? `${b.label} backend` : `${b.label} unavailable on this host`;
    wrap.appendChild(chip);
  }

  // fake live throughput numbers, ticking gently
  const tickStats = () => {
    const inN = (11 + Math.random() * 4).toFixed(1);
    $("#statIn").textContent = `${inN} msg/s`;
    $("#statDrain").textContent = `${Math.floor(Math.random() * 3)} q`;
  };
  tickStats();
  setInterval(tickStats, 1400);
}

// running session clock (starts at 48 min, the meeting duration, and counts up)
function startClock() {
  let t = SESSIONS.find((s) => s.current)?.durationS ?? 2880;
  const node = $("#clock");
  const tick = () => { node.textContent = helpers.clockH(t); t += 1; };
  tick();
  setInterval(tick, 1000);
}

// ---------------------------------------------------------------------------
// SPARKLINE (canvas) — from tap.levels
// ---------------------------------------------------------------------------
function sparkline(levels, { w = 90, h = 22, color = "#38d6e0", active = true } = {}) {
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

  // baseline grid
  ctx.strokeStyle = "rgba(255,255,255,0.05)";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, h - 1.5); ctx.lineTo(w, h - 1.5); ctx.stroke();

  if (!active) {
    // flat idle line
    ctx.strokeStyle = "rgba(73,83,94,0.8)";
    ctx.beginPath(); ctx.moveTo(1, h - 2); ctx.lineTo(w - 1, h - 2); ctx.stroke();
    return c;
  }

  // area fill
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "55");
  grad.addColorStop(1, color + "00");
  ctx.beginPath();
  ctx.moveTo(x(0), h);
  for (let i = 0; i < n; i++) ctx.lineTo(x(i), y(levels[i]));
  ctx.lineTo(x(n - 1), h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // stroke
  ctx.beginPath();
  for (let i = 0; i < n; i++) (i ? ctx.lineTo : ctx.moveTo).call(ctx, x(i), y(levels[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.4;
  ctx.lineJoin = "round";
  ctx.shadowColor = color; ctx.shadowBlur = 4;
  ctx.stroke();

  // head dot
  ctx.shadowBlur = 6;
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(x(n - 1), y(levels[n - 1]), 1.8, 0, Math.PI * 2); ctx.fill();
  return c;
}

// ---------------------------------------------------------------------------
// LIVE TAPS table
// ---------------------------------------------------------------------------
function renderTaps() {
  const body = $("#tapsBody");
  body.innerHTML = "";
  let activeCount = 0;

  for (const t of LIVE_TAPS) {
    if (t.live) activeCount++;
    const sp = speakerById(t.identity);
    const tr = el("tr", t.level > 0 ? "" : "muted");

    // speaker
    const spkTd = el("td", "col-spk");
    const sub = t.diarized ? `<span class="sub"> · ${t.diarized}</span>` : "";
    spkTd.innerHTML = `<span class="spk-cell">
      <span class="spk-chip" style="background:${SPK_VAR(t.spk)}">${sp?.initials ?? "??"}</span>
      <span class="spk-name">${esc(t.name)}${sub}</span></span>`;
    tr.appendChild(spkTd);

    // mic
    tr.appendChild(el("td", "dim", esc(sp?.mic.label ?? "—")));

    // lang
    tr.appendChild(el("td", "col-lang", langChip(t.lang)));

    // level meter
    const levTd = el("td", "col-level");
    const warm = t.level >= 0.55 ? "warm" : "";
    const cls = t.level <= 0.001 ? "idle" : warm;
    levTd.innerHTML = `<span class="levbar ${cls}"><span style="width:${Math.round(t.level * 100)}%"></span></span><span class="lev-num">${t.level.toFixed(2)}</span>`;
    tr.appendChild(levTd);

    // sparkline
    const sparkTd = el("td", "col-spark");
    sparkTd.appendChild(sparkline(t.levels, {
      color: t.level > 0.001 ? SPK_HEX(t.spk) : "#49535e",
      active: t.level > 0.001,
    }));
    tr.appendChild(sparkTd);

    // lag (ms)
    const lagTd = el("td", "num col-lag");
    lagTd.textContent = t.level > 0.001 ? `${Math.round(t.lagS * 1000)} ms` : "—";
    tr.appendChild(lagTd);

    // gate
    const gateTd = el("td", "col-gate");
    gateTd.innerHTML = `<span class="gate ${t.gateOpen ? "open" : "closed"}"><span class="gdot"></span>${t.gateOpen ? "open" : "closed"}</span>`;
    tr.appendChild(gateTd);

    // toggles
    const tgTd = el("td", "col-toggles");
    tgTd.innerHTML = `<span class="toggles">
      <span class="tg r ${t.record ? "on" : ""}" title="record ${t.record ? "on" : "off"}">R</span>
      <span class="tg l ${t.live ? "on" : ""}" title="live ${t.live ? "on" : "off"}">L</span></span>`;
    tr.appendChild(tgTd);

    body.appendChild(tr);
  }
  $("#tapsActive").textContent = `${activeCount} active`;
}

// ---------------------------------------------------------------------------
// NOW SPEAKING lane
// ---------------------------------------------------------------------------
function renderNow() {
  const lane = $("#nowLane");
  lane.innerHTML = "";
  const speaking = LIVE_TAPS.filter((t) => t.level > 0.001 && t.buffer);
  $("#nowCount").textContent = `${speaking.length} talking`;

  for (const t of speaking) {
    const sp = speakerById(t.identity);
    const label = t.diarized ? `${t.name} · ${t.diarized}` : t.name;
    const card = el("div", "now-card");
    card.style.borderLeftColor = SPK_VAR(t.spk);
    const bars = [9, 13, 6, 11].map((hh, i) =>
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
// LIVE FEED
// ---------------------------------------------------------------------------
function renderFeed() {
  const body = $("#feedBody");
  body.innerHTML = "";
  for (const c of LIVE_CAPTIONS) {
    const l = helpers.lang(c.lang);
    const line = el("div", `feed-line ${c.inflight ? "inflight" : ""}`);
    line.innerHTML = `
      <span class="feed-t">[${helpers.clock(c.t)}]</span>
      <span class="feed-spk" style="color:${SPK_VAR(c.spk)}">${esc(c.speaker)}</span>
      <span class="feed-fl" title="${l.name}">·${l.flag}·</span>
      <span class="feed-txt">${esc(c.text)}</span>`;
    body.appendChild(line);
  }
  body.scrollTop = body.scrollHeight;
}

// ---------------------------------------------------------------------------
// SESSIONS table (sortable)
// ---------------------------------------------------------------------------
const sessState = { key: "startedAt", asc: false };
function renderSessions() {
  const body = $("#sessionsBody");
  body.innerHTML = "";
  const rows = [...SESSIONS].sort((a, b) => {
    let av = a[sessState.key], bv = b[sessState.key];
    if (sessState.key === "label") { av = av || "zzz"; bv = bv || "zzz"; }
    const cmp = av < bv ? -1 : av > bv ? 1 : 0;
    return sessState.asc ? cmp : -cmp;
  });

  for (const s of rows) {
    const tr = el("tr", `clickable ${s.current ? "current" : ""}`);
    tr.dataset.session = s.id;
    const labelHtml = s.label
      ? `<span class="spk-name">${esc(s.label)}</span>`
      : `<span class="dim">untitled</span>`;
    const cur = s.current ? ` <span class="badge badge-ok">LIVE</span>` : "";
    const date = new Date(s.startedAt);
    const started = `${date.toISOString().slice(5, 10).replace("-", "/")} ${date.toISOString().slice(11, 16)}`;
    const langs = s.langs.map((c) => langChip(c)).join(" ");
    const tx = s.hasTranscript
      ? `<span class="badge badge-ok">done</span>`
      : `<span class="badge">queued</span>`;
    tr.innerHTML = `
      <td>${labelHtml}${cur}</td>
      <td class="dim mono">${started}</td>
      <td class="num">${helpers.clock(s.durationS)}</td>
      <td class="num">${s.wavCount}</td>
      <td><span class="lang-row">${langs}</span></td>
      <td class="col-tx">${tx}</td>`;
    tr.addEventListener("click", () => {
      if (s.hasTranscript) { setView("transcript"); toast("Open", `${s.label || "untitled"} — merged transcript`); }
      else { toast("Queue", `${s.label || "untitled"} — transcription queued`); }
    });
    body.appendChild(tr);
  }

  $$("#sessionsTable thead th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === sessState.key);
    th.classList.toggle("asc", th.dataset.sort === sessState.key && sessState.asc);
  });
}
function wireSessionSort() {
  $$("#sessionsTable thead th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (sessState.key === k) sessState.asc = !sessState.asc;
      else { sessState.key = k; sessState.asc = k === "label"; }
      renderSessions();
    });
  });
}

// ---------------------------------------------------------------------------
// PROFILES table (board tile, compact) — keyed by mic, cross-session
// ---------------------------------------------------------------------------
function renderSpeakerTable() {
  const body = $("#spkBody");
  body.innerHTML = "";
  for (const s of SPEAKERS) {
    const tr = el("tr");
    const langs = `<span class="lang-row">${langChip(s.primaryLang, "pri")}${s.secondaryLang ? langChip(s.secondaryLang, "sec") : `<span class="dim" style="font-size:10px">—</span>`}</span>`;
    const roomTag = s.isRoom ? ` <span class="badge" style="color:var(--amber);border-color:var(--amber-d);background:var(--amber-d)">room</span>` : "";
    tr.innerHTML = `
      <td><span class="spk-cell"><span class="spk-chip" style="background:${SPK_VAR(s.spk)}">${s.initials}</span><span class="spk-name">${esc(s.name)}</span></span>${roomTag}</td>
      <td class="dim"><span class="mic-glyph" style="color:var(--cyan)">▣</span> ${esc(s.mic.label)}</td>
      <td>${langs}</td>
      <td class="num">${s.gateThreshold.toFixed(2)}</td>
      <td class="num">${s.noiseFloorDb} dB</td>
      <td class="num" style="color:var(--amber)">${s.sessionsSeen}×</td>`;
    body.appendChild(tr);
  }
}

// ---------------------------------------------------------------------------
// DIARIZATION (board mini + full)
// ---------------------------------------------------------------------------
function diarSplitHtml(room) {
  return room.diarizedInto.map((d) => `
    <div class="diar-row" style="border-left-color:${SPK_VAR(d.spk)}">
      <div class="dr-top">
        <span class="spk-chip" style="background:${SPK_VAR(d.spk)};width:18px;height:18px;font-size:8px">${d.label.split(" ")[1]}</span>
        <span class="dr-name">${esc(d.label)}</span>
        ${langChip(d.lang)}
        <span class="dr-pct">${helpers.pct(d.talkPct)}</span>
      </div>
      <div class="talkbar"><span style="width:${d.talkPct}%;background:${SPK_VAR(d.spk)};box-shadow:0 0 8px ${SPK_VAR(d.spk)}"></span></div>
    </div>`).join("");
}
function renderDiarMini() {
  const room = speakerById("room-oslo");
  $("#diarBody").innerHTML = `
    <div class="diar-head">
      <span class="spk-chip" style="background:${SPK_VAR(room.spk)};width:18px;height:18px;font-size:8px">${room.initials}</span>
      <span class="src">${esc(room.name)}</span>
    </div>
    <div class="dim" style="font-size:10.5px;margin:-4px 0 2px">1 tap → 2 voices · ${esc(room.mic.label)}</div>
    <div class="diar-split">${diarSplitHtml(room)}</div>`;
}
function renderDiarFull() {
  const room = speakerById("room-oslo");
  $("#diarMic").textContent = room.mic.label;
  $("#diarFull").innerHTML = `
    <div class="diar-head">
      <span class="spk-chip" style="background:${SPK_VAR(room.spk)};width:22px;height:22px">${room.initials}</span>
      <span class="src">${esc(room.name)}</span>
    </div>
    <p class="prof-diar-note">⚠ shared mic — auto-split into 2 speakers</p>
    <div class="diar-split">${diarSplitHtml(room)}</div>
    <div class="dim" style="font-size:10.5px;border-top:1px dashed var(--hair);padding-top:9px">
      Diarization runs per shared-room tap. Each detected voice is attributed its own
      language so the merged transcript keeps Norwegian and English lines separate.
    </div>`;
}

// ---------------------------------------------------------------------------
// STRIP SILENCE — waveform + live re-cut (mini + big share one renderer)
// ---------------------------------------------------------------------------
const stripState = { ...STRIP_DEFAULTS };

function drawWave(canvas, regions, { h }) {
  const dpr = 2;
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  canvas.width = cssW * dpr; canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssW, h);

  const peaks = REP_WAV.peaks;
  const dur = REP_WAV.durationS;
  const mid = h / 2;
  const xOf = (s) => (s / dur) * cssW;

  // 1. cut zones (everything not in a region) — hatched red
  ctx.fillStyle = "rgba(255,92,108,0.05)";
  ctx.fillRect(0, 0, cssW, h);
  // 2. kept regions — green tint band behind the wave
  for (const r of regions) {
    const x0 = xOf(r.startS), x1 = xOf(r.endS);
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, "rgba(61,220,132,0.16)");
    g.addColorStop(0.5, "rgba(61,220,132,0.05)");
    g.addColorStop(1, "rgba(61,220,132,0.16)");
    ctx.fillStyle = g;
    ctx.fillRect(x0, 0, x1 - x0, h);
    // region edge markers
    ctx.strokeStyle = "rgba(61,220,132,0.85)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x0 + 0.5, 0); ctx.lineTo(x0 + 0.5, h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x1 - 0.5, 0); ctx.lineTo(x1 - 0.5, h); ctx.stroke();
  }

  // 3. floor line (speechFloorDb as amplitude → mirrored band)
  const floorAmp = Math.pow(10, stripState.speechFloorDb / 20);
  const fy = (mid - floorAmp * (mid - 2));
  ctx.strokeStyle = "rgba(245,185,72,0.5)";
  ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, fy); ctx.lineTo(cssW, fy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, h - fy); ctx.lineTo(cssW, h - fy); ctx.stroke();
  ctx.setLineDash([]);

  // helper: is sample inside a kept region?
  const inRegion = (s) => regions.some((r) => s >= r.startS && s <= r.endS);

  // 4. waveform bars
  const n = peaks.length;
  const barW = Math.max(1, cssW / n);
  for (let i = 0; i < n; i++) {
    const s = (i / n) * dur;
    const v = peaks[i];
    const ph = Math.max(0.6, v * (mid - 2));
    const x = (i / n) * cssW;
    ctx.fillStyle = inRegion(s) ? "rgba(61,220,132,0.92)" : "rgba(110,123,135,0.4)";
    if (inRegion(s) && v > floorAmp) { ctx.shadowColor = "rgba(61,220,132,0.6)"; ctx.shadowBlur = 3; }
    else ctx.shadowBlur = 0;
    ctx.fillRect(x, mid - ph, Math.max(0.7, barW - 0.3), ph * 2);
  }
  ctx.shadowBlur = 0;

  // 5. center hairline
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(cssW, mid); ctx.stroke();
}

function buildStripTile(rootSel, { waveH, full }) {
  const root = $(rootSel);
  const res = computeRegions(REP_WAV.peaks, REP_WAV.durationS, stripState);

  root.innerHTML = `
    <div class="wave-wrap">
      <canvas class="wavecanvas"></canvas>
      <div class="wave-axis"><span>0:00</span><span>${helpers.clock(REP_WAV.durationS / 2)}</span><span>${helpers.clock(REP_WAV.durationS)}</span></div>
    </div>
    <div class="strip-readout">
      <span class="clips" data-clips>${res.clips}</span><span class="ratio">clips</span>
      <span class="sep">·</span>
      <span class="ratio"><b data-speech>${res.speechS}</b>s speech / <span data-total>${res.totalS}</span>s</span>
      <div class="strip-legend"><span><i class="lg-keep"></i>keep</span><span><i class="lg-cut"></i>cut</span></div>
    </div>
    <div class="knobs">
      <div class="knob">
        <label>gap <span class="unit">ms</span></label>
        <input type="range" min="100" max="4000" step="50" value="${stripState.minSilenceMs}" data-knob="minSilenceMs">
        <span class="kval" data-kv="minSilenceMs">${stripState.minSilenceMs} ms</span>
      </div>
      <div class="knob">
        <label>pad <span class="unit">ms</span></label>
        <input type="range" min="0" max="600" step="20" value="${stripState.padMs}" data-knob="padMs">
        <span class="kval" data-kv="padMs">${stripState.padMs} ms</span>
      </div>
      <div class="knob">
        <label>floor <span class="unit">dBFS</span></label>
        <input type="range" min="-60" max="-25" step="1" value="${stripState.speechFloorDb}" data-knob="speechFloorDb">
        <span class="kval" data-kv="speechFloorDb">${stripState.speechFloorDb} dB</span>
      </div>
    </div>
    ${full ? `<div class="dim" style="font-size:11px;border-top:1px dashed var(--hair);padding-top:10px">
      Re-cut runs live on <span class="mono" style="color:var(--ink-2)">${esc(REP_WAV.name)}</span>.
      Raise <b style="color:var(--cyan)">gap</b> to merge adjacent clips; lower <b style="color:var(--cyan)">floor</b> to keep quiet speech (down to 1 clip).
    </div>` : ""}`;

  const canvas = $(".wavecanvas", root);
  const redraw = () => {
    const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, stripState);
    drawWave(canvas, r.regions, { h: waveH });
    $("[data-clips]", root).textContent = r.clips;
    $("[data-speech]", root).textContent = r.speechS;
    $("[data-total]", root).textContent = r.totalS;
  };

  $$("[data-knob]", root).forEach((inp) => {
    inp.addEventListener("input", () => {
      const k = inp.dataset.knob;
      stripState[k] = +inp.value;
      const kv = $(`[data-kv="${k}"]`, root);
      if (kv) kv.textContent = k === "speechFloorDb" ? `${stripState[k]} dB` : `${stripState[k]} ms`;
      redraw();
      // keep the *other* strip view in sync if it exists
      syncStripInputs(root);
    });
  });

  // initial draw (defer so clientWidth is laid out)
  requestAnimationFrame(redraw);
  return redraw;
}

// keep both strip tiles' inputs reflecting shared state when one changes
function syncStripInputs(except) {
  $$("[data-knob]").forEach((inp) => {
    if (inp.closest(".tile") === except.closest(".tile")) return;
    const k = inp.dataset.knob;
    inp.value = stripState[k];
    const root = inp.closest(".tile-body");
    const kv = root && $(`[data-kv="${k}"]`, root);
    if (kv) kv.textContent = k === "speechFloorDb" ? `${stripState[k]} dB` : `${stripState[k]} ms`;
  });
  // and redraw the other canvases
  $$(".wavecanvas").forEach((cv) => {
    if (cv.closest(".tile") === except.closest(".tile")) return;
    const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, stripState);
    drawWave(cv, r.regions, { h: cv.clientHeight || 76 });
    const tileBody = cv.closest(".tile-body");
    if (tileBody) {
      const cl = $("[data-clips]", tileBody); if (cl) cl.textContent = r.clips;
      const sp = $("[data-speech]", tileBody); if (sp) sp.textContent = r.speechS;
    }
  });
}

let redrawStripMini, redrawStripBig;
function renderStrip() {
  $("#stripFile").textContent = REP_WAV.name.slice(0, 26) + "…";
  $("#stripFileBig").textContent = REP_WAV.name;
  redrawStripMini = buildStripTile("#stripMini", { waveH: 64, full: false });
  redrawStripBig = buildStripTile("#stripBig", { waveH: 120, full: true });
}

// ---------------------------------------------------------------------------
// MODEL PANEL — backend chips + grouped catalog + Canary src/tgt
// ---------------------------------------------------------------------------
function renderModelPanel() {
  const root = $("#modelPanel");
  const sm = { ...selectedModel };

  const backendChips = MOCK.APP.backends.map((b) => {
    const active = sm.backend === b.kind;
    return `<span class="bchip ${active ? "active" : ""} ${b.available ? "" : "disabled"}" data-backend="${b.kind}">
      <span class="bdot"></span>${b.label}${b.available ? "" : " ·off"}</span>`;
  }).join("");

  const optGroups = MODELS.map((fam) => {
    const opts = fam.models.map((m) =>
      `<option value="${m.id}" ${m.id === sm.model ? "selected" : ""}>${m.display} — ${m.desc}</option>`).join("");
    return `<optgroup label="${fam.family}">${opts}</optgroup>`;
  }).join("");

  const langOpts = (sel) => Object.values(LANGS)
    .map((l) => `<option value="${l.code}" ${l.code === sel ? "selected" : ""}>${l.flag} ${l.name}</option>`).join("");

  root.innerHTML = `
    <div class="fld">
      <label>Backend</label>
      <div class="seg-backends" id="modelBackends">${backendChips}</div>
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
        <div class="knob"><label>source</label><select id="srcLang">${langOpts(sm.sourceLang)}</select></div>
        <div class="canary-arrow">→</div>
        <div class="knob"><label>target</label><select id="tgtLang">${langOpts(sm.targetLang)}</select></div>
      </div>
      <div class="knob" style="margin-top:8px"><label>hotwords</label><input type="text" placeholder="Acme, Vortiago…" id="hotwords"></div>
    </div>`;

  const descFor = (id) => {
    for (const fam of MODELS) { const m = fam.models.find((x) => x.id === id); if (m) return { fam: fam.family, m }; }
    return null;
  };
  const updateDesc = () => {
    const id = $("#modelSelect").value;
    const d = descFor(id);
    const langs = d.m.langs.map((c) => langChip(c)).join(" ");
    $("#modelDesc").innerHTML = `<b style="color:var(--ink-2)">${d.fam}</b> · ${d.m.desc}<div style="margin-top:5px">${langs}</div>`;
    $("#canaryFields").style.display = d.fam === "canary" ? "" : "none";
  };
  $("#modelSelect").addEventListener("change", () => { updateDesc(); toast("Model", $("#modelSelect").value); });
  $$("#modelBackends .bchip").forEach((c) => c.addEventListener("click", () => {
    if (c.classList.contains("disabled")) return;
    $$("#modelBackends .bchip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    toast("Backend", c.dataset.backend);
  }));
  $("#srcLang").addEventListener("change", () => toast("Source", helpers.lang($("#srcLang").value).name));
  $("#tgtLang").addEventListener("change", () => toast("Target", helpers.lang($("#tgtLang").value).name));
  updateDesc();
}

// ---------------------------------------------------------------------------
// PROFILE CARDS (speakers view) — dual lang + transcribe-as switch
// ---------------------------------------------------------------------------
function renderProfileCards() {
  const root = $("#profCards");
  root.innerHTML = "";
  for (const s of SPEAKERS) {
    const card = el("div", `prof ${s.isRoom ? "room" : ""}`);
    card.style.borderTopColor = SPK_VAR(s.spk);

    const secLang = s.secondaryLang
      ? `<span class="ll">SEC</span>${langChip(s.secondaryLang, "sec")}`
      : `<span class="ll">SEC</span><span class="dim" style="font-size:11px">none</span>`;

    // transcribe-as switch: options are this speaker's languages
    const switchLangs = ["en", "nb", "da"];
    const curLang = s.primaryLang;
    const segBtns = switchLangs.map((c) =>
      `<button data-as="${c}" class="${c === curLang ? "on" : ""}">${helpers.lang(c).flag} ${c.toUpperCase()}</button>`).join("");

    const gatePct = Math.round(s.gateThreshold * 100);
    const diarNote = s.isRoom
      ? `<p class="prof-diar-note">⚠ shared room mic → diarized into ${s.diarizedInto.map((d) => d.label).join(" + ")}</p>`
      : "";

    card.innerHTML = `
      <div class="prof-head">
        <span class="spk-chip" style="background:${SPK_VAR(s.spk)};width:30px;height:30px;font-size:11px">${s.initials}</span>
        <div>
          <div class="prof-name">${esc(s.name)}</div>
          <div class="prof-note">${esc(s.note)}</div>
        </div>
        <span class="prof-mic"><span class="mic-glyph">▣</span>${esc(s.mic.label)}</span>
      </div>
      ${diarNote}
      <div class="prof-langs">
        <span class="ll">PRI</span>${langChip(s.primaryLang, "pri")}
        ${secLang}
        <span class="xlate-switch"><span class="xl-lbl">TRANSCRIBE&nbsp;AS</span><span class="seg" data-spk="${s.id}">${segBtns}</span></span>
      </div>
      <div class="prof-grid">
        <div class="metric accent"><div class="mk">Gate threshold</div><div class="mv">${s.gateThreshold.toFixed(2)}</div><div class="gate-meter"><span style="width:${gatePct}%"></span></div></div>
        <div class="metric"><div class="mk">Noise floor</div><div class="mv">${s.noiseFloorDb}<span class="u"> dBFS</span></div></div>
        <div class="metric"><div class="mk">Mic profile</div><div class="mv" style="font-size:12px;color:var(--ink-2)">${esc(s.mic.id)}</div></div>
      </div>
      <div class="prof-foot">
        <span>profile reused across <span class="seen">${s.sessionsSeen} sessions</span></span>
        <span class="dim">· keyed by mic, not name</span>
      </div>`;
    root.appendChild(card);
  }

  // wire transcribe-as switches
  $$('#profCards .seg').forEach((seg) => {
    seg.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      $$("button", seg).forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      const sp = speakerById(seg.dataset.spk);
      toast("Transcribe as", `${sp.name} → ${helpers.lang(btn.dataset.as).name}`);
    });
  });
}

// ---------------------------------------------------------------------------
// MERGED TRANSCRIPT
// ---------------------------------------------------------------------------
function renderTranscript() {
  // header meta + badges
  $("#txHeadMeta").textContent = `/ ${TRANSCRIPT.model} · ${TRANSCRIPT.backend} · ${helpers.clock(TRANSCRIPT.durationS)}`;
  $("#txBadges").innerHTML = `
    ${TRANSCRIPT.translated ? `<span class="tag xlate">translated nb→en</span>` : ""}
    <span class="badge">${TRANSCRIPT.lines.length} lines</span>`;

  // lines
  const body = $("#txBody");
  body.innerHTML = "";
  for (const ln of TRANSCRIPT.lines) {
    const l = helpers.lang(ln.lang);
    const sp = SPEAKERS.find((s) => s.name === ln.speaker || ln.speaker.startsWith("Oslo Room"));
    const initials = ln.speaker.includes("Speaker A") ? "A"
      : ln.speaker.includes("Speaker B") ? "B"
      : (sp?.initials ?? ln.speaker.slice(0, 2).toUpperCase());
    let tags = "";
    if (ln.lowConfidence) tags += `<span class="tag low">low conf ${ln.confidence != null ? Math.round(ln.confidence * 100) + "%" : ""}</span>`;
    if (ln.translatedFrom) tags += `<span class="tag xlate">${ln.translatedFrom}→en</span>`;
    if (ln.suppressed) tags += `<span class="tag sup">suppressed · ${ln.matchedRule}</span>`;

    const line = el("div", `tx-line ${ln.lowConfidence ? "low" : ""} ${ln.suppressed ? "suppressed" : ""}`);
    line.innerHTML = `
      <span class="tx-t">${helpers.clock(ln.t)}</span>
      <span class="tx-spk" style="color:${SPK_VAR(ln.spk)}">
        <span class="spk-chip" style="background:${SPK_VAR(ln.spk)}">${initials}</span>
        <span class="nm">${esc(ln.speaker)}</span></span>
      <span class="tx-txt"><span class="fl" title="${l.name}">${l.flag}</span>${esc(ln.text)}${tags}</span>`;
    body.appendChild(line);
  }

  // speaking-time segmented bar
  const segs = TRANSCRIPT.speakingTime.map((st) =>
    `<span style="width:${st.pct}%;background:${SPK_VAR(st.spk)};box-shadow:inset 0 0 10px rgba(255,255,255,0.1)" title="${esc(st.speaker)} ${st.pct}%"></span>`).join("");
  const legend = TRANSCRIPT.speakingTime.map((st) =>
    `<div class="sl-row"><span class="sl-sw" style="background:${SPK_VAR(st.spk)}"></span><span class="sl-name">${esc(st.speaker)}</span><span class="sl-pct">${st.pct}%</span></div>`).join("");
  $("#txSpeak").innerHTML = `<div class="seg-bar">${segs}</div><div class="speak-legend">${legend}</div>`;

  // per-WAV list: originals + stripped clips (use the strip regions for the rep WAV)
  renderTxWavs();
}

function renderTxWavs() {
  const res = computeRegions(REP_WAV.peaks, REP_WAV.durationS, STRIP_DEFAULTS);
  const root = $("#txWavs");
  // build a small set of "per-WAV" entries: the rep WAV with its clips, plus
  // two more plausible originals to convey a per-WAV list.
  const wavs = [
    { name: REP_WAV.name, speaker: "Atle Håvsø", spk: 0, regions: res.regions, durationS: REP_WAV.durationS },
    { name: "2026-05-28T09-11-40Z_mette_mette_e5f6a7b8.wav", speaker: "Mette Sørensen", spk: 1, regions: [{ startS: 0.4, endS: 14.2 }, { startS: 18.0, endS: 31.6 }], durationS: 34 },
    { name: "2026-05-28T09-18-02Z_room-oslo_speakerB_c9d0e1f2.wav", speaker: "Oslo Room · Speaker B", spk: 4, regions: [{ startS: 1.1, endS: 22.8 }], durationS: 24 },
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
      <div class="wav-name"><span class="spk-chip" style="background:${SPK_VAR(w.spk)};width:16px;height:16px;font-size:7.5px">${w.spk}</span>
        <span class="mono">${esc(w.name.slice(11))}</span>
        <span class="tag xlate" style="border-color:var(--green-d);background:var(--green-d);color:var(--green)">${w.regions.length} clips</span></div>
      <div class="wav-orig">original ${w.durationS}s · stripped to ${speech.toFixed(1)}s speech</div>
      <div class="clip-list">${clips}</div>`;
    root.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// VIEW SWITCHER
// ---------------------------------------------------------------------------
const VIEWS = ["board", "strip", "speakers", "transcript"];
let currentView = "board";
function setView(name) {
  if (!VIEWS.includes(name)) name = "board";
  currentView = name;
  for (const v of VIEWS) {
    const node = $(`#view-${v}`);
    if (node) node.hidden = v !== name;
  }
  $$("#botnav button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  // re-fit canvases that depend on layout width
  requestAnimationFrame(() => {
    if (name === "strip" && redrawStripBig) redrawStripBig();
    if (name === "board" && redrawStripMini) redrawStripMini();
  });
}

// ---------------------------------------------------------------------------
// ⌘K COMMAND PALETTE
// ---------------------------------------------------------------------------
const COMMANDS = [
  { group: "Navigate", title: "Open Board", sub: "live-ops overview grid", glyph: "▤", kbd: ["1"], run: () => setView("board") },
  { group: "Navigate", title: "Open Strip + Model", sub: "cut preview · engine", glyph: "▦", kbd: ["2"], run: () => setView("strip") },
  { group: "Navigate", title: "Open Speaker Profiles", sub: "per-mic · diarization", glyph: "▣", kbd: ["3"], run: () => setView("speakers") },
  { group: "Navigate", title: "Open Nordic Sync transcript", sub: "merged · 48 min", glyph: "▥", kbd: ["4"], run: () => setView("transcript") },
  { group: "Tap control", title: "Start tap", sub: "arm a new recorder stream", glyph: "●", kbd: ["T"], run: () => toast("Tap", "armed — waiting for /tap WS") },
  { group: "Tap control", title: "Pause James' recording", sub: "MacBook built-in", glyph: "⏸", run: () => toast("Recording", "James Park — paused") },
  { group: "Tap control", title: "Stop all live taps", sub: "drain + flush buffers", glyph: "■", run: () => toast("Taps", "stopping 4 live taps") },
  { group: "Pipeline", title: "Strip silence — Atle's WAV", sub: `${REP_WAV.durationS}s · re-cut preview`, glyph: "✂", run: () => { setView("strip"); toast("Strip silence", "recomputing regions"); } },
  { group: "Pipeline", title: "Transcribe Nordic Sync", sub: "canary-1b-v2 · mlx", glyph: "↻", run: () => toast("Transcribe", "Nordic Sync queued on mlx") },
  { group: "Pipeline", title: "Diarize Oslo Conference Room", sub: "split shared mic → 2 voices", glyph: "⌥", run: () => { setView("speakers"); toast("Diarize", "Oslo Room → Speaker A + B"); } },
  { group: "Language", title: "Set Atle → English", sub: "transcribe Atle's mic as en", glyph: "🇬🇧", run: () => toast("Transcribe as", "Atle Håvsø → English") },
  { group: "Language", title: "Set Mette → Danish", sub: "transcribe Mette's mic as da", glyph: "🇩🇰", run: () => toast("Transcribe as", "Mette Sørensen → Danish") },
  { group: "Language", title: "Canary source nb → target en", sub: "translate Norwegian to English", glyph: "→", run: () => { setView("strip"); toast("Canary", "nb → en translation"); } },
];

const pal = {
  open: false,
  filtered: COMMANDS,
  active: 0,
};
function openPalette() {
  pal.open = true;
  $("#palette").hidden = false;
  $("#palInput").value = "";
  filterPalette("");
  $("#palInput").focus();
}
function closePalette() {
  pal.open = false;
  $("#palette").hidden = true;
}
function filterPalette(q) {
  q = q.trim().toLowerCase();
  pal.filtered = COMMANDS.filter((c) =>
    !q || (c.title + " " + c.sub + " " + c.group).toLowerCase().includes(q));
  pal.active = 0;
  renderPaletteList();
}
function renderPaletteList() {
  const list = $("#palList");
  list.innerHTML = "";
  if (!pal.filtered.length) {
    list.innerHTML = `<div class="pal-item"><span class="pi-text"><span class="pi-title dim">No matching commands</span></span></div>`;
    return;
  }
  let lastGroup = null;
  pal.filtered.forEach((c, i) => {
    if (c.group !== lastGroup) {
      list.appendChild(el("div", "pal-group", c.group));
      lastGroup = c.group;
    }
    const item = el("div", `pal-item ${i === pal.active ? "active" : ""}`);
    const kbd = (c.kbd || []).map((k) => `<kbd>${k}</kbd>`).join("");
    item.innerHTML = `
      <span class="pi-glyph">${c.glyph}</span>
      <span class="pi-text"><span class="pi-title">${esc(c.title)}</span><span class="pi-sub">${esc(c.sub)}</span></span>
      <span class="pi-kbd">${kbd || "<span>↵</span>"}</span>`;
    item.addEventListener("mouseenter", () => { pal.active = i; renderPaletteList(); });
    item.addEventListener("click", () => runActive(i));
    list.appendChild(item);
  });
  const activeNode = list.querySelector(".pal-item.active");
  if (activeNode) activeNode.scrollIntoView({ block: "nearest" });
}
function runActive(idx) {
  const c = pal.filtered[idx ?? pal.active];
  if (!c) return;
  closePalette();
  c.run();
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
// KEYS + WIRING
// ---------------------------------------------------------------------------
function wireKeys() {
  document.addEventListener("keydown", (e) => {
    // ⌘K / Ctrl-K
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      pal.open ? closePalette() : openPalette();
      return;
    }
    if (pal.open) {
      if (e.key === "Escape") { e.preventDefault(); closePalette(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); pal.active = Math.min(pal.filtered.length - 1, pal.active + 1); renderPaletteList(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); pal.active = Math.max(0, pal.active - 1); renderPaletteList(); }
      else if (e.key === "Enter") { e.preventDefault(); runActive(); }
      return;
    }
    // view hotkeys 1-4 (ignore when typing in a field)
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    if (["1", "2", "3", "4"].includes(e.key)) setView(VIEWS[+e.key - 1]);
    if (e.key.toLowerCase() === "t") toast("Tap", "armed — waiting for /tap WS");
  });

  $("#cmdkBtn").addEventListener("click", openPalette);
  $("#palInput").addEventListener("input", (e) => filterPalette(e.target.value));
  $("#palette").addEventListener("click", (e) => { if (e.target.id === "palette") closePalette(); });

  $$("#botnav button[data-view]").forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.view)));

  // backend chips in the topbar are decorative-ish but clickable
  $("#topBackends").addEventListener("click", (e) => {
    const chip = e.target.closest(".bchip");
    if (!chip || chip.classList.contains("disabled")) return;
    toast("Backend", chip.textContent.trim());
  });
}

// ---------------------------------------------------------------------------
// NAV HOOK for screenshots
// ---------------------------------------------------------------------------
window.gotoView = (name) => {
  if (name === "palette") { setView("board"); openPalette(); return; }
  closePalette();
  setView(name);
};

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
  wireSessionSort();
  renderSpeakerTable();
  renderDiarMini();
  renderDiarFull();
  renderStrip();
  renderModelPanel();
  renderProfileCards();
  renderTranscript();
  wireKeys();
  setView("board");
  $("#app").setAttribute("aria-busy", "false");

  // redraw canvases on resize (keeps the waveform crisp)
  let rt;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => {
      if (redrawStripMini) redrawStripMini();
      if (redrawStripBig) redrawStripBig();
    }, 120);
  });
}

boot();
