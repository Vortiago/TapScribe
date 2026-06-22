// Variant D — "Centered console": the synthesis. C's substance — the dark
// TapScribe control-surface look + the full matrix that surfaces EVERYTHING that
// gets installed — staged in A's centered, focused card with A's big install
// moment. Doubles as the post-first-run "Manage models" surface (ctx.mode).

import { el } from "./dom.js";
import { VC } from "../../_shared/vc.js";
import { machineStrip, matrix, runRows } from "./console-parts.js";
import { planFor, planTotals, pendingCount, secretsBlock, bar } from "./shared.js";

function header(ctx) {
  return el("header", { class: "d-head" },
    el("div", { class: "c-brand" }, el("span", { class: "c-brand__dot" }), "TapScribe",
      el("span", { class: "c-brand__tag" }, ctx.mode === "manage" ? "models" : "install")),
    machineStrip(ctx),
  );
}

function summary(ctx) {
  const t = planTotals(planFor(ctx));
  if (ctx.mode === "manage") {
    return el("div", { class: "d-summary" },
      VC.statCard({ label: "New download", value: t.size }).el,
      VC.statCard({ label: "Est. time", value: t.time }).el,
      VC.statCard({ label: "New models", value: String(pendingCount(ctx)) }).el,
    );
  }
  return el("div", { class: "d-summary" },
    VC.statCard({ label: "Download", value: t.size }).el,
    VC.statCard({ label: "Est. time", value: t.time }).el,
    VC.statCard({ label: "Disk after", value: t.diskAfter }).el,
  );
}

function chooseView(ctx) {
  const manage = ctx.mode === "manage";
  const n = pendingCount(ctx);
  const cta = manage
    ? VC.button({ label: n ? `Install ${n} model${n > 1 ? "s" : ""}` : "Select models to add", variant: "primary", disabled: n === 0, onClick: () => ctx.startInstall() })
    : VC.button({ label: "Install & launch TapScribe", variant: "primary", onClick: () => ctx.startInstall() });

  return el("div", { class: "d-card" },
    el("div", { class: "d-title" },
      el("h1", { class: "d-h1" }, manage ? "Manage models" : "Set up TapScribe"),
      el("p", { class: "d-sub" }, manage
        ? "Add or change models — your current ones stay installed, only new picks download."
        : "Everything that gets installed is below — tweak any backend, then install. Nothing's hidden."),
    ),
    matrix(ctx),
    summary(ctx),
    el("div", { class: "d-cta" }, cta.el),
    el("p", { class: "d-foot" }, manage
      ? "Re-run anytime · unchanged selections skip pip · nothing leaves this machine."
      : "Editable install · re-run anytime (unchanged selections skip pip) · nothing leaves this machine."),
  );
}

function installView(ctx) {
  const plan = ctx.activePlan;
  const overall = ctx.progress.overall;
  const current = plan.steps.find((s) => s.state === "active") || plan.steps[plan.steps.length - 1];

  return el("div", { class: "d-card" },
    el("h1", { class: "d-h1" }, ctx.mode === "manage" ? "Installing models" : "Installing TapScribe"),
    el("p", { class: "d-sub" }, ctx.mode === "manage" ? "Only the new models download — you can keep working." : "One-time download — you can leave it running."),
    el("div", { class: "d-prog" },
      bar(overall, "accent"),
      el("div", { class: "d-prog__row" },
        el("span", { class: "d-prog__pct" }, `${overall}%`),
        el("span", { class: "d-prog__step" }, current ? current.label : "Finishing up"),
      ),
    ),
    runRows(plan), // per-step transparency — see each component install
    el("pre", { class: "d-term" }, ctx.progress.log.slice(-200).join("\n") + "\n█"),
  );
}

function doneView(ctx) {
  if (ctx.mode === "manage") {
    return el("div", { class: "d-card d-card--done" },
      el("div", { class: "d-check" }, "✓"),
      el("h1", { class: "d-h1" }, "Models installed"),
      el("p", { class: "d-sub" }, "They're loaded into the running engine — pick them on your next job."),
      el("div", { class: "d-cta" }, VC.button({ label: "Back to dashboard →", variant: "primary", href: ctx.progress.secrets.url, target: "_blank" }).el),
    );
  }
  return el("div", { class: "d-card d-card--done" },
    el("div", { class: "d-check" }, "✓"),
    el("h1", { class: "d-h1" }, "TapScribe is running"),
    el("p", { class: "d-sub" }, "Open the dashboard and paste the token into your bridge."),
    secretsBlock(ctx.progress.secrets),
    el("div", { class: "d-cta" }, VC.button({ label: "Open dashboard →", variant: "primary", href: ctx.progress.secrets.url, target: "_blank" }).el),
    el("p", { class: "d-foot" }, "Stop anytime with Ctrl+C in the terminal that launched it."),
  );
}

export default function render(root, ctx) {
  root.className = "stage setup setup--d";
  const body = ctx.phase === "installing" ? installView(ctx) : ctx.phase === "done" ? doneView(ctx) : chooseView(ctx);
  root.append(el("div", { class: "d-wrap" }, header(ctx), body));
}
