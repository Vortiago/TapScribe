// =============================================================================
// TapScribe · "Signal Rack" — the UI IS the audio pipeline.
// Rooms: rack | cutlab | identity | transcript. Inspector dock is shared.
// All charts hand-drawn (canvas/svg). No frameworks, no network.
// =============================================================================

import {
  MOCK, LANGS, SPEAKERS, MODELS, selectedModel,
  LIVE_TAPS, LIVE_CAPTIONS, SESSIONS, STRIP_DEFAULTS, REP_WAV, TRANSCRIPT,
  computeRegions, helpers, speakerById,
} from "../_shared/mock-data.js";

const { clock, clockH, pct } = helpers;

// ---- speaker palette (matches CSS --spkN) ----------------------------------
const SPK_HEX = ["#5fb4ff", "#ff8fb0", "#c79bff", "#7ee0c0", "#ffc06a"];
const spkColor = (n) => SPK_HEX[n] ?? "#8aa";
const flagOf = (code) => (LANGS[code] || LANGS.auto).flag;
const langName = (code) => (LANGS[code] || LANGS.auto).name;

// ---- tiny DOM helpers ------------------------------------------------------
const el = (sel, root = document) => root.querySelector(sel);
function h(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style") n.setAttribute("style", v);
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    n.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return n;
}
const setSpk = (node, n) => { node.style.setProperty("--spk", spkColor(n)); return node; };

// ---- shared app state ------------------------------------------------------
const STATE = {
  room: "rack",
  selectedTap: "atle",      // identity id selected in the rack/inspector
  backend: MOCK.APP.backend,
  family: selectedModel.family,
  model: selectedModel.model,
  sourceLang: selectedModel.sourceLang,
  targetLang: selectedModel.targetLang,
  knobs: { ...STRIP_DEFAULTS },
  quickLang: {},            // speakerId -> overridden transcription lang
};
SPEAKERS.forEach((s) => (STATE.quickLang[s.id] = s.primaryLang));

const currentSession = SESSIONS.find((s) => s.current) || SESSIONS[0];

// ===========================================================================
// TRANSPORT (top bar)
// ===========================================================================
function renderTransport() {
  // session pill
  el("#tx-session").replaceChildren(
    h("div", { class: "sess-pill" },
      h("span", { class: "lbl" }, currentSession.label),
      h("span", { class: "meta" }, `${currentSession.wavCount} wav · ${clock(currentSession.durationS)}`),
      h("span", { class: "langs" }, ...currentSession.langs.map((c) => h("span", {}, flagOf(c)))),
    ),
  );
  // backend chip
  el("#tx-backend").textContent = `⚙ ${STATE.backend}`;
  // clock = session duration
  el("#tx-clock").textContent = clockH(currentSession.durationS);
  // rec toggle
  const rec = el("#tx-rec");
  rec.classList.toggle("off", !MOCK.APP.recordingEnabled);
  rec.querySelector("b").textContent = MOCK.APP.recordingEnabled ? "REC" : "IDLE";

  // room buttons
  document.querySelectorAll(".room-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.room === STATE.room);
    b.onclick = () => gotoView(b.dataset.room);
  });
}

// ===========================================================================
// CANVAS PRIMITIVES (all hand-drawn)
// ===========================================================================
function dpiSetup(canvas, cssH) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.parentElement.clientWidth || 200;
  const hgt = cssH || canvas.clientHeight || 40;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(hgt * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: hgt };
}

function drawSparkline(canvas, levels, color) {
  const { ctx, w, h } = dpiSetup(canvas, 22);
  ctx.clearRect(0, 0, w, h);
  const n = levels.length;
  const bw = w / n;
  for (let i = 0; i < n; i++) {
    const v = Math.max(0.02, levels[i]);
    const bh = v * (h - 2);
    ctx.fillStyle = i === n - 1 ? color : color + "88";
    ctx.fillRect(i * bw + 0.5, h - bh, Math.max(1, bw - 1.5), bh);
  }
}

// big waveform with strip-silence cut regions painted on top
function drawWaveform(canvas, peaks, durationS, regions, opts = {}) {
  const cssH = opts.height || 150;
  const { ctx, w, h } = dpiSetup(canvas, cssH);
  ctx.clearRect(0, 0, w, h);
  const mid = h / 2;
  const n = peaks.length;
  const xOf = (i) => (i / n) * w;
  const tToX = (t) => (t / durationS) * w;

  // 1. silence backdrop
  ctx.fillStyle = "#0a0e14";
  ctx.fillRect(0, 0, w, h);

  // 2. speech-region highlight bands (kept regions)
  for (const r of regions) {
    const x0 = tToX(r.startS), x1 = tToX(r.endS);
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, "rgba(54,224,160,.13)");
    g.addColorStop(1, "rgba(54,224,160,.05)");
    ctx.fillStyle = g;
    ctx.fillRect(x0, 0, x1 - x0, h);
    // edge cut lines
    ctx.strokeStyle = "rgba(54,224,160,.85)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x0 + .5, 0); ctx.lineTo(x0 + .5, h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x1 - .5, 0); ctx.lineTo(x1 - .5, h); ctx.stroke();
  }

  // 3. center line
  ctx.strokeStyle = "#1b2330"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, mid + .5); ctx.lineTo(w, mid + .5); ctx.stroke();

  // 4. peaks — green where inside a kept region, gray where dropped/silence
  const inRegion = (t) => regions.some((r) => t >= r.startS && t <= r.endS);
  const step = Math.max(1, Math.floor(n / w)); // sub-pixel decimation
  for (let i = 0; i < n; i += 1) {
    const t = (i / n) * durationS;
    const v = peaks[i];
    const amp = v * (mid - 3);
    const x = xOf(i);
    ctx.strokeStyle = inRegion(t) ? "#36e0a0" : "#39424f";
    ctx.globalAlpha = inRegion(t) ? 0.95 : 0.8;
    ctx.lineWidth = Math.max(1, w / n - 0.3);
    ctx.beginPath();
    ctx.moveTo(x, mid - amp);
    ctx.lineTo(x, mid + amp);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // 5. floor threshold guide (dotted)
  if (opts.floorDb != null) {
    const floorAmp = Math.pow(10, opts.floorDb / 20) * (mid - 3);
    ctx.strokeStyle = "rgba(255,207,74,.55)";
    ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, mid - floorAmp); ctx.lineTo(w, mid - floorAmp); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, mid + floorAmp); ctx.lineTo(w, mid + floorAmp); ctx.stroke();
    ctx.setLineDash([]);
  }

  // 6. clip index labels
  ctx.fillStyle = "#36e0a0"; ctx.font = "10px ui-monospace, monospace";
  regions.forEach((r, i) => {
    const x = tToX(r.startS) + 3;
    ctx.fillText(`#${i + 1}`, x, 12);
  });
}

