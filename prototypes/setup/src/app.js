// Controller for the setup prototypes: owns the shared ctx, mounts the active
// variant into #stage, and renders the floating switcher (variant arrows +
// detected-machine toggle + restart). Everything here is throwaway.

import { el, clear } from "./dom.js";
import { MACHINES, selectionFromPreset, installedSelection } from "./mock-data.js";
import { runInstall } from "./engine.js";
import { planFor } from "./shared.js";
import { warmAll } from "../../_shared/vc.js";
import renderA from "./variant-a.js";
import renderB from "./variant-b.js";
import renderC from "./variant-c.js";
import renderD from "./variant-d.js";

const VARIANTS = {
  A: { name: "One Tap", render: renderA },
  B: { name: "Setup Assistant", render: renderB },
  C: { name: "Provision", render: renderC },
  D: { name: "Centered console", render: renderD },
};
const ORDER = ["A", "B", "C", "D"];
const MACHINE_CHIPS = { mac: "Apple Silicon", nvidia: "NVIDIA GPU", cpu: "CPU only" };
const MODE_CHIPS = { firstrun: "First run", manage: "Manage models" };

const stageEl = document.getElementById("stage");
const swEl = document.getElementById("switcher");

const params = new URLSearchParams(location.search);
const startVariant = ORDER.includes((params.get("variant") || "").toUpperCase())
  ? params.get("variant").toUpperCase()
  : "D"; // default to the synthesis (C's substance + A's centered install feel)
const startMachine = MACHINES[params.get("machine")] ? params.get("machine") : "mac";

const ctx = {
  variant: startVariant,
  machine: MACHINES[startMachine],
  mode: "firstrun", // 'firstrun' (initial setup) | 'manage' (revisit to add models)
  phase: "choose", // 'choose' | 'installing' | 'done'
  presetId: "balanced",
  selection: selectionFromPreset("balanced"),
  // per-variant scratch
  aAdvancedOpen: false,
  aLogOpen: false,
  bStep: 0,
  bUseCase: null,
  // install run
  activePlan: null,
  cancel: null,
  progress: { overall: 0, log: [], secrets: null },

  setSelection(sel) {
    ctx.selection = sel;
    ctx.rerender();
  },

  startInstall() {
    if (ctx.cancel) ctx.cancel();
    const plan = planFor(ctx); // manage mode plans only the not-yet-installed delta
    ctx.activePlan = plan;
    ctx.phase = "installing";
    ctx.progress = { overall: 0, log: [], secrets: null };
    ctx.rerender();
    ctx.cancel = runInstall(plan, {
      onTick: (overall) => {
        ctx.progress.overall = overall;
        ctx.rerender();
      },
      onLog: (line) => ctx.progress.log.push(line),
      onDone: (secrets) => {
        ctx.progress.secrets = secrets;
        ctx.phase = "done";
        ctx.cancel = null;
        ctx.rerender();
      },
    });
  },

  reset() {
    if (ctx.cancel) {
      ctx.cancel();
      ctx.cancel = null;
    }
    ctx.phase = "choose";
    ctx.activePlan = null;
    ctx.progress = { overall: 0, log: [], secrets: null };
    ctx.bStep = 0;
    ctx.bUseCase = null;
    ctx.aLogOpen = false;
    ctx.rerender();
  },

  rerender() {
    clear(stageEl);
    VARIANTS[ctx.variant].render(stageEl, ctx);
    // keep streaming log panes pinned to the newest line under the 90ms repaint
    for (const pre of stageEl.querySelectorAll(".c-term, .a-log__pre")) {
      pre.scrollTop = pre.scrollHeight;
    }
  },
};

// avoid a stale preset highlight in variant A's advanced panel on first paint
ctx.presetId = "balanced";

function setVariant(v) {
  ctx.variant = v;
  const u = new URL(location.href);
  u.searchParams.set("variant", v);
  history.replaceState(null, "", u);
  ctx.reset();
  renderSwitcher();
}

function setMachine(id) {
  ctx.machine = MACHINES[id];
  const u = new URL(location.href);
  u.searchParams.set("machine", id);
  history.replaceState(null, "", u);
  ctx.reset();
  renderSwitcher();
}

// First run = fresh setup (recommended defaults). Manage = revisit an existing
// install to add models, so the selection starts from what's already installed.
function setMode(m) {
  ctx.mode = m;
  ctx.selection = m === "manage" ? installedSelection() : selectionFromPreset("balanced");
  ctx.presetId = m === "manage" ? "custom" : "balanced";
  ctx.reset();
  renderSwitcher();
}

function cycle(dir) {
  const i = ORDER.indexOf(ctx.variant);
  setVariant(ORDER[(i + dir + ORDER.length) % ORDER.length]);
}

function renderSwitcher() {
  clear(swEl);
  swEl.append(
    el("div", { class: "sw__group" },
      el("button", { class: "sw__arrow", type: "button", title: "previous variant (←)", onClick: () => cycle(-1) }, "◀"),
      el("span", { class: "sw__label" },
        el("b", {}, ctx.variant), " — ", VARIANTS[ctx.variant].name,
      ),
      el("button", { class: "sw__arrow", type: "button", title: "next variant (→)", onClick: () => cycle(1) }, "▶"),
    ),
    el("span", { class: "sw__sep" }),
    el("div", { class: "sw__group sw__machines" },
      el("span", { class: "sw__k" }, "machine"),
      ...Object.keys(MACHINE_CHIPS).map((id) =>
        el("button", {
          class: "sw__mchip" + (ctx.machine.id === id ? " is-on" : ""),
          type: "button",
          onClick: () => setMachine(id),
        }, MACHINE_CHIPS[id]),
      ),
    ),
    el("span", { class: "sw__sep" }),
    el("div", { class: "sw__group sw__modes" },
      el("span", { class: "sw__k" }, "context"),
      ...Object.keys(MODE_CHIPS).map((m) =>
        el("button", {
          class: "sw__mchip" + (ctx.mode === m ? " is-on" : ""),
          type: "button",
          onClick: () => setMode(m),
        }, MODE_CHIPS[m]),
      ),
    ),
    el("span", { class: "sw__sep" }),
    el("button", { class: "sw__restart", type: "button", title: "restart this flow", onClick: () => ctx.reset() }, "↻ restart"),
    el("span", { class: "sw__proto" }, "prototype"),
  );
}

document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  if (e.key === "ArrowLeft") { e.preventDefault(); cycle(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); cycle(1); }
});

// Warm the vanilla-components used by the variants once, so the per-tick
// create*Sync builds inside the render loop are synchronous.
await warmAll();
renderSwitcher();
ctx.rerender();
