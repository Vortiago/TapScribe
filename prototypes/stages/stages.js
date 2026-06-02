// =============================================================================
// TapScribe · Stages — the meeting's life as a guided journey.
// A slim ordered "spine" (Capture → Recordings → Transcript → People) with live
// state on each stop; one dense, logically-grouped workspace at a time.
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
  selectedClip: 0,
  engine: { ...selectedModel },
  // per-speaker "transcribe as" quick switch (defaults to primary lang)
  transcribeAs: Object.fromEntries(SPEAKERS.map((s) => [s.id, s.primaryLang])),
};

const session = () => SESSIONS.find((s) => s.id === state.sessionId) || SESSIONS[0];

// =============================================================================
// Stage definitions — the ORDERED journey. Each carries a live status chip
// derived from the current session/data, so the spine doubles as a readout.
// =============================================================================
function stageDefs() {
  const sess = session();
  const liveCount = sess.current ? LIVE_TAPS.filter((t) => t.live).length : 0;
  const recs = clipModel();
  const needTune = recs.filter((c) => c.needsTune).length;
  const suppressed = TRANSCRIPT.lines.filter((l) => l.suppressed).length;
  return [
    {
      id: "capture", n: 1, ic: "🎙️", name: "Capture",
      chip: sess.current
        ? (liveCount ? { tone: "live", text: `${liveCount} live` } : { tone: "mute", text: "idle" })
        : { tone: "good", text: `${sess.speakers.length} sources` },
      done: !sess.current,
    },
    {
      id: "recordings", n: 2, ic: "🌊", name: "Recordings",
      chip: needTune ? { tone: "warn", text: `${needTune} need tuning` } : { tone: "good", text: `${recs.length} clips` },
      done: sess.wavCount > 0,
    },
    {
      id: "transcript", n: 3, ic: "📝", name: "Transcript",
      chip: sess.hasTranscript
        ? (suppressed ? { tone: "warn", text: `${suppressed} suppressed` } : { tone: "good", text: "reviewed" })
        : { tone: "mute", text: "not run" },
      done: sess.hasTranscript,
    },
    {
      id: "people", n: 4, ic: "👥", name: "People",
      chip: { tone: "mute", text: `${SPEAKERS.length} profiles` },
      done: false,
    },
  ];
}

// Recordings model: synthesize a small clip list for THIS session from REP_WAV +
// strip-silence so the "needs tuning" status is real (clips whose default cut
// produced >1 region are flagged as "tune").
function clipModel() {
  const names = [
    { sp: "atle", t: "09:04:12" }, { sp: "mette", t: "09:05:48" },
    { sp: "room-oslo", t: "09:07:03" }, { sp: "atle", t: "09:09:21" },
    { sp: "james", t: "09:11:40" },
  ].slice(0, Math.min(5, Math.max(2, Math.round(session().wavCount / 8))));
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
  document.getElementById("sessionMeta").textContent = `${clockH(sess.durationS)} · ${sess.wavCount} clips`;
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

  // journey progress fill: how far down the pipeline the session has advanced
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
      <button class="smitem ${s.id === state.sessionId ? "is-current" : ""}">
        <span class="smitem__dot"></span>
        <span class="smitem__body">
          <span class="smitem__label">${esc(s.label || "(untitled)")}</span>
          <span class="smitem__meta">${esc(s.startedAt.slice(0, 10))} · ${clockH(s.durationS)} · ${s.wavCount} clips</span>
        </span>
        ${s.current ? '<span class="smitem__badge" style="color:#ffb3b3;border-color:#4a2626">live</span>' : (s.hasTranscript ? '<span class="smitem__badge">tx</span>' : '<span class="smitem__badge">raw</span>')}
      </button>`);
    item.addEventListener("click", () => {
      state.sessionId = s.id;
      menu.hidden = true;
      // switching session re-seeds the journey at the most relevant stage
      state.stage = s.current ? "capture" : (s.hasTranscript ? "transcript" : "recordings");
      state.selectedClip = 0;
      render();
    });
    menu.appendChild(item);
  }
}

document.getElementById("sessionPick").addEventListener("click", () => {
  const m = document.getElementById("sessionMenu");
  m.hidden = !m.hidden;
});

// =============================================================================
// WORKSPACE shell
// =============================================================================
function header({ eyebrow, title, sub, next }) {
  const nextBtn = next
    ? `<button class="nextstep ${next.ghost ? "is-ghost" : ""}" id="nextStep">${esc(next.label)} <span class="arr">→</span></button>`
    : "";
  return `
    <div class="whead">
      <div class="whead__l">
        <div class="whead__eyebrow">${esc(eyebrow)}</div>
        <h1 class="whead__title">${esc(title)}</h1>
        <div class="whead__sub">${sub}</div>
      </div>
      <div class="whead__r">${nextBtn}</div>
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
    case "recordings": frag = viewRecordings(); break;
    case "transcript": frag = viewTranscript(); break;
    case "people": frag = viewPeople(); break;
    default: frag = viewCapture();
  }
  root.appendChild(frag);
  // wire the "advance the journey" button if present
  const nb = document.getElementById("nextStep");
  if (nb && nb.dataset.go) nb.addEventListener("click", () => goStage(nb.dataset.go));
  // stage-specific post-render hooks
  if (state.stage === "recordings") afterRecordings();
}

