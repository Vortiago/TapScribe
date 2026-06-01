// ============================================================================
// TapScribe — STUDIO prototype (DAW timeline workspace)
// All data from the shared canonical fixture. Waveforms drawn by hand on
// <canvas>. Strip-silence knobs re-run computeRegions() and re-draw the cut
// markers live (the marquee interaction).
// ============================================================================
import {
  MOCK, LANGS, SPEAKERS, MODELS, selectedModel,
  LIVE_TAPS, LIVE_CAPTIONS, SESSIONS, STRIP_DEFAULTS, REP_WAV, TRANSCRIPT,
  computeRegions, helpers, speakerById,
} from "../_shared/mock-data.js";

const SPK = ["--spk0", "--spk1", "--spk2", "--spk3", "--spk4"];
const SPK_SOFT = ["--spk0-soft", "--spk1-soft", "--spk2-soft", "--spk3-soft", "--spk4-soft"];
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const spkColor = (i) => css(SPK[((i % 5) + 5) % 5]);
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };

// ----------------------------------------------------------------------------
// Mutable UI state
// ----------------------------------------------------------------------------
const state = {
  view: "overview",
  playhead: 842,            // 0:14:02 into a 48-min session
  playing: false,
  zoom: 0.62,
  focusTap: "atle",         // which track carries the strip-silence overlay
  openTaps: new Set(["room-oslo"]), // Oslo Room expanded to show diarization
  knobs: { ...STRIP_DEFAULTS },
  backend: selectedModel.backend,
  family: selectedModel.family,
  model: selectedModel.model,
  sourceLang: selectedModel.sourceLang,
  targetLang: selectedModel.targetLang,
  speakerLang: {            // chosen "transcribe as" per speaker (defaults to primary)
    atle: "nb", mette: "da", "room-oslo": "nb", james: "en",
  },
};

const SESSION = SESSIONS.find((s) => s.current) || SESSIONS[0];

// Derive a per-track shaped peak set from REP_WAV so each lane looks distinct
// but stays time-aligned. Atle (focus) uses raw peaks.
function shapePeaks(base, { gain = 1, bias = 0, phase = 0, bursts = null }) {
  return base.map((p, i) => {
    let v = p * gain + bias * (0.4 + 0.6 * Math.abs(Math.sin(i * 0.21 + phase)));
    if (bursts) { // only keep energy inside given [s,e] windows (in peak index)
      const inside = bursts.some(([a, b]) => i >= a && i <= b);
      if (!inside) v *= 0.05;
    }
    return Math.max(0, Math.min(1, v));
  });
}
const PPS = REP_WAV.peaksPerS;
const TRACK_PEAKS = {
  atle: REP_WAV.peaks,
  // Oslo room: two diarized speakers occupy different time windows
  "room-oslo": shapePeaks(REP_WAV.peaks, { gain: 0.62, bias: 0.04, phase: 1.1 }),
  // Mette: quieter, a couple of bursts
  mette: shapePeaks(REP_WAV.peaks, { gain: 0.5, phase: 2.3, bursts: [[200, 360], [560, 700]] }),
  // James: paused/near silent (one short blip)
  james: shapePeaks(REP_WAV.peaks, { gain: 0.32, phase: 0.4, bursts: [[300, 360]] }),
};
// Diarization timeline for Oslo room (alternating A/B speech windows, in seconds)
const OSLO_DIA = [
  { sub: 0, startS: 1.4, endS: 9.2 },   // Speaker A (nb)
  { sub: 1, startS: 12.3, endS: 21.8 }, // Speaker B (en)
  { sub: 0, startS: 23.8, endS: 32.8 }, // Speaker A
  { sub: 1, startS: 37.3, endS: 46.3 }, // Speaker B
];

const TAP_ORDER = ["atle", "room-oslo", "mette", "james"];
const tapByIdentity = (id) => LIVE_TAPS.find((t) => t.identity === id);

// ----------------------------------------------------------------------------
// Canvas waveform drawing
// ----------------------------------------------------------------------------
function fitCanvas(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const r = canvas.getBoundingClientRect();
  const w = Math.max(2, Math.floor(r.width));
  const h = Math.max(2, Math.floor(r.height));
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

// vertical time grid behind a lane
function drawGrid(ctx, w, h, durationS) {
  ctx.save();
  const minor = css("--grid"), major = css("--grid-strong");
  const step = 2; // seconds per minor line
  for (let t = 0; t <= durationS; t += step) {
    const x = (t / durationS) * w;
    ctx.strokeStyle = (t % 10 === 0) ? major : minor;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h); ctx.stroke();
  }
  ctx.restore();
}

