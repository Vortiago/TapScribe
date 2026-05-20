// Session detail pane — the big right-hand side of the dashboard:
// header, controls box (model/source/silence/from-to/prompt/hotwords),
// optional aliases box, WAV list (originals + stripped-region sub-rows,
// each with expandable inline transcripts), regex tester, and the
// merged-transcript mount.
//
// All state and callbacks come in via `ctx` from main.js so this stays
// a pure render-and-wire module.

import { tpl, mount, slot, pick } from "../templates.js";
import { fmtBytes, fmtClock, fmtDur, fmtElapsedShort, fmtMs, truncMid } from "../formatters.js";

// Display labels for backend kinds — appears in the chip row above the model
// select. "auto" is always rendered; the others are disabled when the
// server-reported `available_backends` doesn't list them.
const BACKEND_LABELS = {
  auto: "auto",
  mlx: "mlx",
  cuda: "cuda",
  cpu: "cpu",
};

// Display labels for model families — used as <optgroup> labels in the model
// select. Order here drives the group order in the dropdown.
const FAMILY_LABELS = [
  ["whisper", "Whisper"],
  ["nb-whisper", "NB-Whisper (Norwegian)"],
  ["voxtral", "Voxtral (Mistral)"],
  ["parakeet", "Parakeet (NVIDIA)"],
  ["canary", "Canary (NVIDIA, translation)"],
];

// Return the catalog entries that can run on the operator's chosen backend.
// "auto" passes everything through; explicit kinds filter to models that
// declare a binding for the chosen kind.
function filterCatalogByBackend(catalog, backend) {
  const models = catalog.models || [];
  if (backend === "auto") return models;
  return models.filter((m) => (m.backends || []).includes(backend));
}

// Build the inline-transcript fragment shown when the user clicks a WAV
// row. Kept here (not in merged-transcript.js) because it renders the
// per-WAV transcript record, not the session-merged one.
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

function buildBackendChips(host, ctx) {
  const available = new Set(ctx.modelCatalog?.available_backends || []);
  for (const kind of ["auto", "mlx", "cuda", "cpu"]) {
    const chip = tpl("tpl-backend-chip").firstElementChild;
    chip.textContent = BACKEND_LABELS[kind];
    chip.dataset.backendChip = kind;
    // "auto" is always pickable; explicit kinds disabled when the server
    // reports they're not present on this machine. Grayed-out chips
    // still render so the operator sees what's possible if they install
    // the missing extras.
    if (kind !== "auto" && !available.has(kind)) {
      chip.classList.add("disabled");
      chip.disabled = true;
      chip.title = `${kind} not available on this server`;
    }
    if (kind === ctx.batchBackend) chip.classList.add("active");
    host.appendChild(chip);
  }
}

// Build the model select from the catalog, grouping options by family and
// only showing models that can run on the currently-selected backend.
function buildModelSelect(sel, ctx) {
  const candidates = filterCatalogByBackend(ctx.modelCatalog, ctx.batchBackend);
  // Group entries by family preserving FAMILY_LABELS order; unknown families
  // get an "Other" optgroup so a typo in the catalog doesn't drop options.
  const byFamily = new Map();
  for (const m of candidates) {
    const fam = m.family || "other";
    if (!byFamily.has(fam)) byFamily.set(fam, []);
    byFamily.get(fam).push(m);
  }
  for (const [fam, label] of FAMILY_LABELS) {
    const entries = byFamily.get(fam);
    if (!entries?.length) continue;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const m of entries) {
      const txt = m.description ? `${m.display_name} — ${m.description}` : m.display_name;
      group.appendChild(new Option(txt, m.model_id, false, m.model_id === ctx.batchModel));
    }
    sel.appendChild(group);
    byFamily.delete(fam);
  }
  if (byFamily.size) {
    const group = document.createElement("optgroup");
    group.label = "Other";
    for (const [, entries] of byFamily) {
      for (const m of entries) {
        group.appendChild(new Option(m.display_name, m.model_id, false, m.model_id === ctx.batchModel));
      }
    }
    sel.appendChild(group);
  }
  // If the currently-batch-selected model is gone (filtered out by the
  // backend), default to the first available one so the user sees a
  // valid pick rather than an empty select.
  if (sel.options.length && sel.value !== ctx.batchModel) {
    // Fall through — the select shows whatever browser default option is
    // active. We don't mutate ctx here; the change event will when the
    // user explicitly picks.
  }
}