// talk-time stacked SVG bar already done in DOM; level history grid:
function drawLevelHistory(canvas, levels, color) {
  const { ctx, w, h } = dpiSetup(canvas, 64);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#141a22"; ctx.lineWidth = 1;
  for (let gy = 0; gy <= 4; gy++) {
    const y = (gy / 4) * h;
    ctx.beginPath(); ctx.moveTo(0, y + .5); ctx.lineTo(w, y + .5); ctx.stroke();
  }
  const n = levels.length, bw = w / n;
  ctx.fillStyle = color;
  for (let i = 0; i < n; i++) {
    const bh = Math.max(1, levels[i] * (h - 2));
    ctx.globalAlpha = .85;
    ctx.fillRect(i * bw + 0.5, h - bh, Math.max(1.5, bw - 2), bh);
  }
  ctx.globalAlpha = 1;
}

// ===========================================================================
// ROOM 1 — LIVE RACK
// ===========================================================================
function renderRack(host) {
  const root = h("div", { class: "rack" });
  const rails = h("div", { class: "rack-rails" });

  // helper: a selectable cell carrying a tap, with the spk color var set
  const tapCell = (tap, inner, extraClass = "") => {
    const c = setSpk(h("div", {
      class: `cell ${extraClass} ${STATE.selectedTap === tap.identity ? "sel" : ""} ${tap.record ? "" : "muted"}`.trim(),
      onclick: () => selectTap(tap.identity),
    }, h("span", { class: "spk-tag" }), inner), tap.spk);
    return c;
  };

  // ---- RAIL 1: INPUTS (taps: level, lag, rec/live, in-flight) -------------
  const r1 = rail("01", "INPUTS", `${LIVE_TAPS.length} taps`);
  LIVE_TAPS.forEach((tap) => {
    const hot = tap.level > 0.75;
    const spark = h("canvas", { class: "spark" });
    const body = h("div", {},
      h("div", { class: "cell-row" },
        h("span", { class: "cell-name" }, tap.name),
        tap.isRoom ? h("span", { class: "chip room" }, "ROOM") : null,
      ),
      h("div", { class: "cell-row", style: "margin-top:4px;gap:5px" },
        h("span", { class: `chip ${tap.live ? "live" : ""}` }, tap.live ? "LIVE" : "—"),
        h("span", { class: `chip ${tap.record ? "" : "warn"}` }, tap.record ? "● REC" : "○ paused"),
        h("span", { class: "chip", style: `color:${tap.lagS > 1.2 ? "var(--hot)" : "var(--ink-3)"}` }, `Δ${tap.lagS.toFixed(1)}s`),
      ),
      h("div", { class: "meter-wrap" },
        h("div", { class: "meter" }, h("i", { class: hot ? "hotpk" : "", style: `width:${Math.round(tap.level * 100)}%` })),
        h("span", { class: "meter-val" }, tap.level.toFixed(2)),
      ),
      spark,
      tap.buffer
        ? h("div", { class: "inflight" }, "“", tap.buffer, h("span", { class: "cur" }, "▍"), "”")
        : h("div", { class: "inflight empty" }, "— silent —"),
    );
    r1.body.appendChild(tapCell(tap, body));
    queueMicrotask(() => drawSparkline(spark, tap.levels, spkColor(tap.spk)));
  });

  // ---- RAIL 2: GATE (open/closed + threshold) -----------------------------
  const r2 = rail("02", "GATE", "VAD");
  LIVE_TAPS.forEach((tap) => {
    const spk = speakerById(tap.identity);
    const thr = spk ? spk.gateThreshold : 0.5;
    const body = h("div", { class: "gate-cell" },
      h("div", { class: `lamp ${tap.gateOpen ? "open" : ""}` },
        h("span", { class: "led" }),
        h("span", { class: "lab" }, tap.gateOpen ? "OPEN" : "SHUT"),
      ),
      h("div", { class: "tiny" }, "thr ", h("b", {}, thr.toFixed(2))),
      h("div", { class: "thr" },
        h("i", { style: `width:${Math.round((tap.gateOpen ? tap.level : 0.02) * 100)}%` }),
        h("span", { style: `left:${Math.round(thr * 100)}%` }),
      ),
    );
    r2.body.appendChild(tapCell(tap, body, "gate-cell-wrap"));
  });

  // ---- RAIL 3: DIARIZE (fork for room taps, passthrough otherwise) --------
  const r3 = rail("03", "DIARIZE", "fork");
  LIVE_TAPS.forEach((tap) => {
    const spk = speakerById(tap.identity);
    let inner;
    if (spk && spk.isRoom && spk.diarizedInto) {
      inner = h("div", { class: "fork" },
        h("div", { class: "diar-mark" }, "⑂ splits → ", String(spk.diarizedInto.length), " speakers"),
        ...spk.diarizedInto.map((b) => setSpk(h("div", { class: "branch" },
          h("span", { class: "bdot" }),
          h("span", { class: "blab" }, h("b", {}, b.label), " ", flagOf(b.lang)),
          h("span", { class: "bpct" }, `${b.talkPct}%`),
          h("div", { class: "bbar" }, h("i", { style: `width:${b.talkPct}%` })),
        ), b.spk)),
        tap.diarized ? h("div", { class: "tiny", style: "margin-top:4px;color:var(--live)" }, "▸ now: ", tap.diarized) : null,
      );
    } else {
      inner = h("div", { class: "fork" },
        h("div", { class: "diar-mark", style: "color:var(--ink-4)" }, "│ single voice"),
        h("div", { class: "passthru" }, "no split — pass-through"),
      );
    }
    r3.body.appendChild(tapCell(tap, inner, "fork-wrap"));
  });

  // ---- RAIL 4: IDENTITY (mic + langs) -------------------------------------
  const r4 = rail("04", "IDENTITY", "per-mic");
  LIVE_TAPS.forEach((tap) => {
    const spk = speakerById(tap.identity);
    const cur = STATE.quickLang[tap.identity] || spk?.primaryLang;
    const inner = h("div", { class: "idy" },
      h("div", { class: "micline" }, "🎙 ", h("b", {}, spk?.mic.label || "—")),
      h("div", { class: "langline" },
        h("span", { class: "langpill pri" }, flagOf(spk?.primaryLang), langName(spk?.primaryLang)),
        spk?.secondaryLang ? h("span", { class: "langpill sec" }, flagOf(spk.secondaryLang), langName(spk.secondaryLang)) : null,
      ),
      h("div", { class: "tiny" }, "tx as ", h("b", { style: `color:${cur === spk?.primaryLang ? "var(--ink)" : "var(--info)"}` }, flagOf(cur), " ", cur)),
    );
    r4.body.appendChild(tapCell(tap, inner, "idy-wrap"));
  });

  // ---- RAIL 5: ENGINE (backend chips + model picker + canary xlate) -------
  const r5 = rail("05", "ENGINE", STATE.backend);
  r5.body.appendChild(renderEnginePanel());

  // ---- RAIL 6: TRANSCRIPT (live captions) ---------------------------------
  const r6 = rail("06", "TRANSCRIPT", "live feed");
  LIVE_CAPTIONS.slice().reverse().forEach((c) => {
    r6.body.appendChild(setSpk(h("div", { class: `cap ${c.inflight ? "inflight live" : ""}` },
      h("div", { class: "cap-meta" },
        h("span", { class: "cap-t" }, clock(c.t)),
        h("span", { class: "cap-who" }, c.speaker),
        h("span", { class: "cap-lang" }, flagOf(c.lang)),
      ),
      h("div", { class: "cap-txt" }, c.text),
    ), c.spk));
  });

  [r1, r2, r3, r4, r5, r6].forEach((r) => rails.appendChild(r.el));
  root.appendChild(rails);

  // flow legend ribbon
  const segs = [
    ["INPUTS", "level·lag"], ["GATE", "open/shut"], ["DIARIZE", "A/B fork"],
    ["IDENTITY", "mic·lang"], ["ENGINE", "model"], ["TRANSCRIPT", "captions"],
  ];
  const legend = h("div", { class: "flow-legend" });
  segs.forEach(([a, b], i) => {
    legend.appendChild(h("div", { class: "seg" },
      i ? h("span", { class: "arr" }, "▶") : h("span", { class: "arr" }, "🎙"),
      h("b", {}, a), h("span", {}, b),
    ));
  });
  root.appendChild(legend);
  host.appendChild(root);
}