// the waveform itself (mirrored fill), optionally dimming silent gaps and
// tinting region windows. regions/dimGaps used on the focused track.
function drawWave(canvas, peaks, durationS, opts = {}) {
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const mid = h / 2;
  drawGrid(ctx, w, h, durationS);

  const color = opts.color || css("--spk0");
  const softFill = opts.regions ? "rgba(255,255,255,0.04)" : null;

  // region tint bands (kept clips) drawn behind the wave
  if (opts.regions) {
    for (const r of opts.regions) {
      const x0 = (r.startS / durationS) * w;
      const x1 = (r.endS / durationS) * w;
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, hexA(color, 0.16));
      g.addColorStop(1, hexA(color, 0.04));
      ctx.fillStyle = g;
      ctx.fillRect(x0, 0, x1 - x0, h);
    }
  }
  // diarization alternating bands (Oslo)
  if (opts.diaBands) {
    for (const b of opts.diaBands) {
      const x0 = (b.startS / durationS) * w;
      const x1 = (b.endS / durationS) * w;
      ctx.fillStyle = hexA(b.color, 0.16);
      ctx.fillRect(x0, 0, x1 - x0, h);
      // top label strip
      ctx.fillStyle = hexA(b.color, 0.55);
      ctx.fillRect(x0, 0, x1 - x0, 3);
    }
  }

  // is a given seconds value inside a kept region?
  const inRegion = (t) => !opts.regions || opts.regions.some((r) => t >= r.startS && t <= r.endS);

  // bars
  const n = peaks.length;
  const bw = w / n;
  for (let i = 0; i < n; i++) {
    const t = (i / n) * durationS;
    const amp = Math.pow(peaks[i], 0.72); // perceptual lift
    const barH = Math.max(0.6, amp * (h * 0.46));
    const x = i * bw;
    let fill;
    if (opts.diaBands) {
      // colour bar by which diarized band it lands in
      const band = opts.diaBands.find((b) => t >= b.startS && t <= b.endS);
      fill = band ? band.color : css("--ink-3");
      ctx.globalAlpha = band ? 0.95 : 0.4;
    } else if (opts.regions && !inRegion(t)) {
      fill = css("--ink-3"); ctx.globalAlpha = 0.42;     // dimmed silence
    } else {
      fill = color; ctx.globalAlpha = 1;
    }
    ctx.fillStyle = fill;
    ctx.fillRect(x, mid - barH, Math.max(0.7, bw * 0.82), barH);
    ctx.fillRect(x, mid, Math.max(0.7, bw * 0.82), barH * 0.82);
  }
  ctx.globalAlpha = 1;

  // cut markers at region boundaries (focused track)
  if (opts.regions && opts.cuts) {
    for (const r of opts.regions) {
      for (const xs of [r.startS, r.endS]) {
        const x = (xs / durationS) * w;
        ctx.strokeStyle = css("--cut");
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
        // little scissors notch
        ctx.fillStyle = css("--cut");
        ctx.beginPath();
        ctx.moveTo(x - 3, 0); ctx.lineTo(x + 3, 0); ctx.lineTo(x, 5); ctx.closePath(); ctx.fill();
      }
    }
  }
  if (softFill) { /* reserved */ }
}

function hexA(hex, a) {
  hex = hex.replace("#", "");
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

// time ruler ticks
function drawRuler() {
  const canvas = $("#rulerCanvas");
  if (!canvas) return;
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const dur = SESSION.durationS;
  const labelEvery = 240; // seconds
  const minorEvery = 60;
  ctx.font = "10px ui-monospace, monospace";
  ctx.textBaseline = "alphabetic";
  for (let t = 0; t <= dur; t += minorEvery) {
    const x = (t / dur) * w;
    const major = t % labelEvery === 0;
    ctx.strokeStyle = major ? css("--ink-3") : css("--line");
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, major ? h - 13 : h - 7);
    ctx.lineTo(x + 0.5, h);
    ctx.stroke();
    if (major) {
      ctx.fillStyle = css("--ink-2");
      const label = helpers.clockH(t);
      if (x < w - 28) ctx.fillText(label, x + 4, h - 16);
    }
  }
  // playhead marker on the ruler
  const px = (state.playhead / dur) * w;
  ctx.fillStyle = css("--amber");
  ctx.beginPath();
  ctx.moveTo(px - 5, 2); ctx.lineTo(px + 5, 2); ctx.lineTo(px, 10); ctx.closePath(); ctx.fill();
}

// ----------------------------------------------------------------------------
// Tracks (headers + lanes)
// ----------------------------------------------------------------------------
function gateDial(value, color) {
  // small radial gauge 0..1 (270° sweep) as inline SVG
  const a0 = 135, a1 = 135 + 270 * value;
  const r = 10, cx = 13, cy = 13;
  const pt = (deg) => [cx + r * Math.cos(deg * Math.PI / 180), cy + r * Math.sin(deg * Math.PI / 180)];
  const arc = (s, e, col, wdt) => {
    const [x0, y0] = pt(s), [x1, y1] = pt(e);
    const large = (e - s) > 180 ? 1 : 0;
    return `<path d="M${x0.toFixed(1)} ${y0.toFixed(1)} A${r} ${r} 0 ${large} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}" fill="none" stroke="${col}" stroke-width="${wdt}" stroke-linecap="round"/>`;
  };
  const [hx, hy] = pt(a1);
  return `<svg viewBox="0 0 26 26" class="knobmini__dial">
    ${arc(135, 135 + 270, "#2a3142", 3)}
    ${arc(a0, a1, color, 3)}
    <circle cx="13" cy="13" r="6.5" fill="#0a0c11" stroke="${css("--line")}"/>
    <line x1="13" y1="13" x2="${hx.toFixed(1)}" y2="${hy.toFixed(1)}" stroke="${color}" stroke-width="1.6" stroke-linecap="round"/>
  </svg>`;
}

function langControl(sp) {
  const prim = LANGS[sp.primaryLang];
  const sec = sp.secondaryLang ? LANGS[sp.secondaryLang] : null;
  const chosen = state.speakerLang[sp.id];
  const btn = (lng, role) => `
    <button class="langbtn ${chosen === lng.code ? "is-active" : ""}" data-lang-pick="${sp.id}:${lng.code}">
      <span class="flag">${lng.flag}</span>${lng.name.slice(0, 2) === "No" ? "Norwegian" : lng.name}<small>${role}</small>
    </button>`;
  return `<div class="langctl">
    ${btn(prim, "primary")}
    ${sec ? btn(sec, "2nd") : ""}
  </div>`;
}