// Render the per-model dynamic input rows (textarea/text/select). Each row
// is rendered into the modelInputs ctl-grid alongside the backend chips
// and the model select. Input values are captured into `ctx.rangeState`
// keyed by the input's registry name — main.js reads them on submit.
function buildModelInputs(host, ctx, modelEntry, sessKey) {
  host.replaceChildren();
  if (!modelEntry) return;
  const rng = ctx.rangeState[sessKey] || {};
  for (const input of modelEntry.inputs || []) {
    if (input.type === "text") {
      const tplId = input.kind === "textarea" ? "tpl-input-textarea" : "tpl-input-text";
      const fragNodes = collectInputNodes(tpl(tplId));
      const labelEl = fragNodes[0];
      const fieldEl = fragNodes[1];
      labelEl.textContent = input.label;
      fieldEl.dataset.inputName = input.name;
      fieldEl.dataset.sessId = sessKey;
      fieldEl.placeholder = input.placeholder || "";
      if (input.description) fieldEl.title = input.description;
      fieldEl.value = rng[input.name] || "";
      host.appendChild(labelEl);
      host.appendChild(fieldEl);
    } else if (input.type === "select") {
      const fragNodes = collectInputNodes(tpl("tpl-input-select"));
      const labelEl = fragNodes[0];
      const sel = fragNodes[1];
      labelEl.textContent = input.label;
      sel.dataset.inputName = input.name;
      sel.dataset.sessId = sessKey;
      if (input.description) sel.title = input.description;
      const current = rng[input.name] || input.default || (input.options[0]?.value ?? "");
      for (const opt of input.options || []) {
        sel.add(new Option(opt.label, opt.value, false, opt.value === current));
      }
      host.appendChild(labelEl);
      host.appendChild(sel);
    }
  }
}

// `tpl()` returns a DocumentFragment whose children are the two row
// elements (label + field). We snapshot them as an array so the caller
// can append each into the live ctl-grid individually — appendChild
// moves the node out of the fragment, so by the time the caller is
// done, the fragment is empty.
//
// Critical: snapshot via `Array.from(frag.children)` rather than
// looping on `frag.firstChild` (the loop never terminates unless the
// child is detached, which yields an infinite-push and a RangeError
// when the array length overflows V8's max).
function collectInputNodes(frag) {
  return Array.from(frag.children);
}

function buildControls(s, sessKey, ctx) {
  const frag = tpl("tpl-sess-controls");
  pick(frag, "timerange").textContent =
    `${fmtClock(s.earliest_iso)} → ${fmtClock(s.latest_iso)}`;

  buildBackendChips(pick(frag, "backendChips"), ctx);

  const sel = frag.querySelector("[data-model-pick]");
  buildModelSelect(sel, ctx);

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

  // Render dynamic input rows from the selected model's `inputs` tuple.
  const currentEntry = (ctx.modelCatalog?.models || []).find((m) => m.model_id === ctx.batchModel);
  buildModelInputs(pick(frag, "modelInputs"), ctx, currentEntry, sessKey);

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

  // Region sub-rows — one per WAV strip-silence produced from this original.
  // Backend buckets them by (speaker_slug, ident); they share the parent's
  // controls layout but live under <session>/stripped/.
  for (const r of (f.regions || [])) {
    appendRegionSub(out, r, sessKey, ctx);
  }
  return out;
}

