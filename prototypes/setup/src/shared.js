// Small helpers shared by the three variants. Now sourced from vanilla-components:
// `bar` is the progress atom, the copy action + CTA come from the button atom.

import { el } from "./dom.js";
import { VC } from "../../_shared/vc.js";
import { buildPlan } from "./engine.js";
import { FAMILIES, INSTALLED, fmtSize, fmtTime } from "./mock-data.js";

/** In manage mode, the not-yet-installed subset of the selection (what would
 *  actually download). In first-run mode, the whole selection. */
export function pendingSelection(ctx) {
  if (ctx.mode !== "manage") return ctx.selection;
  return {
    families: ctx.selection.families.filter((k) => !INSTALLED.families.includes(k)),
    summarize: ctx.selection.summarize && !INSTALLED.summarize,
    backends: ctx.selection.backends,
  };
}

/** How many net-new items the current selection would install (manage mode). */
export function pendingCount(ctx) {
  const p = pendingSelection(ctx);
  return p.families.length + (p.summarize ? 1 : 0);
}

/** A resolved plan for the current ctx. Manage mode plans only the delta. */
export function planFor(ctx) {
  return ctx.mode === "manage"
    ? buildPlan(ctx.machine, pendingSelection(ctx), { manage: true })
    : buildPlan(ctx.machine, ctx.selection);
}

/** ["Whisper", "Parakeet", "Silence gate", "Summarizer"] — plan contents. */
export function planContents(ctx) {
  const fams = ctx.selection.families.map((k) => FAMILIES[k].label.split(" ")[0]);
  const bits = [...fams, "Silence gate"];
  if (ctx.selection.summarize) bits.push("Summarizer");
  return bits;
}

export function planTotals(plan) {
  return {
    size: fmtSize(plan.totalMB),
    time: fmtTime(plan.totalMB, plan.steps.length),
    diskAfter: fmtSize(plan.totalMB + 350),
  };
}

/** vanilla-components progress meter (replaces the old hand-rolled .bar).
 * @param {number} pct @param {"ok"|"warn"|"bad"|"accent"|null} [tone] */
export function bar(pct, tone = null) {
  return VC.progress({ value: pct, tone }).el;
}

/** A small ghost "copy" button that confirms in place. */
export function copyButton(getText) {
  const h = VC.button({
    label: "copy",
    variant: "ghost",
    size: "sm",
    onClick: async () => {
      try { await navigator.clipboard.writeText(getText()); } catch { /* clipboard blocked in sandbox — prototype */ }
      h.setLabel("copied ✓");
      setTimeout(() => h.setLabel("copy"), 1100);
    },
  });
  return h.el;
}

/** The "you're ready" secrets block — the bit the terminal flashes past once. */
export function secretsBlock(secrets) {
  const row = (label, value, hint) =>
    el("div", { class: "secret" },
      el("div", { class: "secret__top" }, el("span", { class: "secret__label" }, label), copyButton(() => value)),
      el("code", { class: "secret__val" }, value),
      hint && el("div", { class: "secret__hint" }, hint),
    );
  return el("div", { class: "secrets" },
    row("Dashboard URL", secrets.url, "open this in your browser"),
    row("Dashboard password", secrets.password, "saved to .auth-password"),
    row("Bridge /tap token", secrets.token, "paste into the bridge popup · saved to .tap-token"),
  );
}