function buildTracks() {
  const root = $("#tracks");
  root.innerHTML = "";
  for (const id of TAP_ORDER) {
    const sp = speakerById(id);
    const tap = tapByIdentity(id);
    const color = spkColor(sp.spk);
    const soft = css(SPK_SOFT[((sp.spk % 5) + 5) % 5]);
    const isFocus = state.focusTap === id;
    const isOpen = state.openTaps.has(id);

    const track = el("div", "track" + (isFocus ? " is-focused" : "") + (isOpen ? " is-open" : ""));
    track.style.setProperty("--spk-c", color);
    track.style.setProperty("--spk-soft", soft);
    track.dataset.tap = id;

    // ---- header ----
    const dbNow = tap.level > 0 ? (20 * Math.log10(tap.level)).toFixed(0) : "−∞";
    const muted = !tap.gateOpen || tap.level === 0;
    const head = el("div", "thead");
    head.innerHTML = `
      <div class="thead__top">
        <span class="chip-spk">${sp.initials}</span>
        <div class="thead__id">
          <div class="thead__name">${sp.name}</div>
          <div class="thead__mic">${sp.mic.label}${sp.isRoom ? " · shared room" : ""}</div>
        </div>
        ${sp.isRoom ? `<button class="thead__caret" data-toggle="${id}" aria-label="Toggle diarization">▶</button>` : ""}
      </div>
      <div class="meter">
        <div class="meter__bar"><div class="meter__fill ${muted ? "is-muted" : ""}" style="width:${Math.round(tap.level * 100)}%"></div></div>
        <span class="meter__db">${dbNow} dB</span>
      </div>
      <div class="thead__controls">
        <span class="tgl ${tap.record ? "is-on" : ""}" data-k="rec" data-tap="${id}">REC</span>
        <span class="tgl ${tap.live ? "is-on" : ""}" data-k="live" data-tap="${id}">LIVE</span>
        <span class="knobmini" title="Gate threshold">
          ${gateDial(sp.gateThreshold, color)}
          <span class="knobmini__lbl">gate<br><b>${sp.gateThreshold.toFixed(2)}</b> · ${sp.noiseFloorDb}dB</span>
        </span>
      </div>
      ${langControl(sp)}
      ${sp.isRoom ? buildSublaneHeaders(sp) : ""}
    `;

    // ---- lane ----
    const lane = el("div", "lane");
    const canvas = el("canvas");
    lane.appendChild(canvas);
    lane.appendChild(el("div", "lane__center"));
    if (isFocus && !sp.isRoom) {
      const reg = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
      lane.appendChild(el("div", "lane__tag",
        `<span class="sp">◈ focus</span> <span class="cuts">${reg.clips} clips</span> · ${reg.speechS}s / ${reg.totalS}s`));
    } else if (sp.isRoom) {
      const a = LANGS[sp.diarizedInto[0].lang], b = LANGS[sp.diarizedInto[1].lang];
      lane.appendChild(el("div", "lane__tag",
        `<span style="color:${spkColor(sp.diarizedInto[0].spk)}">◆ Speaker A ${a.flag}</span> <span style="color:${spkColor(sp.diarizedInto[1].spk)}">◆ Speaker B ${b.flag}</span> · diarized`));
    }

    track.appendChild(head);
    track.appendChild(lane);
    root.appendChild(track);
  }
  // playhead overlay (single element spanning the lane column)
  drawAllLanes();
  positionPlayhead();
}

function buildSublaneHeaders(sp) {
  return `<div class="sublanes">
    <div class="subhead__cap" style="font-size:10px;color:var(--ink-2);text-transform:uppercase;letter-spacing:.06em;">Diarized · 2 speakers</div>
    ${sp.diarizedInto.map((d) => {
      const c = spkColor(d.spk);
      const lng = LANGS[d.lang];
      return `<div class="subhead">
        <span class="subdot" style="--c:${c}"></span>
        <span class="subhead__n">${d.label}</span>
        <span class="subhead__lang">${lng.flag} ${lng.name}</span>
        <span class="subhead__talk">${d.talkPct}%</span>
      </div>`;
    }).join("")}
  </div>`;
}

function drawAllLanes() {
  const tracks = document.querySelectorAll(".track");
  tracks.forEach((track) => {
    const id = track.dataset.tap;
    const sp = speakerById(id);
    const lane = $(".lane", track);
    const canvas = $("canvas", lane);
    if (!canvas) return;
    const dur = REP_WAV.durationS;
    const color = spkColor(sp.spk);

    if (sp.isRoom) {
      // a shared room ALWAYS renders as diarized A/B bands — that's its identity
      const bands = OSLO_DIA.map((b) => ({
        ...b, color: spkColor(sp.diarizedInto[b.sub].spk),
      }));
      drawWave(canvas, TRACK_PEAKS[id], dur, { color, diaBands: bands });
    } else if (id === state.focusTap) {
      // the focused per-person track carries the strip-silence cut overlay
      const reg = computeRegions(REP_WAV.peaks, dur, state.knobs);
      drawWave(canvas, TRACK_PEAKS[id], dur, { color, regions: reg.regions, cuts: true });
    } else {
      drawWave(canvas, TRACK_PEAKS[id], dur, { color });
    }
  });
}

function positionPlayhead() {
  // single shared playhead element placed over the lane column
  let ph = $("#tracks .playhead");
  if (!ph) {
    ph = el("div", "playhead");
    ph.id = "globalPlayhead";
  }
  const firstLane = $(".track .lane");
  if (!firstLane) return;
  // place inside the tracks container, aligned to lane column
  const tracksBox = $("#tracks");
  if (!tracksBox.contains(ph)) tracksBox.appendChild(ph);
  const rail = parseInt(css("--rail-w")) || 268;
  const frac = state.playhead / SESSION.durationS;
  const laneW = tracksBox.clientWidth - rail;
  ph.style.left = `${rail + frac * laneW}px`;
}

