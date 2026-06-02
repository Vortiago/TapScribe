// =============================================================================
// TapScribe — "Columns" prototype (progressive drill-down / Miller columns)
//
// State is a DRILL PATH: an array of {kind, id, label}. The roots rail picks
// the root; the navigator shows the parent list; the detail pane renders the
// selected leaf (the rich/dense work area). Only 2–3 columns are ever visible.
// =============================================================================

import {
  MOCK, LANGS, SPEAKERS, MODELS, selectedModel, LIVE_TAPS, LIVE_CAPTIONS,
  SESSIONS, STRIP_DEFAULTS, REP_WAV, TRANSCRIPT, computeRegions, helpers,
  speakerById,
} from "../_shared/mock-data.js";

const { clock, pct } = helpers;
const spkVar = (n) => `var(--spk-${n})`;
const langChip = (code) => {
  const l = LANGS[code] || LANGS.auto;
  return `<span class="chip lang tiny">${l.flag} ${l.name}</span>`;
};
const el = (html) => {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------------------
// Root definitions: the four entry points of the containment tree.
// ---------------------------------------------------------------------------
const ROOTS = [
  { kind: "live", label: "Live", ico: "🟢", badge: () => LIVE_TAPS.filter((t) => t.live).length },
  { kind: "sessions", label: "Sessions", ico: "🗂", badge: () => null },
  { kind: "speakers", label: "Speakers", ico: "👤", badge: () => null },
  { kind: "engines", label: "Engines", ico: "⚙", badge: () => null },
];

// ---------------------------------------------------------------------------
// App state. `path` is the drill stack below the root.
//   path[0] = the selected item in the root's navigator
//   path[1] = a child (speaker-in-session, sub-speaker of a diarized tap)
//   path[2] = a leaf view (clip / transcript)
// ---------------------------------------------------------------------------
const state = {
  root: "live",
  path: [], // [{kind,id,label}]
  // live re-cut knobs (clip detail)
  knobs: { ...STRIP_DEFAULTS },
  // transcript-as language quick switch (speaker detail)
  quickLang: {},
  // engine selection
  engine: { ...selectedModel },
};

// =============================================================================
// RENDER ENTRYPOINT
// =============================================================================
function render() {
  renderRoots();
  renderCrumbs();
  renderNav();
  renderDetail();
}

// ---------------- roots rail ----------------
function renderRoots() {
  const c = document.getElementById("colRoots");
  c.innerHTML = "";
  for (const r of ROOTS) {
    const badge = r.badge();
    const item = el(`
      <div class="root-item ${state.root === r.kind ? "is-active" : ""}" data-root="${r.kind}" role="button" tabindex="0">
        <span class="root-ico">${r.ico}</span>
        <span class="root-label">${r.label}</span>
        ${badge ? `<span class="root-badge">${badge}</span>` : ""}
      </div>`);
    item.addEventListener("click", () => selectRoot(r.kind));
    c.appendChild(item);
  }
  c.appendChild(el(`<div class="roots-spacer"></div>`));
}

// ---------------- breadcrumb ----------------
function renderCrumbs() {
  const c = document.getElementById("crumbs");
  c.innerHTML = "";
  const root = ROOTS.find((r) => r.kind === state.root);
  const crumbs = [{ ico: root.ico, label: root.label, depth: -1 }];
  state.path.forEach((p, i) => crumbs.push({ label: p.label, depth: i, ico: p.ico }));
  crumbs.forEach((cr, i) => {
    if (i > 0) c.appendChild(el(`<span class="crumb-sep">›</span>`));
    const isLeaf = i === crumbs.length - 1 && crumbs.length > 1;
    const b = el(`<button class="crumb ${isLeaf ? "is-leaf" : ""}">${cr.ico ? `<span class="ico">${cr.ico}</span>` : ""}${esc(cr.label)}</button>`);
    if (!isLeaf) {
      b.addEventListener("click", () => {
        if (cr.depth === -1) { state.path = []; }
        else { state.path = state.path.slice(0, cr.depth + 1); }
        render();
      });
    }
    c.appendChild(b);
  });
}

// =============================================================================
// NAVIGATOR — the parent list for the current root (one entity type)
// =============================================================================
function renderNav() {
  const c = document.getElementById("colNav");
  c.innerHTML = "";
  // The navigator reflects the CURRENT drill level (true Miller-columns): for
  // sessions it switches to the session's contents once you drill in, so the
  // detail pane only ever holds the deepest leaf — never the whole chain.
  if (state.root === "live") {
    if (state.path[1]?.kind === "subspeaker") navTapContents(c);     // diarized room → its voices
    else navLive(c);
  }
  else if (state.root === "sessions") {
    const p = state.path;
    if (p[2]?.kind === "clip") navRecordings(c);                     // depth 3: detail=clip, nav=recordings
    else if (p.length >= 2) navSessionContents(c);                   // depth 2: detail=leaf, nav=session contents
    else navSessions(c);                                             // depth 0/1: nav=sessions list
  }
  else if (state.root === "speakers") navSpeakers(c);
  else if (state.root === "engines") navEngines(c);
}

function navHead(title, count) {
  return el(`<div class="col-head"><span>${title}</span>${count != null ? `<span class="count">${count}</span>` : ""}</div>`);
}

function navItem({ edgeVar, title, sub, hasChildren, selected, onClick, right }) {
  const item = el(`
    <div class="nav-item ${selected ? "is-selected" : ""} ${hasChildren ? "" : "no-children"}" role="button" tabindex="0">
      <span class="nav-edge" style="background:${edgeVar}"></span>
      <div class="nav-body">
        <div class="nav-title"><span class="name">${title}</span></div>
        <div class="nav-sub">${sub}</div>
      </div>
      <div class="row-flex">${right || ""}${hasChildren ? `<span class="nav-chev">›</span>` : ""}</div>
    </div>`);
  item.addEventListener("click", onClick);
  return item;
}

// ---- Live taps ----
function navLive(c) {
  c.appendChild(navHead("Live taps", `${LIVE_TAPS.filter((t) => t.live).length} live`));
  const list = el(`<div class="nav-list"></div>`);
  for (const t of LIVE_TAPS) {
    const sel = state.path[0]?.kind === "tap" && state.path[0]?.id === t.identity;
    const spark = `<span class="mini-spark">${t.levels.map((v) => `<i style="height:${Math.max(2, Math.round(v * 14))}px"></i>`).join("")}</span>`;
    const gate = `<span class="gate-dot ${t.gateOpen ? "open" : ""}" title="gate"></span>`;
    const sub = `${gate}<span>${(LANGS[t.lang] || LANGS.auto).flag}</span>${t.identity === "room-oslo" ? `<span class="dot-sep"></span><span>diarized</span>` : ""}${t.record ? "" : `<span class="dot-sep"></span><span style="color:var(--ink-3)">rec off</span>`}`;
    list.appendChild(navItem({
      edgeVar: spkVar(t.spk),
      title: esc(t.name),
      sub,
      hasChildren: true,
      selected: sel,
      right: t.live ? spark : `<span class="chip muted tiny">idle</span>`,
      onClick: () => { state.path = [{ kind: "tap", id: t.identity, label: t.name, ico: "🟢" }]; render(); },
    }));
  }
  c.appendChild(list);
}

// ---- Diarized tap contents: its split voices (live drill level) ----
function navTapContents(c) {
  const room = speakerById(state.path[0].id);
  c.appendChild(navHead(esc(room.name.split(" ")[0] + " voices"), room.diarizedInto.length));
  const list = el(`<div class="nav-list"></div>`);
  for (const d of room.diarizedInto) {
    const sel = state.path[1]?.id === d.label;
    list.appendChild(navItem({
      edgeVar: spkVar(d.spk),
      title: esc(d.label),
      sub: `<span>${(LANGS[d.lang] || LANGS.auto).flag}</span><span class="dot-sep"></span><span>${d.talkPct}% talk</span>`,
      hasChildren: false,
      selected: sel,
      onClick: () => { state.path = [state.path[0], { kind: "subspeaker", id: d.label, label: d.label, ico: "👤", spk: d.spk, lang: d.lang, talkPct: d.talkPct }]; render(); },
    }));
  }
  c.appendChild(list);
}

// ---- Sessions ----
function navSessions(c) {
  c.appendChild(navHead("Sessions", SESSIONS.length));
  const list = el(`<div class="nav-list"></div>`);
  for (const s of SESSIONS) {
    const sel = state.path[0]?.kind === "session" && state.path[0]?.id === s.id;
    const label = s.label || "Untitled session";
    const date = new Date(s.startedAt);
    const when = `${date.toLocaleDateString("en-GB", { day: "2-digit", month: "short" })}`;
    const langs = s.langs.map((l) => (LANGS[l] || LANGS.auto).flag).join(" ");
    const sub = `<span>${when}</span><span class="dot-sep"></span><span>${clock(s.durationS)}</span><span class="dot-sep"></span><span>${s.wavCount} wav</span><span class="dot-sep"></span><span>${langs}</span>`;
    const right = s.current ? `<span class="chip ok tiny">current</span>` : (s.hasTranscript ? "" : `<span class="chip muted tiny">no tx</span>`);
    list.appendChild(navItem({
      edgeVar: "var(--ink-3)",
      title: esc(label),
      sub,
      hasChildren: true,
      selected: sel,
      right,
      onClick: () => { state.path = [{ kind: "session", id: s.id, label, ico: "🗂" }]; render(); },
    }));
  }
  c.appendChild(list);
}

// Deterministic recording list for a speaker within a session (shared by the
// recordings navigator and any per-WAV detail).
function recordingsFor(sp) {
  const n = sp.isRoom ? 9 : 6;
  const recs = [];
  let base = 12;
  for (let i = 0; i < n; i++) {
    const dur = 8 + ((i * 37) % 41);
    recs.push({ idx: i + 1, startS: base, durationS: dur, isRep: i === 1 });
    base += dur + 4 + ((i * 13) % 9);
  }
  return recs;
}

// ---- Session contents: the speakers + the merged transcript (depth-2 nav) ----
function navSessionContents(c) {
  const s = SESSIONS.find((x) => x.id === state.path[0].id);
  c.appendChild(navHead(esc(s.label || "Session"), `${s.speakers.length} + tx`));
  const list = el(`<div class="nav-list"></div>`);
  for (const sid of s.speakers) {
    const sp = speakerById(sid);
    const sel = state.path[1]?.kind === "sessionSpeaker" && state.path[1]?.id === sid;
    const sub = `<span>${esc(sp.mic.label)}</span>${sp.isRoom ? `<span class="dot-sep"></span><span>diarized</span>` : ""}`;
    list.appendChild(navItem({
      edgeVar: spkVar(sp.spk),
      title: esc(sp.name),
      sub,
      hasChildren: true,
      selected: sel,
      onClick: () => { state.path = [state.path[0], { kind: "sessionSpeaker", id: sid, label: sp.name, ico: "👤" }]; render(); },
    }));
  }
  if (s.hasTranscript) {
    const sel = state.path[1]?.kind === "transcript";
    list.appendChild(navItem({
      edgeVar: "var(--accent)",
      title: "Merged transcript",
      sub: `<span>${TRANSCRIPT.lines.length} lines</span><span class="dot-sep"></span><span>${TRANSCRIPT.model}</span>`,
      hasChildren: false,
      selected: sel,
      right: `<span style="font-size:13px">📝</span>`,
      onClick: () => { state.path = [state.path[0], { kind: "transcript", id: "tx", label: "Transcript", ico: "📝" }]; render(); },
    }));
  }
  c.appendChild(list);
}

// ---- Recordings of a speaker-in-session (depth-3 nav; each is a WAV) ----
function navRecordings(c) {
  const sp = speakerById(state.path[1].id);
  const recs = recordingsFor(sp);
  c.appendChild(navHead(`${esc(sp.name.split(" ")[0])} · WAVs`, recs.length));
  const list = el(`<div class="nav-list"></div>`);
  for (const r of recs) {
    const sel = state.path[2]?.kind === "clip" && state.path[2]?.id === `clip-${r.idx}`;
    list.appendChild(navItem({
      edgeVar: spkVar(sp.spk),
      title: `Clip #${r.idx}`,
      sub: `<span>${clock(r.startS)} → ${clock(r.startS + r.durationS)}</span><span class="dot-sep"></span><span>${r.durationS}s</span>`,
      hasChildren: false,
      selected: sel,
      right: r.isRep ? `<span class="chip solid tiny">preview</span>` : "",
      onClick: () => {
        state.knobs = { ...STRIP_DEFAULTS };
        state.path = [state.path[0], state.path[1], { kind: "clip", id: `clip-${r.idx}`, label: `Clip #${r.idx}`, ico: "🎵", durationS: r.durationS }];
        render();
      },
    }));
  }
  c.appendChild(list);
}

// ---- Speakers ----
function navSpeakers(c) {
  c.appendChild(navHead("Speakers", SPEAKERS.length));
  const list = el(`<div class="nav-list"></div>`);
  for (const s of SPEAKERS) {
    const sel = state.path[0]?.kind === "speaker" && state.path[0]?.id === s.id;
    const sub = `<span>${s.mic.label}</span><span class="dot-sep"></span><span>${(LANGS[s.primaryLang] || LANGS.auto).flag}${s.secondaryLang ? " " + (LANGS[s.secondaryLang]).flag : ""}</span>`;
    const right = s.isRoom ? `<span class="chip tiny" style="color:var(--spk-2);border-color:#e7d3a8">room</span>` : "";
    list.appendChild(navItem({
      edgeVar: spkVar(s.spk),
      title: esc(s.name),
      sub,
      hasChildren: true,
      selected: sel,
      right,
      onClick: () => { state.path = [{ kind: "speaker", id: s.id, label: s.name, ico: "👤" }]; render(); },
    }));
  }
  c.appendChild(list);
}

// ---- Engines (grouped by family) ----
function navEngines(c) {
  c.appendChild(navHead("Engines", "by family"));
  const list = el(`<div class="nav-list"></div>`);
  for (const fam of MODELS) {
    list.appendChild(el(`<div class="nav-group-label">${esc(fam.family)}</div>`));
    for (const m of fam.models) {
      const sel = state.path[0]?.kind === "model" && state.path[0]?.id === m.id;
      const translate = fam.family === "canary";
      const sub = `<span>${esc(m.desc)}</span>${translate ? `<span class="dot-sep"></span><span style="color:var(--accent-ink)">translates</span>` : ""}`;
      list.appendChild(navItem({
        edgeVar: sel ? "var(--accent)" : "var(--rule)",
        title: esc(m.display),
        sub,
        hasChildren: true,
        selected: sel,
        right: m.id === state.engine.model ? `<span class="chip solid tiny">active</span>` : "",
        onClick: () => { state.path = [{ kind: "model", id: m.id, label: m.display, ico: "⚙", family: fam.family }]; render(); },
      }));
    }
  }
  c.appendChild(list);
}

// =============================================================================
// DETAIL PANE — the rich, dense, rightmost work area
// =============================================================================
function renderDetail() {
  const c = document.getElementById("colDetail");
  c.innerHTML = "";
  const top = state.path[0];
  if (!top) { c.appendChild(emptyHint(state.root)); return; }

  if (top.kind === "tap") return detailTap(c);
  if (top.kind === "session") return detailSessionOrChild(c);
  if (top.kind === "speaker") return detailSpeaker(c);
  if (top.kind === "model") return detailModel(c);
}

function emptyHint(root) {
  const map = {
    live: ["🟢", "Pick a live tap", "Each tap shows its level, lag, gate and live captions. A room tap drills into its diarized speakers."],
    sessions: ["🗂", "Pick a session", "Drill into a session to see its speakers, clips and the merged transcript."],
    speakers: ["👤", "Pick a speaker", "Each speaker carries a per-mic profile reused across sessions, plus primary + secondary language."],
    engines: ["⚙", "Pick a model", "Models are grouped by family across mlx / cuda / cpu backends. Canary adds source → target translation."],
  };
  const [big, t, d] = map[root];
  return el(`<div class="empty"><div><div class="big">${big}</div><div class="muted-strong">${t}</div><div class="note lead" style="max-width:320px;margin:6px auto 0">${d}</div></div></div>`);
}

const colHead = (title, note) => `<div class="col-head"><span>${title}</span>${note ? `<span class="head-note">${note}</span>` : ""}</div>`;

// ---------------- LIVE TAP detail (captions + waveform; diarization) -------
function detailTap(c) {
  // Drilled into a diarized sub-speaker → focused isolated-voice view.
  if (state.path[1]?.kind === "subspeaker") return detailSubSpeaker(c);

  const t = LIVE_TAPS.find((x) => x.identity === state.path[0].id);
  const sp = speakerById(t.identity);
  c.appendChild(el(colHead("Live tap", `lag ${t.lagS.toFixed(1)}s`)));
  const wrap = el(`<div class="detail-wrap"></div>`);

  // header
  wrap.appendChild(el(`
    <div class="dh">
      <div class="dh-avatar" style="background:${spkVar(t.spk)}">${sp ? sp.initials : "··"}</div>
      <div class="dh-main">
        <div class="dh-title">${esc(t.name)}</div>
        <div class="dh-meta">
          <span class="pill pill-rec ${t.record ? "" : "off"}">● ${t.record ? "REC" : "rec off"}</span>
          <span class="pill pill-live ${t.live ? "" : "off"}">live</span>
          <span class="row-flex"><span class="gate-dot ${t.gateOpen ? "open" : ""}"></span> gate ${t.gateOpen ? "open" : "closed"}</span>
          <span class="dot-sep"></span>${langChip(t.lang)}
          ${sp ? `<span class="chip muted tiny">${esc(sp.mic.label)}</span>` : ""}
        </div>
      </div>
    </div>`));

  // level + lag stats + live waveform
  const liveCard = el(`
    <div class="card">
      <div class="card-head"><h3>Signal</h3><span class="head-note">live monitor</span></div>
      <div class="card-body">
        <div class="row-flex" style="gap:20px">
          <div style="flex:1;min-width:200px">
            <div class="row-flex" style="justify-content:space-between"><span class="note">Input level</span><span class="note" style="font-family:var(--mono)">${Math.round(t.level * 100)}%</span></div>
            <div class="meter mt8" style="width:100%"><span style="width:${Math.round(t.level * 100)}%"></span></div>
          </div>
          <div class="stats">
            <div class="stat"><div class="k">Lag</div><div class="v">${t.lagS.toFixed(1)}<small>s</small></div></div>
            <div class="stat"><div class="k">Gate</div><div class="v" style="color:${t.gateOpen ? "var(--ok)" : "var(--ink-3)"}">${t.gateOpen ? "open" : "shut"}</div></div>
          </div>
        </div>
        <canvas class="livewave mt12" width="820" height="128"></canvas>
      </div>
    </div>`);
  wrap.appendChild(liveCard);

  // diarization (only for room tap) — a PROPERTY of the tap, shown as drillable
  if (sp && sp.isRoom && sp.diarizedInto) {
    const dcard = el(`
      <div class="card">
        <div class="card-head"><h3>Diarization</h3><span class="head-note">one tap · ${sp.diarizedInto.length} voices · drill to isolate</span></div>
        <div class="card-body"><div class="diar-grid"></div></div>
      </div>`);
    const grid = dcard.querySelector(".diar-grid");
    for (const d of sp.diarizedInto) {
      const card = el(`
        <div class="diar-card" role="button" tabindex="0">
          <div class="dc-head"><span class="spk-dot" style="background:${spkVar(d.spk)}"></span> ${esc(d.label)} <span class="nav-chev" style="margin-left:auto">›</span></div>
          <div class="row-flex mt8">${langChip(d.lang)}<span class="chip muted tiny">${d.talkPct}% of talk</span></div>
          <div class="st-bar dc-talk"><span style="width:${d.talkPct}%;background:${spkVar(d.spk)}"></span></div>
        </div>`);
      card.addEventListener("click", () => {
        state.path = [state.path[0], { kind: "subspeaker", id: d.label, label: d.label, ico: "👤", spk: d.spk, lang: d.lang, talkPct: d.talkPct }];
        render();
      });
      grid.appendChild(card);
    }
    wrap.appendChild(dcard);
  }

  // live captions for this tap
  const caps = LIVE_CAPTIONS.filter((cap) => {
    if (sp && sp.isRoom) return cap.speaker.startsWith("Oslo");
    return cap.spk === t.spk;
  });
  const capCard = el(`
    <div class="card">
      <div class="card-head"><h3>Live captions</h3><span class="head-note">settled + in-flight · tagged by speaker & language</span></div>
      <div class="card-body tight"><div class="cap-feed"></div></div>
    </div>`);
  const feed = capCard.querySelector(".cap-feed");
  const shown = caps.length ? caps : [{ t: t.lagS, speaker: t.name, spk: t.spk, lang: t.lang, text: t.buffer || "(silence — gate closed)", inflight: !!t.buffer }];
  for (const cap of shown) {
    feed.appendChild(el(`
      <div class="cap ${cap.inflight ? "inflight" : ""}">
        <span class="t">${clock(cap.t)}</span>
        <div>
          <div class="who"><span class="spk-dot" style="background:${spkVar(cap.spk)}"></span>${esc(cap.speaker)} ${langChip(cap.lang)}</div>
          <div class="ctext">${esc(cap.text)}</div>
        </div>
      </div>`));
  }
  wrap.appendChild(capCard);

  c.appendChild(wrap);
  // draw the live waveform after layout
  requestAnimationFrame(() => drawLiveWave(liveCard.querySelector("canvas"), t));

  // sub-speaker leaf?
  if (state.path[1]?.kind === "subspeaker") detailSubSpeaker(c);
}

function drawLiveWave(cv, tap) {
  const ctx = cv.getContext("2d");
  const w = cv.width, h = cv.height;
  ctx.clearRect(0, 0, w, h);
  // backdrop grid
  ctx.strokeStyle = "rgba(255,255,255,0.05)";
  ctx.lineWidth = 1;
  for (let x = 0; x < w; x += 40) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
  // a scrolling bar meter from the tap's levels, repeated to fill width
  const lv = tap.levels;
  const bars = 90;
  const bw = w / bars;
  for (let i = 0; i < bars; i++) {
    const base = lv[i % lv.length];
    const jitter = 0.85 + 0.3 * Math.abs(Math.sin(i * 1.7));
    const v = Math.min(1, base * jitter);
    const bh = Math.max(2, v * (h * 0.86));
    const x = i * bw;
    const grd = ctx.createLinearGradient(0, h, 0, h - bh);
    grd.addColorStop(0, tap.gateOpen ? "#4f5bd5" : "#39405a");
    grd.addColorStop(1, tap.gateOpen ? "#8b95ff" : "#4a5270");
    ctx.fillStyle = grd;
    const y = (h - bh) / 2;
    ctx.fillRect(x + 1, y, Math.max(1, bw - 2), bh);
  }
  // center line
  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
}

// sub-speaker (drilled from a diarized tap) — focused isolated-voice view
function detailSubSpeaker(c) {
  const d = state.path[1];
  const room = speakerById(state.path[0].id);
  c.appendChild(el(colHead("Isolated voice", `from ${esc(room ? room.name : "room tap")}`)));
  const wrap = el(`<div class="detail-wrap"></div>`);
  wrap.appendChild(el(`
    <div class="dh">
      <div class="dh-avatar" style="background:${spkVar(d.spk)}">${d.label.split(" ").map((x) => x[0]).join("")}</div>
      <div class="dh-main">
        <div class="dh-title">${esc(d.label)}</div>
        <div class="dh-meta">${langChip(d.lang)}<span class="chip muted tiny">${d.talkPct}% of room talk</span><span class="chip muted tiny">diarized — not a separate mic</span></div>
      </div>
    </div>`));
  wrap.appendChild(el(`
    <div class="card">
      <div class="card-head"><h3>Talk share</h3><span class="head-note">within the room tap</span></div>
      <div class="card-body">
        <div class="st-bar" style="height:14px"><span style="width:${d.talkPct}%;background:${spkVar(d.spk)}"></span></div>
        <p class="note mt8">Diarization splits the single Oslo Room tap into who-spoke-when. This voice inherits the room mic profile (gate / floor) but its captions and clips are attributed to <strong>${esc(d.label)}</strong>.</p>
      </div>
    </div>`));
  // this voice's captions only
  const caps = LIVE_CAPTIONS.filter((cap) => cap.speaker.endsWith(d.label));
  const capCard = el(`
    <div class="card">
      <div class="card-head"><h3>Captions · ${esc(d.label)}</h3><span class="head-note">this voice only</span></div>
      <div class="card-body tight"><div class="cap-feed"></div></div>
    </div>`);
  const feed = capCard.querySelector(".cap-feed");
  const shown = caps.length ? caps : [{ t: 0, speaker: d.label, spk: d.spk, lang: d.lang, text: "(no settled lines yet for this voice)" }];
  for (const cap of shown) {
    feed.appendChild(el(`
      <div class="cap">
        <span class="t">${clock(cap.t)}</span>
        <div>
          <div class="who"><span class="spk-dot" style="background:${spkVar(cap.spk)}"></span>${esc(cap.speaker)} ${langChip(cap.lang)}</div>
          <div class="ctext">${esc(cap.text)}</div>
        </div>
      </div>`));
  }
  wrap.appendChild(capCard);
  c.appendChild(wrap);
}

// ---------------- SESSION dispatch: render ONLY the deepest leaf -----------
// The parent chain lives in the breadcrumb + the navigator column, NOT stacked
// in the detail pane. So the detail pane is always exactly one focused thing.
function detailSessionOrChild(c) {
  const leaf = state.path[state.path.length - 1];
  if (leaf.kind === "clip") return detailClip(c);
  if (leaf.kind === "transcript") return detailTranscript(c);
  if (leaf.kind === "sessionSpeaker") return detailSessionSpeaker(c);
  return detailSessionOverview(c);
}

// session overview (depth 1) — header, stats, and the drill targets
function detailSessionOverview(c) {
  const s = SESSIONS.find((x) => x.id === state.path[0].id);
  c.appendChild(el(colHead("Session", s.current ? "recording now" : clock(s.durationS))));
  const wrap = el(`<div class="detail-wrap"></div>`);
  const langs = s.langs.map((l) => langChip(l)).join("");
  wrap.appendChild(el(`
    <div class="dh">
      <div class="dh-avatar" style="background:#2b3344">🗂</div>
      <div class="dh-main">
        <div class="dh-title">${esc(s.label || "Untitled session")} ${s.current ? `<span class="chip ok tiny" style="vertical-align:3px">● live</span>` : ""}</div>
        <div class="dh-meta">
          <span>${new Date(s.startedAt).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
          <span class="dot-sep"></span><span>${clock(s.durationS)}</span>
          <span class="dot-sep"></span><span>${s.wavCount} recordings</span>
          ${langs}
        </div>
      </div>
    </div>`));
  wrap.appendChild(el(`
    <div class="stats mt12">
      <div class="stat"><div class="k">Duration</div><div class="v">${clock(s.durationS)}</div></div>
      <div class="stat"><div class="k">Recordings</div><div class="v">${s.wavCount}</div></div>
      <div class="stat"><div class="k">Speakers</div><div class="v">${s.speakers.length}</div></div>
      <div class="stat"><div class="k">Transcript</div><div class="v" style="color:${s.hasTranscript ? "var(--ok)" : "var(--ink-3)"};font-size:13px">${s.hasTranscript ? "merged" : "pending"}</div></div>
    </div>`));

  const spCard = el(`
    <div class="card">
      <div class="card-head"><h3>Speakers in session</h3><span class="head-note">drill a speaker → their recordings</span></div>
      <div class="card-body tight"></div>
    </div>`);
  const body = spCard.querySelector(".card-body");
  for (const sid of s.speakers) {
    const sp = speakerById(sid);
    const row = el(`
      <div class="clip-row" role="button" tabindex="0" style="grid-template-columns:auto 1fr auto auto;cursor:pointer">
        <span class="spk-dot" style="background:${spkVar(sp.spk)}"></span>
        <span class="muted-strong">${esc(sp.name)} ${sp.isRoom ? `<span class="chip tiny" style="color:var(--spk-2);border-color:#e7d3a8">diarized</span>` : ""}</span>
        <span class="chip muted tiny">${esc(sp.mic.label)}</span>
        <span class="nav-chev">›</span>
      </div>`);
    row.addEventListener("click", () => {
      state.path = [state.path[0], { kind: "sessionSpeaker", id: sid, label: sp.name, ico: "👤" }];
      render();
    });
    body.appendChild(row);
  }
  wrap.appendChild(spCard);

  if (s.hasTranscript) {
    const txLink = el(`
      <div class="card flat" style="margin-top:12px">
        <div class="card-head" style="cursor:pointer">
          <h3>Merged transcript ›</h3>
          <span class="head-note">${TRANSCRIPT.lines.length} lines · ${TRANSCRIPT.model} · ${TRANSCRIPT.translated ? "incl. translation" : ""}</span>
        </div>
      </div>`);
    txLink.querySelector(".card-head").addEventListener("click", () => {
      state.path = [state.path[0], { kind: "transcript", id: "tx", label: "Transcript", ico: "📝" }];
      render();
    });
    wrap.appendChild(txLink);
  }
  c.appendChild(wrap);
}

// speaker within a session → just the per-WAV recording listing (focused)
function detailSessionSpeaker(c) {
  const sp = speakerById(state.path[1].id);
  const recs = recordingsFor(sp);
  c.appendChild(el(colHead("Speaker recordings", `${recs.length} WAVs`)));
  const wrap = el(`<div class="detail-wrap"></div>`);
  wrap.appendChild(el(`
    <div class="dh">
      <div class="dh-avatar" style="background:${spkVar(sp.spk)};width:38px;height:38px;font-size:13px">${sp.initials}</div>
      <div class="dh-main">
        <div class="dh-title" style="font-size:16px">${esc(sp.name)}</div>
        <div class="dh-meta"><span class="chip muted tiny">${esc(sp.mic.label)}</span>${sp.isRoom ? `<span class="chip tiny" style="color:var(--spk-2);border-color:#e7d3a8">diarized room</span>` : ""}<span>in ${esc(state.path[0].label)}</span></div>
      </div>
    </div>`));
  const card = el(`
    <div class="card">
      <div class="card-head"><h3>Recordings</h3><span class="head-note">each is a WAV · drill a clip for its waveform + strip-silence cuts</span></div>
      <div class="card-body tight"></div>
    </div>`);
  const body = card.querySelector(".card-body");
  for (const r of recs) {
    const row = el(`
      <div class="clip-row" role="button" tabindex="0" style="cursor:pointer">
        <span class="idx">#${r.idx}</span>
        <span class="rng">${clock(r.startS)} → ${clock(r.startS + r.durationS)}</span>
        <span class="clip-bar" style="width:${Math.min(180, r.durationS * 3.6)}px"></span>
        <span class="dur">${r.durationS}s ${r.isRep ? `<span class="chip solid tiny">preview</span>` : ""} <span class="nav-chev">›</span></span>
      </div>`);
    row.addEventListener("click", () => {
      state.knobs = { ...STRIP_DEFAULTS };
      state.path = [state.path[0], state.path[1], { kind: "clip", id: `clip-${r.idx}`, label: `Clip #${r.idx}`, ico: "🎵", durationS: r.durationS }];
      render();
    });
    body.appendChild(row);
  }
  wrap.appendChild(card);
  c.appendChild(wrap);
}

// ---------------- CLIP detail: waveform + strip-silence live re-cut --------
function detailClip(c) {
  const leaf = state.path[state.path.length - 1];
  const spId = state.path[1]?.id;
  const sp = spId ? speakerById(spId) : null;
  c.appendChild(el(colHead("Clip · strip-silence", `${REP_WAV.durationS}s WAV`)));
  const wrap = el(`<div class="detail-wrap"></div>`);
  wrap.appendChild(el(`
    <div class="dh" style="margin-bottom:2px">
      <div class="dh-avatar" style="background:${sp ? spkVar(sp.spk) : "var(--accent)"};width:38px;height:38px;font-size:15px">🎵</div>
      <div class="dh-main">
        <div class="dh-title" style="font-size:16px">${esc(leaf.label)} ${sp ? `<span class="chip muted tiny" style="vertical-align:2px">${esc(sp.name)}</span>` : ""}</div>
        <div class="dh-meta"><span class="note" style="font-family:var(--mono)">${esc(REP_WAV.name)}</span></div>
      </div>
    </div>`));
  c.appendChild(wrap);
  const card = el(`
    <div class="card">
      <div class="card-head"><h3>Waveform &amp; cut preview</h3><span class="head-note">drag a knob → re-cuts live</span></div>
      <div class="card-body">
        <div class="wave-canvas-wrap"><canvas class="wave" width="1640" height="264"></canvas></div>
        <div class="wave-legend">
          <span><span class="swatch" style="background:#c7cfdd"></span>silence (dropped)</span>
          <span><span class="swatch" style="background:#4f5bd5"></span>speech clip (kept)</span>
          <span><span class="swatch" style="background:#e0483c;width:3px;border-radius:2px"></span>cut point</span>
          <span class="recut-stat" style="margin-left:auto;color:var(--ink-2)"></span>
        </div>
        <hr class="hair" />
        <div class="wave-tools">
          ${knobHtml("minSilenceMs", "Silence gap", 100, 4000, 50)}
          ${knobHtml("padMs", "Edge pad", 0, 600, 25)}
          ${knobHtml("speechFloorDb", "Speech floor", -60, -25, 1)}
        </div>
      </div>
    </div>`);
  wrap.appendChild(card);

  // clip listing card (the cut result, re-rendered live)
  const listCard = el(`
    <div class="card">
      <div class="card-head"><h3>Resulting clips</h3><span class="head-note clip-count"></span></div>
      <div class="card-body tight"><div class="clip-list"></div></div>
    </div>`);
  wrap.appendChild(listCard);

  const canvas = card.querySelector("canvas");
  const recut = () => {
    const res = computeRegions(REP_WAV.peaks, REP_WAV.durationS, state.knobs);
    drawWave(canvas, res);
    card.querySelector(".recut-stat").textContent = `${res.clips} clips · ${res.speechS}s speech of ${res.totalS}s (${pct((res.speechS / res.totalS) * 100)})`;
    listCard.querySelector(".clip-count").textContent = `${res.clips} kept`;
    const cl = listCard.querySelector(".clip-list");
    cl.innerHTML = "";
    res.regions.forEach((r, i) => {
      cl.appendChild(el(`
        <div class="clip-row">
          <span class="idx">#${i + 1}</span>
          <span class="rng">${clock(r.startS)} → ${clock(r.endS)}</span>
          <span class="clip-bar" style="width:${Math.round(((r.endS - r.startS) / res.totalS) * 220)}px"></span>
          <span class="dur">${(r.endS - r.startS).toFixed(1)}s</span>
        </div>`));
    });
    if (!res.regions.length) cl.appendChild(el(`<div class="note" style="padding:8px 4px">No speech above floor — raise the speech floor dB.</div>`));
  };

  // wire knobs
  card.querySelectorAll('input[type="range"]').forEach((inp) => {
    inp.addEventListener("input", () => {
      const key = inp.dataset.knob;
      state.knobs[key] = Number(inp.value);
      const out = card.querySelector(`[data-val="${key}"]`);
      out.textContent = formatKnob(key, Number(inp.value));
      recut();
    });
  });
  requestAnimationFrame(recut);
}

function knobHtml(key, label, min, max, step) {
  // current applied value (not the default) so the slider reflects state.knobs
  const v = state.knobs[key];
  return `
    <div class="knob">
      <div class="knob-head"><label>${label}</label><span class="val" data-val="${key}">${formatKnob(key, v)}</span></div>
      <input type="range" data-knob="${key}" min="${min}" max="${max}" step="${step}" value="${v}" />
    </div>`;
}
function formatKnob(key, v) {
  if (key === "minSilenceMs") return `${v} ms`;
  if (key === "padMs") return `${v} ms`;
  if (key === "speechFloorDb") return `${v} dB`;
  return String(v);
}

function drawWave(cv, res) {
  const ctx = cv.getContext("2d");
  const w = cv.width, h = cv.height, mid = h / 2;
  ctx.clearRect(0, 0, w, h);
  const peaks = REP_WAV.peaks;
  const n = peaks.length;
  const total = res.totalS;
  const xOf = (sec) => (sec / total) * w;

  // shade kept regions
  for (const r of res.regions) {
    ctx.fillStyle = "rgba(79,91,213,0.09)";
    ctx.fillRect(xOf(r.startS), 0, xOf(r.endS) - xOf(r.startS), h);
  }
  const inRegion = (sec) => res.regions.some((r) => sec >= r.startS && sec <= r.endS);

  // waveform bars
  const bw = w / n;
  for (let i = 0; i < n; i++) {
    const sec = (i / n) * total;
    const amp = peaks[i];
    const bh = Math.max(1.5, amp * (h * 0.92));
    const x = i * bw;
    ctx.fillStyle = inRegion(sec) ? "#4f5bd5" : "#c7cfdd";
    ctx.fillRect(x, mid - bh / 2, Math.max(0.8, bw * 0.9), bh);
  }

  // cut points (region boundaries)
  ctx.strokeStyle = "#e0483c";
  ctx.lineWidth = 2.4;
  for (const r of res.regions) {
    for (const sec of [r.startS, r.endS]) {
      const x = xOf(sec);
      ctx.beginPath(); ctx.moveTo(x, 6); ctx.lineTo(x, h - 6); ctx.stroke();
    }
  }
}

// ---------------- TRANSCRIPT detail: dense line-oriented -------------------
function detailTranscript(c) {
  const s = SESSIONS.find((x) => x.id === state.path[0].id);
  c.appendChild(el(colHead("Merged transcript", `${esc(s.label || "Session")}`)));
  const wrap = el(`<div class="detail-wrap"></div>`);
  c.appendChild(wrap);
  // speaking-time summary
  const stCard = el(`
    <div class="card">
      <div class="card-head"><h3>Merged transcript</h3><span class="head-note">${TRANSCRIPT.model} · ${TRANSCRIPT.backend} · ${clock(TRANSCRIPT.durationS)}</span></div>
      <div class="card-body">
        <div class="note" style="margin-bottom:9px">Speaking time</div>
        <div class="speaking-time"></div>
      </div>
    </div>`);
  const st = stCard.querySelector(".speaking-time");
  for (const row of TRANSCRIPT.speakingTime) {
    st.appendChild(el(`
      <div class="st-row">
        <span class="who"><span class="spk-dot" style="background:${spkVar(row.spk)}"></span>${esc(row.speaker)}</span>
        <span class="st-bar"><span style="width:${row.pct}%;background:${spkVar(row.spk)}"></span></span>
        <span class="pct">${row.pct}%</span>
      </div>`));
  }
  wrap.appendChild(stCard);

  // dense lines
  const counts = {
    low: TRANSCRIPT.lines.filter((l) => l.lowConfidence).length,
    sup: TRANSCRIPT.lines.filter((l) => l.suppressed).length,
    tr: TRANSCRIPT.lines.filter((l) => l.translatedFrom).length,
  };
  const linesCard = el(`
    <div class="card">
      <div class="card-head">
        <h3>Lines</h3>
        <span class="head-note row-flex">
          <span>${TRANSCRIPT.lines.length} total</span>
          <span class="tag tr">${counts.tr} translated</span>
          <span class="tag low">${counts.low} low-conf</span>
          <span class="tag sup">${counts.sup} suppressed</span>
        </span>
      </div>
      <div class="card-body tight"><div class="tx-lines"></div></div>
    </div>`);
  const lines = linesCard.querySelector(".tx-lines");
  for (const l of TRANSCRIPT.lines) {
    const badges = [];
    if (l.translatedFrom) badges.push(`<span class="tag tr" title="translated by Canary">${l.translatedFrom}→en</span>`);
    if (l.lowConfidence) badges.push(`<span class="tag low" title="confidence ${l.confidence}">low ${Math.round((l.confidence || 0) * 100)}%</span>`);
    if (l.suppressed) badges.push(`<span class="tag sup" title="matched rule: ${l.matchedRule}">suppressed · ${l.matchedRule}</span>`);
    lines.appendChild(el(`
      <div class="tx-line ${l.lowConfidence ? "low" : ""} ${l.suppressed ? "suppressed" : ""}">
        <span class="t">${clock(l.t)}</span>
        <span class="who"><span class="spk-dot" style="background:${spkVar(l.spk)}"></span><span class="name">${esc(l.speaker)}</span> <span style="opacity:.8">${(LANGS[l.lang] || LANGS.auto).flag}</span></span>
        <span class="text">${esc(l.text)}<span class="badges">${badges.join("")}</span></span>
      </div>`));
  }
  wrap.appendChild(linesCard);

  // suppressed audit note
  wrap.appendChild(el(`<p class="note mt12">Suppressed lines are kept in the audit log (not deleted). The hallucination rule <code style="font-family:var(--mono)">youtube-outro</code> matched the strike-through line above — edit rules in Settings.</p>`));
}

// ---------------- SPEAKER detail: per-mic profile + languages -------------
function detailSpeaker(c) {
  const sp = speakerById(state.path[0].id);
  c.appendChild(el(colHead("Speaker profile", sp.isRoom ? "shared room mic" : `${sp.sessionsSeen} sessions`)));
  const wrap = el(`<div class="detail-wrap"></div>`);

  wrap.appendChild(el(`
    <div class="dh">
      <div class="dh-avatar" style="background:${spkVar(sp.spk)}">${sp.initials}</div>
      <div class="dh-main">
        <div class="dh-title">${esc(sp.name)} ${sp.isRoom ? `<span class="chip tiny" style="vertical-align:3px;color:var(--spk-2);border-color:#e7d3a8">room mic</span>` : ""}</div>
        <div class="dh-meta">${esc(sp.note)}</div>
      </div>
    </div>`));

  // per-mic profile — reused across sessions
  wrap.appendChild(el(`
    <div class="card">
      <div class="card-head"><h3>Microphone profile</h3><span class="head-note">keyed by mic · reused across ${sp.sessionsSeen} sessions</span></div>
      <div class="card-body">
        <div class="row-flex" style="justify-content:space-between;align-items:flex-start">
          <div>
            <div class="row-flex"><span style="font-size:18px">🎙</span><span class="muted-strong" style="font-size:14px">${esc(sp.mic.label)}</span></div>
            <div class="note lead"><code style="font-family:var(--mono)">${esc(sp.mic.id)}</code></div>
          </div>
          <span class="chip solid"><span style="font-weight:700">${sp.sessionsSeen}×</span> reused</span>
        </div>
        <hr class="hair" />
        <dl class="kv">
          <dt>Gate threshold</dt><dd>${sp.gateThreshold.toFixed(2)} <span class="note">(speech vs. floor)</span></dd>
          <dt>Noise floor</dt><dd>${sp.noiseFloorDb} dB</dd>
          <dt>Profile scope</dt><dd>This mic, every session — change once, applies everywhere.</dd>
        </dl>
        <div class="mt12">
          <div class="note" style="margin-bottom:5px">Gate threshold</div>
          <div class="meter" style="width:100%;background:#e3e8f1"><span style="width:${Math.round(sp.gateThreshold * 100)}%;background:linear-gradient(90deg,#c7cfdd,${spkVar(sp.spk)})"></span></div>
        </div>
      </div>
    </div>`));

  // languages + quick switch
  const cur = state.quickLang[sp.id] || sp.primaryLang;
  const opts = [sp.primaryLang, sp.secondaryLang, "en"].filter((x, i, a) => x && a.indexOf(x) === i);
  const langCard = el(`
    <div class="card">
      <div class="card-head"><h3>Languages</h3><span class="head-note">primary + secondary · per-speaker</span></div>
      <div class="card-body">
        <div class="lang-flow">
          <span class="chip"><span style="opacity:.7;font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-right:4px">primary</span>${(LANGS[sp.primaryLang]).flag} ${(LANGS[sp.primaryLang]).name}</span>
          ${sp.secondaryLang ? `<span class="chip"><span style="opacity:.7;font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-right:4px">secondary</span>${(LANGS[sp.secondaryLang]).flag} ${(LANGS[sp.secondaryLang]).name}</span>` : `<span class="chip muted">no secondary</span>`}
        </div>
        <hr class="hair" />
        <div class="quick-switch">
          <span class="qs-label">Transcribe this speaker as:</span>
          ${opts.map((code) => `<button class="qbtn ${code === cur ? "active" : ""}" data-lang="${code}">${(LANGS[code]).flag} ${(LANGS[code]).name}</button>`).join("")}
        </div>
        <p class="note mt8">Quick override for this session only; the saved profile keeps ${(LANGS[sp.primaryLang]).name} as primary.</p>
      </div>
    </div>`);
  langCard.querySelectorAll(".qbtn").forEach((b) => b.addEventListener("click", () => {
    state.quickLang[sp.id] = b.dataset.lang;
    render();
  }));
  wrap.appendChild(langCard);

  // diarization note for room
  if (sp.isRoom && sp.diarizedInto) {
    const dcard = el(`
      <div class="card">
        <div class="card-head"><h3>Diarized voices</h3><span class="head-note">this shared mic splits into ${sp.diarizedInto.length}</span></div>
        <div class="card-body"><div class="diar-grid"></div></div>
      </div>`);
    const grid = dcard.querySelector(".diar-grid");
    for (const d of sp.diarizedInto) {
      grid.appendChild(el(`
        <div class="diar-card">
          <div class="dc-head"><span class="spk-dot" style="background:${spkVar(d.spk)}"></span>${esc(d.label)}</div>
          <div class="row-flex mt8">${langChip(d.lang)}<span class="chip muted tiny">${d.talkPct}% talk</span></div>
        </div>`));
    }
    wrap.appendChild(dcard);
  }

  // where seen
  const seen = SESSIONS.filter((s) => s.speakers.includes(sp.id));
  wrap.appendChild(el(`
    <div class="card flat">
      <div class="card-head"><h3>Seen in</h3><span class="head-note">${seen.length} of ${SESSIONS.length} sessions</span></div>
      <div class="card-body tight">${seen.map((s) => `<div class="clip-row" style="grid-template-columns:1fr auto"><span class="muted-strong">${esc(s.label || "Untitled")}</span><span class="note">${new Date(s.startedAt).toLocaleDateString("en-GB", { day: "2-digit", month: "short" })}</span></div>`).join("")}</div>
    </div>`));

  c.appendChild(wrap);
}

// ---------------- MODEL detail: backends + family picker + Canary selects ---
function detailModel(c) {
  const top = state.path[0];
  let model, family;
  for (const fam of MODELS) {
    const m = fam.models.find((x) => x.id === top.id);
    if (m) { model = m; family = fam.family; break; }
  }
  c.appendChild(el(colHead("Engine", family)));
  const wrap = el(`<div class="detail-wrap"></div>`);

  wrap.appendChild(el(`
    <div class="dh">
      <div class="dh-avatar" style="background:#2b3344">⚙</div>
      <div class="dh-main">
        <div class="dh-title">${esc(model.display)} ${model.id === state.engine.model ? `<span class="chip solid tiny" style="vertical-align:3px">active</span>` : ""}</div>
        <div class="dh-meta"><span class="chip muted tiny">${esc(family)}</span><span>${esc(model.desc)}</span><span class="row-flex">${model.langs.map((l) => (LANGS[l] || LANGS.auto).flag).join(" ")}</span></div>
      </div>
    </div>`));

  // backend chips (cuda disabled)
  const backends = MOCK.APP.backends;
  const beCard = el(`
    <div class="card">
      <div class="card-head"><h3>Backend</h3><span class="head-note">resolved: ${MOCK.APP.backend}</span></div>
      <div class="card-body">
        <div class="seg">
          ${backends.map((b) => {
            if (!b.available) return `<span class="chip disabled" title="not available on this host">${esc(b.label)} · unavailable</span>`;
            const sel = b.kind === state.engine.backend;
            return `<button class="chip ${sel ? "sel" : ""}" data-be="${b.kind}">${esc(b.label)}</button>`;
          }).join("")}
        </div>
        <p class="note mt8">CUDA is greyed out — no NVIDIA runtime on this host. MLX is resolved for Apple-silicon.</p>
      </div>
    </div>`);
  beCard.querySelectorAll("button[data-be]").forEach((b) => b.addEventListener("click", () => { state.engine.backend = b.dataset.be; render(); }));
  wrap.appendChild(beCard);

  // family picker (other families/models, grouped)
  const pickCard = el(`
    <div class="card">
      <div class="card-head"><h3>Switch model</h3><span class="head-note">grouped by family</span></div>
      <div class="card-body"></div>
    </div>`);
  const pb = pickCard.querySelector(".card-body");
  for (const fam of MODELS) {
    const grp = el(`<div style="margin-bottom:10px"><div class="nav-group-label" style="padding:0 0 4px">${esc(fam.family)}${fam.family === "canary" ? ` <span class="tag tr">translates</span>` : ""}</div><div class="seg"></div></div>`);
    const seg = grp.querySelector(".seg");
    for (const m of fam.models) {
      const sel = m.id === top.id;
      const btn = el(`<button class="chip ${sel ? "sel" : ""}" data-model="${m.id}" data-fam="${fam.family}">${esc(m.display)}</button>`);
      btn.addEventListener("click", () => { state.path = [{ kind: "model", id: m.id, label: m.display, ico: "⚙", family: fam.family }]; render(); });
      seg.appendChild(btn);
    }
    pb.appendChild(grp);
  }
  wrap.appendChild(pickCard);

  // Canary translation selects (only canary has inputs)
  if (family === "canary" && model.inputs) {
    const tCard = el(`
      <div class="card">
        <div class="card-head"><h3>Translation</h3><span class="head-note">Canary · source → target</span></div>
        <div class="card-body">
          <div class="two-col"></div>
          <div class="field mt12" data-hotwords></div>
          <div class="row-flex mt8"><span class="note">Preview:</span> <span class="lang-flow"><span class="chip">${(LANGS[state.engine.sourceLang]).flag} ${(LANGS[state.engine.sourceLang]).name}</span><span class="arrow">→</span><span class="chip solid">${(LANGS[state.engine.targetLang]).flag} ${(LANGS[state.engine.targetLang]).name}</span></span></div>
        </div>
      </div>`);
    const two = tCard.querySelector(".two-col");
    for (const inp of model.inputs.filter((x) => x.kind === "select")) {
      const cur = inp.name === "source_lang" ? state.engine.sourceLang : state.engine.targetLang;
      const f = el(`<div class="field"><label>${esc(inp.label)}</label><select class="sel-input" data-input="${inp.name}"></select></div>`);
      const sel = f.querySelector("select");
      for (const code of model.langs) {
        const o = document.createElement("option");
        o.value = code; o.textContent = `${(LANGS[code] || LANGS.auto).flag} ${(LANGS[code] || LANGS.auto).name}`;
        if (code === cur) o.selected = true;
        sel.appendChild(o);
      }
      sel.addEventListener("change", () => {
        if (inp.name === "source_lang") state.engine.sourceLang = sel.value; else state.engine.targetLang = sel.value;
        render();
      });
      two.appendChild(f);
    }
    const hw = model.inputs.find((x) => x.kind === "text");
    if (hw) tCard.querySelector("[data-hotwords]").appendChild(el(`<label>${esc(hw.label)}</label><input class="text-input" placeholder="${esc(hw.placeholder || "")}" style="width:100%" />`));
    wrap.appendChild(tCard);
  } else {
    wrap.appendChild(el(`<p class="note mt12">This family transcribes only — translation is a Canary capability. Pick <strong>canary-1b-v2</strong> above to set source → target languages.</p>`));
  }

  c.appendChild(wrap);
}

// =============================================================================
// SETTINGS drawer (progressive disclosure)
// =============================================================================
function renderSettings() {
  const d = document.getElementById("settingsDrawer");
  d.innerHTML = `
    <button class="close" id="drawerClose">✕</button>
    <h2>Settings</h2>
    <p class="note">Recorder-wide options. Per-speaker mic profiles live on each speaker.</p>

    <div class="set-block">
      <h3>Recording</h3>
      <div class="toggle-row"><span>Master recording</span><span class="switch ${MOCK.APP.recordingEnabled ? "on" : ""}" id="recSwitch"></span></div>
      <div class="toggle-row"><span>Strip silence on save</span><span class="switch on"></span></div>
    </div>

    <div class="set-block">
      <h3>Prompt</h3>
      <textarea class="text-input" rows="3">Nordic Sync standup. Speakers: Atle, Mette, James. Topics: revenue, Nordic segment, dashboard.</textarea>
    </div>

    <div class="set-block">
      <h3>Hotwords</h3>
      <input class="text-input" style="width:100%" value="Vortiago, TapScribe, kvartalstall, MLX" />
    </div>

    <div class="set-block">
      <h3>Hallucination rules</h3>
      <div class="rule-item"><span>Drop YouTube outros</span><code>youtube-outro</code></div>
      <div class="rule-item"><span>Drop "thanks for watching"</span><code>thanks-watching</code></div>
      <div class="rule-item"><span>Drop sub-300ms [noise]</span><code>short-noise</code></div>
      <p class="note mt8">Suppressed lines stay in the per-session audit log.</p>
    </div>`;
  d.querySelector("#drawerClose").addEventListener("click", closeSettings);
  d.querySelector("#recSwitch").addEventListener("click", () => {
    MOCK.APP.recordingEnabled = !MOCK.APP.recordingEnabled;
    document.querySelector("#recSwitch").classList.toggle("on", MOCK.APP.recordingEnabled);
    updateRecState();
  });
}
function openSettings() {
  renderSettings();
  document.getElementById("settingsDrawer").hidden = false;
  document.getElementById("drawerScrim").hidden = false;
}
function closeSettings() {
  document.getElementById("settingsDrawer").hidden = true;
  document.getElementById("drawerScrim").hidden = true;
}
function updateRecState() {
  const on = MOCK.APP.recordingEnabled;
  const rs = document.getElementById("recState");
  rs.classList.toggle("is-off", !on);
  document.getElementById("recLabel").textContent = on ? "Recording on" : "Recording off";
}

// =============================================================================
// NAVIGATION helpers
// =============================================================================
function selectRoot(kind) {
  state.root = kind;
  // default selection per root so the detail pane is never empty on a root switch
  state.path = [];
  if (kind === "live") state.path = [{ kind: "tap", id: LIVE_TAPS[0].identity, label: LIVE_TAPS[0].name, ico: "🟢" }];
  else if (kind === "sessions") { const s = SESSIONS.find((x) => x.current) || SESSIONS[0]; state.path = [{ kind: "session", id: s.id, label: s.label || "Untitled session", ico: "🗂" }]; }
  else if (kind === "speakers") state.path = [{ kind: "speaker", id: SPEAKERS[0].id, label: SPEAKERS[0].name, ico: "👤" }];
  else if (kind === "engines") { const m = MODELS.find((f) => f.family === "canary").models[0]; state.path = [{ kind: "model", id: m.id, label: m.display, ico: "⚙", family: "canary" }]; }
  render();
}

// keyboard: ← collapse one level, ↑/↓ move within the active navigator
window.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  if (e.key === "ArrowLeft") {
    if (state.path.length > 1) { state.path = state.path.slice(0, -1); render(); e.preventDefault(); }
  } else if (e.key === "Escape") {
    if (!document.getElementById("settingsDrawer").hidden) closeSettings();
  } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    moveSelection(e.key === "ArrowDown" ? 1 : -1);
    e.preventDefault();
  }
});

