// @ts-check
// Shared "group models by family into <optgroup>s" builder (#225) — used by
// every family-grouped model <select> in the dashboard: the Stages batch
// engine picker (next/components/engine.js), the classic live-channel panel
// (components/live-channel.js), and the Stages live-model row
// (next/views/settings.js). One label table + one builder means adding a
// family (e.g. Parakeet going live-eligible per CONTEXT.md) is a one-line
// change here instead of three hand-edited loops.
//
// groupModelsByFamily is pure (no DOM) so it's unit-tested under
// `node --test` without a browser; buildModelSelect is the thin DOM half,
// covered by the playwright dashboard e2e (test_dashboard_ui.py) — same
// pure/DOM split as live-feed.js's coalescing helpers.

/** @type {[string, string][]} */
export const FAMILY_LABELS = [
  ["whisper", "Whisper"],
  ["nb-whisper", "NB-Whisper (Norwegian)"],
  ["voxtral", "Voxtral (Mistral)"],
  ["parakeet", "Parakeet (NVIDIA)"],
];

// Families with at least one live-eligible model today, sliced from
// FAMILY_LABELS — adding a family there doesn't silently make it
// live-eligible too; a family becomes live-eligible only when its key is
// added here as well.
const LIVE_ELIGIBLE_FAMILIES = new Set(["whisper", "nb-whisper", "voxtral"]);

/** @type {[string, string][]} */
export const LIVE_FAMILY_LABELS = FAMILY_LABELS.filter(([fam]) => LIVE_ELIGIBLE_FAMILIES.has(fam));

/**
 * Bucket `models` by family and order the groups per `familyLabels`; models
 * whose family isn't in `familyLabels` all spill into one trailing "Other"
 * group (first-seen order). Pure — no DOM — so it's unit-testable in Node.
 *
 * @param {import('./types.js').ModelEntry[]} models
 * @param {[string, string][]} familyLabels
 * @returns {{ label: string, models: import('./types.js').ModelEntry[] }[]}
 */
export function groupModelsByFamily(models, familyLabels) {
  /** @type {Map<string, import('./types.js').ModelEntry[]>} */
  const byFamily = new Map();
  for (const m of models) {
    const fam = m.family || "other";
    if (!byFamily.has(fam)) byFamily.set(fam, []);
    (byFamily.get(fam) ?? []).push(m);
  }
  /** @type {{ label: string, models: import('./types.js').ModelEntry[] }[]} */
  const groups = [];
  for (const [fam, label] of familyLabels) {
    const entries = byFamily.get(fam);
    if (!entries?.length) continue;
    groups.push({ label, models: entries });
    byFamily.delete(fam);
  }
  if (byFamily.size) {
    /** @type {import('./types.js').ModelEntry[]} */
    const rest = [];
    for (const [, entries] of byFamily) rest.push(...entries);
    groups.push({ label: "Other", models: rest });
  }
  return groups;
}

/**
 * Populate `sel` with one <option> per model, grouped into <optgroup>s via
 * groupModelsByFamily. Clears any existing children first.
 *
 * @param {HTMLSelectElement} sel
 * @param {import('./types.js').ModelEntry[]} models
 * @param {{
 *   selected: string,
 *   familyLabels: [string, string][],
 *   withDescriptions?: boolean,
 *   unregisteredFallback?: boolean,
 *   emptyLabel?: string,
 * }} opts
 */
export function buildModelSelect(sel, models, opts) {
  const { selected, familyLabels, withDescriptions, unregisteredFallback, emptyLabel } = opts;
  sel.replaceChildren();
  let found = false;
  for (const { label, models: entries } of groupModelsByFamily(models, familyLabels)) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const m of entries) {
      const text = withDescriptions && m.description
        ? `${m.display_name || m.model_id} — ${m.description}`
        : (m.display_name || m.model_id);
      const isSelected = m.model_id === selected;
      if (isSelected) found = true;
      group.appendChild(new Option(text, m.model_id, false, isSelected));
    }
    sel.appendChild(group);
  }
  if (!found && unregisteredFallback && selected) {
    sel.add(new Option(`${selected} (unregistered)`, selected, false, true));
  }
  if (!models.length && emptyLabel) {
    sel.add(new Option(emptyLabel, "", true, true));
    sel.disabled = true;
  }
}