// ----------------------------------------------------------------------------
// Live captions log
// ----------------------------------------------------------------------------
function buildLiveLog() {
  const body = $("#liveBody");
  body.innerHTML = "";
  for (const c of LIVE_CAPTIONS) {
    const lng = LANGS[c.lang];
    const color = spkColor(c.spk);
    const row = el("div", "cap" + (c.inflight ? " cap--inflight" : ""));
    row.innerHTML = `
      <span class="cap__t">${helpers.clock(c.t)}</span>
      <span class="cap__who">
        <span class="cap__swatch" style="--c:${color}"></span>
        <span class="cap__name">${c.speaker}</span>
        <span class="cap__flag" title="${lng.name}">${lng.flag}</span>
      </span>
      <span class="cap__txt">${c.text}${c.inflight ? '<span class="cap__live">LIVE</span>' : ""}</span>`;
    body.appendChild(row);
  }
  const liveCount = LIVE_TAPS.filter((t) => t.live && t.gateOpen).length;
  $("#liveStatus").textContent = `${liveCount} taps live · ${state.model} → ${LANGS[state.targetLang].name}`;
}

// ----------------------------------------------------------------------------
// Inspector (context-sensitive)
// ----------------------------------------------------------------------------
function buildInspector() {
  const root = $("#inspector");
  const reg = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
  const keptPct = Math.round((reg.speechS / reg.totalS) * 100);
  const focusSp = speakerById(state.focusTap);

  // top section: diarization panel for a room, else strip-silence knobs
  const strip = el("section", "insp-sec");
  if (focusSp.isRoom) {
    strip.innerHTML = diarizationMarkup(focusSp);
  } else {
    strip.innerHTML = `
      <div class="insp-sec__h">Strip-silence <span class="badge">${focusSp.name} · ${focusSp.mic.label}</span></div>
      ${knobMarkup("Silence gap", "gap", state.knobs.minSilenceMs, "ms", 100, 4000, 50, "Merge clips when the pause between them is shorter than this.")}
      ${knobMarkup("Edge pad", "pad", state.knobs.padMs, "ms", 0, 800, 25, "Keep this much audio on each side of a clip.")}
      ${knobMarkup("Speech floor", "floor", state.knobs.speechFloorDb, "dB", -60, -25, 1, "Anything quieter than this reads as silence.")}
      <div class="readout">
        <div class="readout__big"><span class="cuts">${reg.clips}</span> clips · ${reg.speechS}<span style="color:var(--ink-3)">s</span></div>
        <div class="readout__sub"><span class="keep">${reg.speechS}s kept</span> of ${reg.totalS}s — ${keptPct}% speech</div>
        <div class="readout__bar" id="readoutBar"></div>
      </div>`;
  }

  // model / backend section
  const model = el("section", "insp-sec");
  const fam = MODELS.find((m) => m.family === state.family);
  const md = fam.models.find((m) => m.id === state.model) || fam.models[0];
  model.innerHTML = `
    <div class="insp-sec__h">Engine</div>
    <div class="field__lbl">Backend</div>
    <div class="chips" id="backendChips">
      ${MOCK.APP.backends.map((b) => `
        <span class="chip ${b.kind === state.backend ? "is-active" : ""} ${b.available ? "" : "is-disabled"}"
              data-backend="${b.kind}" ${b.available ? "" : "aria-disabled=\"true\""}>${b.label}</span>`).join("")}
    </div>
    <div class="field">
      <label class="field__lbl">Model</label>
      <select class="selectish" id="modelSelect">
        ${MODELS.map((m) => `<optgroup label="${m.family}">${m.models.map((mm) =>
          `<option value="${m.family}:${mm.id}" ${mm.id === state.model ? "selected" : ""}>${mm.display}</option>`).join("")}</optgroup>`).join("")}
      </select>
    </div>
    <div class="model-card">
      <div class="model-card__top">
        <span class="model-card__fam">${state.family}</span>
        <span class="model-card__id">${md.display}</span>
      </div>
      <div class="model-card__desc">${md.desc}</div>
      <div class="model-card__langs">${(md.langs || []).map((c) => `<span class="flag" title="${LANGS[c].name}">${LANGS[c].flag}</span>`).join("")}</div>
    </div>
    ${state.family === "canary" ? canaryMarkup(md) : ""}`;

  // focused-speaker profile mini
  const prof = el("section", "insp-sec");
  prof.innerHTML = `
    <div class="insp-sec__h">Input profile <span class="badge">saved · ${focusSp.sessionsSeen} sessions</span></div>
    <div class="profcard" style="--spk-c:${spkColor(focusSp.spk)}">
      <div class="profcard__top">
        <span class="chip-spk" style="--spk-c:${spkColor(focusSp.spk)}">${focusSp.initials}</span>
        <div>
          <div class="profcard__name">${focusSp.name}</div>
          <div class="profcard__seen">${focusSp.mic.label} · reused across ${focusSp.sessionsSeen} sessions</div>
        </div>
      </div>
      <div class="profcard__grid">
        <div class="stat"><div class="stat__k">Gate threshold</div><div class="stat__v">${focusSp.gateThreshold.toFixed(2)}</div></div>
        <div class="stat"><div class="stat__k">Noise floor</div><div class="stat__v">${focusSp.noiseFloorDb} dB</div></div>
        <div class="stat"><div class="stat__k">Primary</div><div class="stat__v">${LANGS[focusSp.primaryLang].flag} ${focusSp.primaryLang}</div></div>
        <div class="stat"><div class="stat__k">Secondary</div><div class="stat__v">${focusSp.secondaryLang ? LANGS[focusSp.secondaryLang].flag + " " + focusSp.secondaryLang : "—"}</div></div>
      </div>
      <div class="profcard__note">${focusSp.note}</div>
    </div>`;

  root.innerHTML = "";
  root.appendChild(strip);
  root.appendChild(model);
  root.appendChild(prof);
  paintReadoutBar(reg);
}

