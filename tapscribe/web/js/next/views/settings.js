// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
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
import { getJson, putJson, errText, getBridgeCatalog } from "../../api.js";
import { wireConfigSave, wireSave } from "../../save-status.js";
import { wireSummarizerControls } from "../components/summarizer-controls.js";
import { fillLanguageOptions, setSelectedLanguages, selectedLanguages } from "../components/language-picker.js";
import { header } from "../shell.js";
import { makeStatusFlasher, copyToClipboard } from "../ui.js";
import * as configCard from "../../components/config-card.js";
import { LIVE_FAMILY_LABELS, buildModelSelect } from "../../model-select.js";

/**
 * @param {{
 *   rebuildEngine: (host: Element) => void,
 *   selectedSupport: () => { batch_prompt: boolean, batch_hotwords: boolean } | null,
 *   liveCatalog: import('../../types.js').ModelCatalog,
 *   languageCatalog: import('../../types.js').LanguageCatalog,
 *   applyLiveModel: (model: string) => void,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState) => void, rebuildEngine: () => void }}
 */
export function build(ctx) {
  const { rebuildEngine, selectedSupport, liveCatalog, languageCatalog, applyLiveModel, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-settings");

  header(pick(frag, "head"), {
    eyebrow: "Global · Defaults",
    title: "Settings",
    sub: "live & batch engines + prompts — each prompt gates on its own model; sessions can override",
  });

  // ---- Models card: link to the browser setup / manage-models page ----------
  // /setup is a separate full-page app, so the button itself is a plain
  // <a href="/setup"> in the template (no JS for navigation). We only fill the
  // installed-summary line once, best-effort, from the same state /setup reads.
  const modelsInstalled = pick(frag, "modelsInstalled");
  getJson("/api/setup/state")
    .then((/** @type {{ families?: { installed: boolean, label: string }[] }} */ s) => {
      const installed = (s?.families || []).filter((f) => f.installed).map((f) => f.label);
      modelsInstalled.textContent = installed.length
        ? `Installed: ${installed.join(", ")}`
        : "No models installed yet — open setup to add one.";
    })
    .catch(() => { modelsInstalled.textContent = "Couldn't load installed models."; });

  // ---- Connect-a-bridge card (#190) ------------------------------------------
  // Host/port are read straight off window.location — the browser already
  // reached this dashboard via the same host:port a Bridge needs, so there's
  // no backend state to keep in sync. The token itself is fetched from
  // GET /api/tap-token only on an explicit reveal or copy click (never on
  // load, never on a poll tick) and cached in a closure var so a second
  // click doesn't re-fetch; the card is built once here and never touched
  // by `update`, so there is nothing to renderRegion-guard.
  const bridgeHost = pick(frag, "bridgeHost");
  const bridgePort = pick(frag, "bridgePort");
  const bridgeToken = pick(frag, "bridgeToken");
  const bridgeReveal = /** @type {HTMLButtonElement} */ (pick(frag, "bridgeReveal"));
  const bridgeCopy = /** @type {HTMLButtonElement} */ (pick(frag, "bridgeCopy"));
  const bridgeStatus = pick(frag, "bridgeStatus");

  bridgeHost.textContent = window.location.hostname || "localhost";
  bridgePort.textContent = window.location.port || (window.location.protocol === "https:" ? "443" : "80");

  const TOKEN_MASK = "••••••••••••";
  /** @type {string | null} */
  let cachedTapToken = null;
  let tokenRevealed = false;
  const flashBridgeStatus = makeStatusFlasher(bridgeStatus);

  /** Fetch the tap token once and cache it; every subsequent reveal/copy
   * reuses the cached value instead of hitting the endpoint again.
   * @returns {Promise<string>} */
  const loadTapToken = async () => {
    if (cachedTapToken === null) {
      const j = await getJson("/api/tap-token");
      cachedTapToken = j.token || "";
    }
    return /** @type {string} */ (cachedTapToken);
  };

  bridgeReveal.addEventListener("click", async () => {
    if (tokenRevealed) {
      // Hiding again re-masks the DOM rather than leaving the plaintext
      // token sitting in a display:none node — "explicit reveal", not
      // "revealed once, then forever in the DOM".
      tokenRevealed = false;
      bridgeToken.textContent = TOKEN_MASK;
      bridgeReveal.textContent = "👁 reveal";
      return;
    }
    bridgeReveal.disabled = true;
    try {
      const t = await loadTapToken();
      tokenRevealed = true;
      bridgeToken.textContent = t;
      bridgeReveal.textContent = "🙈 hide";
    } catch (e) {
      flashBridgeStatus(`couldn't load token: ${errText(e)}`);
    } finally {
      bridgeReveal.disabled = false;
    }
  });

  bridgeCopy.addEventListener("click", async () => {
    let t;
    try {
      t = await loadTapToken();
    } catch (e) {
      flashBridgeStatus(`couldn't load token: ${errText(e)}`);
      return;
    }
    // Shared copy flow (ui.js copyToClipboard) — the fallback here is a
    // prompt() the operator can select-copy from, covering both the
    // non-secure context and a rejected clipboard write.
    await copyToClipboard(t, {
      onOk: () => flashBridgeStatus("✓ copied"),
      onFallback: () => { window.prompt("Copy the /tap bearer token (Ctrl/Cmd-C, Enter):", t); },
    });
  });

  // ---- Get-a-bridge card ------------------------------------------------------
  // static-render: built ONCE here, never touched by `update`. The two download
  // anchors point at GitHub-Release assets (releases/latest/download/<asset>),
  // so they are plain cross-origin hrefs — NOT same-origin triggerDownload
  // targets. The hrefs are filled from the memoized bridge catalog
  // (api.js getBridgeCatalog — ONE best-effort GET /api/bridges per page,
  // even across the boot-time view rebuild), matched by id; on failure the
  // hint line degrades gracefully. The latest/download URLs 404 until the
  // first tagged release, so the hint always names that caveat.
  const bridgeDlSpatial = /** @type {HTMLAnchorElement} */ (pick(frag, "bridgeDlSpatial"));
  const bridgeDlTray = /** @type {HTMLAnchorElement} */ (pick(frag, "bridgeDlTray"));
  const bridgeDlHint = pick(frag, "bridgeDlHint");
  /** @type {Record<string, HTMLAnchorElement>} */
  const bridgeAnchors = { spacialchat: bridgeDlSpatial, "windows-tray": bridgeDlTray };
  getBridgeCatalog()
    .then((bridges) => {
      for (const b of bridges || []) {
        const a = bridgeAnchors[b.id];
        if (a && b.download_url) {
          a.href = b.download_url;
          a.rel = "noopener";
          a.setAttribute("download", "");
        }
      }
      bridgeDlHint.textContent = "Links resolve to the newest tagged release — available after the first one is cut.";
    })
    .catch(() => { bridgeDlHint.textContent = "Couldn't load bridge downloads — see the repo's Releases page."; });

  // BATCH card hosts (reused engine selector + config-card).
  const engineHost = pick(frag, "engineHost");
  const configCardCtx = {
    gridEl: pick(frag, "configGrid"),
    headerNoteEl: pick(frag, "configHeaderNote"),
  };
  // LIVE card host (renderRegion target).
  const liveCardHost = pick(frag, "liveCardHost");

  rebuildEngine(engineHost);

  // ---- Default candidate languages (ADR-0010) -------------------------------
  // A multi-select over the static language catalog. Built + filled once;
  // seeded ONCE from the first poll carrying `languages.default`, then never
  // re-touched (the summarizer card's interaction-hold discipline), so a poll
  // can't clobber an in-progress edit. Save persists the global default.
  const langSel = /** @type {HTMLSelectElement} */ (pick(frag, "langSel"));
  const langSave = /** @type {HTMLButtonElement} */ (pick(frag, "langSave"));
  const langStatus = pick(frag, "langStatus");
  fillLanguageOptions(langSel, languageCatalog);
  let langSeeded = false;
  wireSave({
    btn: langSave,
    status: langStatus,
    put: () => putJson("/api/config/languages", { content: selectedLanguages(langSel).join(",") }),
    onSuccess: () => afterMutate(),
  });

  // ---- Advanced card (#210) --------------------------------------------------
  // The operator knobs that used to be env-only. Each row is the same shape —
  // number input + save + status, saving through PUT /api/config/{key} and
  // seeding from the resolved value on /api/state — so one row record drives the
  // wiring and the seeding below instead of four hand-written regions. The
  // pick() names stay literal (check-slots resolves the .html ↔ .js seam from
  // exactly those literals).
  /**
   * @typedef {"idle_ttl_s" | "parakeet_chunk_s" | "parakeet_overlap_s"
   *   | "summarize_timeout_s" | "summarize_gguf_ctx"} KnobField
   */
  /**
   * @param {string} key the /api/config/{key} this row saves through
   * @param {KnobField} field the /api/state field carrying its value in force
   * @param {HTMLInputElement} input
   * @param {HTMLButtonElement} btn
   * @param {HTMLElement} status
   */
  const knobRow = (key, field, input, btn, status) => {
    // Two holds, both needed. The ROW (input + its save button) covers focus, so
    // a repaint can't land between the click and the PUT reading `input.value`.
    // `baseline` — the last value the poll or a successful save put in the box —
    // covers the rest: it tells "untouched since we wrote it" from "typed but
    // not saved yet", so filling two knobs and saving one can't have the poll
    // wipe the other one's pending edit (config-card.js keeps the same baseline
    // for its unsaved badge).
    const knob = { field, input, row: input.closest(".field") || input, baseline: "" };
    wireConfigSave({
      key,
      btn,
      textarea: input,
      status,
      onSuccess: (v) => {
        knob.baseline = v;
        afterMutate();
      },
    });
    return knob;
  };
  const knobs = [
    knobRow("model-idle-ttl", "idle_ttl_s",
      /** @type {HTMLInputElement} */ (pick(frag, "setIdleTtlS")),
      /** @type {HTMLButtonElement} */ (pick(frag, "setIdleTtlSSave")),
      pick(frag, "setIdleTtlSStatus")),
    knobRow("parakeet-chunk-s", "parakeet_chunk_s",
      /** @type {HTMLInputElement} */ (pick(frag, "setChunkS")),
      /** @type {HTMLButtonElement} */ (pick(frag, "setChunkSSave")),
      pick(frag, "setChunkSStatus")),
    knobRow("parakeet-overlap-s", "parakeet_overlap_s",
      /** @type {HTMLInputElement} */ (pick(frag, "setOverlapS")),
      /** @type {HTMLButtonElement} */ (pick(frag, "setOverlapSSave")),
      pick(frag, "setOverlapSStatus")),
    knobRow("summarize-timeout-s", "summarize_timeout_s",
      /** @type {HTMLInputElement} */ (pick(frag, "setSummarizeTimeoutS")),
      /** @type {HTMLButtonElement} */ (pick(frag, "setSummarizeTimeoutSSave")),
      pick(frag, "setSummarizeTimeoutSStatus")),
    knobRow("summarize-gguf-ctx", "summarize_gguf_ctx",
      /** @type {HTMLInputElement} */ (pick(frag, "setGgufCtx")),
      /** @type {HTMLButtonElement} */ (pick(frag, "setGgufCtxSave")),
      pick(frag, "setGgufCtxStatus")),
  ];
  const specialistsEl = pick(frag, "setSpecialists");

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
  const sdApi = /** @type {HTMLElement} */ (pick(frag, "sdApi"));
  const sdModelSel = /** @type {HTMLSelectElement} */ (pick(frag, "sdModel"));
  const sdModelNote = pick(frag, "sdModelNote");
  const sdMaxTok = /** @type {HTMLInputElement} */ (pick(frag, "sdMaxTokens"));
  const sdPresetSel = /** @type {HTMLSelectElement} */ (pick(frag, "sdCmdPreset"));
  const sdPresetNote = pick(frag, "sdCmdPresetNote");
  const sdCmd = /** @type {HTMLInputElement} */ (pick(frag, "sdCmd"));
  const sdApiBase = /** @type {HTMLInputElement} */ (pick(frag, "sdApiBase"));
  const sdApiModel = /** @type {HTMLInputElement} */ (pick(frag, "sdApiModel"));
  const sdApiKey = /** @type {HTMLInputElement} */ (pick(frag, "sdApiKey"));
  const sdApiKeyNote = pick(frag, "sdApiKeyNote");
  const sdPrompt = /** @type {HTMLTextAreaElement} */ (pick(frag, "sdPrompt"));
  const sdOverrides = pick(frag, "sdOverrides");

  /** One-shot: seed the controls from the first poll carrying the saved
   * default, then never touch them again (interaction hold). */
  let sdSeeded = false;

  // Source segctl + model/preset/max-tokens wiring is the shared
  // summarizer-controls component (the Summary view uses the same one).
  const ctl = wireSummarizerControls({
    buttons: sdButtons,
    srcKey: "sdSrc",
    localPane: sdLocal,
    commandPane: sdCommand,
    apiPane: sdApi,
    modelSel: sdModelSel,
    modelNote: sdModelNote,
    maxTokInput: sdMaxTok,
    presetSel: sdPresetSel,
    presetNote: sdPresetNote,
    cmdInput: sdCmd,
    apiBaseInput: sdApiBase,
    apiModelInput: sdApiModel,
    apiKeyInput: sdApiKey,
    apiKeyNote: sdApiKeyNote,
  });

  /** Seed the controls from the saved global default — once. The card is the
   * stored object's EDITOR, so it mirrors it exactly (clearEmptyCommand:
   * an empty stored command clears the field, unlike the Summary view's
   * keep-the-template-default convenience). */
  /** @param {import('../../types.js').SummarizerDefault} d */
  const sdSeed = (d) => {
    ctl.setSource(d.source || "local"); // "" (unset) shows the built-in default
    sdPrompt.value = d.prompt || "";
    ctl.seedSaved(d, { clearEmptyCommand: true });
  };

  wireSave({
    btn: /** @type {HTMLButtonElement} */ (pick(frag, "sdSave")),
    status: pick(frag, "sdStatus"),
    put: () => putJson("/api/summarize/config", { ...ctl.values(), prompt: sdPrompt.value }),
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
    catch (e) { alert(`Save live model failed: ${errText(e)}`); }
    finally { afterMutate(); }
  };

  /** Build the compact live-model <select> row (grouped by family, shared
   * with the classic live-channel panel and the Stages engine picker — #225). */
  /** @param {string} selected */
  const buildModelRow = (selected) => {
    const row = tpl("tpl-next-eng-row");
    pick(row, "cap").textContent = "Model";
    const sel = /** @type {HTMLSelectElement} */ (tpl("tpl-next-modelsel").firstElementChild);
    sel.id = "nextLiveModelSelect";
    const models = liveCatalog?.models || [];
    buildModelSelect(sel, models, {
      selected,
      familyLabels: LIVE_FAMILY_LABELS,
      unregisteredFallback: true,
      emptyLabel: "no live models",
    });
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

    // Seed the default-languages multi-select once from the first poll carrying
    // the saved default (flag flips before the seed so a re-entrant tick can't
    // double-seed); thereafter it's the operator's to edit, untouched by polls.
    if (!langSeeded && j.languages) {
      langSeeded = true;
      setSelectedLanguages(langSel, j.languages.default || []);
    }
    const n = j.default_override_counts?.summarizer || 0;
    sdOverrides.textContent = n ? `· ${n} session${n === 1 ? "" : "s"} override this` : "";

    // Advanced knobs: render the value IN FORCE on every poll, behind the usual
    // interaction hold (skip the row the operator is in). Not a one-shot seed —
    // /api/state carries the RESOLVED value, so a save the server accepted but
    // did not honour verbatim (the joint chunk/overlap clamp reduces an overlap
    // the moment a small chunk lands) must correct the field, or the card shows
    // a number the recorder isn't using and the operator's next save persists it.
    for (const knob of knobs) {
      const { field, input, row } = knob;
      if (j[field] === undefined || row.contains(document.activeElement)) continue;
      if (input.value !== knob.baseline) continue; // typed, not saved — the operator's
      const next = String(j[field]);
      if (input.value !== next) input.value = next;
      knob.baseline = next;
    }
    const specialists = Object.entries(j.specialists || {});
    specialistsEl.textContent = specialists.length
      ? specialists.map(([lang, model]) => `${lang}: ${model}`).join(", ")
      : "(none)";
  };

  return { node: frag, update, rebuildEngine: () => rebuildEngine(engineHost) };
}