function goStage(id) { state.stage = id; render(); window.scrollTo(0, 0); }

// =============================================================================
// STAGE 1 — CAPTURE  (live taps + gate + rec/live + diarization + captions)
// =============================================================================
function viewCapture() {
  const sess = session();
  if (!sess.current) return viewCaptureArchived(sess);
  const live = LIVE_TAPS.filter((t) => t.live).length;
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 1 · Live",
    title: "Capture",
    sub: `${live} taps streaming into <b>${esc(sess.label)}</b> · recorder ${APP.recordingEnabled ? "<span style='color:var(--good)'>on</span>" : "<span class='muted'>paused</span>"} · backend <span class='mono'>${esc(APP.backend)}</span>`,
    next: { label: "Tune the recordings", go: "recordings" },
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

  // ---- RIGHT: live captions feed (settled + in-flight) ----
  const capsPanel = el(`
    <div class="panel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">💬</span>Live captions</div>
        <div class="panel__hint">tagged by speaker · language</div>
      </div>
      <div class="panel__body flush"><div class="caps"></div></div>
    </div>`);
  const caps = capsPanel.querySelector(".caps");
  for (const c of LIVE_CAPTIONS) {
    caps.appendChild(el(`
      <div class="cap ${c.inflight ? "inflight" : ""}">
        <span class="cap__t">${clock(c.t)}</span>
        <span class="cap__body">
          <span class="cap__who spk-ink-${c.spk}"><span class="av sm spk-${c.spk}"></span>${esc(c.speaker)} <span class="flag">${LANGS[c.lang]?.flag || ""}</span></span>
          <span class="cap__txt">${esc(c.text)}</span>
        </span>
      </div>`));
  }
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
          <div class="profcell"><div class="profcell__k">Max lag</div><div class="profcell__v ${maxLag > 1.2 ? "" : ""}">${maxLag.toFixed(1)}s</div></div>
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

  // hook next button
  queueMicrotask(() => { const n = document.getElementById("nextStep"); if (n) n.dataset.go = "recordings"; });
  // draw sparklines after mount
  queueMicrotask(() => drawSparks(wrap));
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
    next: { label: "Open the recordings", go: "recordings" },
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
  queueMicrotask(() => { const n = document.getElementById("nextStep"); if (n) n.dataset.go = "recordings"; });
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
// STAGE 2 — RECORDINGS  (clip list + live waveform cut preview + knobs)
// =============================================================================
function viewRecordings() {
  const clips = clipModel();
  const sel = clips[state.selectedClip] || clips[0];
  const regions = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
  const wrap = el(`<div></div>`);
  const needTune = clips.filter((c) => c.needsTune).length;
  wrap.innerHTML = header({
    eyebrow: "Stage 2 · Recordings",
    title: "Recordings",
    sub: `${clips.length} WAV clips in <b>${esc(session().folder)}</b> · ${needTune ? `<span style='color:var(--warn)'>${needTune} need silence tuning</span>` : "all tuned"}`,
    next: { label: "Run the transcript", go: "transcript" },
  });

  const grid = el(`<div class="grid cols-rec"></div>`);

  // ---- LEFT: clip list ----
  const listPanel = el(`
    <div class="panel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">📁</span>Clips</div>
        <div class="panel__hint">${clips.length} WAVs</div>
      </div>
      <div class="panel__body flush"><div class="cliplist"></div></div>
    </div>`);
  const list = listPanel.querySelector(".cliplist");
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
    node.addEventListener("click", () => { state.selectedClip = i; render(); });
    list.appendChild(node);
  });
  grid.appendChild(listPanel);

  // ---- RIGHT: waveform + knobs (two grouped panels) ----
  const right = el(`<div class="grid" style="gap:14px"></div>`);

  const wavePanel = el(`
    <div class="panel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">🌊</span>Waveform · strip-silence preview</div>
        <div class="panel__hint">re-cuts live as you drag</div>
      </div>
      <div class="wavewrap">
        <div class="wave-meta">
          <span class="wave-meta__name">${esc(sel.t)} · ${esc(speakerById(sel.sp)?.name || sel.sp)} · ${clock(REP_WAV.durationS)}</span>
          <div class="wave-stats">
            <div class="wstat"><span class="wstat__v accent" id="statClips">${regions.clips}</span><span class="wstat__k">clips</span></div>
            <div class="wstat"><span class="wstat__v" id="statSpeech">${regions.speechS}s</span><span class="wstat__k">speech</span></div>
            <div class="wstat"><span class="wstat__v" id="statTrim">${(REP_WAV.durationS - regions.speechS).toFixed(1)}s</span><span class="wstat__k">trimmed</span></div>
          </div>
        </div>
        <div class="wavecanvas-wrap"><canvas id="waveCanvas" width="1640" height="336"></canvas></div>
        <div class="wave-axis"><span>0:00</span><span>${clock(REP_WAV.durationS / 2)}</span><span>${clock(REP_WAV.durationS)}</span></div>
        <div class="wave-legend">
          <span><span class="sw" style="background:linear-gradient(180deg,#f5a623,#b87a12)"></span>kept (speech clip)</span>
          <span><span class="sw" style="background:#2a313c"></span>dropped (silence)</span>
          <span class="dim">│ = cut point</span>
        </div>
      </div>
    </div>`);
  right.appendChild(wavePanel);

  const k = state.knobs;
  const knobPanel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">🎚️</span>Strip-silence knobs</div><div class="panel__hint">drag → re-cut</div></div>
      <div class="panel__body">
        <div class="knobs">
          <div class="knob">
            <div class="knob__top"><span class="knob__label">Min silence gap</span><span class="knob__val" id="vGap">${k.minSilenceMs} ms</span></div>
            <input type="range" id="kGap" min="150" max="1500" step="50" value="${k.minSilenceMs}">
            <div class="knob__desc">Longer gap → fewer, longer clips (merges across short pauses).</div>
          </div>
          <div class="knob">
            <div class="knob__top"><span class="knob__label">Speech floor</span><span class="knob__val" id="vFloor">${k.speechFloorDb} dB</span></div>
            <input type="range" id="kFloor" min="-60" max="-25" step="1" value="${k.speechFloorDb}">
            <div class="knob__desc">Lower toward −60 → room tone counts as speech, gaps vanish → 1 clip.</div>
          </div>
          <div class="knob">
            <div class="knob__top"><span class="knob__label">Edge pad</span><span class="knob__val" id="vPad">${k.padMs} ms</span></div>
            <input type="range" id="kPad" min="0" max="500" step="25" value="${k.padMs}">
            <div class="knob__desc">Padding added before/after each kept region.</div>
          </div>
          <div class="recount">
            <span>⤷</span><span>This WAV cuts into <b id="reCount">${regions.clips}</b> speech clip${regions.clips !== 1 ? "s" : ""}, keeping <b id="reSpeech">${regions.speechS}s</b> of ${REP_WAV.durationS}s.</span>
          </div>
        </div>
      </div>
    </div>`);
  right.appendChild(knobPanel);
  grid.appendChild(right);

  wrap.appendChild(grid);
  queueMicrotask(() => { const n = document.getElementById("nextStep"); if (n) n.dataset.go = "transcript"; });
  return wrap;
}

function afterRecordings() {
  drawWaveform();
  const wire = (id, vId, fmt, key, mul = 1) => {
    const inp = document.getElementById(id);
    if (!inp) return;
    inp.addEventListener("input", () => {
      state.knobs[key] = Number(inp.value) * mul;
      document.getElementById(vId).textContent = fmt(Number(inp.value));
      const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
      document.getElementById("reCount").textContent = r.clips;
      document.getElementById("reSpeech").textContent = `${r.speechS}s`;
      document.getElementById("statClips").textContent = r.clips;
      document.getElementById("statSpeech").textContent = `${r.speechS}s`;
      document.getElementById("statTrim").textContent = `${(REP_WAV.durationS - r.speechS).toFixed(1)}s`;
      drawWaveform();
      // keep the spine chip honest: the journey reflects current cut state
      renderSpine();
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

// =============================================================================
// STAGE 3 — TRANSCRIPT  (dense line-oriented + engine picker + audit)
// =============================================================================
function viewTranscript() {
  const sess = session();
  const wrap = el(`<div></div>`);

  if (!sess.hasTranscript) {
    wrap.innerHTML = header({
      eyebrow: "Stage 3 · Transcript",
      title: "Transcript",
      sub: `<b>${esc(sess.label)}</b> has not been transcribed yet`,
    });
    wrap.appendChild(el(`
      <div class="panel"><div class="panel__body"><div class="empty">
        <div style="font-size:30px;margin-bottom:8px">📝</div>
        <div style="font-weight:600;color:var(--ink-2);margin-bottom:4px">No transcript for this session yet</div>
        <div>Pick an engine and run it on the ${sess.wavCount} recorded clips. This stage stays empty until the journey reaches it.</div>
      </div></div></div>`));
    return wrap;
  }

  const tx = TRANSCRIPT;
  const suppressed = tx.lines.filter((l) => l.suppressed).length;
  const low = tx.lines.filter((l) => l.lowConfidence).length;
  wrap.innerHTML = header({
    eyebrow: "Stage 3 · Transcript",
    title: "Transcript",
    sub: `merged · <span class='mono'>${esc(tx.model)}</span> on <span class='mono'>${esc(tx.backend)}</span> · ${tx.translated ? "<span style='color:#8fd0ff'>contains translations</span>" : "no translation"}`,
    next: { label: "Review people", go: "people", ghost: true },
  });

  const grid = el(`<div class="grid cols-tx"></div>`);

  // ---- LEFT: dense line-oriented transcript ----
  const txPanel = el(`
    <div class="panel">
      <div class="panel__head">
        <div class="panel__title"><span class="ic">📝</span>Merged transcript</div>
        <div class="panel__hint">${tx.lines.length} lines · ${low} low-conf · ${suppressed} suppressed</div>
      </div>
      <div class="panel__body flush"><div class="tx"></div></div>
    </div>`);
  const txBody = txPanel.querySelector(".tx");
  for (const ln of tx.lines) {
    const cls = ln.suppressed ? "sup" : ln.lowConfidence ? "low" : "";
    let badges = "";
    if (ln.translatedFrom) badges += `<span class="txbadge tr">${esc(ln.translatedFrom)}→en</span>`;
    if (ln.lowConfidence) badges += `<span class="txbadge low">low ${(ln.confidence ?? 0).toFixed(2)}</span>`;
    if (ln.suppressed) badges += `<span class="txbadge sup">suppressed · ${esc(ln.matchedRule || "rule")}</span>`;
    txBody.appendChild(el(`
      <div class="txline ${cls}">
        <span class="txline__t">${clock(ln.t)}</span>
        <span class="txline__who"><span class="av sm spk-${ln.spk}"></span><span class="txline__whoname spk-ink-${ln.spk}">${esc(ln.speaker)}</span><span class="flag">${LANGS[ln.lang]?.flag || ""}</span></span>
        <span class="txline__txt">${esc(ln.text)}${badges}</span>
      </div>`));
  }
  grid.appendChild(txPanel);

  // ---- RIGHT: stacked grouped panels (speaking time, engine, audit) ----
  const right = el(`<div class="grid" style="gap:14px"></div>`);

  // speaking time
  const stPanel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">⏱️</span>Speaking time</div><div class="panel__hint">${clockH(tx.durationS)} total</div></div>
      <div class="panel__body"><div class="spk-time"></div></div>
    </div>`);
  const st = stPanel.querySelector(".spk-time");
  for (const s of tx.speakingTime) {
    st.appendChild(el(`
      <div class="sptrow">
        <span class="sptrow__name spk-ink-${s.spk}"><span class="av sm spk-${s.spk}"></span><span class="sptrow__nm">${esc(s.speaker)}</span></span>
        <span class="sptrow__bar"><span class="sptrow__fill spk-bar-${s.spk}" style="width:${s.pct}%"></span></span>
        <span class="sptrow__pct">${s.pct}%</span>
      </div>`));
  }
  right.appendChild(stPanel);

  // engine / model picker
  right.appendChild(viewEnginePanel());

  // audit panel for suppressed/low lines
  const auditPanel = el(`
    <div class="panel">
      <div class="panel__head"><div class="panel__title"><span class="ic">🛡️</span>Filter audit</div><div class="panel__hint">${suppressed + low} flagged</div></div>
      <div class="panel__body"></div>
    </div>`);
  const ab = auditPanel.querySelector(".panel__body");
  const flagged = tx.lines.filter((l) => l.suppressed || l.lowConfidence);
  for (const l of flagged) {
    const kind = l.suppressed ? `suppressed · ${esc(l.matchedRule)}` : `low confidence ${(l.confidence ?? 0).toFixed(2)}`;
    const tone = l.suppressed ? "sup" : "low";
    ab.appendChild(el(`
      <div style="padding:8px 0;border-bottom:1px solid var(--line-soft)">
        <div class="row-between" style="margin-bottom:3px">
          <span class="mono dim" style="font-size:10px">${clock(l.t)} · ${esc(l.speaker)}</span>
          <span class="txbadge ${tone}">${kind}</span>
        </div>
        <div style="font-size:11.5px;color:var(--ink-3);font-style:italic">“${esc(l.text)}”</div>
      </div>`));
  }
  ab.appendChild(el(`<div class="muted" style="font-size:10.5px;padding-top:8px">Suppressed lines are kept out of the merge but logged here so a wrong filter can be audited and restored.</div>`));
  right.appendChild(auditPanel);

  grid.appendChild(right);
  wrap.appendChild(grid);
  queueMicrotask(() => wireEnginePanel());
  return wrap;
}

