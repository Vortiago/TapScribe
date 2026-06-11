// @ts-check
// Stages · Settings (GLOBAL). TWO clearly-separated engine cards so live vs
// batch is never ambiguous:
//
//   - LIVE engine  — the streaming model (a saved DEFAULT) + the live initial
//     prompt. The model persists via PUT /api/config/live-model but does NOT
//     restart the running channel; the card flags "restart to apply" when the
//     saved default differs from live_info.model, with an apply button that
//     POSTs /api/live/start (mirrors the classic dashboard's "apply (restart)").
//   - BATCH engine — the recordings model + batch prompt + hotwords +
//     hallucination filter (reuses engine.js + config-card.js).
//
// Each card's prompt gates on THAT card's selected model (Whisper declares
// initial_prompt; Voxtral/Parakeet don't), so the model you pick drives which
// prompt fields show.
//
// The Live card holds a <select> + <textarea> rebuilt each poll tick, so it
// renders through renderRegion (focus-guarded swap). The Batch card reuses
// config-card (also renderRegion-backed) + the engine selector (rebuilt on
// change).

import { tpl, pick, renderRegion } from "../../templates.js";
import { getJson, putJson, wireConfigSave, wireSave } from "../../api.js";
import { header } from "../shell.js";
import * as configCard from "../../components/config-card.js";

// Live model family labels + order — same set the classic live-channel panel
// uses (only families with live-eligible models).
/** @type {[string, string][]} */
const LIVE_FAMILY_LABELS = [
  ["whisper", "Whisper"],
  ["nb-whisper", "NB-Whisper (Norwegian)"],
  ["voxtral", "Voxtral (Mistral)"],
];

/**
 * @param {{
 *   rebuildEngine: (host: Element) => void,
 *   selectedSupport: () => { batch_prompt: boolean, batch_hotwords: boolean } | null,
 *   liveCatalog: import('../../types.js').ModelCatalog,
 *   applyLiveModel: (model: string) => void,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState) => void, rebuildEngine: () => void }}
 */
