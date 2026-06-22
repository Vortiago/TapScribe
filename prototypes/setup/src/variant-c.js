// Variant C — "Provision": dense operator console, two-pane (matrix + plan rail).
// Full per-family/backend control, instrumented with estimates + live progress.
// The matrix/host-strip/step-rows are shared with variant D via console-parts.js;
// C owns the two-pane shell + the right-hand plan rail. Mode-aware like D.

import { el } from "./dom.js";
import { VC } from "../../_shared/vc.js";
import { fmtSize } from "./mock-data.js";
import { machineStrip, matrix, runRows } from "./console-parts.js";
import { planFor, planTotals, pendingCount, secretsBlock, bar } from "./shared.js";

function planRail(ctx) {
  const manage = ctx.mode === "manage";
  const plan = planFor(ctx);
  const t = planTotals(plan);
  const n = pendingCount(ctx);

  const kv = el("div", { class: "c-kv" });
  if (manage) {
    kv.append(
      VC.kvRow({ label: "new download", value: t.size }).el,
      VC.kvRow({ label: "est. time", value: t.time }).el,
      VC.kvRow({ label: "new models", value: String(n) }).el,
    );
  } else {
    kv.append(
      VC.kvRow({ label: "download", value: t.size }).el,
      VC.kvRow({ label: "est. time", value: t.time }).el,
      VC.kvRow({ label: "disk after", value: t.diskAfter }).el,
      VC.kvRow({ label: "steps", value: String(plan.steps.length) }).el,
    );
  }

  const cta = manage
    ? VC.button({ label: n ? `Install ${n} model${n > 1 ? "s" : ""}` : "Select models to add", variant: "primary", disabled: n === 0, onClick: () => ctx.startInstall() })
    : VC.button({ label: "Provision →", variant: "primary", onClick: () => ctx.startInstall() });

  return el("aside", { class: "c-rail" },
    el("div", { class: "c-rail__h" }, manage ? "Add models" : "Install plan"),
    kv,
    el("ol", { class: "c-rail__steps" },
      plan.steps.filter((s) => s.kind === "model" || s.kind === "extra")
        .map((s) => el("li", {}, el("span", {}, s.short), el("span", { class: "c-rail__sz" }, fmtSize(s.sizeMB)))),
    ),
    el("div", { class: "c-cta" }, cta.el),
    el("div", { class: "c-rail__foot" }, "editable install. re-run anytime; unchanged selections skip pip."),
  );
}

function chooseView(ctx) {
  return el("div", { class: "c-body" },
    el("div", { class: "c-main" }, matrix(ctx)),
    planRail(ctx),
  );
}

function installView(ctx) {
  const log = el("aside", { class: "c-rail c-rail--log" },
    el("div", { class: "c-rail__h" }, `${ctx.mode === "manage" ? "installing" : "provisioning"} · ${ctx.progress.overall}%`),
    bar(ctx.progress.overall, "accent"),
    el("pre", { class: "c-term" }, ctx.progress.log.slice(-200).join("\n") + "\n█"),
  );
  return el("div", { class: "c-body" }, el("div", { class: "c-main" }, runRows(ctx.activePlan)), log);
}

function doneView(ctx) {
  const manage = ctx.mode === "manage";
  const rail = manage
    ? el("aside", { class: "c-rail c-rail--done" },
        el("div", { class: "c-rail__h" }, VC.dot({ tone: "ok", pulse: true, label: "models loaded" }).el),
        el("p", { class: "c-rail__foot" }, "New models are loaded into the running engine — pick them on your next job."),
        el("div", { class: "c-cta" }, VC.button({ label: "Back to dashboard →", variant: "primary", href: ctx.progress.secrets.url, target: "_blank" }).el),
      )
    : el("aside", { class: "c-rail c-rail--done" },
        el("div", { class: "c-rail__h" }, VC.dot({ tone: "ok", pulse: true, label: "recorder running" }).el),
        secretsBlock(ctx.progress.secrets),
        el("div", { class: "c-cta" }, VC.button({ label: "Open dashboard →", variant: "primary", href: ctx.progress.secrets.url, target: "_blank" }).el),
      );
  return el("div", { class: "c-body" }, el("div", { class: "c-main" }, runRows(ctx.activePlan, true)), rail);
}

export default function render(root, ctx) {
  root.className = "stage setup setup--c";
  const body = ctx.phase === "installing" ? installView(ctx) : ctx.phase === "done" ? doneView(ctx) : chooseView(ctx);
  root.append(
    el("div", { class: "c-shell" },
      el("header", { class: "c-top" },
        el("div", { class: "c-brand" }, el("span", { class: "c-brand__dot" }), "TapScribe", el("span", { class: "c-brand__tag" }, ctx.mode === "manage" ? "models" : "provision")),
        machineStrip(ctx),
      ),
      body,
    ),
  );
}