function moveSelection(delta) {
  // move within the root's primary navigator list
  const items = currentNavIds();
  if (!items.length) return;
  const curId = state.path[0]?.id;
  let idx = items.findIndex((x) => x.id === curId);
  idx = Math.max(0, Math.min(items.length - 1, idx + delta));
  const it = items[idx];
  state.path = [it.make()];
  render();
}
function currentNavIds() {
  if (state.root === "live") return LIVE_TAPS.map((t) => ({ id: t.identity, make: () => ({ kind: "tap", id: t.identity, label: t.name, ico: "🟢" }) }));
  if (state.root === "sessions") return SESSIONS.map((s) => ({ id: s.id, make: () => ({ kind: "session", id: s.id, label: s.label || "Untitled session", ico: "🗂" }) }));
  if (state.root === "speakers") return SPEAKERS.map((s) => ({ id: s.id, make: () => ({ kind: "speaker", id: s.id, label: s.name, ico: "👤" }) }));
  if (state.root === "engines") { const out = []; for (const f of MODELS) for (const m of f.models) out.push({ id: m.id, make: () => ({ kind: "model", id: m.id, label: m.display, ico: "⚙", family: f.family }) }); return out; }
  return [];
}

// =============================================================================
// Screenshotter hooks + boot
// =============================================================================
document.getElementById("settingsBtn").addEventListener("click", openSettings);
document.getElementById("drawerScrim").addEventListener("click", closeSettings);

