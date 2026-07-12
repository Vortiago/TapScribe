// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
// Stages engine selector — a VISIBLE backend-chip row + a COMPACT model
// dropdown. Used in ONE place now:
//   - Settings (the global batch DEFAULT engine = the ADR-0010 generalist).
// The Transcript stage dropped its engine selector — the operator declares
// LANGUAGES there, not a model (ADR-0011), and its transcribe jobs resolve the
// generalist server-side (batch-model.txt).
// Mirrors the data flow of the classic dashboard's session-detail engine
// controls (backend chips from `available_backends`, a model <select> grouped
// by family with <optgroup> from /api/models). The model list used to be a
// tall model-by-family grid; we ship few models, so it's now a single dropdown
// that matches the classic UI's session-detail model <select>.

import { tpl, mount, pick } from "../../templates.js";
import { FAMILY_LABELS, buildModelSelect } from "../../model-select.js";

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
  // Mirrors the classic dashboard's session-detail model <select>: one
  // <select>, <optgroup> per family (FAMILY_LABELS order), an "Other" group
  // for unknown families, and each option labelled "display_name —
  // description". Shared with live-channel.js / settings.js — see #225.
  const candidates = filterByBackend(models, state.backend);
  const sel = /** @type {HTMLSelectElement} */ (tpl("tpl-next-modelsel").firstElementChild);
  buildModelSelect(sel, candidates, {
    selected: state.model,
    familyLabels: FAMILY_LABELS,
    withDescriptions: true,
    emptyLabel: "no models for this backend",
  });
  sel.addEventListener("change", () => {
    if (!sel.value || sel.value === state.model) return;
    onChange({ ...state, model: sel.value });
  });
  frag.appendChild(row("Model", sel));

  mount(host, frag);
}
