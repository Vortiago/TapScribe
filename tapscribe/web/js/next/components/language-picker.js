// @ts-check
// Shared helpers for the candidate-language <select multiple> pickers
// (ADR-0009). The per-meeting override (Capture) and the global default
// (Settings) render the same catalog of selectable languages; these three
// tiny, framework-free helpers populate the options and read/write the
// selection so the two call sites can't drift in how a code maps to an option.

/**
 * Populate a <select multiple> with one option per catalog language.
 * @param {HTMLSelectElement} sel
 * @param {import('../../types.js').LanguageCatalog} catalog
 */
export function fillLanguageOptions(sel, catalog) {
  sel.replaceChildren();
  for (const { code, name } of catalog?.languages || []) {
    sel.add(new Option(`${name} (${code})`, code));
  }
}

/**
 * Mark exactly the options in `codes` as selected (others cleared).
 * @param {HTMLSelectElement} sel
 * @param {string[]} codes
 */
export function setSelectedLanguages(sel, codes) {
  const want = new Set(codes || []);
  for (const opt of Array.from(sel.options)) opt.selected = want.has(opt.value);
}

/**
 * The currently-selected language codes, in option order.
 * @param {HTMLSelectElement} sel
 * @returns {string[]}
 */
export function selectedLanguages(sel) {
  return Array.from(sel.selectedOptions).map((o) => o.value);
}