// Build and append one stripped-region sub-row. Each region has its own
// unique filename, so its toggle/transcribe/inflight keys are independent
// from the parent original's keys.
function appendRegionSub(host, r, sessKey, ctx) {
  const wavKey = `${sessKey}/${r.name}`;
  const toggleKey = `${wavKey}@stripped`;
  const inflightKey = `${wavKey}@stripped`;
  const busy = ctx.wavInflight.has(inflightKey);
  const open = ctx.expandedWav === toggleKey;
  const dlHref = `/api/wav/${encodeURIComponent(sessKey)}/${encodeURIComponent(r.name)}?source=stripped`;

  const frag = tpl("tpl-wav-row-stripped");
  const row = frag.firstElementChild;
  if (busy) row.classList.add("in-flight");
  if (ctx.wavJustDone.has(inflightKey)) row.classList.add("just-completed");

  const nameEl = pick(row, "name");
  nameEl.dataset.toggleWav = toggleKey;
  nameEl.title = `${r.name} (stripped region)${r.transcript ? "\n\nClick to expand the transcript." : ""}`;
  nameEl.textContent = `↳ ${truncMid(r.name, 40)}`;
  if (r.transcript) nameEl.classList.add("has-tx");

  pick(row, "duration").textContent = fmtDur(r.duration_s);

  const sizeHost = pick(row, "sizeCell");
  if (busy) {
    const cell = tpl("tpl-wav-size-inflight");
    const span = cell.firstElementChild;
    span.dataset.elapsedFor = inflightKey;
    span.textContent = `transcribing… ${fmtElapsedShort((Date.now() - ctx.wavInflight.get(inflightKey)) / 1000)}`;
    sizeHost.replaceWith(cell);
  } else {
    const cell = tpl("tpl-wav-size-static");
    let text = fmtBytes(r.size);
    if (r.transcript?.transcribe_ms != null) text += ` · took ${fmtMs(r.transcript.transcribe_ms)}`;
    pick(cell, "text").textContent = text;
    sizeHost.replaceWith(cell);
  }

  pick(row, "download").href = dlHref;
  const txBtn = pick(row, "txButton");
  // data-tx-wav uses the region's own name so the dispatch passes that
  // name straight to /api/transcribe with source=stripped.
  txBtn.dataset.txWav = wavKey;
  txBtn.dataset.txSource = "stripped";
  if (busy) {
    txBtn.disabled = true;
    txBtn.replaceChildren(tpl("tpl-wav-tx-busy"));
  } else {
    txBtn.textContent = r.transcript ? "re-tx" : "transcribe";
  }

  host.appendChild(frag);
  if (open && r.transcript) host.appendChild(buildExpandTx(r.transcript));
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

  // Absorb-into picker: lets the operator merge another session's WAVs
  // into this one. Hidden when there's nothing to merge from (only this
  // session exists, or every other session is the current one).
  const absorbHost = pick(frag, "absorb");
  const mergeCandidates = (ctx.lastJson?.sessions || []).filter(
    (other) => other.session !== sessKey && !other.is_current,
  );
  if (mergeCandidates.length) {
    const absorbFrag = tpl("tpl-sess-absorb");
    const sel = absorbFrag.querySelector("[data-absorb-pick]");
    sel.dataset.absorbTarget = sessKey;
    for (const other of mergeCandidates) {
      const otherMeta = ctx.effectiveMeta(other);
      const label = otherMeta.label
        ? `${otherMeta.label} (${other.wav_count || 0}w)`
        : `${other.session} (${other.wav_count || 0}w)`;
      sel.add(new Option(label, other.session));
    }
    absorbHost.appendChild(absorbFrag);
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

  // Backend chips: each one carries the kind in `data-backend-chip`.
  for (const chip of host.querySelectorAll("[data-backend-chip]")) {
    chip.addEventListener("click", () => {
      if (chip.disabled) return;
      ctx.onBackendChange(chip.dataset.backendChip);
    });
  }

  // Dynamic input rows: capture changes into rangeState so the next
  // submit (transcribe / transcribe-session) sees the operator's pick.
  for (const el of host.querySelectorAll("[data-input-name]")) {
    el.addEventListener("input", () => ctx.onRangeEdit(el.dataset.sessId, el.dataset.inputName, el.value));
    if (el.tagName === "SELECT") {
      el.addEventListener("change", () => ctx.onRangeEdit(el.dataset.sessId, el.dataset.inputName, el.value));
    }
  }

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

  const absorbPick = host.querySelector("[data-absorb-pick]");
  absorbPick?.addEventListener("change", () => {
    const source = absorbPick.value;
    if (!source) return;
    // Reset the dropdown immediately so a refused merge doesn't leave it
    // pinned to the failed choice. Blur too — otherwise the focused-input
    // guard in renderSessionsIfChanged blocks the post-merge re-render.
    absorbPick.value = "";
    absorbPick.blur();
    ctx.onAbsorbSession(absorbPick.dataset.absorbTarget, source);
  });

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