function knobMarkup(label, key, val, unit, min, max, step, hint) {
  const display = unit === "dB" ? `${val} ${unit}` : `${val} ${unit}`;
  return `<div class="knob">
    <div class="knob__row"><span class="knob__lbl">${label}</span><span class="knob__val" id="kv-${key}">${display}</span></div>
    <input type="range" id="kn-${key}" min="${min}" max="${max}" step="${step}" value="${val}" data-knob="${key}">
    <div class="knob__hint">${hint}</div>
  </div>`;
}

function diarizationMarkup(sp) {
  const total = OSLO_DIA.reduce((a, b) => a + (b.endS - b.startS), 0);
  const turns = sp.diarizedInto.map((d, i) => {
    const c = spkColor(d.spk);
    const lng = LANGS[d.lang];
    const turnCount = OSLO_DIA.filter((b) => b.sub === i).length;
    return `<div class="profcard" style="--spk-c:${c};margin-bottom:10px">
      <div class="profcard__top">
        <span class="subdot" style="--c:${c};width:16px;height:16px;border-radius:5px"></span>
        <div>
          <div class="profcard__name" style="font-size:14px">${d.label}</div>
          <div class="profcard__seen">${lng.flag} ${lng.name} · ${turnCount} turns this session</div>
        </div>
        <span class="line__t" style="margin-left:auto;font-size:15px;color:${c};font-family:var(--mono);font-weight:700">${d.talkPct}%</span>
      </div>
    </div>`;
  }).join("");
  // talk split bar
  const splitBar = `<div class="readout__bar" style="margin-top:0">
    ${sp.diarizedInto.map((d) => `<div class="readout__seg" style="width:${d.talkPct}%;background:${spkColor(d.spk)}"></div>`).join("")}
  </div>`;
  return `
    <div class="insp-sec__h">Diarization <span class="badge">${sp.mic.label} · shared</span></div>
    <div style="font-size:11.5px;color:var(--ink-2);margin-bottom:13px;line-height:1.5">
      One physical tap, two voices. TapScribe split <b style="color:var(--ink-1)">${sp.name}</b> into
      ${sp.diarizedInto.length} speakers and routed each to its own language.
    </div>
    ${splitBar}
    <div style="margin-top:14px">${turns}</div>
    <div class="xlate-badge" style="color:var(--teal);border-color:rgba(45,212,191,.3);background:rgba(45,212,191,.07)">
      ◆ ${(total).toFixed(0)}s diarized speech across ${OSLO_DIA.length} turns
    </div>`;
}

function paintReadoutBar(reg) {
  const bar = $("#readoutBar");
  if (!bar) return;
  bar.innerHTML = "";
  const total = reg.totalS;
  for (const r of reg.regions) {
    const seg = el("div", "readout__seg");
    seg.style.width = `${((r.endS - r.startS) / total) * 100}%`;
    bar.appendChild(seg);
    const gap = el("div");
    gap.style.flex = "0 0 auto";
    bar.appendChild(gap);
  }
}

function canaryMarkup(md) {
  const opt = (sel) => Object.values(LANGS).filter((l) => l.code !== "auto").map((l) =>
    `<option value="${l.code}" ${sel === l.code ? "selected" : ""}>${l.flag} ${l.name}</option>`).join("");
  return `
    <div class="translate-row">
      <div>
        <label class="field__lbl">Source</label>
        <select class="selectish" id="srcLang">${opt(state.sourceLang)}</select>
      </div>
      <span class="arrow">→</span>
      <div>
        <label class="field__lbl">Target</label>
        <select class="selectish" id="tgtLang">${opt(state.targetLang)}</select>
      </div>
    </div>
    ${state.sourceLang !== state.targetLang
      ? `<div class="xlate-badge">↻ Translating ${LANGS[state.sourceLang].name} → ${LANGS[state.targetLang].name}</div>`
      : `<div class="xlate-badge" style="color:var(--ink-2);border-color:var(--line);background:var(--bg-rail)">Transcribing ${LANGS[state.sourceLang].name} (no translation)</div>`}`;
}

