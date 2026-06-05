// @ts-check
// Stages engine selector — a VISIBLE backend-chip row + a COMPACT model
// dropdown + (for Canary) source/target language selects. Used in two places:
//   - Settings (the global batch DEFAULT engine), and
//   - Transcript (the engine for the open session, drives its transcribe jobs).
// Mirrors the data flow of the classic dashboard's session-detail engine
// controls (backend chips from `available_backends`, a model <select> grouped
// by family with <optgroup> from /api/models, Canary's source_lang/target_lang
// from the model's declared `inputs`). The model list used to be a tall
// model-by-family grid; we ship few models, so it's now a single dropdown that
// matches the classic UI's session-detail model <select>.

import { tpl, pick } from "../../templates.js";

// Family display labels + order — used as <optgroup> labels in the model
// dropdown, same set + order the classic dashboard uses (session-detail.js
// FAMILY_LABELS); order here drives the group order in the dropdown.
/** @type {[string, string][]} */
const FAMILY_LABELS = [
  ["whisper", "Whisper"],
  ["nb-whisper", "NB-Whisper (Norwegian)"],
  ["voxtral", "Voxtral (Mistral)"],
  ["parakeet", "Parakeet (NVIDIA)"],
  ["canary", "Canary (NVIDIA, translation)"],
];

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

  // ---- Model · compact dropdown grouped by family ----
  // Mirrors session-detail.js buildModelSelect: one <select>, <optgroup> per
  // family (FAMILY_LABELS order), an "Other" group for unknown families, and
  // each option labelled "display_name — description" like the classic UI.
  const candidates = filterByBackend(models, state.backend);
  const sel = /** @type {HTMLSelectElement} */ (tpl("tpl-next-modelsel").firstElementChild);
  /** @type {Map<string, import('../../types.js').ModelEntry[]>} */
  const byFamily = new Map();
  for (const m of candidates) {
    const fam = m.family || "other";
    if (!byFamily.has(fam)) byFamily.set(fam, []);
    (byFamily.get(fam) ?? []).push(m);
  }
  /** @param {string} label @param {import('../../types.js').ModelEntry[]} entries */
  const addGroup = (label, entries) => {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const m of entries) {
      const txt = m.description ? `${m.display_name || m.model_id} — ${m.description}` : (m.display_name || m.model_id);
      group.appendChild(new Option(txt, m.model_id, false, m.model_id === state.model));
    }
    sel.appendChild(group);
  };
  for (const [fam, label] of FAMILY_LABELS) {
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
  if (!candidates.length) {
    sel.add(new Option("no models for this backend", "", true, true));
    sel.disabled = true;
  }
  sel.addEventListener("change", () => {
    if (!sel.value || sel.value === state.model) return;
    onChange({ ...state, model: sel.value });
  });
  frag.appendChild(row("Model", sel));

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
      // The chosen lang lives only on the <select> (tagged with
      // data-input-name); the Transcript view reads it back from this panel at
      // submit time — see langValues() in views/transcript.js — so nothing here
      // wires a change handler.
      wrap.appendChild(sf);
    }
    frag.appendChild(row("Canary translation", wrap));
  }

  host.replaceChildren(frag);
}
