// Clarity prototype — calm, document-first TapScribe UI.
// All data comes from the shared canonical fixture; nothing here is real.
import {
  MOCK,
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

// ---------------------------------------------------------------------------
// Pastel speaker palette (5 slots, matches spk:0..4). Light, friendly, high
// enough contrast on white. Each slot carries: soft fill, ring, ink text.
// ---------------------------------------------------------------------------
const PALETTE = [
  { fill: "#e8eefc", ink: "#3258d8", ring: "#c5d4f7", bar: "#5b7df0" }, // 0 indigo
  { fill: "#fdeaf2", ink: "#c43b78", ring: "#f6cbdd", bar: "#e673a4" }, // 1 rose
  { fill: "#e6f7f1", ink: "#1f8f6b", ring: "#bfeadc", bar: "#3fbf95" }, // 2 teal
  { fill: "#fef0e2", ink: "#c4732a", ring: "#f8d8bd", bar: "#ef9d54" }, // 3 amber
  { fill: "#f0eafb", ink: "#7a47c9", ring: "#ddccf4", bar: "#a479e6" }, // 4 violet
];
const slot = (spk) => PALETTE[spk % PALETTE.length];

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const flagOf = (code) => (helpers.lang(code) ? helpers.lang(code).flag : "");
const langName = (code) => (helpers.lang(code) ? helpers.lang(code).name : code);

// Avatar = pastel circle with initials. Rooms get a soft square-ish badge.
function avatar(spk, initials, { room = false, size = 38 } = {}) {
  const c = slot(spk);
  const a = el("span", "avatar" + (room ? " avatar--room" : ""));
  a.style.cssText = `--fill:${c.fill};--ink:${c.ink};--ring:${c.ring};width:${size}px;height:${size}px;font-size:${Math.round(size * 0.36)}px`;
  a.textContent = initials;
  return a;
}

function langChip(code, { soft = false } = {}) {
  const chip = el("span", "lang-chip" + (soft ? " lang-chip--soft" : ""));
  chip.innerHTML = `<span class="flag">${flagOf(code)}</span>${langName(code)}`;
  return chip;
}

// ===========================================================================
// VIEW: LIVE
// ===========================================================================
function renderLive(root) {
  const session = SESSIONS.find((s) => s.current) || SESSIONS[0];
  const wrap = el("div", "view view--live");

  // Header
  const head = el("div", "page-head");
  head.appendChild(
    el(
      "div",
      "page-head__main",
      `<div class="live-dot"><span class="live-dot__pulse"></span>Recording</div>
       <h1 class="page-title">${session.label || "Untitled session"}</h1>
       <p class="page-sub">In progress · started ${fmtTime(session.startedAt)} · ${session.wavCount} recordings</p>`,
    ),
  );
  const elapsed = el(
    "div",
    "elapsed",
    `<div class="elapsed__time" id="elapsedClock">${helpers.clockH(session.durationS)}</div>
     <div class="elapsed__label">elapsed</div>`,
  );
  head.appendChild(elapsed);
  wrap.appendChild(head);

  // Active speaker cards
  const grid = el("div", "tap-grid");
  for (const tap of LIVE_TAPS) grid.appendChild(tapCard(tap));
  wrap.appendChild(sectionLabel("Active microphones", `${LIVE_TAPS.filter((t) => t.gateOpen).length} speaking now`));
  wrap.appendChild(grid);

  // Live captions (document-style)
  wrap.appendChild(sectionLabel("Live transcript", "auto-scrolling"));
  const feed = el("div", "caption-doc");
  for (const cap of LIVE_CAPTIONS) feed.appendChild(captionRow(cap));
  wrap.appendChild(feed);

  root.appendChild(wrap);
}

function tapCard(tap) {
  const c = slot(tap.spk);
  const card = el("div", "tap-card" + (tap.gateOpen ? " tap-card--open" : " tap-card--idle"));
  card.style.setProperty("--bar", c.bar);

  // Top row: avatar + name + lang + gate state
  const top = el("div", "tap-card__top");
  top.appendChild(avatar(tap.spk, initialsFor(tap.identity), { room: tap.identity === "room-oslo" }));
  const id = el("div", "tap-card__id");
  const nameLine = el("div", "tap-card__name");
  nameLine.textContent = tap.name;
  if (tap.diarized) {
    const tag = el("span", "diar-tag");
    tag.textContent = tap.diarized;
    nameLine.appendChild(tag);
  }
  id.appendChild(nameLine);
  const meta = el("div", "tap-card__meta");
  meta.appendChild(langChip(tap.lang, { soft: true }));
  const gate = el("span", "gate-pill " + (tap.gateOpen ? "gate-pill--open" : "gate-pill--closed"));
  gate.textContent = tap.gateOpen ? "gate open" : "gate closed";
  meta.appendChild(gate);
  id.appendChild(meta);
  top.appendChild(id);
  card.appendChild(top);

  // Level meter + sparkline
  const meterWrap = el("div", "meter");
  const bar = el("div", "meter__bar");
  bar.innerHTML = `<span class="meter__fill" style="width:${Math.round(tap.level * 100)}%"></span>`;
  meterWrap.appendChild(bar);
  meterWrap.appendChild(sparkline(tap.levels, c.bar));
  const lag = el("div", "meter__lag");
  lag.innerHTML =
    tap.gateOpen || tap.level > 0
      ? `<span class="dot-lvl">●</span> ${Math.round(tap.level * 100)}% · lag ${tap.lagS.toFixed(1)}s`
      : `<span class="dot-lvl dot-lvl--mute">●</span> silent`;
  meterWrap.appendChild(lag);
  card.appendChild(meterWrap);

  // In-flight buffer text
  if (tap.buffer) {
    const buf = el("div", "tap-card__buffer");
    buf.innerHTML = `<span class="flag">${flagOf(tap.lang)}</span><span class="buffer-text">${tap.buffer}<span class="caret"></span></span>`;
    card.appendChild(buf);
  }

  // Rec / live toggles
  const toggles = el("div", "tap-card__toggles");
  toggles.appendChild(toggle("Record", tap.record, "rec"));
  toggles.appendChild(toggle("Live", tap.live, "live"));
  card.appendChild(toggles);

  return card;
}

function toggle(label, on, kind) {
  const t = el("button", "toggle" + (on ? " toggle--on" : "") + ` toggle--${kind}`);
  t.type = "button";
  t.innerHTML = `<span class="toggle__track"><span class="toggle__knob"></span></span><span class="toggle__label">${label}</span>`;
  t.addEventListener("click", () => t.classList.toggle("toggle--on"));
  return t;
}

function sparkline(levels, color) {
  const w = 120;
  const h = 26;
  const n = levels.length;
  const step = w / (n - 1);
  const pts = levels.map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - v * (h - 4)).toFixed(1)}`).join(" ");
  const area = `0,${h} ${pts} ${w},${h}`;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.innerHTML = `<polygon points="${area}" fill="${color}" fill-opacity="0.12"/><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>`;
  return svg;
}

function captionRow(cap, { inflight = cap.inflight } = {}) {
  const c = slot(cap.spk);
  const row = el("div", "caption" + (inflight ? " caption--live" : ""));
  row.appendChild(avatar(cap.spk, capInitials(cap.speaker), { size: 30, room: /Oslo Room/.test(cap.speaker) }));
  const body = el("div", "caption__body");
  const head = el("div", "caption__head");
  head.innerHTML = `<span class="caption__name" style="color:${c.ink}">${cap.speaker}</span>
    <span class="caption__lang"><span class="flag">${flagOf(cap.lang)}</span>${langName(cap.lang)}</span>
    <span class="caption__t">${helpers.clock(cap.t)}</span>`;
  body.appendChild(head);
  const txt = el("div", "caption__text");
  txt.innerHTML = inflight ? `${cap.text}<span class="caret"></span>` : cap.text;
  body.appendChild(txt);
  row.appendChild(body);
  return row;
}

// ===========================================================================
// VIEW: SESSIONS (list)
// ===========================================================================
function renderSessions(root) {
  const wrap = el("div", "view view--sessions");
  const head = el("div", "page-head");
  head.appendChild(
    el(
      "div",
      "page-head__main",
      `<h1 class="page-title">Sessions</h1><p class="page-sub">${SESSIONS.length} recorded sessions</p>`,
    ),
  );
  const newBtn = el("button", "btn btn--primary", "New session");
  newBtn.addEventListener("click", () => openWizard());
  head.appendChild(newBtn);
  wrap.appendChild(head);

  const list = el("div", "session-list");
  for (const s of SESSIONS) list.appendChild(sessionCard(s));
  wrap.appendChild(list);
  root.appendChild(wrap);
}

function sessionCard(s) {
  const card = el("button", "session-card" + (s.current ? " session-card--live" : ""));
  card.type = "button";
  // speaker stack
  const stack = el("div", "avatar-stack");
  for (const id of s.speakers.slice(0, 4)) {
    const sp = speakerById(id);
    if (sp) stack.appendChild(avatar(sp.spk, sp.initials, { size: 30, room: sp.isRoom }));
  }
  card.appendChild(stack);

  const main = el("div", "session-card__main");
  const title = el("div", "session-card__title");
  title.textContent = s.label || "Untitled session";
  if (s.current) title.appendChild(el("span", "chip chip--live", "● live"));
  main.appendChild(title);
  const sub = el("div", "session-card__sub");
  sub.innerHTML = `${fmtDate(s.startedAt)} · ${helpers.clockH(s.durationS)} · ${s.wavCount} recordings`;
  main.appendChild(sub);
  const langs = el("div", "session-card__langs");
  for (const code of s.langs) langs.appendChild(langChip(code, { soft: true }));
  main.appendChild(langs);
  card.appendChild(main);

  const tx = el("div", "session-card__tx");
  tx.innerHTML = s.hasTranscript
    ? `<span class="tx-badge tx-badge--ok">✓ Transcript</span>`
    : `<span class="tx-badge tx-badge--pending">Processing…</span>`;
  card.appendChild(tx);

  card.addEventListener("click", () => gotoView("transcript"));
  return card;
}

// ===========================================================================
// VIEW: TRANSCRIPT (the reading experience + recordings + waveform)
// ===========================================================================
let stripState = { ...STRIP_DEFAULTS };
let waveCanvas = null;

function renderTranscript(root) {
  const session = SESSIONS.find((s) => s.current) || SESSIONS[0];
  const wrap = el("div", "view view--transcript");

  // Header with back link + meta + translate-as switch
  const head = el("div", "page-head");
  const main = el("div", "page-head__main");
  const back = el("button", "back-link", "← Sessions");
  back.addEventListener("click", () => gotoView("sessions"));
  main.appendChild(back);
  main.appendChild(el("h1", "page-title", session.label || "Untitled session"));
  main.appendChild(
    el(
      "p",
      "page-sub",
      `${fmtDate(session.startedAt)} · ${helpers.clockH(TRANSCRIPT.durationS)} · ${TRANSCRIPT.lines.length} segments · transcribed with <strong>${TRANSCRIPT.model}</strong> on ${TRANSCRIPT.backend}`,
    ),
  );
  head.appendChild(main);

  // Quick "transcribe as" switch
  const asSwitch = el("div", "as-switch");
  asSwitch.innerHTML = `<span class="as-switch__label">Transcribe as</span>`;
  const seg = el("div", "seg");
  ["en", "nb", "da"].forEach((code, i) => {
    const b = el("button", "seg__btn" + (code === "en" ? " seg__btn--on" : ""));
    b.innerHTML = `<span class="flag">${flagOf(code)}</span>${langName(code)}`;
    b.addEventListener("click", () => {
      seg.querySelectorAll(".seg__btn").forEach((x) => x.classList.remove("seg__btn--on"));
      b.classList.add("seg__btn--on");
    });
    seg.appendChild(b);
  });
  asSwitch.appendChild(seg);
  head.appendChild(asSwitch);
  wrap.appendChild(head);

  // Two-column: left = recordings + waveform card, right = transcript doc
  const cols = el("div", "tx-cols");

  // ---- LEFT column ----
  const left = el("div", "tx-left");
  left.appendChild(waveformCard()); // marquee
  left.appendChild(recordingsCard());
  cols.appendChild(left);

  // ---- RIGHT column: the document ----
  const right = el("div", "tx-right");
  const doc = el("article", "transcript-doc");

  // Speaking-time stacked bar + legend
  doc.appendChild(speakingTimeCard());

  doc.appendChild(el("h2", "doc-h", "Transcript"));
  for (const line of TRANSCRIPT.lines) doc.appendChild(transcriptLine(line));
  right.appendChild(doc);
  cols.appendChild(right);

  wrap.appendChild(cols);
  root.appendChild(wrap);

  // draw the waveform after layout
  requestAnimationFrame(() => drawWave());
}

function speakingTimeCard() {
  const card = el("div", "speaking-card");
  card.appendChild(el("div", "speaking-card__title", "Speaking time"));
  const bar = el("div", "stack-bar");
  for (const seg of TRANSCRIPT.speakingTime) {
    const c = slot(seg.spk);
    const part = el("span", "stack-bar__seg");
    part.style.cssText = `width:${seg.pct}%;background:${c.bar}`;
    part.title = `${seg.speaker} · ${helpers.pct(seg.pct)}`;
    bar.appendChild(part);
  }
  card.appendChild(bar);
  const legend = el("div", "legend");
  for (const seg of TRANSCRIPT.speakingTime) {
    const c = slot(seg.spk);
    const item = el("div", "legend__item");
    item.innerHTML = `<span class="legend__dot" style="background:${c.bar}"></span><span class="legend__name">${seg.speaker}</span><span class="legend__pct">${helpers.pct(seg.pct)}</span>`;
    legend.appendChild(item);
  }
  card.appendChild(legend);
  return card;
}

function transcriptLine(line) {
  const c = slot(line.spk);
  const row = el("div", "tline" + (line.suppressed ? " tline--suppressed" : ""));
  row.style.setProperty("--ink", c.ink);

  const gutter = el("div", "tline__gutter");
  gutter.appendChild(avatar(line.spk, capInitials(line.speaker), { size: 30, room: /Oslo Room/.test(line.speaker) }));
  row.appendChild(gutter);

  const body = el("div", "tline__body");
  const head = el("div", "tline__head");
  head.innerHTML = `<span class="tline__name" style="color:${c.ink}">${line.speaker}</span>
    <span class="tline__t">${helpers.clock(line.t)}</span>
    <span class="tline__lang"><span class="flag">${flagOf(line.lang)}</span></span>`;
  if (line.translatedFrom) {
    head.appendChild(
      el("span", "badge badge--translate", `translated ${flagOf(line.translatedFrom)}→${flagOf("en")}`),
    );
  }
  if (line.suppressed) {
    head.appendChild(el("span", "badge badge--filtered", `filtered · ${line.matchedRule}`));
  }
  if (line.lowConfidence) {
    head.appendChild(el("span", "badge badge--lowconf", `low confidence ${helpers.pct(line.confidence * 100)}`));
  }
  body.appendChild(head);

  const txt = el(
    "p",
    "tline__text" +
      (line.suppressed ? " tline__text--struck" : "") +
      (line.lowConfidence ? " tline__text--lowconf" : ""),
  );
  txt.textContent = line.text;
  body.appendChild(txt);
  row.appendChild(body);
  return row;
}

function recordingsCard() {
  const card = el("div", "card recordings");
  card.appendChild(el("div", "card__title", `Recordings <span class="muted">· ${SESSIONS.find((s) => s.current).wavCount} WAVs</span>`));

  // The representative WAV with its stripped clips, plus a couple peers.
  const region = computeRegions(REP_WAV.peaks, REP_WAV.durationS, stripState);
  const items = [
    { name: REP_WAV.name, sp: "atle", durationS: REP_WAV.durationS, clips: region.regions },
    { name: "…09-11-48Z_mette_mette.wav", sp: "mette", durationS: 36, clips: [{ startS: 2.1, endS: 14.6 }, { startS: 18.0, endS: 33.2 }] },
    { name: "…09-14-02Z_room-oslo_speakerB.wav", sp: "room-oslo", durationS: 41, clips: [{ startS: 0.6, endS: 19.1 }, { startS: 22.3, endS: 38.0 }] },
  ];
  for (const it of items) {
    const sp = speakerById(it.sp);
    const r = el("div", "rec-item");
    const top = el("div", "rec-item__top");
    top.appendChild(avatar(sp.spk, sp.initials, { size: 26, room: sp.isRoom }));
    top.appendChild(el("span", "rec-item__name", it.name.replace(/^.*?([^/]+)$/, "$1")));
    top.appendChild(el("span", "rec-item__dur", helpers.clock(it.durationS)));
    r.appendChild(top);
    const clips = el("div", "rec-item__clips");
    clips.appendChild(el("span", "clip-label", `${it.clips.length} clips`));
    for (const cl of it.clips) {
      const tag = el("span", "clip-tag");
      tag.textContent = `${helpers.clock(cl.startS)}–${helpers.clock(cl.endS)}`;
      clips.appendChild(tag);
    }
    r.appendChild(clips);
    card.appendChild(r);
  }
  return card;
}

// ===========================================================================
// MARQUEE: Waveform + strip-silence cut preview
// ===========================================================================
function waveformCard() {
  const card = el("div", "card wave-card");
  const head = el("div", "wave-card__head");
  head.innerHTML = `<div class="card__title">Strip silence <span class="muted">· preview</span></div>`;
  const chip = el("span", "scissor-chip", "✂ 4 clips");
  chip.id = "scissorChip";
  head.appendChild(chip);
  card.appendChild(head);

  const sp = speakerById(REP_WAV.speakerId);
  card.appendChild(
    el(
      "div",
      "wave-card__file",
      `<span class="flag">${flagOf(sp.primaryLang)}</span><span class="wave-card__fname">${REP_WAV.name}</span><span class="muted">${helpers.clock(REP_WAV.durationS)}</span>`,
    ),
  );

  // canvas
  const canvasWrap = el("div", "wave-canvas-wrap");
  const cv = el("canvas", "wave-canvas");
  cv.width = 1160;
  cv.height = 280;
  waveCanvas = cv;
  canvasWrap.appendChild(cv);
  card.appendChild(canvasWrap);

  // readout
  const readout = el("div", "wave-readout", "");
  readout.id = "waveReadout";
  card.appendChild(readout);

  // sliders
  const knobs = el("div", "knobs");
  knobs.appendChild(
    knob("Gap", "minSilenceMs", 0, 3000, stripState.minSilenceMs, 50, (v) => `${v} ms`),
  );
  knobs.appendChild(knob("Pad", "padMs", 0, 500, stripState.padMs, 10, (v) => `${v} ms`));
  knobs.appendChild(knob("Floor", "speechFloorDb", -55, -30, stripState.speechFloorDb, 1, (v) => `${v} dB`));
  card.appendChild(knobs);

  return card;
}

function knob(label, key, min, max, value, step, fmt) {
  const wrap = el("div", "knob");
  const top = el("div", "knob__top");
  top.innerHTML = `<span class="knob__label">${label}</span><span class="knob__val" data-val="${key}">${fmt(value)}</span>`;
  wrap.appendChild(top);
  const input = el("input", "knob__range");
  input.type = "range";
  input.min = min;
  input.max = max;
  input.step = step;
  input.value = value;
  input.setAttribute("aria-label", label);
  input.addEventListener("input", () => {
    const v = Number(input.value);
    stripState[key] = v;
    $(`.knob__val[data-val="${key}"]`).textContent = fmt(v);
    drawWave();
  });
  wrap.appendChild(input);
  return wrap;
}

function drawWave() {
  if (!waveCanvas) return;
  const cv = waveCanvas;
  const ctx = cv.getContext("2d");
  const W = cv.width;
  const H = cv.height;
  ctx.clearRect(0, 0, W, H);

  const peaks = REP_WAV.peaks;
  const n = peaks.length;
  const mid = H / 2;
  const padX = 8;
  const innerW = W - padX * 2;
  const region = computeRegions(peaks, REP_WAV.durationS, stripState);

  // helper: x for a given second
  const xForS = (s) => padX + (s / REP_WAV.durationS) * innerW;
  const xForI = (i) => padX + (i / (n - 1)) * innerW;

  // 1) kept-region soft bands (drawn first, behind waveform)
  for (const r of region.regions) {
    const x0 = xForS(r.startS);
    const x1 = xForS(r.endS);
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, "rgba(91,125,240,0.10)");
    g.addColorStop(1, "rgba(91,125,240,0.04)");
    ctx.fillStyle = g;
    roundRect(ctx, x0, 10, x1 - x0, H - 20, 12);
    ctx.fill();
    // dashed boundary
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "rgba(91,125,240,0.55)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x0 + 0.5, 12);
    ctx.lineTo(x0 + 0.5, H - 12);
    ctx.moveTo(x1 - 0.5, 12);
    ctx.lineTo(x1 - 0.5, H - 12);
    ctx.stroke();
    ctx.restore();
  }

  // 2) center baseline
  ctx.strokeStyle = "rgba(15,23,42,0.06)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padX, mid);
  ctx.lineTo(W - padX, mid);
  ctx.stroke();

  // 3) waveform bars — kept regions in speaker indigo, dropped in soft grey
  const inRegion = (s) => region.regions.some((r) => s >= r.startS && s <= r.endS);
  const barW = Math.max(1, innerW / n - 0.6);
  for (let i = 0; i < n; i++) {
    const s = (i / (n - 1)) * REP_WAV.durationS;
    const x = xForI(i);
    const amp = Math.pow(peaks[i], 0.8); // gentle gamma so quiet detail shows
    const h = Math.max(1.2, amp * (H * 0.42));
    const kept = inRegion(s);
    ctx.fillStyle = kept ? "rgba(70,103,224,0.85)" : "rgba(148,163,184,0.40)";
    roundRectTop(ctx, x, mid - h, barW, h * 2, Math.min(2, barW / 2));
  }

  // 4) floor line (the dB threshold), mapped to amplitude height
  const floorAmp = Math.pow(10, stripState.speechFloorDb / 20);
  const floorH = Math.pow(floorAmp, 0.8) * (H * 0.42);
  ctx.save();
  ctx.setLineDash([2, 5]);
  ctx.strokeStyle = "rgba(196,59,120,0.5)";
  ctx.lineWidth = 1.25;
  ctx.beginPath();
  ctx.moveTo(padX, mid - floorH);
  ctx.lineTo(W - padX, mid - floorH);
  ctx.moveTo(padX, mid + floorH);
  ctx.lineTo(W - padX, mid + floorH);
  ctx.stroke();
  ctx.restore();

  // update readout + chip
  const ro = $("#waveReadout");
  if (ro) {
    ro.innerHTML = `<strong>${region.clips} clips</strong> · ${region.speechS}s of ${region.totalS}s kept <span class="wave-readout__drop">(${(region.totalS - region.speechS).toFixed(1)}s silence removed)</span>`;
  }
  const chip = $("#scissorChip");
  if (chip) chip.textContent = `✂ ${region.clips} clips`;
}

function roundRect(ctx, x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function roundRectTop(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x, y + r);
  ctx.arcTo(x, y, x + r, y, r);
  ctx.arcTo(x + w, y, x + w, y + r, r);
  ctx.lineTo(x + w, y + h - r);
  ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
  ctx.arcTo(x, y + h, x, y + h - r, r);
  ctx.closePath();
  ctx.fill();
}

// ===========================================================================
// VIEW: PEOPLE (directory + dual-language quick switch + diarization)
// ===========================================================================
function renderPeople(root) {
  const wrap = el("div", "view view--people");
  const head = el("div", "page-head");
  head.appendChild(
    el(
      "div",
      "page-head__main",
      `<h1 class="page-title">People</h1><p class="page-sub">${SPEAKERS.length} people · profiles reused across sessions</p>`,
    ),
  );
  wrap.appendChild(head);

  const grid = el("div", "people-grid");
  for (const sp of SPEAKERS) grid.appendChild(personCard(sp));
  wrap.appendChild(grid);
  root.appendChild(wrap);
}

function personCard(sp) {
  const c = slot(sp.spk);
  const card = el("div", "person-card" + (sp.isRoom ? " person-card--room" : ""));
  card.style.setProperty("--ink", c.ink);

  const top = el("div", "person-card__top");
  top.appendChild(avatar(sp.spk, sp.initials, { size: 46, room: sp.isRoom }));
  const id = el("div", "person-card__id");
  const nm = el("div", "person-card__name");
  nm.textContent = sp.name;
  if (sp.isRoom) nm.appendChild(el("span", "chip chip--room", "Shared room"));
  id.appendChild(nm);
  id.appendChild(el("div", "person-card__note", sp.note));
  top.appendChild(id);
  card.appendChild(top);

  // device chip + seen-in
  const facts = el("div", "person-card__facts");
  facts.appendChild(el("span", "device-chip", `🎙 ${sp.mic.label}`));
  facts.appendChild(el("span", "seen-chip", `seen in ${sp.sessionsSeen} sessions`));
  card.appendChild(facts);

  // languages: primary + secondary + quick switch
  const langWrap = el("div", "person-langs");
  const langRow = el("div", "person-langs__row");
  const prim = el("span", "lang-chip lang-chip--primary");
  prim.innerHTML = `<span class="flag">${flagOf(sp.primaryLang)}</span>${langName(sp.primaryLang)}<span class="lang-chip__tag">primary</span>`;
  langRow.appendChild(prim);
  if (sp.secondaryLang) {
    const sec = el("span", "lang-chip lang-chip--secondary");
    sec.innerHTML = `<span class="flag">${flagOf(sp.secondaryLang)}</span>${langName(sp.secondaryLang)}<span class="lang-chip__tag">secondary</span>`;
    langRow.appendChild(sec);
  }
  langWrap.appendChild(langRow);

  // quick "transcribe as" segmented control (primary | secondary)
  if (sp.secondaryLang) {
    const seg = el("div", "seg seg--sm");
    seg.appendChild(el("span", "seg__pre", "Transcribe as"));
    [sp.primaryLang, sp.secondaryLang].forEach((code, i) => {
      const b = el("button", "seg__btn" + (i === 0 ? " seg__btn--on" : ""));
      b.innerHTML = `<span class="flag">${flagOf(code)}</span>${langName(code)}`;
      b.addEventListener("click", () => {
        seg.querySelectorAll(".seg__btn").forEach((x) => x.classList.remove("seg__btn--on"));
        b.classList.add("seg__btn--on");
      });
      seg.appendChild(b);
    });
    langWrap.appendChild(seg);
  }
  card.appendChild(langWrap);

  // diarization sub-people for the room
  if (sp.isRoom && sp.diarizedInto) {
    const diar = el("div", "diar");
    diar.appendChild(el("div", "diar__title", "Diarized into"));
    for (const d of sp.diarizedInto) {
      const dc = slot(d.spk);
      const sub = el("div", "diar__sub");
      sub.appendChild(avatar(d.spk, d.label.replace("Speaker ", ""), { size: 28 }));
      const info = el("div", "diar__info");
      info.innerHTML = `<span class="diar__name" style="color:${dc.ink}">${d.label}</span><span class="diar__lang"><span class="flag">${flagOf(d.lang)}</span>${langName(d.lang)}</span>`;
      sub.appendChild(info);
      const talk = el("div", "diar__talk");
      talk.innerHTML = `<span class="diar__talkbar"><span style="width:${d.talkPct}%;background:${dc.bar}"></span></span><span class="diar__talkpct">${d.talkPct}%</span>`;
      sub.appendChild(talk);
      diar.appendChild(sub);
    }
    card.appendChild(diar);
  }
  return card;
}

// ===========================================================================
// VIEW: DEVICES (mics as cross-session presets)
// ===========================================================================
function renderDevices(root) {
  const wrap = el("div", "view view--devices");
  const head = el("div", "page-head");
  head.appendChild(
    el(
      "div",
      "page-head__main",
      `<h1 class="page-title">Devices</h1><p class="page-sub">Saved gate &amp; noise-floor presets · applied automatically when the mic reconnects</p>`,
    ),
  );
  wrap.appendChild(head);

  const list = el("div", "device-list");
  // header row
  const hrow = el("div", "device-row device-row--head");
  hrow.innerHTML = `<div>Microphone</div><div>Owner</div><div>Gate threshold</div><div>Noise floor</div><div>Sessions</div>`;
  list.appendChild(hrow);

  for (const sp of SPEAKERS) {
    const c = slot(sp.spk);
    const row = el("div", "device-row");
    row.appendChild(el("div", "device-row__mic", `<span class="device-ic">🎙</span><div><div class="device-row__label">${sp.mic.label}</div><div class="device-row__id muted">${sp.mic.id}</div></div>`));
    const owner = el("div", "device-row__owner");
    owner.appendChild(avatar(sp.spk, sp.initials, { size: 26, room: sp.isRoom }));
    owner.appendChild(el("span", "", sp.name));
    row.appendChild(owner);
    // gate threshold meter
    row.appendChild(
      el(
        "div",
        "device-row__gate",
        `<span class="mini-meter"><span style="width:${Math.round(sp.gateThreshold * 100)}%;background:${c.bar}"></span></span><span class="mono">${sp.gateThreshold.toFixed(2)}</span>`,
      ),
    );
    row.appendChild(el("div", "device-row__floor", `<span class="mono">${sp.noiseFloorDb} dBFS</span>`));
    row.appendChild(el("div", "device-row__seen", `<span class="seen-chip">${sp.sessionsSeen}×</span>`));
    list.appendChild(row);
  }
  wrap.appendChild(list);
  root.appendChild(wrap);
}

// ===========================================================================
// VIEW: SETTINGS (small, real-feeling)
// ===========================================================================
function renderSettings(root) {
  const wrap = el("div", "view view--settings");
  const head = el("div", "page-head");
  head.appendChild(
    el("div", "page-head__main", `<h1 class="page-title">Settings</h1><p class="page-sub">${MOCK.APP.name} ${MOCK.APP.version}</p>`),
  );
  wrap.appendChild(head);

  const card = el("div", "card settings-card");
  card.appendChild(el("div", "card__title", "Default transcription engine"));
  card.appendChild(backendChips());
  card.appendChild(modelPicker());
  wrap.appendChild(card);

  const themeCard = el("div", "card settings-card");
  themeCard.appendChild(el("div", "card__title", "Appearance"));
  const tRow = el("div", "settings-row");
  tRow.innerHTML = `<div><div class="settings-row__label">Dark mode</div><div class="settings-row__hint muted">Clarity is tuned for light; dark is available.</div></div>`;
  const tBtn = el("button", "toggle", `<span class="toggle__track"><span class="toggle__knob"></span></span><span class="toggle__label">Theme</span>`);
  tBtn.addEventListener("click", toggleTheme);
  if (document.documentElement.dataset.theme === "dark") tBtn.classList.add("toggle--on");
  tRow.appendChild(tBtn);
  themeCard.appendChild(tRow);
  wrap.appendChild(themeCard);

  root.appendChild(wrap);
}

// ---- shared engine controls (used in Settings + Wizard) ----
function backendChips() {
  const wrap = el("div", "backend-chips");
  wrap.appendChild(el("div", "field-label", "Backend"));
  const row = el("div", "chip-row");
  for (const b of MOCK.APP.backends) {
    const on = b.kind === selectedModel.backend;
    const chip = el(
      "button",
      "bchip" + (on ? " bchip--on" : "") + (b.available ? "" : " bchip--disabled"),
    );
    chip.type = "button";
    chip.disabled = !b.available;
    chip.innerHTML = `${b.label}${!b.available ? '<span class="bchip__x">unavailable</span>' : ""}`;
    if (b.available) {
      chip.addEventListener("click", () => {
        row.querySelectorAll(".bchip").forEach((x) => x.classList.remove("bchip--on"));
        chip.classList.add("bchip--on");
      });
    }
    row.appendChild(chip);
  }
  wrap.appendChild(row);
  return wrap;
}

function modelPicker() {
  const wrap = el("div", "model-picker");
  wrap.appendChild(el("div", "field-label", "Model"));
  const groups = el("div", "model-groups");
  for (const fam of MODELS) {
    const g = el("div", "model-group");
    g.appendChild(el("div", "model-group__fam", fam.family));
    for (const m of fam.models) {
      const on = m.id === selectedModel.model;
      const opt = el("button", "model-opt" + (on ? " model-opt--on" : ""));
      opt.type = "button";
      opt.innerHTML = `<span class="model-opt__name">${m.display}</span><span class="model-opt__desc">${m.desc}</span><span class="model-opt__langs">${m.langs.map((l) => flagOf(l)).join(" ")}</span>`;
      opt.addEventListener("click", () => {
        groups.querySelectorAll(".model-opt").forEach((x) => x.classList.remove("model-opt--on"));
        opt.classList.add("model-opt--on");
        canaryOpts.style.display = m.inputs ? "" : "none";
      });
      g.appendChild(opt);
    }
    groups.appendChild(g);
  }
  wrap.appendChild(groups);

  // Canary source/target lang selects (shown because canary is selected)
  const canaryOpts = el("div", "canary-opts");
  const canary = MODELS.find((f) => f.family === "canary").models[0];
  canaryOpts.appendChild(el("div", "canary-opts__title", `${canary.display} options`));
  const grid = el("div", "canary-grid");
  for (const inp of canary.inputs) {
    const f = el("div", "field");
    f.appendChild(el("label", "field-label", inp.label));
    if (inp.kind === "select") {
      const sel = el("select", "select");
      const codes = ["nb", "da", "en", "sv", "de", "fr"];
      for (const code of codes) {
        const o = el("option", null, `${flagOf(code)} ${langName(code)}`);
        o.value = code;
        const def = inp.name === "source_lang" ? selectedModel.sourceLang : selectedModel.targetLang;
        if (code === def) o.selected = true;
        sel.appendChild(o);
      }
      f.appendChild(sel);
    } else {
      const txt = el("input", "input");
      txt.type = "text";
      txt.placeholder = inp.placeholder || "";
      f.appendChild(txt);
    }
    grid.appendChild(f);
  }
  canaryOpts.appendChild(grid);
  wrap.appendChild(canaryOpts);
  return wrap;
}

// ===========================================================================
// SETUP WIZARD (slide-over stepper)
// ===========================================================================
let wizardStep = 0;
const WIZ_STEPS = ["Name & languages", "Speakers & mics", "Model"];

function openWizard() {
  wizardStep = 0;
  const scrim = $("#wizard");
  scrim.classList.add("open");
  renderWizard();
}
function closeWizard() {
  $("#wizard").classList.remove("open");
}

function renderWizard() {
  const panel = $("#wizardPanel");
  panel.innerHTML = "";

  // header
  const head = el("div", "wizard__head");
  head.appendChild(el("div", "wizard__eyebrow", "New session"));
  head.appendChild(el("h2", "wizard__title", "Set up a recording"));
  const close = el("button", "wizard__close", "✕");
  close.addEventListener("click", closeWizard);
  head.appendChild(close);
  panel.appendChild(head);

  // stepper
  const steps = el("div", "stepper");
  WIZ_STEPS.forEach((label, i) => {
    const s = el(
      "div",
      "step" + (i === wizardStep ? " step--on" : "") + (i < wizardStep ? " step--done" : ""),
    );
    s.innerHTML = `<span class="step__num">${i < wizardStep ? "✓" : i + 1}</span><span class="step__label">${label}</span>`;
    steps.appendChild(s);
    if (i < WIZ_STEPS.length - 1) steps.appendChild(el("span", "step__line"));
  });
  panel.appendChild(steps);

  const body = el("div", "wizard__body");
  if (wizardStep === 0) body.appendChild(wizStep1());
  else if (wizardStep === 1) body.appendChild(wizStep2());
  else body.appendChild(wizStep3());
  panel.appendChild(body);

  // footer nav
  const foot = el("div", "wizard__foot");
  const back = el("button", "btn btn--ghost", "Back");
  back.disabled = wizardStep === 0;
  back.addEventListener("click", () => {
    if (wizardStep > 0) {
      wizardStep--;
      renderWizard();
    }
  });
  foot.appendChild(back);
  const next = el("button", "btn btn--primary", wizardStep === WIZ_STEPS.length - 1 ? "Start recording" : "Continue");
  next.addEventListener("click", () => {
    if (wizardStep < WIZ_STEPS.length - 1) {
      wizardStep++;
      renderWizard();
    } else {
      closeWizard();
      gotoView("live");
    }
  });
  foot.appendChild(next);
  panel.appendChild(foot);
}

function wizStep1() {
  const f = el("div", "wiz-pane");
  const name = el("div", "field");
  name.innerHTML = `<label class="field-label">Session name</label>`;
  const inp = el("input", "input");
  inp.type = "text";
  inp.value = "Nordic Sync";
  name.appendChild(inp);
  f.appendChild(name);

  f.appendChild(el("div", "field-label", "Expected languages"));
  const chips = el("div", "lang-pick");
  ["nb", "da", "en", "sv", "de", "fr", "auto"].forEach((code) => {
    const on = ["nb", "da", "en"].includes(code);
    const b = el("button", "lang-pick__chip" + (on ? " lang-pick__chip--on" : ""));
    b.innerHTML = `<span class="flag">${flagOf(code)}</span>${langName(code)}`;
    b.addEventListener("click", () => b.classList.toggle("lang-pick__chip--on"));
    chips.appendChild(b);
  });
  f.appendChild(chips);
  return f;
}

function wizStep2() {
  const f = el("div", "wiz-pane");
  f.appendChild(el("p", "wiz-hint", "Assign each speaker to a microphone and a language. Saved profiles are matched automatically."));
  const table = el("div", "assign");
  const hr = el("div", "assign__row assign__row--head");
  hr.innerHTML = `<div>Speaker</div><div>Microphone</div><div>Language</div>`;
  table.appendChild(hr);
  for (const sp of SPEAKERS) {
    const row = el("div", "assign__row");
    const who = el("div", "assign__who");
    who.appendChild(avatar(sp.spk, sp.initials, { size: 30, room: sp.isRoom }));
    who.appendChild(el("span", "", sp.name + (sp.isRoom ? " · room" : "")));
    row.appendChild(who);
    // mic select
    const micSel = el("select", "select select--sm");
    SPEAKERS.forEach((o) => {
      const opt = el("option", null, o.mic.label);
      opt.value = o.mic.id;
      if (o.mic.id === sp.mic.id) opt.selected = true;
      micSel.appendChild(opt);
    });
    row.appendChild(micSel);
    // lang select (primary preselected)
    const langSel = el("select", "select select--sm");
    ["nb", "da", "en", "sv", "de", "fr", "auto"].forEach((code) => {
      const opt = el("option", null, `${flagOf(code)} ${langName(code)}`);
      opt.value = code;
      if (code === sp.primaryLang) opt.selected = true;
      langSel.appendChild(opt);
    });
    row.appendChild(langSel);
    table.appendChild(row);
  }
  f.appendChild(table);
  return f;
}

function wizStep3() {
  const f = el("div", "wiz-pane");
  f.appendChild(backendChips());
  f.appendChild(modelPicker());
  return f;
}

// ===========================================================================
// SHELL / ROUTER
// ===========================================================================
const VIEWS = {
  live: renderLive,
  sessions: renderSessions,
  transcript: renderTranscript,
  waveform: renderTranscript, // waveform shot = transcript view scrolled/focused
  people: renderPeople,
  devices: renderDevices,
  settings: renderSettings,
};

const NAV = [
  { id: "live", label: "Live", icon: liveIcon, badge: "rec" },
  { id: "sessions", label: "Sessions", icon: sessionsIcon },
  { id: "people", label: "People", icon: peopleIcon },
  { id: "devices", label: "Devices", icon: devicesIcon },
  { id: "settings", label: "Settings", icon: settingsIcon },
];

let currentView = "live";

function buildSidebar() {
  const nav = $("#navItems");
  nav.innerHTML = "";
  for (const item of NAV) {
    const a = el("button", "nav-item" + (item.id === navTarget(currentView) ? " nav-item--on" : ""));
    a.dataset.nav = item.id;
    a.innerHTML = `<span class="nav-item__ic">${item.icon()}</span><span class="nav-item__label">${item.label}</span>`;
    if (item.badge === "rec") a.innerHTML += `<span class="nav-item__rec"></span>`;
    a.addEventListener("click", () => gotoView(item.id));
    nav.appendChild(a);
  }
}

// transcript/waveform both highlight Sessions in nav
function navTarget(view) {
  if (view === "transcript" || view === "waveform") return "sessions";
  return view;
}

function gotoView(name) {
  if (!VIEWS[name]) name = "live";
  currentView = name;
  const main = $("#main");
  main.innerHTML = "";
  main.scrollTop = 0;
  VIEWS[name](main);
  // update nav highlight
  document.querySelectorAll(".nav-item").forEach((n) => {
    n.classList.toggle("nav-item--on", n.dataset.nav === navTarget(name));
  });
  // for the dedicated waveform shot, scroll the marquee card into view
  if (name === "waveform") {
    requestAnimationFrame(() => {
      const card = $(".wave-card");
      if (card) card.scrollIntoView({ block: "start" });
      drawWave();
    });
  }
}
window.gotoView = gotoView;
window.openWizard = openWizard;

function toggleTheme() {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  // redraw canvas (colors are theme-independent here but keep crisp)
  if (currentView === "transcript" || currentView === "waveform") drawWave();
}
window.toggleTheme = toggleTheme;

// ---------------------------------------------------------------------------
// small helpers for names/dates
// ---------------------------------------------------------------------------
function initialsFor(identity) {
  const sp = speakerById(identity);
  return sp ? sp.initials : identity.slice(0, 2).toUpperCase();
}
function capInitials(name) {
  // "Oslo Room · Speaker B" -> "SB"; "Atle Håvsø" -> "AH"
  const m = name.match(/Speaker\s+([A-Z])/);
  if (m) return "S" + m[1];
  const parts = name.replace(/·.*/, "").trim().split(/\s+/);
  return (parts[0]?.[0] || "") + (parts[1]?.[0] || "");
}
function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function sectionLabel(title, hint) {
  const s = el("div", "section-label");
  s.innerHTML = `<span class="section-label__title">${title}</span>${hint ? `<span class="section-label__hint">${hint}</span>` : ""}`;
  return s;
}

// ---------------------------------------------------------------------------
// Inline SVG icons (1.5px stroke, currentColor) — no icon library.
// ---------------------------------------------------------------------------
function svgIcon(paths) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
}
function liveIcon() {
  return svgIcon(`<circle cx="12" cy="12" r="3.5"/><path d="M5.5 5.5a9 9 0 0 0 0 13M18.5 5.5a9 9 0 0 1 0 13"/>`);
}
function sessionsIcon() {
  return svgIcon(`<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 13h8M8 17h5"/>`);
}
function peopleIcon() {
  return svgIcon(`<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 6.5a3 3 0 0 1 0 5.8M20.5 19a5 5 0 0 0-3.2-4.6"/>`);
}
function devicesIcon() {
  return svgIcon(`<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v3M9 20h6"/>`);
}
function settingsIcon() {
  return svgIcon(`<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M4.2 7l2.2 1.3M17.6 15.7l2.2 1.3M19.8 7l-2.2 1.3M6.4 15.7L4.2 17M3 12h0M21 12h0"/>`);
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
buildSidebar();
gotoView("live");