// ----------------------------------------------------------------------------
// Transcript view
// ----------------------------------------------------------------------------
function buildTranscript() {
  const root = $("#transcriptView");
  const t = TRANSCRIPT;

  const head = `
    <div class="tx-head">
      <div>
        <div class="tx-head__title">${SESSION.label} — merged transcript</div>
        <div class="tx-head__sub">${SESSION.folder} · ${helpers.clockH(t.durationS)} · ${SESSION.wavCount} WAVs</div>
        <div class="tx-meta-chips">
          <span class="metachip">⚙ ${t.model}</span>
          <span class="metachip">${t.backend}</span>
          <span class="metachip">${SESSION.langs.map((c) => LANGS[c].flag).join(" ")} ${SESSION.langs.join(" · ")}</span>
          ${t.translated ? `<span class="metachip metachip--xlate">↻ translated nb→en</span>` : ""}
        </div>
      </div>
    </div>`;

  // transcript lines
  const linesHtml = t.lines.map((ln) => {
    const c = spkColor(ln.spk);
    const soft = css(SPK_SOFT[((ln.spk % 5) + 5) % 5]);
    const lng = LANGS[ln.lang];
    const badges = [];
    if (ln.translatedFrom) badges.push(`<span class="lbadge lbadge--xlate">↻ ${ln.translatedFrom}→en</span>`);
    if (ln.lowConfidence) badges.push(`<span class="lbadge lbadge--lc">low conf ${Math.round((ln.confidence || 0) * 100)}%</span>`);
    if (ln.suppressed) badges.push(`<span class="lbadge lbadge--sup">⊘ suppressed · ${ln.matchedRule}</span>`);
    const cls = "line" + (ln.lowConfidence ? " line--low" : "") + (ln.suppressed ? " line--sup" : "");
    return `<div class="${cls}" style="--c:${c};--c-soft:${soft}">
      <span class="line__t">${helpers.clock(ln.t)}</span>
      <span class="line__who"><span class="line__sw" style="--c:${c}"></span><span class="line__name">${ln.speaker}</span><span class="line__flag">${lng.flag}</span></span>
      <span class="line__txt">${ln.text}<span class="line__badges">${badges.join("")}</span></span>
    </div>`;
  }).join("");

  // speaking-time bar
  const speaking = `
    <div class="speaking">
      <div class="speaking__h">Speaking time</div>
      <div class="speaking__bar">
        ${t.speakingTime.map((s) => `<div class="speaking__seg" style="width:${s.pct}%;background:${spkColor(s.spk)}"></div>`).join("")}
      </div>
      <div class="speaking__legend">
        ${t.speakingTime.map((s) => `<div class="legrow"><span class="sw" style="--c:${spkColor(s.spk)}"></span><span class="nm">${s.speaker}</span><span class="pc">${helpers.pct(s.pct)}</span></div>`).join("")}
      </div>
    </div>`;

  // per-WAV originals + stripped clips. Each WAV gets its own peaks (a masked
  // variant of REP_WAV) so the strip result differs per file: Atle 4, Mette 2,
  // room 3. Masked-out windows are driven well below the -45 dB floor.
  const maskPeaks = (gain, keep) => REP_WAV.peaks.map((p, i) => {
    const inside = keep.some(([a, b]) => i >= a && i <= b);
    return Math.max(0, Math.min(1, p * gain * (inside ? 1 : 0.004)));
  });
  const wavPeaks = [
    REP_WAV.peaks,                          // Atle — all 4 bursts
    maskPeaks(0.7, [[20, 150], [590, 740]]), // Mette — first + last → 2 clips
    maskPeaks(0.78, [[0, 540]]),             // room — first three → 3 clips
  ];
  const reg = computeRegions(wavPeaks[0], REP_WAV.durationS, STRIP_DEFAULTS);
  const wavBox = `
    <div class="wavbox">
      <div class="wavbox__h">Per-WAV · originals &amp; clips</div>
      <div class="wavbox__sub">${SESSION.wavCount} source files · strip-silence applied</div>
      <div class="wavrow" id="wavRows">
        ${perWavItem(REP_WAV, reg, 0)}
        ${perWavItem({ ...REP_WAV, name: "…_mette_b7e1.wav", speakerId: "mette", durationS: 31 }, computeRegions(wavPeaks[1], REP_WAV.durationS, STRIP_DEFAULTS), 1)}
        ${perWavItem({ ...REP_WAV, name: "…_room-oslo_c4f2.wav", speakerId: "room-oslo", durationS: 48 }, computeRegions(wavPeaks[2], REP_WAV.durationS, STRIP_DEFAULTS), 2)}
      </div>
    </div>`;
  // stash for the canvas pass below
  buildTranscript._wavPeaks = wavPeaks;

  root.innerHTML = `<div class="tx-wrap">
    ${head}
    <div class="tx-grid">
      <div class="lines">${linesHtml}</div>
      <div>${speaking}${wavBox}</div>
    </div>
  </div>`;

  // draw the per-wav mini waveforms (with their own clip overlays)
  const wp = buildTranscript._wavPeaks;
  root.querySelectorAll(".minibar canvas").forEach((cv) => {
    const idx = +cv.dataset.idx;
    const pk = wp[idx];
    const sp = speakerById([REP_WAV.speakerId, "mette", "room-oslo"][idx]);
    const r = computeRegions(pk, REP_WAV.durationS, STRIP_DEFAULTS);
    drawWave(cv, pk, REP_WAV.durationS, { color: spkColor(sp.spk), regions: r.regions, cuts: true });
  });
}

function perWavItem(wav, reg, idx) {
  return `<div class="wavitem">
    <div class="wavitem__top">
      <span class="wavitem__name" title="${wav.name}">${wav.name}</span>
      <span class="wavitem__dur">${helpers.clock(wav.durationS)}</span>
    </div>
    <div class="minibar"><canvas data-idx="${idx}"></canvas></div>
    <div class="wavitem__foot">
      <span class="cliptag">orig <b>${helpers.clock(wav.durationS)}</b></span>
      <span class="cliptag">→ <b>${reg.clips}</b> clip${reg.clips === 1 ? "" : "s"}</span>
      <span class="cliptag">speech <b>${reg.speechS}s</b></span>
    </div>
  </div>`;
}

