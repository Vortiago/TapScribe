// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
// Summarizer source controls — the shared wiring behind the Summary view's
// Summarizer panel and the Settings stage's Summarizer-default card. Each
// view's template owns the MARKUP (different slots, different extras); this
// module owns the BEHAVIOR they'd otherwise duplicate:
//
//   - source segctl (Local / API / Command) toggling the detail panes
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
 *   apiPane: HTMLElement,
 *   modelSel: HTMLSelectElement,
 *   modelNote: Element,
 *   emptyModelNote?: string,
 *   maxTokInput: HTMLInputElement,
 *   presetSel: HTMLSelectElement,
 *   presetNote: Element,
 *   cmdInput: HTMLInputElement,
 *   apiBaseInput: HTMLInputElement,
 *   apiModelInput: HTMLInputElement,
 *   apiKeyInput: HTMLInputElement,
 *   apiKeyNote: Element,
 *   canSwitch?: () => boolean,
 *   onCommandInput?: () => void,
 * }} els — the view's prebuilt elements. `srcKey` names the dataset field
 *   carrying each button's source id (Summary uses `data-src`, Settings
 *   `data-sd-src` so the two views' selectors stay distinct while mounted
 *   together). The `api*` elements drive the #85 API pane (OpenAI-compatible /
 *   Ollama): `apiModelInput` is the remote model name (free text, NOT the local
 *   catalog <select>); `apiKeyInput` is WRITE-ONLY — its value is sent only when
 *   non-empty (so the never-serialised key is preserved on save), and
 *   `apiKeyNote` reflects whether a key is already stored (`key_set`).
 *   `canSwitch` vetoes a source click (e.g. mid-Generate); `onCommandInput`
 *   fires whenever the command template changes (preset pick, hand-edit, seed)
 *   so a view can refresh derived UI like the preview.
 */
export function wireSummarizerControls(els) {
  const {
    buttons,
    srcKey,
    localPane,
    commandPane,
    apiPane,
    modelSel,
    modelNote,
    maxTokInput,
    presetSel,
    presetNote,
    cmdInput,
    apiBaseInput,
    apiModelInput,
    apiKeyInput,
    apiKeyNote,
  } = els;

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
    apiPane.hidden = source !== "api";
  };

  /** Reflect whether a key is already stored: the field is write-only (never
   * pre-filled), so the note is the only signal that a save persisted one.
   * @param {boolean} keySet */
  const reflectKeyNote = (keySet) => {
    apiKeyInput.placeholder = keySet ? "•••• stored — leave blank to keep" : "(optional — e.g. blank for local Ollama)";
    apiKeyNote.textContent = keySet
      ? "a key is stored — leave blank to keep it, or type a new one to replace"
      : "no key stored — leave blank for a keyless endpoint (local Ollama)";
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
      modelSel.replaceChildren(); // static-render — one-shot catalog fill at build
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
      presetSel.replaceChildren(); // static-render — one-shot catalog fill at build
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
        apiModelInput.value = d.model; // `model` is one stored field, shared by local + api
      }
      if (typeof d.max_tokens === "number") maxTokInput.value = String(d.max_tokens);
      // API pane: base_url mirrors the stored value (empty clears it); the key
      // is write-only — never seeded, only its presence reflected via key_set.
      if (typeof d.base_url === "string") apiBaseInput.value = d.base_url;
      reflectKeyNote(!!d.key_set);
    });

  /** The card's current values, PUT/POST-body-shaped. `model` comes from
   * whichever source owns the shared field (the local <select> or the api text
   * input). `base_url` is always carried (non-secret, so a local-source save
   * doesn't wipe a configured endpoint); `api_key` is included ONLY when the
   * write-only field is non-empty, so the stored key is preserved-on-omit.
   * @returns {{ source: string, command: string, model: string, max_tokens: number | null, base_url: string, api_key?: string }} */
  const values = () => {
    const mt = parseInt(maxTokInput.value, 10);
    const key = apiKeyInput.value;
    return {
      source,
      command: cmdInput.value.trim(),
      model: (source === "api" ? apiModelInput.value : modelSel.value).trim() || "",
      max_tokens: Number.isFinite(mt) ? mt : null,
      base_url: apiBaseInput.value.trim(),
      ...(key ? { api_key: key } : {}),
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
