// @ts-check
// Stages · Settings (GLOBAL). The default batch engine (backend chips +
// model-by-family + Canary source/target) + the global prompt / hotwords /
// hallucination-rules editors.
//
// REUSES config-card.js verbatim for the prompt/hotwords/hallucination editors
// (its save buttons PUT /api/config/{prompt|hotwords}), and the new Stages
// engine.js for the default engine selector.
//
// Built once for the page; `update(j)` re-runs config-card (which self-gates
// and respects focus) each poll tick. The engine panel is re-rendered by main
// whenever the default engine state changes (rebuildEngine).

import { tpl, pick } from "../../templates.js";
import { header } from "../shell.js";
import * as configCard from "../../components/config-card.js";

/**
 * @param {{
 *   rebuildEngine: (host: Element) => void,
 *   selectedSupport: () => { batch_prompt: boolean, batch_hotwords: boolean } | null,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState) => void, rebuildEngine: () => void }}
 */
export function build(ctx) {
  const frag = tpl("tpl-next-view-settings");

  header(pick(frag, "head"), {
    eyebrow: "Global · Defaults",
    title: "Settings",
    sub: "default engine & prompts applied to every session unless a session overrides them",
  });

  const engineHost = pick(frag, "engineHost");
  const configCardCtx = {
    gridEl: pick(frag, "configGrid"),
    headerNoteEl: pick(frag, "configHeaderNote"),
  };

  ctx.rebuildEngine(engineHost);
  // config-card has a module-level signature cache; clear it once so the
  // first update populates this view's fresh grid.
  configCard.invalidate();

  /** @param {import('../../types.js').AppState} j */
  const update = (j) => {
    // Gate the prompt/hotwords editors on the model picked in the Default
    // engine selector (not the registry-wide flag), and drop the per-session
    // override-count footnote — these are the global defaults, not a session.
    configCard.render(j, {
      ...configCardCtx,
      supportOverride: ctx.selectedSupport(),
      showOverrideCounts: false,
    });
  };

  return { node: frag, update, rebuildEngine: () => ctx.rebuildEngine(engineHost) };
}