// ----------------------------------------------------------------------------
// Profiles modal
// ----------------------------------------------------------------------------
function buildProfiles() {
  const grid = $("#presetGrid");
  grid.innerHTML = SPEAKERS.map((sp) => {
    const color = spkColor(sp.spk);
    const prim = LANGS[sp.primaryLang], sec = sp.secondaryLang ? LANGS[sp.secondaryLang] : null;
    const room = sp.isRoom ? `
      <div class="preset__room">
        <div class="preset__roomh">Diarized into</div>
        ${sp.diarizedInto.map((d) => `<div class="diarow">
          <span class="sw" style="--c:${spkColor(d.spk)}"></span>
          <span class="nm">${d.label}</span>
          <span class="fl">${LANGS[d.lang].flag}</span>
          <span class="talk">${d.talkPct}% talk</span>
        </div>`).join("")}
      </div>` : "";
    return `<div class="preset" style="--spk-c:${color}">
      <div class="preset__top">
        <span class="chip-spk" style="--spk-c:${color}">${sp.initials}</span>
        <div>
          <div class="preset__name">${sp.name}</div>
          <div class="preset__role">${sp.isRoom ? "Shared room mic" : "Personal mic"}</div>
        </div>
        <span class="preset__mic">${sp.mic.label}</span>
      </div>
      <div class="preset__langs">
        <span class="langpill is-primary"><span class="flag">${prim.flag}</span>${prim.name}<span class="role">primary</span></span>
        ${sec ? `<span class="langpill"><span class="flag">${sec.flag}</span>${sec.name}<span class="role">2nd</span></span>` : `<span class="langpill" style="opacity:.5"><span class="role">no 2nd lang</span></span>`}
      </div>
      <div class="preset__stats">
        <div class="stat"><div class="stat__k">Gate</div><div class="stat__v">${sp.gateThreshold.toFixed(2)}</div></div>
        <div class="stat"><div class="stat__k">Floor</div><div class="stat__v">${sp.noiseFloorDb}dB</div></div>
        <div class="stat"><div class="stat__k">Sessions</div><div class="stat__v">${sp.sessionsSeen}</div></div>
      </div>
      ${room}
      <div class="reuse">⟳ reused automatically when ${sp.mic.label} appears</div>
    </div>`;
  }).join("");
}

// ----------------------------------------------------------------------------
// Session dropdown
// ----------------------------------------------------------------------------
function buildSessionMenu() {
  let menu = $("#sessmenu");
  if (!menu) {
    menu = el("div", "sessmenu");
    menu.id = "sessmenu";
    menu.hidden = true;
    $("#app").appendChild(menu);
  }
  menu.innerHTML = `<div class="sessmenu__h">Sessions · ${SESSIONS.length}</div>` +
    SESSIONS.map((s) => {
      const label = s.label || "Untitled session";
      const d = new Date(s.startedAt);
      const date = d.toISOString().slice(0, 10);
      return `<div class="sessrow ${s.current ? "is-current" : ""}">
        <div class="sessrow__l">
          <div class="sessrow__nm">${label} ${s.current ? '<span class="live">LIVE</span>' : ""}</div>
          <div class="sessrow__sub">${date} · ${helpers.clockH(s.durationS)} · ${s.wavCount} WAVs ${s.hasTranscript ? "· ✓ tx" : "· no tx"}</div>
        </div>
        <div class="sessrow__r">
          <div class="sessrow__flags">${s.langs.map((c) => LANGS[c].flag).join(" ")}</div>
          ${s.speakers.length} spk
        </div>
      </div>`;
    }).join("");
}

// ----------------------------------------------------------------------------
// Events
// ----------------------------------------------------------------------------
function wireEvents() {
  // tabs
  $("#tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (tab) gotoView(tab.dataset.view);
  });

  // transport
  $("#btnPlay").addEventListener("click", () => {
    state.playing = !state.playing;
    $("#app").dataset.playing = String(state.playing);
  });
  $("#btnSkipBack").addEventListener("click", () => { state.playhead = 0; refreshTime(); });
  $("#btnSkipFwd").addEventListener("click", () => { state.playhead = SESSION.durationS; refreshTime(); });

  // zoom
  $("#zoomIn").addEventListener("click", () => setZoom(state.zoom + 0.12));
  $("#zoomOut").addEventListener("click", () => setZoom(state.zoom - 0.12));

  // scrub by clicking ruler
  $("#rulerCanvas").addEventListener("click", (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    state.playhead = Math.round(((e.clientX - r.left) / r.width) * SESSION.durationS);
    refreshTime();
  });

  // profiles modal
  $("#openProfiles").addEventListener("click", () => openProfiles(true));
  $("#modalClose").addEventListener("click", () => openProfiles(false));
  $("#modalScrim").addEventListener("click", () => openProfiles(false));

  // session menu toggle
  $("#sessionPicker").addEventListener("click", () => {
    const m = $("#sessmenu");
    if (m) m.hidden = !m.hidden;
  });

  // delegated clicks inside the stage (track headers)
  $("#stage").addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-toggle]");
    if (toggle) {
      const id = toggle.dataset.toggle;
      if (state.openTaps.has(id)) state.openTaps.delete(id); else state.openTaps.add(id);
      buildTracks();
      return;
    }
    const tgl = e.target.closest(".tgl");
    if (tgl) {
      const tap = tapByIdentity(tgl.dataset.tap);
      const k = tgl.dataset.k;
      tap[k] = !tap[k];
      tgl.classList.toggle("is-on");
      buildLiveLog();
      return;
    }
    const langPick = e.target.closest("[data-lang-pick]");
    if (langPick) {
      const [id, code] = langPick.dataset.langPick.split(":");
      state.speakerLang[id] = code;
      buildTracks();
      return;
    }
    // focus a track by clicking its lane
    const track = e.target.closest(".track");
    if (track && !e.target.closest(".thead")) {
      state.focusTap = track.dataset.tap;
      buildTracks();
      buildInspector();
    }
  });

  // inspector delegated (knobs are input events) — attach on the panel
  $("#inspector").addEventListener("input", onInspectorInput);
  $("#inspector").addEventListener("change", onInspectorChange);
  $("#inspector").addEventListener("click", onInspectorClick);

  window.addEventListener("resize", debounce(() => {
    drawRuler(); drawAllLanes(); positionPlayhead();
    if (state.view === "transcript") buildTranscript();
  }, 120));
}