function rail(idx, name, sub) {
  const body = h("div", { class: "rail-body" });
  const wrap = h("div", { class: "rail" },
    h("div", { class: "rail-head" },
      h("span", { class: "rail-idx" }, idx),
      h("span", { class: "rail-name" }, name),
      h("span", { class: "rail-sub" }, sub),
    ),
    body,
  );
  return { el: wrap, body };
}

// the ENGINE picker — reused in rail 5 (backends, family models, canary xlate)
function renderEnginePanel() {
  const stack = h("div", { class: "engine-stack" });

  // backend chips
  const beBlock = h("div", { class: "eng-block" },
    h("h5", {}, "BACKEND", h("span", {}, "hw")),
    h("div", { class: "be-chips" },
      ...MOCK.APP.backends.map((b) => h("button", {
        class: `be ${!b.available ? "off" : STATE.backend === b.kind ? "on" : ""}`.trim(),
        title: b.available ? `use ${b.label}` : `${b.label} unavailable on this host`,
        onclick: b.available ? () => { STATE.backend = b.kind; renderRoom(); renderTransport(); } : null,
      }, b.label)),
    ),
  );

  // family -> models
  const famBlock = h("div", { class: "eng-block" },
    h("h5", {}, "MODEL · BY FAMILY", h("span", {}, `${MODELS.length} fam`)),
  );
  const famList = h("div", { class: "fam-list" });
  MODELS.forEach((fam) => {
    const isCur = fam.family === STATE.family;
    const det = h("details", { class: "fam", ...(isCur ? { open: true } : {}) });
    const sum = h("summary", {},
      h("span", { class: "caret" }, "▸"),
      h("b", {}, fam.family),
      h("span", { style: "margin-left:auto;color:var(--ink-4)" }, `${fam.models.length}`),
    );
    det.appendChild(sum);
    const models = h("div", { class: "models" });
    fam.models.forEach((m) => {
      const on = STATE.family === fam.family && STATE.model === m.id;
      models.appendChild(h("div", {
        class: `modrow ${on ? "on" : ""}`.trim(),
        onclick: () => {
          STATE.family = fam.family; STATE.model = m.id;
          if (fam.family === "canary") {
            const ins = m.inputs || [];
            STATE.sourceLang = (ins.find((i) => i.name === "source_lang") || {}).default || STATE.sourceLang;
            STATE.targetLang = (ins.find((i) => i.name === "target_lang") || {}).default || STATE.targetLang;
          }
          renderRoom();
        },
      },
        h("span", { class: "pick" }),
        h("span", { class: "mid" }, m.display),
        h("span", { class: "mdesc" }, m.desc),
      ));
    });
    det.appendChild(models);
    famList.appendChild(det);
  });
  famBlock.appendChild(famList);

  // canary translation row (only when canary family selected)
  if (STATE.family === "canary") {
    const langOpts = ["auto", "nb", "da", "en", "sv", "de", "fr"];
    const mkSel = (val, on) => {
      const s = h("select", { onchange: (e) => { STATE[on] = e.target.value; renderRoom(); } });
      langOpts.forEach((c) => {
        const o = h("option", { value: c }, `${flagOf(c)} ${c}`);
        if (c === val) o.selected = true;
        s.appendChild(o);
      });
      return s;
    };
    famBlock.appendChild(h("div", { class: "xlate" },
      mkSel(STATE.sourceLang, "sourceLang"),
      h("span", { class: "xarrow" }, "→"),
      mkSel(STATE.targetLang, "targetLang"),
    ));
    famBlock.appendChild(h("div", { class: "tiny", style: "margin-top:5px;text-align:center;color:var(--info)" },
      "translate ", flagOf(STATE.sourceLang), STATE.sourceLang, " → ", flagOf(STATE.targetLang), STATE.targetLang));
  }

  stack.append(beBlock, famBlock);
  return stack;
}

