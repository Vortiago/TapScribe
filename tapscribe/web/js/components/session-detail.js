// Session detail pane — the big right-hand side of the dashboard:
// header, controls box (model/source/silence/from-to/prompt/hotwords),
// optional aliases box, WAV list (with stripped sub-rows + expandable
// inline transcripts), regex tester, and the merged-transcript mount.
//
// All state and callbacks come in via `ctx` from main.js so this stays
// a pure render-and-wire module.

import { tpl, mount, slot, pick } from "../templates.js";
import { fmtBytes, fmtClock, fmtDur, fmtElapsedShort, fmtMs, truncMid } from "../formatters.js";

const MODEL_OPTS = [
  ["tiny.en", "tiny.en (Whisper, English, fast)"],
  ["small.en", "small.en (Whisper, English)"],
  ["medium.en", "medium.en (Whisper, English, better)"],
  ["large-v3", "large-v3 (Whisper, multilingual incl. Norwegian, slow)"],
  ["nb-whisper-tiny", "nb-whisper-tiny (NB-AiLab, Norwegian-tuned, fastest)"],
  ["nb-whisper-base", "nb-whisper-base (NB-AiLab, Norwegian-tuned, fast)"],
  ["nb-whisper-small", "nb-whisper-small (NB-AiLab, Norwegian-tuned)"],
  ["nb-whisper-medium", "nb-whisper-medium (NB-AiLab, Norwegian-tuned, better)"],
  ["nb-whisper-large", "nb-whisper-large (NB-AiLab, Norwegian-tuned, slow)"],
  ["voxtral-mini", "voxtral-mini (Mistral 3B, EN/ES/FR/PT/HI/DE/NL/IT — no Norwegian)"],
];

// Build the inline-transcript fragment shown when the user clicks a WAV row
// or its stripped sub-row. Kept here (not in merged-transcript.js) because
// it renders the per-WAV transcript record, not the session-merged one.
function buildExpandTx(t) {
  const frag = tpl("tpl-expand-tx");
  const metaHost = pick(frag, "meta");
  const fields = [
    ["device", t.device || "?"],
    ["backend", t.backend || "?"],
    ["model", t.model || "?"],
    ["lang", t.language || "?"],
    ["took", fmtMs(t.transcribe_ms)],
  ];
  if (t.source) fields.push(["source", t.source]);
  for (const [k, v] of fields) {
    metaHost.appendChild(slot(tpl("tpl-expand-meta-field"), { label: k, value: v }));
  }
  pick(frag, "body").textContent = t.text || "";

  const sup = t.suppressed_hallucinations || [];
  if (sup.length) {
    const details = pick(frag, "auditDetails");
    details.hidden = false;
    pick(frag, "auditSummary").textContent =
      `${sup.length} suppressed segment${sup.length === 1 ? "" : "s"}`;
    const rows = pick(frag, "auditRows");
    for (const it of sup) {
      const start = it.start != null ? Number(it.start).toFixed(2) : "?";
      const end = it.end != null ? Number(it.end).toFixed(2) : "?";
      rows.appendChild(slot(tpl("tpl-expand-audit-row"), {
        time: `${start}–${end}`,
        text: it.text || "",
        rule: it.matched_rule || "",
      }));
    }
  }
  return frag;
}

function buildSourceRow(host, s, sessKey, ctx) {
  const stripped = s.stripped || null;
  const want = ctx.sourcePick.get(sessKey) || "original";
  const current = (want === "stripped" && !stripped) ? "original" : want;

  const orig = tpl("tpl-source-original");
  const origInput = orig.querySelector("input");
  origInput.name = `src-${sessKey}`;
  origInput.dataset.sessId = sessKey;
  if (current === "original") origInput.checked = true;
  pick(orig, "count").textContent = `(${s.wav_count || 0})`;
  host.appendChild(orig);

  if (stripped) {
    const sub = tpl("tpl-source-stripped");
    const subInput = sub.querySelector("input");
    subInput.name = `src-${sessKey}`;
    subInput.dataset.sessId = sessKey;
    if (current === "stripped") subInput.checked = true;
    pick(sub, "meta").textContent = `(${stripped.count} · ${fmtDur(stripped.speech_seconds)} speech)`;
    host.appendChild(sub);
  } else {
    host.appendChild(tpl("tpl-source-stripped-none"));
  }
}