function onInspectorInput(e) {
  const slider = e.target.closest("[data-knob]");
  if (!slider) return;
  const key = slider.dataset.knob;
  const v = +slider.value;
  if (key === "gap") { state.knobs.minSilenceMs = v; $("#kv-gap").textContent = `${v} ms`; }
  if (key === "pad") { state.knobs.padMs = v; $("#kv-pad").textContent = `${v} ms`; }
  if (key === "floor") { state.knobs.speechFloorDb = v; $("#kv-floor").textContent = `${v} dB`; }
  // live re-cut: recompute + redraw the focused lane + readout + tag
  const reg = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
  liveRecut(reg);
}

function liveRecut(reg) {
  // redraw focused lane cut markers
  drawAllLanes();
  positionPlayhead();
  // update readout numbers
  const big = $(".readout__big");
  if (big) big.innerHTML = `<span class="cuts">${reg.clips}</span> clips · ${reg.speechS}<span style="color:var(--ink-3)">s</span>`;
  const sub = $(".readout__sub");
  if (sub) sub.innerHTML = `<span class="keep">${reg.speechS}s kept</span> of ${reg.totalS}s — ${Math.round((reg.speechS / reg.totalS) * 100)}% speech`;
  paintReadoutBar(reg);
  // update the on-lane focus tag
  const tag = $(".track.is-focused .lane__tag");
  if (tag) tag.innerHTML = `<span class="sp">◈ focus</span> <span class="cuts">${reg.clips} clips</span> · ${reg.speechS}s / ${reg.totalS}s`;
}

function onInspectorChange(e) {
  if (e.target.id === "modelSelect") {
    const [fam, id] = e.target.value.split(":");
    state.family = fam; state.model = id;
    // sync canary lang defaults if switching into canary
    if (fam === "canary") {
      const m = MODELS.find((x) => x.family === "canary").models[0];
      state.sourceLang = state.sourceLang || m.inputs[0].default;
      state.targetLang = state.targetLang || m.inputs[1].default;
    }
    buildInspector();
    buildLiveLog();
  }
  if (e.target.id === "srcLang") { state.sourceLang = e.target.value; buildInspector(); }
  if (e.target.id === "tgtLang") { state.targetLang = e.target.value; buildInspector(); buildLiveLog(); }
}

function onInspectorClick(e) {
  const chip = e.target.closest(".chip[data-backend]");
  if (chip && !chip.classList.contains("is-disabled")) {
    state.backend = chip.dataset.backend;
    buildInspector();
  }
}

// ----------------------------------------------------------------------------
// Helpers / lifecycle
// ----------------------------------------------------------------------------
function refreshTime() {
  $("#playheadTc").textContent = helpers.clockH(state.playhead);
  drawRuler();
  positionPlayhead();
}
function setZoom(z) {
  state.zoom = Math.max(0.1, Math.min(1, z));
  $("#zoomFill").style.width = `${Math.round(state.zoom * 100)}%`;
}
function openProfiles(open) {
  $("#profilesModal").hidden = !open;
}
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function setActiveTab() {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === state.view));
}

// global navigation hook for screenshots
window.gotoView = function gotoView(name) {
  const valid = ["overview", "strip", "diarization", "profiles", "transcript"];
  if (!valid.includes(name)) name = "overview";
  state.view = name;
  setActiveTab();

  if (name === "profiles") {
    $("#app").dataset.view = "overview"; // keep timeline behind the modal
    openProfiles(true);
    return;
  }
  openProfiles(false);

  if (name === "strip") {
    // marquee: focus Atle and dial the gap up so two clips visibly MERGE —
    // proves the knob is driving the cut markers on the timeline live.
    state.focusTap = "atle";
    state.knobs = { minSilenceMs: 3200, padMs: 300, speechFloorDb: -45 };
    $("#app").dataset.view = "overview";
    buildTracks(); buildInspector();
    return;
  }
  if (name === "diarization") {
    state.focusTap = "room-oslo";
    state.openTaps.add("room-oslo");
    $("#app").dataset.view = "overview";
    buildTracks(); buildInspector();
    return;
  }
  if (name === "transcript") {
    $("#app").dataset.view = "transcript";
    buildTranscript();
    return;
  }
  // overview
  $("#app").dataset.view = "overview";
  state.focusTap = "atle";
  state.knobs = { ...STRIP_DEFAULTS };
  buildTracks(); buildInspector();
};

function init() {
  $("#sessionName").textContent = SESSION.label;
  const d = new Date(SESSION.startedAt);
  $("#sessionMeta").textContent = `${d.toISOString().slice(0, 10)} · mixed ${SESSION.langs.join(" / ")}`;
  $("#totalTc").textContent = helpers.clockH(SESSION.durationS);
  $("#playheadTc").textContent = helpers.clockH(state.playhead);
  $("#app").dataset.playing = String(state.playing);
  setZoom(state.zoom);

  buildTracks();
  buildLiveLog();
  buildInspector();
  buildProfiles();
  buildSessionMenu();
  buildTranscript();
  drawRuler();
  setActiveTab();
  wireEvents();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