// ===========================================================================
// ROOM 2 — CUT LAB (the marquee waveform + live re-cut)
// ===========================================================================
function renderCutLab(host) {
  const recompute = () => computeRegions(REP_WAV.peaks, REP_WAV.durationS, STATE.knobs);
  let res = recompute();

  const head = h("div", { class: "cl-head" },
    h("span", { class: "wname" }, "🎙 ", h("b", {}, "strip-silence"), " · ", REP_WAV.name),
    h("div", { class: "pillrow" },
      h("span", { class: "stat-pill", id: "p-clips" }, "clips ", h("b", {}, String(res.clips))),
      h("span", { class: "stat-pill alt", id: "p-speech" }, "speech ", h("b", { style: "color:var(--info)" }, `${res.speechS}s`)),
      h("span", { class: "stat-pill", id: "p-total" }, "of ", h("b", { style: "color:var(--ink)" }, `${res.totalS}s`)),
      h("span", { class: "stat-pill", id: "p-ratio" }, "kept ", h("b", {}, pct((res.speechS / res.totalS) * 100))),
    ),
  );

  const wave = h("canvas", { class: "wave" });
  const body = h("div", { class: "cl-body" },
    h("div", { class: "cl-main" },
      h("div", { class: "wave-card" },
        h("h4", {}, "WAVEFORM · CUT PREVIEW", h("span", { class: "sub" }, "green = kept clip · amber dots = speech floor · vertical lines = cuts")),
        h("div", { class: "wave-holder" }, wave),
        h("div", { class: "wave-axis", id: "wave-axis" }),
      ),
      h("div", { class: "wave-card" },
        h("h4", {}, "CLIPS → WAV SPLIT", h("span", { class: "sub", id: "clip-sub" }, `${res.clips} files`)),
        clipTable(res),
      ),
    ),
    h("div", { class: "cl-side" },
      h("div", { class: "knob-group" },
        h("h4", {}, "STRIP-SILENCE KNOBS"),
        h("div", { class: "desc" }, "drag to re-cut live. raise the gap → fewer clips; lower the floor → one clip."),
        knob("minSilenceMs", "Min silence gap", "ms", 100, 5000, 50, STATE.knobs.minSilenceMs, (v) => v + " ms"),
        knob("padMs", "Edge pad", "ms", 0, 600, 25, STATE.knobs.padMs, (v) => v + " ms"),
        knob("speechFloorDb", "Speech floor", "dB", -60, -25, 1, STATE.knobs.speechFloorDb, (v) => v + " dBFS"),
        h("div", { class: "btn-row" },
          h("button", { class: "btn primary", onclick: applyDefaults }, "reset defaults"),
          h("button", { class: "btn", onclick: () => { STATE.knobs.minSilenceMs = 5000; sync(); setSliders(); } }, "merge → 1 clip"),
          h("button", { class: "btn", onclick: () => { STATE.knobs.minSilenceMs = 2000; STATE.knobs.speechFloorDb = -45; sync(); setSliders(); } }, "split → 4"),
        ),
      ),
      h("div", { class: "knob-group" },
        h("h4", {}, "EFFECT"),
        h("div", { id: "effect-readout", class: "desc" }),
      ),
    ),
  );

  host.append(head, body);

  // axis labels
  const axis = el("#wave-axis", host);
  for (let s = 0; s <= REP_WAV.durationS; s += 8) axis.appendChild(h("span", {}, `${s}s`));

  const setSliders = () => {
    el("#k-minSilenceMs", host).value = STATE.knobs.minSilenceMs;
    el("#k-padMs", host).value = STATE.knobs.padMs;
    el("#k-speechFloorDb", host).value = STATE.knobs.speechFloorDb;
    el("#kv-minSilenceMs", host).childNodes[0].nodeValue = STATE.knobs.minSilenceMs + " ms";
    el("#kv-padMs", host).childNodes[0].nodeValue = STATE.knobs.padMs + " ms";
    el("#kv-speechFloorDb", host).childNodes[0].nodeValue = STATE.knobs.speechFloorDb + " dBFS";
  };

  function applyDefaults() { STATE.knobs = { ...STRIP_DEFAULTS }; sync(); setSliders(); }

  function sync() {
    res = recompute();
    drawWaveform(wave, REP_WAV.peaks, REP_WAV.durationS, res.regions, { floorDb: STATE.knobs.speechFloorDb });
    el("#p-clips", host).querySelector("b").textContent = String(res.clips);
    el("#p-speech", host).querySelector("b").textContent = `${res.speechS}s`;
    el("#p-ratio", host).querySelector("b").textContent = pct((res.speechS / res.totalS) * 100);
    el("#clip-sub", host).textContent = `${res.clips} files`;
    el("#clip-tbody", host).replaceChildren(...clipRows(res));
    el("#effect-readout", host).innerHTML =
      `floor <b style="color:var(--hot)">${STATE.knobs.speechFloorDb} dBFS</b> · gap merges runs &lt; <b>${STATE.knobs.minSilenceMs} ms</b> · each clip padded <b>±${STATE.knobs.padMs} ms</b>. ` +
      `→ <b style="color:var(--live)">${res.clips} clip${res.clips === 1 ? "" : "s"}</b> spanning <b style="color:var(--info)">${res.speechS}s</b> of ${res.totalS}s.`;
    el("#sb-mid").innerHTML = `cut: <span class="k">${res.clips}</span> clips · <b>${res.speechS}s</b> speech`;
  }

  // live re-cut wiring
  host.querySelectorAll("input[type=range]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const key = inp.dataset.key;
      STATE.knobs[key] = Number(inp.value);
      const fmt = inp.dataset.fmt;
      el(`#kv-${key}`, host).childNodes[0].nodeValue =
        fmt === "ms" ? STATE.knobs[key] + " ms" : STATE.knobs[key] + " dBFS";
      sync();
    });
  });

  requestAnimationFrame(sync);
}