function buildSilenceCtl(host, s, sessKey, ctx) {
  if (ctx.sessStripInflight.has(sessKey)) {
    host.appendChild(tpl("tpl-silence-stripping"));
    return;
  }
  if (s.stripped) {
    const frag = tpl("tpl-silence-existing");
    for (const btn of frag.querySelectorAll("[data-strip-run], [data-strip-remove]")) {
      const attr = btn.hasAttribute("data-strip-run") ? "stripRun" : "stripRemove";
      btn.dataset[attr] = sessKey;
    }
    pick(frag, "strippedAt").textContent = `stripped ${fmtClock(s.stripped.stripped_at)}`;
    host.appendChild(frag);
  } else {
    const frag = tpl("tpl-silence-none");
    frag.querySelector("[data-strip-run]").dataset.stripRun = sessKey;
    host.appendChild(frag);
  }
}

// Session-transcribe button content. The same logic runs each poll tick to
// keep the spinner / counter live, so initial render and `updateProgress`
// share this one builder and produce the same DOM.
function spinNode(label) {
  const frag = document.createDocumentFragment();
  const spin = document.createElement("span");
  spin.className = "spin";
  spin.textContent = "⟳";
  frag.appendChild(spin);
  frag.appendChild(document.createTextNode(label));
  return frag;
}

export function sessionProgressInner(s, sessInflight) {
  const startMs = sessInflight.get(s.session);
  const elapsed = startMs ? fmtElapsedShort((Date.now() - startMs) / 1000) : null;
  if (s.progress) {
    const filePart = s.progress.current_file ? ` · ${s.progress.current_file}` : "";
    const node = spinNode(` transcribing ${s.progress.current + 1}/${s.progress.total}${filePart}`);
    if (elapsed) {
      const dim = document.createElement("span");
      dim.className = "dim";
      dim.textContent = ` (${elapsed})`;
      node.appendChild(dim);
    }
    return { node, busy: true };
  }
  if (startMs != null) return { node: spinNode(` transcribing… ${elapsed || "0:00"}`), busy: true };
  const node = document.createDocumentFragment();
  node.appendChild(document.createTextNode(
    s.session_transcript ? "▶ re-transcribe whole session" : "▶ transcribe whole session",
  ));
  return { node, busy: false };
}

function buildActionRow(host, s, sessKey, ctx) {
  const { node, busy } = sessionProgressInner(s, ctx.sessInflight);
  const btn = tpl("tpl-sess-tx-button").firstElementChild;
  btn.dataset.txSess = sessKey;
  if (busy) btn.disabled = true;
  if (!s.session_transcript) btn.classList.add("primary");
  if (ctx.sessJustDone.has(sessKey)) btn.classList.add("just-completed");
  btn.appendChild(node);
  host.appendChild(btn);

  if (s.session_transcript) {
    const copy = tpl("tpl-sess-copy-button").firstElementChild;
    copy.dataset.copySess = sessKey;
    host.appendChild(copy);
  }
}

function buildControls(s, sessKey, ctx) {
  const frag = tpl("tpl-sess-controls");
  pick(frag, "timerange").textContent =
    `${fmtClock(s.earliest_iso)} → ${fmtClock(s.latest_iso)}`;

  const sel = frag.querySelector("[data-model-pick]");
  for (const [v, label] of MODEL_OPTS) {
    sel.add(new Option(label, v, false, v === ctx.batchModel));
  }

  buildSourceRow(pick(frag, "sourceRow"), s, sessKey, ctx);
  buildSilenceCtl(pick(frag, "silenceCtl"), s, sessKey, ctx);

  const rng = ctx.rangeState[sessKey] || {};
  const fromEl = pick(frag, "rangeFrom");
  fromEl.dataset.sessId = sessKey;
  fromEl.placeholder = s.earliest_iso || "optional ISO timestamp";
  fromEl.value = rng.from || "";
  const toEl = pick(frag, "rangeTo");
  toEl.dataset.sessId = sessKey;
  toEl.placeholder = s.latest_iso || "optional ISO timestamp";
  toEl.value = rng.to || "";
  const promptEl = pick(frag, "rangePrompt");
  promptEl.dataset.sessId = sessKey;
  promptEl.placeholder = "meeting context — overrides prompt.txt for this job";
  promptEl.value = rng.prompt || "";
  const hwEl = pick(frag, "rangeHotwords");
  hwEl.dataset.sessId = sessKey;
  hwEl.placeholder = "e.g. Acme Inc., Patricia Lin";
  hwEl.value = rng.hotwords || "";

  buildActionRow(pick(frag, "actions"), s, sessKey, ctx);
  return frag;
}

