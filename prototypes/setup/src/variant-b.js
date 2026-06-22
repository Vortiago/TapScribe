// Variant B — "Setup Assistant": a guided wizard, one decision per screen, framed
// by USE-CASE not model name. Buttons / progress / list-row / stat-card / chip come
// from vanilla-components; the stepper + use-case card grid are variant-specific.

import { el } from "./dom.js";
import { VC } from "../../_shared/vc.js";
import { USE_CASES, selectionFromPreset, fmtSize } from "./mock-data.js";
import { planFor, planTotals, secretsBlock, bar } from "./shared.js";

const STEPS = ["Detect", "Use", "Review", "Install", "Ready"];

function currentStep(ctx) {
  if (ctx.phase === "installing") return 3;
  if (ctx.phase === "done") return 4;
  return ctx.bStep;
}

function stepper(active) {
  return el("ol", { class: "b-steps" },
    STEPS.map((label, i) =>
      el("li", { class: "b-step" + (i === active ? " is-on" : "") + (i < active ? " is-done" : "") },
        el("span", { class: "b-step__num" }, i < active ? "✓" : String(i + 1)),
        el("span", { class: "b-step__label" }, label),
      ),
    ),
  );
}

function nav(...buttons) {
  return el("div", { class: "b-nav" }, ...buttons);
}

function detectStep(ctx) {
  const m = ctx.machine;
  return el("div", { class: "b-panel" },
    el("h2", { class: "b-h2" }, "Let's check your machine"),
    el("div", { class: "b-machine" },
      el("div", { class: "b-machine__icon" }, m.cuda ? "▮" : m.mlx ? "▣" : "▤"),
      el("div", {},
        el("div", { class: "b-machine__name" }, m.label),
        el("div", { class: "b-machine__detail" }, m.detail),
        el("div", { class: "b-machine__accel" }, "Acceleration: ", el("strong", {}, m.accel)),
      ),
    ),
    el("p", { class: "b-hint" }, "We'll use the fastest backend your hardware supports. (Switch machine from the bar below to compare.)"),
    nav(VC.button({ label: "Looks good →", variant: "primary", onClick: () => { ctx.bStep = 1; ctx.rerender(); } }).el),
  );
}

function useStep(ctx) {
  const grid = el("div", { class: "b-cases" });
  for (const uc of USE_CASES) {
    const on = ctx.bUseCase === uc.id;
    grid.append(
      el("button", {
        class: "b-case" + (on ? " is-on" : ""),
        type: "button",
        onClick: () => { ctx.bUseCase = uc.id; ctx.presetId = uc.preset; ctx.setSelection(selectionFromPreset(uc.preset)); },
      },
        el("span", { class: "b-case__icon" }, uc.icon),
        el("span", { class: "b-case__title" }, uc.title, uc.recommended ? VC.chip({ text: "popular", tone: "ok" }).el : null),
        el("span", { class: "b-case__sub" }, uc.sub),
      ),
    );
  }
  return el("div", { class: "b-panel" },
    el("h2", { class: "b-h2" }, "What will you use TapScribe for?"),
    el("p", { class: "b-hint" }, "We'll translate your answer into models and backends — you don't have to."),
    grid,
    nav(
      VC.button({ label: "← Back", onClick: () => { ctx.bStep = 0; ctx.rerender(); } }).el,
      VC.button({ label: "Continue →", variant: "primary", disabled: !ctx.bUseCase, onClick: () => { ctx.bStep = 2; ctx.rerender(); } }).el,
    ),
  );
}

function reviewStep(ctx) {
  const plan = planFor(ctx);
  const totals = planTotals(plan);
  const rows = el("div", { class: "b-rev" });
  for (const s of plan.steps.filter((x) => x.kind === "model" || x.kind === "extra")) {
    rows.append(VC.listRow({ title: s.label, meta: s.note || null, trailing: fmtSize(s.sizeMB) }).el);
  }

  return el("div", { class: "b-panel" },
    el("h2", { class: "b-h2" }, "Here's the plan"),
    el("p", { class: "b-hint" }, `For "${USE_CASES.find((u) => u.id === ctx.bUseCase)?.title || "your"}" use on your ${ctx.machine.label}.`),
    rows,
    el("div", { class: "b-totals" },
      VC.statCard({ label: "Download", value: totals.size }).el,
      VC.statCard({ label: "Time", value: totals.time }).el,
      VC.statCard({ label: "Disk after", value: totals.diskAfter }).el,
    ),
    nav(
      VC.button({ label: "← Back", onClick: () => { ctx.bStep = 1; ctx.rerender(); } }).el,
      VC.button({ label: "Install now", variant: "primary", onClick: () => ctx.startInstall() }).el,
    ),
  );
}

function installStep(ctx) {
  const plan = ctx.activePlan;
  const rows = plan.steps.map((s) => {
    const tone = s.state === "done" ? "ok" : s.state === "active" ? "accent" : null;
    const icon = s.state === "done" ? "✓" : s.state === "active" ? "▸" : "·";
    return el("div", { class: "b-irow b-irow--" + s.state },
      el("span", { class: "b-irow__icon" }, icon),
      el("span", { class: "b-irow__label" }, s.label),
      el("span", { class: "b-irow__bar" }, bar(s.pct, tone)),
    );
  });
  return el("div", { class: "b-panel" },
    el("h2", { class: "b-h2" }, "Installing"),
    el("div", { class: "b-overall" }, bar(ctx.progress.overall, "accent"), el("span", { class: "b-overall__pct" }, `${ctx.progress.overall}%`)),
    el("div", { class: "b-irows" }, rows),
    el("p", { class: "b-hint" }, "One-time download. Grab a coffee — we'll land on your dashboard when it's done."),
  );
}

function readyStep(ctx) {
  return el("div", { class: "b-panel b-panel--ready" },
    el("div", { class: "b-ready__badge" }, VC.chip({ text: "✓ All set", tone: "ok" }).el),
    el("h2", { class: "b-h2" }, "TapScribe is live"),
    secretsBlock(ctx.progress.secrets),
    nav(VC.button({ label: "Open dashboard →", variant: "primary", href: ctx.progress.secrets.url, target: "_blank" }).el),
  );
}

export default function render(root, ctx) {
  root.className = "stage setup setup--b";
  const step = currentStep(ctx);
  const body =
    step === 0 ? detectStep(ctx)
    : step === 1 ? useStep(ctx)
    : step === 2 ? reviewStep(ctx)
    : step === 3 ? installStep(ctx)
    : readyStep(ctx);

  root.append(
    el("div", { class: "b-shell" },
      el("header", { class: "b-top" },
        el("div", { class: "b-brand" }, el("span", { class: "b-brand__dot" }), "TapScribe setup"),
        stepper(step),
      ),
      el("main", { class: "b-main" }, body),
    ),
  );
}