// window.gotoView(name): set up a deterministic state for each shot.
window.gotoView = function (name) {
  closeSettings();
  switch (name) {
    case "live": selectRoot("live"); break;
    case "live-room": // diarized room tap (diarization shown as a property)
      state.root = "live";
      state.path = [{ kind: "tap", id: "room-oslo", label: "Oslo Conference Room", ico: "🟢" }];
      render();
      break;
    case "live-voice": // drilled INTO a diarized voice (Speaker B / English)
      state.root = "live";
      state.path = [
        { kind: "tap", id: "room-oslo", label: "Oslo Conference Room", ico: "🟢" },
        { kind: "subspeaker", id: "Speaker B", label: "Speaker B", ico: "👤", spk: 4, lang: "en", talkPct: 42 },
      ];
      render();
      break;
    case "sessions": selectRoot("sessions"); break;
    case "session-speaker":
      state.root = "sessions";
      { const s = SESSIONS.find((x) => x.current);
        state.path = [
          { kind: "session", id: s.id, label: s.label, ico: "🗂" },
          { kind: "sessionSpeaker", id: "atle", label: "Atle Håvsø", ico: "👤" },
        ]; }
      render();
      break;
    case "clip":
      state.root = "sessions";
      state.knobs = { ...STRIP_DEFAULTS };
      { const s = SESSIONS.find((x) => x.current);
        state.path = [
          { kind: "session", id: s.id, label: s.label, ico: "🗂" },
          { kind: "sessionSpeaker", id: "atle", label: "Atle Håvsø", ico: "👤" },
          { kind: "clip", id: "clip-2", label: "Clip #2", ico: "🎵", durationS: 48 },
        ]; }
      render();
      break;
    case "clip-recut": // same clip, silence-gap dragged up → bursts merge to fewer clips
      window.gotoView("clip");
      state.knobs = { minSilenceMs: 3700, padMs: 200, speechFloorDb: -45 };
      render();
      break;
    case "transcript":
      state.root = "sessions";
      { const s = SESSIONS.find((x) => x.current);
        state.path = [
          { kind: "session", id: s.id, label: s.label, ico: "🗂" },
          { kind: "transcript", id: "tx", label: "Transcript", ico: "📝" },
        ]; }
      render();
      break;
    case "speaker": // Atle: per-mic profile + languages
      state.root = "speakers";
      state.path = [{ kind: "speaker", id: "atle", label: "Atle Håvsø", ico: "👤" }];
      render();
      break;
    case "speaker-room":
      state.root = "speakers";
      state.path = [{ kind: "speaker", id: "room-oslo", label: "Oslo Conference Room", ico: "👤" }];
      render();
      break;
    case "engines": selectRoot("engines"); break;
    case "settings":
      selectRoot("sessions");
      openSettings();
      break;
    default: selectRoot("live");
  }
};