function knob(key, label, fmt, min, max, step, val, _disp) {
  const valTxt = fmt === "ms" ? val + " ms" : val + " dBFS";
  return h("div", { class: "knob" },
    h("div", { class: "kt" },
      h("label", { for: `k-${key}` }, label),
      h("span", { class: "kv", id: `kv-${key}` }, valTxt),
    ),
    h("input", { type: "range", id: `k-${key}`, min, max, step, value: val, "data-key": key, "data-fmt": fmt }),
    h("div", { class: "krange" },
      h("span", {}, fmt === "ms" ? `${min}ms` : `${min}dB`),
      h("span", {}, fmt === "ms" ? `${max}ms` : `${max}dB`),
    ),
  );
}

function clipTable(res) {
  return h("table", { class: "clip-table" },
    h("thead", {}, h("tr", {},
      h("th", {}, "#"), h("th", {}, "clip wav"), h("th", { style: "text-align:right" }, "start"),
      h("th", { style: "text-align:right" }, "end"), h("th", { style: "text-align:right" }, "dur"), h("th", {}, "span"),
    )),
    h("tbody", { id: "clip-tbody" }, ...clipRows(res)),
  );
}
function clipRows(res) {
  return res.regions.map((r, i) => {
    const dur = +(r.endS - r.startS).toFixed(2);
    const barW = Math.round((dur / res.totalS) * 120);
    const base = REP_WAV.name.replace(/\.wav$/, "");
    return h("tr", {},
      h("td", { class: "cidx" }, `#${i + 1}`),
      h("td", {}, `${base}_c${i + 1}.wav`),
      h("td", { class: "num" }, `${r.startS.toFixed(2)}s`),
      h("td", { class: "num" }, `${r.endS.toFixed(2)}s`),
      h("td", { class: "num", style: "color:var(--live)" }, `${dur.toFixed(2)}s`),
      h("td", {}, h("span", { class: "clip-mini-bar", style: `width:${Math.max(4, barW)}px` })),
    );
  });
}

