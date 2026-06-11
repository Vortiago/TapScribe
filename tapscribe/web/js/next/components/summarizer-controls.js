// @ts-check
// Summarizer source controls — the shared wiring behind the Summary view's
// Summarizer panel and the Settings stage's Summarizer-default card. Each
// view's template owns the MARKUP (different slots, different extras); this
// module owns the BEHAVIOR they'd otherwise duplicate:
//
//   - source segctl (Local / API-disabled / Command) toggling the detail panes
//   - local model <select> + footprint note, populated once from the memoized
//     catalog fetch (getSummaryCatalog), with the unavailable fallback
//   - max-output-tokens bounds + server default
//   - command preset <select> that SEEDS the editable template (NOT an
//     allowlist) + flip-to-"custom…" when the operator hand-edits
//   - `catReady`-sequenced application of saved values (seedSaved), so the
//     model option exists and the preset match sees the real list whichever
//     of seed and fetch lands first
//
// Both cards are BUILD-ONCE interactive regions, so renderRegion is never
// involved and the interaction hold holds by construction (the focus-sweep
// e2e covers every control). The views keep what genuinely differs:
// Summary's "will run" preview + Generate body, Settings' save-the-whole-
// object semantics. #85's API pane lands HERE once, for both cards.

import { getSummaryCatalog } from "../../api.js";

/** @param {number} t */
const ctxLabel = (t) => (t >= 1000 ? `${Math.round(t / 1000)}K` : `${t}`);

/**
 * @param {{
 *   buttons: NodeListOf<HTMLButtonElement>,
 *   srcKey: string,
 *   localPane: HTMLElement,
 *   commandPane: HTMLElement,
 *   modelSel: HTMLSelectElement,
 *   modelNote: Element,
 *   emptyModelNote?: string,
 *   maxTokInput: HTMLInputElement,
 *   presetSel: HTMLSelectElement,
 *   presetNote: Element,
 *   cmdInput: HTMLInputElement,
 *   canSwitch?: () => boolean,
 *   onCommandInput?: () => void,
 * }} els — the view's prebuilt elements. `srcKey` names the dataset field
 *   carrying each button's source id (Summary uses `data-src`, Settings
 *   `data-sd-src` so the two views' selectors stay distinct while mounted
 *   together). `canSwitch` vetoes a source click (e.g. mid-Generate);
 *   `onCommandInput` fires whenever the command template changes (preset
 *   pick, hand-edit, seed) so a view can refresh derived UI like the preview.
 */