function viewEnginePanel() {
  const e = state.engine;
  const panel = el(`
    <div class="panel" id="enginePanel">
      <div class="panel__head"><div class="panel__title"><span class="ic">⚙️</span>Engine</div><div class="panel__hint">family · backend</div></div>
      <div class="panel__body"><div class="engine"></div></div>
    </div>`);
  const eng = panel.querySelector(".engine");

  // backend chips (cuda disabled)
  const beChips = APP.backends.map((b) =>
    `<button class="chip ${b.kind === e.backend ? "is-sel" : ""}" data-backend="${b.kind}" ${b.available ? "" : "disabled"}>${esc(b.label)}${b.available ? "" : '<span class="chip__x">n/a</span>'}</button>`
  ).join("");
  eng.appendChild(el(`<div class="eng-row"><span class="eng-cap">Backend</span><div class="chips">${beChips}</div></div>`));

  // model by family
  const fam = el(`<div class="eng-row"><span class="eng-cap">Model · by family</span><div class="famgrid"></div></div>`);
  const fg = fam.querySelector(".famgrid");
  for (const f of MODELS) {
    const block = el(`<div class="fam"><div class="fam__head">${esc(f.family)}</div><div class="fam__models"></div></div>`);
    const fm = block.querySelector(".fam__models");
    for (const m of f.models) {
      const seld = e.family === f.family && e.model === m.id;
      const node = el(`
        <button class="model ${seld ? "is-sel" : ""}" data-family="${esc(f.family)}" data-model="${esc(m.id)}">
          <span class="model__l"><span class="model__name">${esc(m.display)}</span><span class="model__desc">${esc(m.desc)}</span></span>
          <span class="model__dot"></span>
        </button>`);
      fm.appendChild(node);
    }
    fg.appendChild(block);
  }
  eng.appendChild(fam);

  // Canary source/target selects (only when canary selected)
  if (e.family === "canary") {
    const langOpts = (selCode) => ["nb", "da", "en", "sv", "de", "fr"].map((c) =>
      `<option value="${c}" ${c === selCode ? "selected" : ""}>${LANGS[c].flag} ${LANGS[c].name}</option>`).join("");
    eng.appendChild(el(`
      <div class="eng-row">
        <span class="eng-cap">Canary translation</span>
        <div class="selrow">
          <div class="selfield"><label>Source</label><select id="srcLang">${langOpts(e.sourceLang)}</select></div>
          <div style="align-self:flex-end;padding-bottom:9px;color:var(--ink-4)">→</div>
          <div class="selfield"><label>Target</label><select id="tgtLang">${langOpts(e.targetLang)}</select></div>
        </div>
        <div class="translate-note">🌐 Canary translates <b id="trSrc">${LANGS[e.sourceLang].name}</b> → <b id="trTgt">${LANGS[e.targetLang].name}</b> during transcription.</div>
      </div>`));
  }
  return panel;
}