// ===========================================================================
// ROOM 3 — IDENTITY BANK (per-mic profiles, dual-lang, quick switch, diar)
// ===========================================================================
function renderIdentity(host) {
  const root = h("div", { class: "idbank" },
    h("div", { class: "idbank-head" },
      h("h3", {}, "Identity Bank"),
      h("span", { class: "note" }, "profiles key on the MICROPHONE and reuse across every session · ⑂ = diarized room tap"),
    ),
  );
  const grid = h("div", { class: "id-grid" });

  SPEAKERS.forEach((spk) => {
    const cur = STATE.quickLang[spk.id] || spk.primaryLang;
    const fade = spkColor(spk.spk) + "22";
    const card = h("div", { class: "id-card", style: `--spk:${spkColor(spk.spk)};--spk-fade:${fade}` },
      h("div", { class: "ic-head" },
        h("div", { class: "id-av" }, spk.initials),
        h("div", { class: "ic-name" },
          h("b", {}, spk.name),
          h("span", { class: "role" }, spk.isRoom ? "shared room mic" : spk.note.split(".")[0]),
        ),
        h("div", { class: "ic-seen" }, h("b", {}, String(spk.sessionsSeen)), h("br"), "sessions"),
      ),
    );

    const bodyCls = "ic-body";
    const fields = [];

    // per-mic profile
    fields.push(h("div", { class: "fieldset" },
      h("div", { class: "fl" }, "PROFILE · KEYED ON MIC"),
      h("div", { class: "profile-mic" },
        h("span", {}, "🎙"),
        h("span", { class: "micname" }, spk.mic.label),
        h("span", { class: "reuse" }, "reused ↻"),
      ),
      h("div", { class: "tiny", style: "margin-top:5px" }, "id ", h("b", {}, spk.mic.id)),
    ));

    // gate / floor readout
    const thrW = Math.round(spk.gateThreshold * 100);
    const floorW = Math.round(((spk.noiseFloorDb + 60) / 35) * 100); // -60..-25 -> 0..100
    fields.push(h("div", { class: "fieldset" },
      h("div", { class: "fl" }, "GATE PROFILE (SAVED)"),
      h("div", { class: "gate-readout" },
        h("div", { class: "gr-row" },
          h("span", { class: "grk" }, "gate thr"),
          h("div", { class: "gr-bar" }, h("i", { class: "thr", style: `width:${thrW}%` })),
          h("span", { class: "grv" }, spk.gateThreshold.toFixed(2)),
        ),
        h("div", { class: "gr-row" },
          h("span", { class: "grk" }, "noise floor"),
          h("div", { class: "gr-bar" }, h("i", { class: "floor", style: `width:${floorW}%` })),
          h("span", { class: "grv" }, `${spk.noiseFloorDb} dB`),
        ),
      ),
    ));

    // dual-language + quick switch
    fields.push(h("div", { class: "fieldset" },
      h("div", { class: "fl" }, "LANGUAGE"),
      h("div", { class: "lang-switch" },
        h("div", { class: "ls-row" }, h("span", { class: "lsk" }, "primary"),
          h("span", { class: "langpill pri" }, flagOf(spk.primaryLang), langName(spk.primaryLang))),
        h("div", { class: "ls-row" }, h("span", { class: "lsk" }, "secondary"),
          spk.secondaryLang
            ? h("span", { class: "langpill sec" }, flagOf(spk.secondaryLang), langName(spk.secondaryLang))
            : h("span", { class: "tiny" }, "— none —")),
      ),
    ));

    // quick language switch buttons
    fields.push(h("div", { class: "fieldset" },
      h("div", { class: "fl" }, "TRANSCRIBE THIS AS →"),
      h("div", { class: "quick-langs" },
        ...["nb", "da", "en"].map((c) => h("button", {
          class: `qlang ${cur === c ? "act" : ""}`.trim(),
          onclick: () => { STATE.quickLang[spk.id] = c; renderRoom(); },
        }, flagOf(c), c.toUpperCase())),
      ),
    ));

    const bodyEl = h("div", { class: bodyCls }, ...fields);
    card.appendChild(bodyEl);

    // diarization split (room mic only) — full width
    if (spk.isRoom && spk.diarizedInto) {
      card.appendChild(h("div", { class: "ic-body diar" },
        h("div", { class: "fieldset" },
          h("div", { class: "fl" }, "⑂ DIARIZED INTO (this tap resolves to multiple speakers)"),
          h("div", { class: "diar-split" },
            ...spk.diarizedInto.map((b) => h("div", { class: "ds-row", style: `--spk:${spkColor(b.spk)}` },
              h("span", { class: "ds-dot" }),
              h("span", { class: "dsname" }, b.label),
              h("span", { class: "langpill", style: "border-color:var(--line)" }, flagOf(b.lang), langName(b.lang)),
              h("div", { class: "dsbar" }, h("i", { style: `width:${b.talkPct}%` })),
              h("span", { class: "dspct" }, `${b.talkPct}%`),
            )),
          ),
        ),
      ));
    }

    grid.appendChild(card);
  });

  root.appendChild(grid);
  host.appendChild(root);
}