function buildAliases(meta, aliasKeys, sessKey) {
  if (!aliasKeys.length) return null;
  const frag = tpl("tpl-sess-aliases");
  const rows = pick(frag, "rows");
  for (const k of aliasKeys) {
    const row = tpl("tpl-alias-row");
    const code = pick(row, "key");
    code.textContent = k;
    code.title = k;
    const input = pick(row, "input");
    input.dataset.aliasKey = k;
    input.dataset.aliasSess = sessKey;
    input.placeholder = k.replace(/[_-]+/g, " ");
    input.value = meta.aliases[k] || "";
    rows.appendChild(row);
  }
  return frag;
}

function buildWavRow(f, sessKey, ctx) {
  const wavKey = `${sessKey}/${f.name}`;
  const busy = ctx.wavInflight.has(wavKey);
  const open = ctx.expandedWav === wavKey;
  const dlHref = `/api/wav/${encodeURIComponent(sessKey)}/${encodeURIComponent(f.name)}`;

  const frag = tpl("tpl-wav-row");
  const row = frag.firstElementChild;
  if (busy) row.classList.add("in-flight");
  if (ctx.wavJustDone.has(wavKey)) row.classList.add("just-completed");

  const nameEl = pick(row, "name");
  nameEl.dataset.toggleWav = wavKey;
  nameEl.title = f.name + (f.transcript ? "\n\nClick to expand the transcript." : "");
  nameEl.textContent = truncMid(f.name, 42);
  if (f.transcript) nameEl.classList.add("has-tx");

  pick(row, "duration").textContent = fmtDur(f.duration_s);

  const sizeHost = pick(row, "sizeCell");
  if (busy) {
    const cell = tpl("tpl-wav-size-inflight");
    // The template's outer span *is* the slot — set its dataset + text
    // directly. `updateWavInflightInPlace` finds the cell by data-elapsed-for.
    const span = cell.firstElementChild;
    span.dataset.elapsedFor = wavKey;
    span.textContent = `transcribing… ${fmtElapsedShort((Date.now() - ctx.wavInflight.get(wavKey)) / 1000)}`;
    sizeHost.replaceWith(cell);
  } else {
    const cell = tpl("tpl-wav-size-static");
    const m = pick(cell, "text");
    let text = fmtBytes(f.size);
    if (f.transcript?.transcribe_ms != null) text += ` · took ${fmtMs(f.transcript.transcribe_ms)}`;
    m.textContent = text;
    sizeHost.replaceWith(cell);
  }

  pick(row, "download").href = dlHref;
  const txBtn = pick(row, "txButton");
  txBtn.dataset.txWav = wavKey;
  txBtn.dataset.txSource = "original";
  if (busy) {
    txBtn.disabled = true;
    txBtn.replaceChildren(tpl("tpl-wav-tx-busy"));
  } else {
    txBtn.textContent = f.transcript ? "re-tx" : "transcribe";
  }

  // Append the inline transcript after the row when expanded. Returning a
  // fragment of (row, expand?) keeps both at the same level under wav-list.
  const out = document.createDocumentFragment();
  out.appendChild(frag);
  if (open && f.transcript) out.appendChild(buildExpandTx(f.transcript));

  // Stripped sub-row — only when strip-silence has produced a sibling.
  if (f.stripped) appendStrippedSub(out, f, wavKey, dlHref, ctx);
  return out;
}

function appendStrippedSub(host, f, wavKey, dlHref, ctx) {
  const stripKey = `${wavKey}@stripped`;
  const sBusy = ctx.wavInflight.has(stripKey);
  const sOpen = ctx.expandedWav === stripKey;
  const sTx = f.stripped.transcript;

  const frag = tpl("tpl-wav-row-stripped");
  const row = frag.firstElementChild;
  if (sBusy) row.classList.add("in-flight");
  if (ctx.wavJustDone.has(stripKey)) row.classList.add("just-completed");

  const nameEl = pick(row, "name");
  nameEl.dataset.toggleWav = stripKey;
  nameEl.title = `${f.name} (stripped)${sTx ? "\n\nClick to expand the transcript." : ""}`;
  if (sTx) nameEl.classList.add("has-tx");

  pick(row, "duration").textContent = fmtDur(f.stripped.duration_s);

  const sizeHost = pick(row, "sizeCell");
  if (sBusy) {
    const cell = tpl("tpl-wav-size-inflight");
    const span = cell.firstElementChild;
    span.dataset.elapsedFor = stripKey;
    span.textContent = `transcribing… ${fmtElapsedShort((Date.now() - ctx.wavInflight.get(stripKey)) / 1000)}`;
    sizeHost.replaceWith(cell);
  } else {
    const cell = tpl("tpl-wav-size-static");
    let text = fmtBytes(f.stripped.size);
    if (sTx?.transcribe_ms != null) text += ` · took ${fmtMs(sTx.transcribe_ms)}`;
    pick(cell, "text").textContent = text;
    sizeHost.replaceWith(cell);
  }

  pick(row, "download").href = `${dlHref}?source=stripped`;
  const txBtn = pick(row, "txButton");
  // Stripped sub-row's transcribe button uses the SAME wavKey as the
  // original (no "@stripped"), but its data-tx-source flags the source.
  txBtn.dataset.txWav = wavKey;
  txBtn.dataset.txSource = "stripped";
  if (sBusy) {
    txBtn.disabled = true;
    txBtn.replaceChildren(tpl("tpl-wav-tx-busy"));
  } else {
    txBtn.textContent = sTx ? "re-tx" : "transcribe";
  }

  host.appendChild(frag);
  if (sOpen && sTx) host.appendChild(buildExpandTx(sTx));
}

