// @ts-check
// Stages engine selector — a VISIBLE backend-chip row + model-by-family
// picker + (for Canary) source/target language selects. Used in two places:
//   - Settings (the global batch DEFAULT engine), and
//   - Transcript (the engine for the open session).
// Mirrors the data flow of the classic dashboard's session-detail engine
// controls (backend chips from `available_backends`, models grouped by
// family from /api/models, Canary's source_lang/target_lang from the model's
// declared `inputs`) but rendered as the prototype's de-bubbled chip/grid UI.

import { tpl, pick } from "../../templates.js";

// Family display labels + order — same set the classic dashboard uses
// (session-detail.js FAMILY_LABELS).
/** @type {[string, string][]} */
const FAMILY_LABELS = [
  ["whisper", "whisper"],
  ["nb-whisper", "nb-whisper"],
  ["voxtral", "voxtral"],
  ["parakeet", "parakeet"],
  ["canary", "canary"],
];

// Which families run live + batch vs batch-only. Drives the per-family tag.
// Only Whisper / NB-Whisper / Voxtral are live-eligible (see CONTEXT.md).
const LIVE_FAMILIES = new Set(["whisper", "nb-whisper", "voxtral"]);

/** @type {Record<string, string>} */
const BACKEND_LABELS = { auto: "auto", mlx: "mlx", cuda: "cuda", cpu: "cpu" };

/**
 * @typedef {{ backend: string, model: string }} EngineState
 */

/**
 * @param {import('../../types.js').ModelEntry[]} models
 * @param {string} backend
 * @returns {import('../../types.js').ModelEntry[]}
 */
function filterByBackend(models, backend) {
  if (backend === "auto") return models;
  return models.filter((m) => (m.backends || []).includes(backend));
}

/** @param {string} cap @param {Node} body */
function row(cap, body) {
  const frag = tpl("tpl-next-eng-row");
  pick(frag, "cap").textContent = cap;
  pick(frag, "body").appendChild(body);
  return frag;
}

/**
 * Render the engine controls into `host`, wiring chip / model / select
 * changes back through `onChange(next)` where `next` is the new EngineState.
 * Re-render is driven by the caller (it re-invokes render with the new state),
 * matching the prototype's rebuild-on-family/backend-change behaviour.
 *
 * @param {Element} host
 * @param {{
 *   state: EngineState,
 *   catalog: import('../../types.js').ModelCatalog,
 *   onChange: (next: EngineState) => void,
 * }} ctx
 */
export function render(host, { state, catalog, onChange }) {
  const available = new Set(catalog.available_backends || []);
  const models = catalog.models || [];
  const frag = document.createDocumentFragment();

  // ---- Backend chip row ----
  const chips = document.createElement("div");
  chips.className = "chips";
  for (const kind of ["auto", "mlx", "cuda", "cpu"]) {
    const chip = /** @type {HTMLButtonElement} */ (tpl("tpl-next-chip").firstElementChild);
    chip.textContent = BACKEND_LABELS[kind] ?? kind;
    const unavailable = kind !== "auto" && !available.has(kind);
    if (unavailable) {
      chip.disabled = true;
      chip.classList.add("is-off");
      chip.title = `${kind} not available on this server`;
    }
    if (kind === state.backend) chip.classList.add("is-sel");
    chip.addEventListener("click", () => {
      if (chip.disabled || kind === state.backend) return;
      onChange({ ...state, backend: kind });
    });
    chips.appendChild(chip);
  }
  frag.appendChild(row("Backend", chips));

  // ---- Model, grouped by family ----
  const candidates = filterByBackend(models, state.backend);
  const byFamily = new Map();
  for (const m of candidates) {
    const fam = m.family || "other";
    if (!byFamily.has(fam)) byFamily.set(fam, []);
    byFamily.get(fam).push(m);
  }
  const famGrid = document.createElement("div");
  famGrid.className = "famgrid";
  /** @param {string} fam @param {string} label @param {import('../../types.js').ModelEntry[]} entries */
  const addFamily = (fam, label, entries) => {
    const block = tpl("tpl-next-fam");
    pick(block, "family").textContent = label;
    pick(block, "tag").textContent = LIVE_FAMILIES.has(fam) ? "live + batch" : "batch only";
    const fm = pick(block, "models");
    for (const m of entries) {
      const btn = /** @type {HTMLButtonElement} */ (tpl("tpl-next-model").firstElementChild);
      pick(btn, "name").textContent = m.display_name || m.model_id;
      pick(btn, "desc").textContent = m.description || "";
      if (m.model_id === state.model) btn.classList.add("is-sel");
      btn.addEventListener("click", () => {
        if (m.model_id === state.model) return;
        onChange({ ...state, model: m.model_id });
      });
      fm.appendChild(btn);
    }
    famGrid.appendChild(block);
  };
  for (const [fam, label] of FAMILY_LABELS) {
    const entries = byFamily.get(fam);
    if (!entries?.length) continue;
    addFamily(fam, label, entries);
    byFamily.delete(fam);
  }
  for (const [fam, entries] of byFamily) addFamily(fam, fam, entries);
  if (!candidates.length) {
    const none = document.createElement("div");
    none.className = "dim mono eng-none";
    none.textContent = "no models for this backend";
    famGrid.appendChild(none);
  }
  frag.appendChild(row("Model · grouped by family", famGrid));

  // ---- Canary translation: source_lang → target_lang (from the model's
  // declared SelectInputs, exactly like the classic dashboard) ----
  const entry = models.find((m) => m.model_id === state.model);
  const selects = (entry?.inputs || []).filter(
    /** @returns {x is import('../../types.js').SelectInput} */
    (x) => x.type === "select",
  );
  if (selects.length) {
    const wrap = document.createElement("div");
    wrap.className = "selrow";
    for (const input of selects) {
      const sf = tpl("tpl-next-sel");
      pick(sf, "label").textContent = input.label;
      const sel = /** @type {HTMLSelectElement} */ (pick(sf, "select"));
      sel.dataset.inputName = input.name;
      if (input.description) sel.title = input.description;
      for (const opt of input.options || []) {
        sel.add(new Option(opt.label, opt.value, false, opt.value === input.default));
      }
      // Selects are display-only in Phase 1 (no transcribe wiring yet on the
      // Stages engine panel) — the chosen lang is read at submit time when
      // the Recordings transcribe flow lands. Keep the value local.
      wrap.appendChild(sf);
    }
    frag.appendChild(row("Canary translation", wrap));
  }

  host.replaceChildren(frag);
}