// ===========================================================================
// ROOM 4 — TRANSCRIPT LEDGER
// ===========================================================================
function renderTranscript(host) {
  const T = TRANSCRIPT;
  const total = T.lines.length;
  const lowN = T.lines.filter((l) => l.lowConfidence).length;
  const supN = T.lines.filter((l) => l.suppressed).length;
  const xlN = T.lines.filter((l) => l.translatedFrom).length;

  // talk-time stacked bar
  const talkBar = h("div", { class: "talk-bar" });
  T.speakingTime.forEach((s) => talkBar.appendChild(
    h("i", { style: `width:${s.pct}%;background:${spkColor(s.spk)}`, title: `${s.speaker} ${s.pct}%` })));

  const head = h("div", { class: "ledger-head" },
    h("span", { class: "lh-title" }, "Merged transcript"),
    h("span", { class: "lh-meta" }, "model ", h("b", {}, T.model), " · ", h("b", {}, T.backend), " · ", clockH(T.durationS), " · ", h("b", {}, `${total} lines`)),
    T.translated ? h("span", { class: "badge xl", style: "margin:0" }, "⇄ translated") : null,
    h("div", { class: "talk-summary" },
      h("span", { class: "lh-meta" }, "speaking time"),
      talkBar,
      h("div", { class: "talk-legend" },
        ...T.speakingTime.map((s) => h("span", {},
          h("span", { class: "sw", style: `background:${spkColor(s.spk)}` }),
          s.speaker.replace("Oslo Room · ", ""), " ", h("b", { style: "color:var(--ink-2)" }, `${s.pct}%`))),
      ),
    ),
  );

  // lines table
  const tbody = h("tbody", {});
  T.lines.forEach((l) => {
    const cls = l.lowConfidence ? "low" : l.suppressed ? "sup" : "";
    const badges = [];
    if (l.lowConfidence) badges.push(h("span", { class: "badge low" }, `low ${(l.confidence * 100) | 0}%`));
    if (l.suppressed) badges.push(h("span", { class: "badge sup" }, `✕ ${l.matchedRule}`));
    if (l.translatedFrom) badges.push(h("span", { class: "badge xl" }, `⇄ ${l.translatedFrom}→${T.model.includes("canary") ? "en" : "en"}`));
    tbody.appendChild(h("tr", { class: `lrow ${cls}`.trim() },
      h("td", { class: "lt" }, clock(l.t)),
      h("td", { class: "lwho", style: `--spk:${spkColor(l.spk)}` }, h("span", { class: "swdot" }), l.speaker.replace("Oslo Room · ", "")),
      h("td", { class: "llang" }, flagOf(l.lang)),
      h("td", { class: "ltext" }, l.text, ...badges),
    ));
  });
  const lines = h("div", { class: "lines" }, h("table", {}, tbody));

  // audit sidebar
  const audit = h("div", { class: "audit" },
    h("div", {},
      h("h4", {}, "LEDGER STATS"),
      auditStat("total lines", String(total)),
      auditStat("low-confidence", String(lowN), "var(--hot)"),
      auditStat("suppressed", String(supN), "var(--bad)"),
      auditStat("translated", String(xlN), "var(--info)"),
      auditStat("languages", currentSession.langs.map(flagOf).join(" ")),
    ),
    h("div", {},
      h("h4", {}, "FILTER AUDIT"),
      ...T.lines.filter((l) => l.suppressed).map((l) => h("div", { class: "audit-item sup" },
        h("div", { class: "ai-head" }, h("span", { class: "tag" }, "SUPPRESSED"), h("span", { style: "color:var(--ink-4)" }, clock(l.t))),
        h("div", { class: "ai-txt" }, h("q", {}, l.text)),
        h("div", { class: "ai-why" }, "rule ", h("b", { style: "color:var(--bad)" }, l.matchedRule), " — hallucination pattern, dropped from merge"),
      )),
      ...T.lines.filter((l) => l.lowConfidence).map((l) => h("div", { class: "audit-item low" },
        h("div", { class: "ai-head" }, h("span", { class: "tag" }, "LOW-CONF"), h("span", { style: "color:var(--ink-4)" }, clock(l.t))),
        h("div", { class: "ai-txt" }, h("q", {}, l.text)),
        h("div", { class: "ai-why" }, "confidence ", h("b", { style: "color:var(--hot)" }, `${(l.confidence * 100) | 0}%`), " < 50% — kept but flagged"),
      )),
      ...T.lines.filter((l) => l.translatedFrom).map((l) => h("div", { class: "audit-item xl" },
        h("div", { class: "ai-head" }, h("span", { class: "tag" }, "TRANSLATED"), h("span", { style: "color:var(--ink-4)" }, clock(l.t))),
        h("div", { class: "ai-txt" }, "source ", flagOf(l.translatedFrom), " ", h("b", {}, langName(l.translatedFrom)), " → ", flagOf("en"), " English (Canary)"),
        h("div", { class: "ai-why" }, h("q", {}, l.text)),
      )),
    ),
  );

  host.appendChild(h("div", { class: "ledger" }, head, h("div", { class: "ledger-body" }, lines, audit)));
}
function auditStat(k, v, color) {
  return h("div", { class: "audit-stat" }, h("span", {}, k), h("b", { style: color ? `color:${color}` : "" }, v));
}

