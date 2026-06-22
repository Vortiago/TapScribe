// Variant A — "One Tap": calm consumer card. One recommended plan, one button,
// advanced as a disclosure. Atoms (button, chip, progress, list-row) come from
// vanilla-components; the card layout is variant-specific.

import { el } from "./dom.js";
import { VC } from "../../_shared/vc.js";
import { PRESETS, selectionFromPreset } from "./mock-data.js";
import { planFor, planContents, planTotals, secretsBlock, bar } from "./shared.js";

function machineLine(ctx) {
  const m = ctx.machine;
  return el("div", { class: "a-machine" },
    el("span", { class: "a-machine__eye" }, "◉"),
    el("div", {},
      el("div", { class: "a-machine__label" }, `Detected: ${m.label}`),
      el("div", { class: "a-machine__detail" }, `${m.detail} → ${m.accel}`),
    ),
  );
}

/** full-width CTA: the button atom, wrapped so setup.css can stretch it. */
function cta(props) {
  return el("div", { class: "a-cta" }, VC.button(props).el);
}

function chooseView(ctx) {
  const plan = planFor(ctx);
  const totals = planTotals(plan);
  const contents = planContents(ctx);

  const advanced = el("div", { class: "a-advanced", hidden: !ctx.aAdvancedOpen });
  for (const p of PRESETS) {
    const on = ctx.presetId === p.id;
    advanced.append(
      VC.listRow({
        title: p.label,
        meta: p.tagline,
        leading: on ? "●" : "○",
        trailing: on ? VC.chip({ text: "selected", tone: "accent" }).el : null,
        onSelect: () => { ctx.presetId = p.id; ctx.setSelection(selectionFromPreset(p.id)); },
      }).el,
    );
  }

  return el("div", { class: "a-wrap" },
    el("div", { class: "a-card" },
      el("div", { class: "a-brand" }, el("span", { class: "a-brand__dot" }), "TapScribe"),
      el("h1", { class: "a-h1" }, "Ready in one tap"),
      el("p", { class: "a-sub" }, "Local audio transcription. We picked sensible defaults for your machine — change them only if you want to."),

      machineLine(ctx),

      el("div", { class: "a-plan" },
        el("div", { class: "a-plan__head" },
          el("span", { class: "a-plan__title" }, "Recommended setup"),
          el("span", { class: "a-plan__size" }, `${totals.size} · ${totals.time}`),
        ),
        el("div", { class: "a-chips" }, contents.map((c) => VC.chip({ text: c }).el)),
        el("p", { class: "a-plan__note" }, "Transcribe meetings live, split silence, and summarize — all offline."),
      ),

      cta({ label: "Install & launch TapScribe", variant: "primary", onClick: () => ctx.startInstall() }),

      el("div", { class: "a-customize" },
        VC.button({
          label: ctx.aAdvancedOpen ? "Hide options ▲" : "Customize ▾",
          variant: "ghost",
          size: "sm",
          onClick: () => { ctx.aAdvancedOpen = !ctx.aAdvancedOpen; ctx.rerender(); },
        }).el,
      ),

      advanced,

      el("p", { class: "a-foot" }, "Takes ~a few minutes the first time. Nothing leaves this machine."),
    ),
  );
}

function installingView(ctx) {
  const plan = ctx.activePlan;
  const overall = ctx.progress.overall;
  const current = plan.steps.find((s) => s.state === "active") || plan.steps[plan.steps.length - 1];

  return el("div", { class: "a-wrap" },
    el("div", { class: "a-card" },
      machineLine(ctx),
      el("h1", { class: "a-h1" }, "Setting things up…"),
      el("p", { class: "a-sub" }, "This is a one-time download. You can leave it running."),

      el("div", { class: "a-prog" },
        bar(overall, "accent"),
        el("div", { class: "a-prog__row" },
          el("span", { class: "a-prog__pct" }, `${overall}%`),
          el("span", { class: "a-prog__step" }, current ? current.label : "Finishing up"),
        ),
      ),

      el("details", {
        class: "a-log", open: ctx.aLogOpen,
        onToggle: (e) => { ctx.aLogOpen = e.target.open; },
      },
        el("summary", {}, "Show details"),
        el("pre", { class: "a-log__pre" }, ctx.progress.log.slice(-80).join("\n")),
      ),
    ),
  );
}

function doneView(ctx) {
  return el("div", { class: "a-wrap" },
    el("div", { class: "a-card a-card--done" },
      el("div", { class: "a-check" }, "✓"),
      el("h1", { class: "a-h1" }, "TapScribe is running"),
      el("p", { class: "a-sub" }, "Open the dashboard and paste the token into your bridge."),

      secretsBlock(ctx.progress.secrets),

      cta({ label: "Open dashboard →", variant: "primary", href: ctx.progress.secrets.url, target: "_blank" }),
      el("p", { class: "a-foot" }, "Stop anytime with Ctrl+C in the terminal that launched it."),
    ),
  );
}

export default function render(root, ctx) {
  root.className = "stage setup setup--a";
  if (ctx.phase === "installing") root.append(installingView(ctx));
  else if (ctx.phase === "done") root.append(doneView(ctx));
  else root.append(chooseView(ctx));
}