function wireEnginePanel() {
  const panel = document.getElementById("enginePanel");
  if (!panel) return;
  panel.querySelectorAll("[data-backend]").forEach((b) => {
    if (b.disabled) return;
    b.addEventListener("click", () => { state.engine.backend = b.dataset.backend; rerenderEngine(); });
  });
  panel.querySelectorAll("[data-model]").forEach((m) => {
    m.addEventListener("click", () => {
      state.engine.family = m.dataset.family;
      state.engine.model = m.dataset.model;
      rerenderEngine();
    });
  });
  const src = document.getElementById("srcLang"), tgt = document.getElementById("tgtLang");
  if (src) src.addEventListener("change", () => { state.engine.sourceLang = src.value; document.getElementById("trSrc").textContent = LANGS[src.value].name; });
  if (tgt) tgt.addEventListener("change", () => { state.engine.targetLang = tgt.value; document.getElementById("trTgt").textContent = LANGS[tgt.value].name; });
}

function rerenderEngine() {
  const old = document.getElementById("enginePanel");
  if (!old) return;
  const fresh = viewEnginePanel();
  old.replaceWith(fresh);
  wireEnginePanel();
}

// =============================================================================
// STAGE 4 — PEOPLE  (cross-session per-mic profiles + dual language + switch)
// =============================================================================
function viewPeople() {
  const wrap = el(`<div></div>`);
  wrap.innerHTML = header({
    eyebrow: "Stage 4 · People",
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
  const map = { capture: "capture", recordings: "recordings", transcript: "transcript", people: "people" };
  if (map[name]) goStage(map[name]);
};
window.stagesGo = window.gotoView;
window.stagesPickSession = (id) => { if (SESSIONS.some((s) => s.id === id)) { state.sessionId = id; render(); } };
window.stagesSetKnob = (key, val) => { if (key in state.knobs) { state.knobs[key] = val; render(); } };
