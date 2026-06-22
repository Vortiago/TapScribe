// Shared "operator console" parts — the dense, everything-surfaced matrix that
// both variant C (two-pane console) and variant D (centered console) render.
// Extracted so the two share one matrix instead of drifting copies.
//
// In manage mode (ctx.mode === "manage") rows for already-installed models are
// marked "installed", locked on, and excluded from the download — so a revisit
// reads as "add to what's there", not "set up from scratch".

import { el } from "./dom.js";
import { VC } from "../../_shared/vc.js";
import {
  FAMILIES, FAMILY_ORDER, VAD, SUMMARIZE, INSTALLED,
  resolveBackend, backendLabel, familySizeMB, fmtSize,
} from "./mock-data.js";
import { bar } from "./shared.js";

export function backendAvailable(machine, fam, b) {
  if (b === "cpu") return true;
  if (b === "mlx") return machine.mlx && fam.mlxOk;
  if (b === "cuda") return machine.cuda;
  return false;
}

export function machineStrip(ctx) {
  const m = ctx.machine;
  return el("div", { class: "c-machine" },
    el("span", { class: "c-machine__k" }, "host"),
    el("code", {}, `${m.os.toLowerCase()}/${m.arch}`),
    VC.chip({ text: `MLX ${m.mlx ? "✓" : "✗"}`, tone: m.mlx ? "ok" : null, dot: true }).el,
    VC.chip({ text: `CUDA ${m.cuda ? "✓" : "✗"}`, tone: m.cuda ? "ok" : null, dot: true }).el,
    el("span", { class: "c-machine__detail" }, m.detail),
  );
}

function familyRow(ctx, key) {
  const fam = FAMILIES[key];
  const installed = ctx.mode === "manage" && INSTALLED.families.includes(key);
  const enabled = ctx.selection.families.includes(key);
  const override = ctx.selection.backends?.[key]; // undefined ⇒ use the auto pick
  const autoPick = resolveBackend(ctx.machine, fam, "auto"); // best for this host
  const resolved = override || autoPick;

  // backend cell: a static label for installed rows; the concrete-backend picker
  // (recommended pre-selected) when enabled; a dash when off.
  let bkCell;
  if (installed) {
    bkCell = el("span", { class: "c-row__bkoff" }, backendLabel(resolved));
  } else if (enabled) {
    const options = [];
    for (const b of ["mlx", "cuda", "cpu"]) {
      if (backendAvailable(ctx.machine, fam, b)) options.push({ id: b, label: backendLabel(b) });
    }
    bkCell = VC.seg({
      options,
      current: resolved,
      onSelect: (b) => {
        const next = { ...ctx.selection.backends };
        if (b === autoPick) delete next[key];
        else next[key] = b;
        ctx.selection = { ...ctx.selection, backends: next };
        if (b !== autoPick) ctx.presetId = "custom";
        ctx.rerender();
      },
    }).el;
  } else {
    bkCell = el("span", { class: "c-row__bkoff" }, "—");
  }

  const enable = installed
    ? VC.button({ label: "■", variant: "ghost", size: "sm", pressed: true, disabled: true }).el
    : VC.button({
        label: enabled ? "■" : "□", variant: "ghost", size: "sm", pressed: enabled,
        onClick: () => {
          const fams = new Set(ctx.selection.families);
          enabled ? fams.delete(key) : fams.add(key);
          ctx.selection = { ...ctx.selection, families: FAMILY_ORDER.filter((k) => fams.has(k)) };
          ctx.presetId = "custom";
          ctx.rerender();
        },
      }).el;

  const sizeText = installed ? "installed" : enabled ? fmtSize(familySizeMB(fam, resolved)) : "—";

  return el("tr", { class: "c-row" + (enabled || installed ? "" : " is-off") },
    el("td", { class: "c-row__chk" }, enable),
    el("td", { class: "c-row__name" },
      el("div", { class: "c-row__label" }, fam.label,
        VC.chip({ text: fam.live ? "live + batch" : "batch only", tone: fam.live ? "info" : null }).el,
        installed ? VC.chip({ text: "installed", tone: "ok" }).el : null),
      el("div", { class: "c-row__blurb" }, fam.langs),
    ),
    el("td", { class: "c-row__bk" }, bkCell),
    el("td", { class: "c-row__size" + (installed ? " is-installed" : "") }, sizeText),
  );
}