export function build(ctx) {
  const { rebuildEngine, selectedSupport, liveCatalog, applyLiveModel, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-settings");

  header(pick(frag, "head"), {
    eyebrow: "Global · Defaults",
    title: "Settings",
    sub: "live & batch engines + prompts — each prompt gates on its own model; sessions can override",
  });

  // BATCH card hosts (reused engine selector + config-card).
  const engineHost = pick(frag, "engineHost");
  const configCardCtx = {
    gridEl: pick(frag, "configGrid"),
    headerNoteEl: pick(frag, "configHeaderNote"),
  };
  // LIVE card host (renderRegion target).
  const liveCardHost = pick(frag, "liveCardHost");

  rebuildEngine(engineHost);

  // ---- Summarizer-default card (#84) ----------------------------------------
  // Built ONCE — every control is interactive, so there is no renderRegion and
  // no sig to drift (the Summary view's discipline). Values seed once from the
  // first poll carrying `summarizer_default`; the per-tick update only writes
  // the override-count hint in place. One Save persists the whole structured
  // object via PUT /api/summarize/config.
  const sdSourceWrap = pick(frag, "sdSource");
  const sdButtons = /** @type {NodeListOf<HTMLButtonElement>} */ (
    sdSourceWrap.querySelectorAll("[data-sd-src]")
  );
  const sdLocal = /** @type {HTMLElement} */ (pick(frag, "sdLocal"));
  const sdCommand = /** @type {HTMLElement} */ (pick(frag, "sdCommand"));
  const sdModelSel = /** @type {HTMLSelectElement} */ (pick(frag, "sdModel"));
  const sdModelNote = pick(frag, "sdModelNote");
  const sdMaxTok = /** @type {HTMLInputElement} */ (pick(frag, "sdMaxTokens"));
  const sdPresetSel = /** @type {HTMLSelectElement} */ (pick(frag, "sdCmdPreset"));
  const sdPresetNote = pick(frag, "sdCmdPresetNote");
  const sdCmd = /** @type {HTMLInputElement} */ (pick(frag, "sdCmd"));
  const sdPrompt = /** @type {HTMLTextAreaElement} */ (pick(frag, "sdPrompt"));
  const sdOverrides = pick(frag, "sdOverrides");

  /** The card's selected default source ("local" until the seed lands). */
  let sdSource = "local";
  /** One-shot: seed the controls from the first poll carrying the saved
   * default, then never touch them again (interaction hold). */
  let sdSeeded = false;
  /** The saved model id when the seed arrives BEFORE the catalog fetch — the
   * catalog IIFE applies it after populating the <select>. */
  let sdPendingModel = "";
  /** @type {import('../../types.js').SummaryModel[]} */
  let sdModels = [];
  /** @type {import('../../types.js').CommandPreset[]} */
  let sdPresets = [];

  const sdApplySource = () => {
    for (const b of sdButtons) {
      const on = b.dataset.sdSrc === sdSource;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
    sdLocal.hidden = sdSource !== "local";
    sdCommand.hidden = sdSource !== "command";
  };
  for (const b of sdButtons) {
    b.addEventListener("click", () => {
      const next = b.dataset.sdSrc;
      if (b.disabled || !next || next === sdSource) return;
      sdSource = next;
      sdApplySource();
    });
  }
  sdApplySource();

  /** @param {number} t */
  const sdCtxLabel = (t) => (t >= 1000 ? `${Math.round(t / 1000)}K` : `${t}`);
  const sdReflectModelNote = () => {
    const m = sdModels.find((x) => x.repo_id === sdModelSel.value);
    sdModelNote.textContent = m
      ? `≈${m.approx_gb} GB · ${sdCtxLabel(m.context_tokens)} ctx${m.note ? ` · ${m.note}` : ""}`
      : "";
  };
  sdModelSel.addEventListener("change", sdReflectModelNote);

  const sdReflectPresetNote = () => {
    const p = sdPresets.find((x) => x.key === sdPresetSel.value);
    sdPresetNote.textContent = p ? p.note : "";
  };
  sdPresetSel.addEventListener("change", () => {
    const p = sdPresets.find((x) => x.key === sdPresetSel.value);
    if (p) sdCmd.value = p.template;
    sdReflectPresetNote();
  });
  sdCmd.addEventListener("input", () => {
    const p = sdPresets.find((x) => x.key === sdPresetSel.value);
    if (p && p.template !== sdCmd.value) {
      sdPresetSel.value = "";
      sdReflectPresetNote();
    }
  });

  // Populate model + preset selects ONCE from the hardware-routed catalog
  // (the Summary view's pattern, including the unavailable fallback).
  (async () => {
    try {
      const cat = /** @type {import('../../types.js').SummaryModelCatalog} */ (
        await getJson("/api/summarize/models")
      );
      sdModels = cat.models || [];
      sdModelSel.replaceChildren();
      for (const m of sdModels) sdModelSel.add(new Option(m.label || m.repo_id, m.repo_id, m.is_default, m.is_default));
      if (!sdModels.length) {
        sdModelSel.add(new Option("no local models", "", true, true));
        sdModelSel.disabled = true;
      }
      if (typeof cat.max_tokens_min === "number") sdMaxTok.min = String(cat.max_tokens_min);
      if (typeof cat.max_tokens_max === "number") sdMaxTok.max = String(cat.max_tokens_max);
      sdPresets = cat.command_presets || [];
      sdPresetSel.replaceChildren();
      sdPresetSel.add(new Option("custom…", ""));
      for (const p of sdPresets) sdPresetSel.add(new Option(p.label, p.key));
      // A saved model that arrived before the catalog: apply it now.
      if (sdPendingModel) { sdModelSel.value = sdPendingModel; sdPendingModel = ""; }
      const match = sdPresets.find((x) => x.template === sdCmd.value);
      sdPresetSel.value = match ? match.key : "";
      sdReflectPresetNote();
      sdReflectModelNote();
    } catch {
      // Best-effort (the Summary view's fallback): leave the selects disabled
      // and let the server resolve its defaults from an empty model field.
      sdModelSel.add(new Option("model list unavailable — using default", "", true, true));
      sdModelSel.disabled = true;
      sdPresetSel.add(new Option("presets unavailable", "", true, true));
      sdPresetSel.disabled = true;
    }
  })();

  /** Seed the controls from the saved global default — once. */
  /** @param {import('../../types.js').SummarizerDefault} d */
  const sdSeed = (d) => {
    sdSource = d.source || "local"; // "" (unset) shows the built-in default
    sdApplySource();
    sdPrompt.value = d.prompt || "";
    sdCmd.value = d.command || "";
    if (d.model) {
      sdModelSel.value = d.model;
      if (sdModelSel.value !== d.model) sdPendingModel = d.model; // catalog not loaded yet
      sdReflectModelNote();
    }
    if (typeof d.max_tokens === "number") sdMaxTok.value = String(d.max_tokens);
    const match = sdPresets.find((x) => x.template === sdCmd.value);
    sdPresetSel.value = match ? match.key : "";
    sdReflectPresetNote();
  };

  wireSave({
    btn: /** @type {HTMLButtonElement} */ (pick(frag, "sdSave")),
    status: pick(frag, "sdStatus"),
    put: () => {
      const mt = parseInt(sdMaxTok.value, 10);
      return putJson("/api/summarize/config", {
        source: sdSource,
        prompt: sdPrompt.value,
        command: sdCmd.value.trim(),
        model: sdModelSel.value || "",
        max_tokens: Number.isFinite(mt) ? mt : null,
      });
    },
    onSuccess: () => afterMutate(),
  });

  /** Does this live model declare an initial_prompt input? Falls back to the
   * registry-wide flag when the model isn't in the live catalog. */
  /** @param {string} modelId @param {import('../../types.js').AppState} j */
  const liveModelSupportsPrompt = (modelId, j) => {
    const m = (liveCatalog?.models || []).find((x) => x.model_id === modelId);
    if (m) return (m.inputs || []).some((i) => i.name === "initial_prompt");
    return j.inputs_support?.live_prompt !== false;
  };

  /** Save the chosen live model as the persisted DEFAULT (no restart). */
  /** @param {string} modelId */
  const saveLiveModel = async (modelId) => {
    try { await putJson("/api/config/live-model", { content: modelId }); }
    catch (e) { alert(`Save live model failed: ${String(e).replace(/^Error:\s*/, "")}`); }
    finally { afterMutate(); }
  };

  /** Build the compact live-model <select> row (grouped by family). */
  /** @param {string} selected */
  const buildModelRow = (selected) => {
    const row = tpl("tpl-next-eng-row");
    pick(row, "cap").textContent = "Model";
    const sel = /** @type {HTMLSelectElement} */ (tpl("tpl-next-modelsel").firstElementChild);
    sel.id = "nextLiveModelSelect";
    const models = liveCatalog?.models || [];
    /** @type {Map<string, import('../../types.js').ModelEntry[]>} */
    const byFamily = new Map();
    for (const m of models) {
      if (!byFamily.has(m.family)) byFamily.set(m.family, []);
      (byFamily.get(m.family) ?? []).push(m);
    }
    let found = false;
    /** @param {string} label @param {import('../../types.js').ModelEntry[]} entries */
    const addGroup = (label, entries) => {
      const og = document.createElement("optgroup");
      og.label = label;
      for (const m of entries) {
        og.appendChild(new Option(m.display_name || m.model_id, m.model_id, false, m.model_id === selected));
        if (m.model_id === selected) found = true;
      }
      sel.appendChild(og);
    };
    for (const [fam, label] of LIVE_FAMILY_LABELS) {
      const entries = byFamily.get(fam);
      if (!entries?.length) continue;
      addGroup(label, entries);
      byFamily.delete(fam);
    }
    if (byFamily.size) {
      /** @type {import('../../types.js').ModelEntry[]} */
      const rest = [];
      for (const [, entries] of byFamily) rest.push(...entries);
      addGroup("Other", rest);
    }
    if (!found && selected) sel.add(new Option(`${selected} (unregistered)`, selected, false, true));
    if (!models.length) { sel.add(new Option("no live models", "", true, true)); sel.disabled = true; }
    // blur() so the per-tick renderRegion no longer sees the <select> focused
    // and rebuilds the card (revealing "restart to apply" for the new model).
    sel.addEventListener("change", () => { if (sel.value) { sel.blur(); saveLiveModel(sel.value); } });
    pick(row, "body").appendChild(sel);
    return row;
  };

  /**
   * Build the Live engine card fragment for the current state.
   * @param {import('../../types.js').AppState} j
   */
  const buildLiveCard = (j) => {
    const li = j.live_info || {};
    const runningModel = li.model || "";
    const state = li.state || "stopped";
    const savedDefault = j.live_model_default || "";
    const selected = savedDefault || runningModel;

    const frag = tpl("tpl-next-liveeng");
    pick(frag, "modelRow").appendChild(buildModelRow(selected));

    // "restart to apply" — shown when the saved default differs from what's
    // running (mirrors the classic live-channel "apply (restart)" button).
    const needsApply = !!savedDefault && savedDefault !== runningModel;
    if (needsApply) {
      const running = state === "running" || state === "starting";
      const restart = pick(frag, "restart");
      restart.hidden = false;
      const msg = document.createElement("span");
      msg.className = "restartnote mono";
      msg.textContent = running
        ? `running ${runningModel || "?"} — restart to apply`
        : "live channel stopped — start to apply";
      const btn = document.createElement("button");
      btn.className = "act act--sm act--primary";
      btn.type = "button";
      btn.textContent = running ? "apply (restart)" : "start";
      btn.disabled = state === "starting";
      btn.addEventListener("click", () => applyLiveModel(savedDefault));
      restart.append(msg, btn);
    }

    // Live prompt editor — gated by the selected live model's support.
    if (!liveModelSupportsPrompt(selected, j)) {
      pick(frag, "promptField").hidden = true;
    } else {
      const ta = /** @type {HTMLTextAreaElement} */ (pick(frag, "promptTa"));
      ta.value = j.live_prompt?.content || "";
      wireConfigSave({
        key: "live-prompt",
        btn: /** @type {HTMLButtonElement} */ (pick(frag, "promptSave")),
        textarea: ta,
        status: pick(frag, "promptStatus"),
        onSuccess: () => afterMutate(),
      });
    }
    return frag;
  };

  /** @param {import('../../types.js').AppState} j */
  const renderLiveCard = (j) => {
    const li = j.live_info || {};
    const lp = j.live_prompt || {};
    const selected = j.live_model_default || li.model || "";
    const sig = [
      selected, li.model || "", li.state || "", j.live_model_default || "",
      lp.content || "", liveModelSupportsPrompt(selected, j) ? 1 : 0,
      (liveCatalog?.models || []).length,
    ].join("§");
    renderRegion(liveCardHost, () => buildLiveCard(j), { sig });
  };

  /** @param {import('../../types.js').AppState} j */
  const update = (j) => {
    // Batch: gate prompt/hotwords on the selected batch model; drop the
    // per-session override-count footnote (these are global defaults).
    configCard.render(j, {
      ...configCardCtx,
      supportOverride: selectedSupport(),
      showOverrideCounts: false,
    });
    renderLiveCard(j);

    // Summarizer-default card: seed ONCE from the first poll carrying the
    // saved default (flag flips before the seed so a re-entrant tick can't
    // double-seed), then only the non-interactive hint updates — in place,
    // never a rebuild (interaction hold).
    if (!sdSeeded && j.summarizer_default) {
      sdSeeded = true;
      sdSeed(j.summarizer_default);
    }
    const n = j.default_override_counts?.summarizer || 0;
    sdOverrides.textContent = n ? `· ${n} session${n === 1 ? "" : "s"} override this` : "";
  };

  return { node: frag, update, rebuildEngine: () => rebuildEngine(engineHost) };
}