function buildWavList(s, sessKey, ctx) {
  const frag = tpl("tpl-wav-list");
  const files = s.files || [];
  pick(frag, "fileCount").textContent = `${files.length} file${files.length === 1 ? "" : "s"}`;
  const list = pick(frag, "list");
  if (!files.length) {
    list.appendChild(tpl("tpl-wav-list-empty"));
  } else {
    for (const f of files) list.appendChild(buildWavRow(f, sessKey, ctx));
  }
  return frag;
}

export function renderRegexHits(segs, { rxPattern, rxFlags }) {
  if (!rxPattern) {
    return slot(tpl("tpl-regex-empty"), { msg: `enter a regex to test against ${segs.length} segments` });
  }
  let rx;
  try { rx = new RegExp(rxPattern, rxFlags); }
  catch (e) { return slot(tpl("tpl-regex-error"), { msg: String(e.message || e) }); }
  const hits = segs.filter((seg) => seg?.text && rx.test(seg.text));
  if (!hits.length) {
    return slot(tpl("tpl-regex-empty"), { msg: `no matches in ${segs.length} segments` });
  }
  const out = document.createDocumentFragment();
  out.appendChild(slot(tpl("tpl-regex-header"), {
    count: hits.length,
    suffix: ` match${hits.length === 1 ? "" : "es"} in ${segs.length} segments`,
  }));
  for (const h of hits) {
    out.appendChild(slot(tpl("tpl-regex-hit"), {
      text: h.text || "",
      ctx: `[${fmtClock(h.abs_start)}] ${h.speaker || ""}`,
    }));
  }
  return out;
}

function buildRegexTester(s, ctx) {
  const segs = s.session_transcript?.segments || [];
  const existingRules = ctx.lastJson?.hallucinations?.rules || [];

  const frag = tpl("tpl-regex-tester");
  pick(frag, "toggleLabel").textContent = `${ctx.rxOpen ? "▾" : "▸"} regex tester`;

  if (ctx.rxOpen) {
    const body = pick(frag, "body");
    body.hidden = false;
    pick(frag, "patternInput").value = ctx.rxPattern;
    pick(frag, "flagsInput").value = ctx.rxFlags;

    if (existingRules.length) {
      const seeds = pick(frag, "seeds");
      seeds.hidden = false;
      const list = pick(frag, "seedList");
      for (const r of existingRules) {
        // Rules look like "amara.org" or "re:..." or "exact:..." — strip
        // the prefix so the regex tester gets a workable starting point.
        let seed = r;
        const lower = r.toLowerCase();
        if (lower.startsWith("re:")) seed = r.slice(3).trim();
        else if (lower.startsWith("exact:")) seed = `^${r.slice(6).trim()}$`;
        const code = tpl("tpl-regex-seed").firstElementChild;
        code.dataset.rxSeed = seed;
        code.textContent = r;
        list.appendChild(code);
      }
    }
    mount(pick(frag, "result"), renderRegexHits(segs, ctx));
  }
  return frag;
}