function extrasRows(ctx) {
  const manage = ctx.mode === "manage";
  const vadInstalled = manage; // vad is always part of an existing install

  const vad = el("tr", { class: "c-row c-row--extra" },
    el("td", { class: "c-row__chk" }, VC.button({ label: "■", variant: "ghost", size: "sm", pressed: true, disabled: true }).el),
    el("td", { class: "c-row__name" },
      el("div", { class: "c-row__label" }, VAD.label,
        vadInstalled ? VC.chip({ text: "installed", tone: "ok" }).el : VC.chip({ text: "required", tone: "accent" }).el),
      el("div", { class: "c-row__blurb" }, VAD.blurb),
    ),
    el("td", { class: "c-row__bk" }, el("span", { class: "c-row__bkoff" }, "torch")),
    el("td", { class: "c-row__size" + (vadInstalled ? " is-installed" : "") }, vadInstalled ? "installed" : fmtSize(VAD.sizeMB)),
  );

  const sumInstalled = manage && INSTALLED.summarize;
  const sumOn = ctx.selection.summarize;
  const summ = el("tr", { class: "c-row c-row--extra" + (sumOn || sumInstalled ? "" : " is-off") },
    el("td", { class: "c-row__chk" },
      sumInstalled
        ? VC.button({ label: "■", variant: "ghost", size: "sm", pressed: true, disabled: true }).el
        : VC.button({
            label: sumOn ? "■" : "□", variant: "ghost", size: "sm", pressed: sumOn,
            onClick: () => { ctx.selection = { ...ctx.selection, summarize: !sumOn }; ctx.presetId = "custom"; ctx.rerender(); },
          }).el,
    ),
    el("td", { class: "c-row__name" },
      el("div", { class: "c-row__label" }, SUMMARIZE.label, sumInstalled ? VC.chip({ text: "installed", tone: "ok" }).el : null),
      el("div", { class: "c-row__blurb" }, SUMMARIZE.blurb),
    ),
    el("td", { class: "c-row__bk" }, el("span", { class: "c-row__bkoff" }, ctx.machine.mlx ? "mlx-lm" : "llama-cpp")),
    el("td", { class: "c-row__size" + (sumInstalled ? " is-installed" : "") }, sumInstalled ? "installed" : sumOn ? fmtSize(SUMMARIZE.sizeMB) : "—"),
  );
  return [vad, summ];
}

export function matrix(ctx) {
  return el("table", { class: "c-matrix" },
    el("thead", {}, el("tr", {},
      el("th", {}, ""), el("th", {}, "family / extra"), el("th", {}, "backend"), el("th", { class: "c-th-r" }, "size"),
    )),
    el("tbody", {}, FAMILY_ORDER.map((k) => familyRow(ctx, k)), extrasRows(ctx)),
  );
}

export function runRows(plan, done = false) {
  return el("table", { class: "c-matrix c-matrix--run" },
    el("tbody", {}, plan.steps.map((s) => {
      const state = done ? "done" : s.state;
      const tone = state === "done" ? "ok" : state === "active" ? "accent" : null;
      return el("tr", { class: "c-prow c-prow--" + state },
        el("td", { class: "c-prow__icon" }, state === "done" ? "✓" : state === "active" ? "▸" : "·"),
        el("td", { class: "c-prow__label" }, s.label),
        el("td", { class: "c-prow__bar" }, bar(done ? 100 : s.pct, tone)),
        el("td", { class: "c-prow__pct" }, done ? "done" : state === "pending" ? "" : `${s.pct}%`),
      );
    })),
  );
}