// Sub-state hook: colSelect("root/id[/childId[/leafId]]") drills deterministically
// by id at each column. Examples:
//   colSelect("sessions/2026-05-28T09-00-00Z/atle/clip-2")  → that clip's waveform
//   colSelect("live/room-oslo/Speaker B")                    → diarized voice
//   colSelect("speakers/mette")                              → Mette's profile
window.colSelect = function (path) {
  const parts = String(path).split("/").filter(Boolean);
  if (!parts.length) return;
  const root = parts[0];
  state.root = root;
  state.path = [];

  if (root === "live") {
    const id = parts[1] || LIVE_TAPS[0].identity;
    state.path = [{ kind: "tap", id, label: (speakerById(id) || {}).name || id, ico: "🟢" }];
    if (parts[2]) {
      const room = speakerById(id);
      const d = room?.diarizedInto?.find((x) => x.label === parts[2]);
      if (d) state.path.push({ kind: "subspeaker", id: d.label, label: d.label, ico: "👤", spk: d.spk, lang: d.lang, talkPct: d.talkPct });
    }
  } else if (root === "sessions") {
    const s = SESSIONS.find((x) => x.id === parts[1]) || SESSIONS[0];
    state.path = [{ kind: "session", id: s.id, label: s.label || "Untitled session", ico: "🗂" }];
    if (parts[2] === "tx" || parts[2] === "transcript") {
      state.path.push({ kind: "transcript", id: "tx", label: "Transcript", ico: "📝" });
    } else if (parts[2]) {
      const sp = speakerById(parts[2]);
      if (sp) {
        state.path.push({ kind: "sessionSpeaker", id: sp.id, label: sp.name, ico: "👤" });
        if (parts[3]) {
          state.knobs = { ...STRIP_DEFAULTS };
          state.path.push({ kind: "clip", id: parts[3], label: `Clip #${parts[3].replace(/\D/g, "") || "1"}`, ico: "🎵" });
        }
      }
    }
  } else if (root === "speakers") {
    const sp = speakerById(parts[1]) || SPEAKERS[0];
    state.path = [{ kind: "speaker", id: sp.id, label: sp.name, ico: "👤" }];
  } else if (root === "engines") {
    let found;
    for (const f of MODELS) { const m = f.models.find((x) => x.id === parts[1]); if (m) { found = { m, f }; break; } }
    if (!found) found = { m: MODELS[0].models[0], f: MODELS[0] };
    state.path = [{ kind: "model", id: found.m.id, label: found.m.display, ico: "⚙", family: found.f.family }];
  }
  render();
};

// boot
updateRecState();
window.gotoView("live");