// Main render. `host` is replaced with the new session detail; `ctx` carries
// all state + callbacks (see main.js's `sessionDetailCtx`).
export function render(s, host, ctx) {
  if (!s) { host.replaceChildren(); return; }

  const sessKey = s.session;
  const meta = ctx.effectiveMeta(s);
  const aliasKeys = ctx.deriveSpeakerKeys(s);

  const frag = tpl("tpl-sess-detail");

  // Header row
  const nameInput = frag.querySelector("[data-sess-name]");
  nameInput.dataset.sessName = sessKey;
  nameInput.value = meta.label || "";
  if (!meta.label) nameInput.classList.add("unnamed");
  pick(frag, "folder").textContent = sessKey;
  const trEl = pick(frag, "timerange");
  trEl.append(
    `${fmtClock(s.earliest_iso)} → ${fmtClock(s.latest_iso)} · ${s.wav_count || 0} wavs`,
  );
  if (s.is_current) {
    const rec = Object.assign(document.createElement("span"), { className: "c-rec", textContent: "● recording" });
    trEl.append(" · ", rec);
  }

  // Side column
  const side = pick(frag, "side");
  side.appendChild(buildControls(s, sessKey, ctx));
  const aliasesNode = buildAliases(meta, aliasKeys, sessKey);
  if (aliasesNode) side.appendChild(aliasesNode);
  side.appendChild(buildWavList(s, sessKey, ctx));
  side.appendChild(buildRegexTester(s, ctx));

  // Main column — merged transcript mount
  const mergedHost = pick(frag, "merged");
  if (s.session_transcript) {
    mergedHost.appendChild(ctx.renderMerged(s.session_transcript, meta));
  } else {
    mergedHost.appendChild(tpl("tpl-merged-empty"));
  }

  mount(host, frag);
  wire(host, s, sessKey, ctx);
}

function wire(host, s, sessKey, ctx) {
  for (const btn of host.querySelectorAll("[data-tx-sess]")) {
    btn.addEventListener("click", () => ctx.onTranscribeSession(btn.dataset.txSess));
  }
  for (const btn of host.querySelectorAll("[data-copy-sess]")) {
    btn.addEventListener("click", (e) => ctx.onCopyMerged(btn.dataset.copySess, e.currentTarget));
  }
  for (const btn of host.querySelectorAll("[data-tx-wav]")) {
    btn.addEventListener("click", (e) => {
      // Immediate visual feedback — the next tick will reskin properly.
      const t = e.currentTarget;
      if (t && !t.disabled) {
        t.disabled = true;
        t.replaceChildren(tpl("tpl-wav-tx-busy"));
        t.closest(".wav-row")?.classList.add("in-flight");
      }
      const wk = btn.dataset.txWav;
      const idx = wk.indexOf("/");
      ctx.onTranscribeWav(wk.slice(0, idx), wk.slice(idx + 1), btn.dataset.txSource || null);
    });
  }
  for (const a of host.querySelectorAll("[data-toggle-wav]")) {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      ctx.onToggleWav(a.dataset.toggleWav, s);
    });
  }
  for (const el of host.querySelectorAll("[data-range-key]")) {
    el.addEventListener("input", () => ctx.onRangeEdit(el.dataset.sessId, el.dataset.rangeKey, el.value));
  }

  const modelPick = host.querySelector("[data-model-pick]");
  modelPick?.addEventListener("change", () => ctx.onModelChange(modelPick.value));

  for (const r of host.querySelectorAll("[data-source-pick]")) {
    r.addEventListener("change", () => {
      if (r.checked) ctx.onSourcePick(r.dataset.sessId, r.dataset.sourcePick);
    });
  }
  for (const btn of host.querySelectorAll("[data-strip-run]")) {
    btn.addEventListener("click", () => ctx.onStripRun(btn.dataset.stripRun));
  }
  for (const btn of host.querySelectorAll("[data-strip-remove]")) {
    btn.addEventListener("click", () => ctx.onStripRemove(btn.dataset.stripRemove));
  }

  const nameInput = host.querySelector("[data-sess-name]");
  nameInput?.addEventListener("input", () => {
    ctx.onNameEdit(nameInput.dataset.sessName, nameInput.value);
    nameInput.classList.toggle("unnamed", !nameInput.value);
  });

  for (const el of host.querySelectorAll("[data-alias-key]")) {
    el.addEventListener("input", () =>
      ctx.onAliasEdit(el.dataset.aliasSess, el.dataset.aliasKey, el.value));
  }

  host.querySelector("[data-rx-toggle]")?.addEventListener("click", () => ctx.onRxToggle(sessKey));
  host.querySelector("[data-rx-pattern]")?.addEventListener("input", (e) =>
    ctx.onRxPatternInput(sessKey, e.target.value));
  host.querySelector("[data-rx-flags]")?.addEventListener("input", (e) =>
    ctx.onRxFlagsInput(sessKey, e.target.value));
  for (const seed of host.querySelectorAll("[data-rx-seed]")) {
    seed.addEventListener("click", () => ctx.onRxSeed(sessKey, seed.dataset.rxSeed));
  }
  host.querySelector("[data-toggle-audit]")?.addEventListener("click", ctx.onAuditToggle);
}