// ===========================================================================
// INSPECTOR DOCK (shared) — reflects the selected tap as a signal path
// ===========================================================================
function renderInspector() {
  const body = el("#insp-body");
  const kicker = el("#insp-kicker");
  const hint = el("#insp-hint");

  const spk = speakerById(STATE.selectedTap);
  const tap = LIVE_TAPS.find((t) => t.identity === STATE.selectedTap);

  if (!spk) {
    kicker.textContent = "INSPECTOR";
    hint.textContent = "—";
    body.replaceChildren(h("div", { class: "empty-insp" }, h("span", { class: "big" }, "⌥"), "Select a cord in the rack."));
    return;
  }

  kicker.textContent = "TAP · SIGNAL PATH";
  hint.textContent = STATE.selectedTap;
  const cur = STATE.quickLang[spk.id] || spk.primaryLang;

  const stages = [
    ["INPUT", tap ? `${tap.level.toFixed(2)} lvl · Δ${tap.lagS.toFixed(1)}s` : "offline", !!tap?.level],
    ["GATE", tap?.gateOpen ? `OPEN · thr ${spk.gateThreshold}` : `shut · thr ${spk.gateThreshold}`, !!tap?.gateOpen],
    ["DIARIZE", spk.isRoom ? `fork → ${spk.diarizedInto.length} speakers` : "pass-through", spk.isRoom],
    ["IDENTITY", `${spk.mic.label} · ${flagOf(cur)}${cur}`, true],
    ["ENGINE", `${STATE.family}/${STATE.model}`, true],
    ["OUTPUT", tap?.buffer ? "streaming…" : "idle", !!tap?.buffer],
  ];

  body.replaceChildren(
    setSpk(h("div", { class: "insp-title" },
      h("span", { class: "iav" }, spk.initials),
      h("div", {}, h("b", {}, spk.name), h("br"), h("span", { class: "sub" }, spk.isRoom ? "shared room mic" : "single voice")),
    ), spk.spk),

    h("table", { class: "kv-tab" },
      kv("mic", spk.mic.label),
      kv("gate threshold", spk.gateThreshold.toFixed(2)),
      kv("noise floor", `${spk.noiseFloorDb} dBFS`),
      kv("primary lang", `${flagOf(spk.primaryLang)} ${langName(spk.primaryLang)}`),
      kv("secondary", spk.secondaryLang ? `${flagOf(spk.secondaryLang)} ${langName(spk.secondaryLang)}` : "—"),
      kv("tx as now", `${flagOf(cur)} ${cur}`, true),
      kv("sessions seen", String(spk.sessionsSeen)),
      kv("recording", tap?.record ? "● on" : "○ paused"),
    ),

    h("div", { class: "insp-sec" }, "SIGNAL PATH (this cord, L→R)"),
    h("div", { class: "insp-flow" },
      ...stages.map(([n, d, act]) => h("div", { class: `fstep ${act ? "act" : ""}`.trim() },
        h("span", { class: "fn" }, n), h("span", { class: "fd" }, d))),
    ),

    tap?.buffer ? h("div", { class: "insp-sec" }, "IN-FLIGHT HYPOTHESIS") : null,
    tap?.buffer ? h("div", { class: "inflight", style: "border:0;padding:0" }, "“", tap.buffer, "”") : null,
  );
}
function kv(k, v, hot) {
  return h("tr", {}, h("td", {}, k), h("td", {}, hot ? h("span", { class: "k" }, v) : v));
}

// ===========================================================================
// ROUTER + interactions
// ===========================================================================
function selectTap(id) {
  STATE.selectedTap = id;
  // re-highlight cells without full rebuild
  document.querySelectorAll(".rack .cell").forEach((c) => c.classList.remove("sel"));
  renderRoom(); // cheap enough; keeps rails in sync
  renderInspector();
}

function renderRoom() {
  const host = el("#room-host");
  host.replaceChildren();
  host.scrollTop = 0;
  switch (STATE.room) {
    case "rack": renderRack(host); break;
    case "cutlab": renderCutLab(host); break;
    case "identity": renderIdentity(host); break;
    case "transcript": renderTranscript(host); break;
    default: renderRack(host);
  }
  updateStatus();
}

function updateStatus() {
  const names = { rack: "LIVE RACK", cutlab: "CUT LAB", identity: "IDENTITY BANK", transcript: "TRANSCRIPT LEDGER" };
  el("#sb-left").innerHTML = `<b>${names[STATE.room]}</b> · ${LIVE_TAPS.filter((t) => t.live).length}/${LIVE_TAPS.length} taps live · engine <span class="k">${STATE.family}/${STATE.model}</span> @ ${STATE.backend}`;
  if (STATE.room !== "cutlab") {
    const r = computeRegions(REP_WAV.peaks, REP_WAV.durationS, STATE.knobs);
    el("#sb-mid").innerHTML = `strip-silence: <span class="k">${r.clips}</span> clips`;
  }
  el("#sb-right").innerHTML = `${MOCK.APP.name} <b>${MOCK.APP.version}</b> · session <b>${currentSession.label}</b>`;
}

// the deterministic navigator for the screenshotter
window.gotoView = function (name) {
  const valid = ["rack", "cutlab", "identity", "transcript"];
  STATE.room = valid.includes(name) ? name : "rack";
  renderTransport();
  renderRoom();
  renderInspector();
  return STATE.room;
};

// optional helpers the shooter may call
window.labSelectTap = (id) => selectTap(id);
window.labSetKnobs = (k) => { Object.assign(STATE.knobs, k); if (STATE.room === "cutlab") renderRoom(); };

// keyboard: F1..F4 switch rooms
window.addEventListener("keydown", (e) => {
  const map = { F1: "rack", F2: "cutlab", F3: "identity", F4: "transcript" };
  if (map[e.key]) { e.preventDefault(); gotoView(map[e.key]); }
});

// ---- boot ------------------------------------------------------------------
renderTransport();
renderRoom();
renderInspector();

// keep canvases crisp on resize
let rT;
window.addEventListener("resize", () => { clearTimeout(rT); rT = setTimeout(() => renderRoom(), 120); });