export function wireSummarizerControls(els) {
  const { buttons, srcKey, localPane, commandPane, modelSel, modelNote, maxTokInput, presetSel, presetNote, cmdInput } =
    els;

  /** @type {import('../../types.js').SummaryModel[]} */
  let models = [];
  /** @type {import('../../types.js').CommandPreset[]} */
  let presets = [];
  /** The selected source ("local" until a click or a seed says otherwise). */
  let source = "local";

  /** Reflect `source` onto the segmented buttons + detail panes. Pure view
   * sync, no fetch. */
  const applySource = () => {
    for (const b of buttons) {
      const on = b.dataset[srcKey] === source;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
    localPane.hidden = source !== "local";
    commandPane.hidden = source !== "command";
  };
  for (const b of buttons) {
    b.addEventListener("click", () => {
      const next = b.dataset[srcKey];
      if (b.disabled || !next || next === source) return;
      if (els.canSwitch && !els.canSwitch()) return;
      source = next;
      applySource();
    });
  }
  applySource();

  /** Show the picked model's footprint + context + note under the dropdown. */
  const reflectModelNote = () => {
    const m = models.find((x) => x.repo_id === modelSel.value);
    modelNote.textContent = m
      ? `≈${m.approx_gb} GB · ${ctxLabel(m.context_tokens)} ctx${m.note ? ` · ${m.note}` : ""}`
      : els.emptyModelNote || "";
  };
  modelSel.addEventListener("change", reflectModelNote);

  /** Reflect the picked preset's caveat note under the dropdown. */
  const reflectPresetNote = () => {
    const p = presets.find((x) => x.key === presetSel.value);
    presetNote.textContent = p ? p.note : "";
  };

  /** Re-point the preset dropdown at whichever preset matches the current
   * template verbatim — "custom…" when none does. */
  const syncPresetToCommand = () => {
    const match = presets.find((x) => x.template === cmdInput.value);
    presetSel.value = match ? match.key : "";
    reflectPresetNote();
  };

  presetSel.addEventListener("change", () => {
    const p = presets.find((x) => x.key === presetSel.value);
    if (p) cmdInput.value = p.template;
    reflectPresetNote();
    els.onCommandInput?.();
  });
  cmdInput.addEventListener("input", () => {
    // A hand-edited template is no longer the preset verbatim — flip the
    // dropdown back to "custom…" so it doesn't claim otherwise.
    const p = presets.find((x) => x.key === presetSel.value);
    if (p && p.template !== cmdInput.value) {
      presetSel.value = "";
      reflectPresetNote();
    }
    els.onCommandInput?.();
  });

  // Populate the selects ONCE from the memoized catalog fetch, then leave
  // them alone (never a per-tick rebuild). `catReady` resolves once the
  // options (or the unavailable fallback) are in place — `seedSaved`
  // sequences saved-value application on it.
  const catReady = (async () => {
    try {
      const cat = await getSummaryCatalog();
      models = cat.models || [];
      modelSel.replaceChildren();
      for (const m of models) modelSel.add(new Option(m.label || m.repo_id, m.repo_id, m.is_default, m.is_default));
      if (!models.length) {
        modelSel.add(new Option("no local models", "", true, true));
        modelSel.disabled = true;
      }
      reflectModelNote();
      // Seed the output-cap input's bounds + default from the server (one
      // source of truth — the HTML value is only a placeholder until this
      // lands). A saved default's max_tokens lands AFTER this via catReady,
      // so it wins.
      if (typeof cat.max_tokens_min === "number") maxTokInput.min = String(cat.max_tokens_min);
      if (typeof cat.max_tokens_max === "number") maxTokInput.max = String(cat.max_tokens_max);
      if (typeof cat.max_tokens_default === "number") maxTokInput.value = String(cat.max_tokens_default);
      // Command presets (same fetch): "custom…" + one option per known tool,
      // pre-selecting whichever matches the template field's current value.
      presets = cat.command_presets || [];
      presetSel.replaceChildren();
      presetSel.add(new Option("custom…", ""));
      for (const p of presets) presetSel.add(new Option(p.label, p.key));
      syncPresetToCommand();
    } catch {
      // Best-effort: leave the dropdowns disabled and let the server resolve
      // its defaults from an empty `model`, rather than blocking the operator.
      modelSel.add(new Option("model list unavailable — using default", "", true, true));
      modelSel.disabled = true;
      presetSel.add(new Option("presets unavailable", "", true, true));
      presetSel.disabled = true;
    }
  })();

  /**
   * Apply a saved config's global-layer fields (command/model/max_tokens) —
   * sequenced on `catReady` so the option lists exist first.
   * `clearEmptyCommand` is the Settings-editor semantic (the card mirrors
   * the stored object exactly, so an empty stored command clears the field);
   * the Summary view omits it to keep its template default as a convenience.
   * @param {Partial<import('../../types.js').SummarizerDefault>} d
   * @param {{ clearEmptyCommand?: boolean }} [opts]
   */
  const seedSaved = (d, { clearEmptyCommand = false } = {}) =>
    catReady.then(() => {
      if (d.command || clearEmptyCommand) {
        cmdInput.value = d.command || "";
        syncPresetToCommand();
        els.onCommandInput?.();
      }
      if (d.model) {
        modelSel.value = d.model; // stays on the fallback option if unlisted
        reflectModelNote();
      }
      if (typeof d.max_tokens === "number") maxTokInput.value = String(d.max_tokens);
    });

  /** The card's current values, PUT/POST-body-shaped. */
  const values = () => {
    const mt = parseInt(maxTokInput.value, 10);
    return {
      source,
      command: cmdInput.value.trim(),
      model: modelSel.value || "",
      max_tokens: Number.isFinite(mt) ? mt : null,
    };
  };

  return {
    catReady,
    get source() {
      return source;
    },
    /** @param {string} s */
    setSource(s) {
      source = s;
      applySource();
    },
    seedSaved,
    values,
  };
}
