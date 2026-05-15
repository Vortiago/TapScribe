// Live channel panel — model/lang form + start/stop/apply controls + recent
// log. Body rebuild is skipped while the user is editing the form or the
// payload hasn't actually changed, so open <details>/<select> stay open.

import { tpl, mount, pick } from "../templates.js";

const LIVE_MODELS = [
  "tiny.en", "base.en", "small.en", "medium.en",
  "large-v3", "large-v3-turbo",
  "nb-whisper-tiny", "nb-whisper-base", "nb-whisper-small",
  "nb-whisper-medium", "nb-whisper-large",
];

let lastSig = "";
let logOpen = false;

export function render(j, { stateEl, mlxEl, bodyEl, mlxAvail, onAction }) {
  const li = j.live_info || {};
  const log = j.live_log || [];
  const state = li.state || "stopped";

  stateEl.textContent = state;
  mlxEl.textContent = mlxAvail ? "mlx available" : "cpu only";

  // Don't rebuild while the user is editing — would close their <select>.
  const focused = document.activeElement;
  if (focused && (focused.id === "liveModelSelect" || focused.id === "liveLangInput")) return;

  const sig = [
    state, li.model || "", li.language || "", li.pid || "", li.host || "",
    li.port || "", li.backend || "", li.device || "", li.last_error || "",
    log.length, log.length ? log[log.length - 1] : "",
  ].join("§");
  if (sig === lastSig) return;
  lastSig = sig;

  const frag = tpl("tpl-live-channel");
  const sel = frag.querySelector("#liveModelSelect");
  const langInput = frag.querySelector("#liveLangInput");
  const currentModel = li.model || "tiny.en";
  const models = LIVE_MODELS.includes(currentModel) ? LIVE_MODELS : [...LIVE_MODELS, currentModel];
  for (const m of models) {
    const opt = new Option(m, m, false, m === currentModel);
    sel.add(opt);
  }
  langInput.value = li.language || "en";

  const starting = state === "starting";
  const running = starting || state === "running";
  // While starting, lock the form so the user can't queue another change.
  if (starting) { sel.disabled = true; langInput.disabled = true; }

  const actionsHost = pick(frag, "actions");
  const actionsTpl = starting ? "tpl-live-actions-starting"
                   : running  ? "tpl-live-actions-running"
                              : "tpl-live-actions-stopped";
  actionsHost.appendChild(tpl(actionsTpl));

  pick(frag, "port").textContent = li.port || "?";
  pick(frag, "backend").textContent = li.backend || "?";
  pick(frag, "device").textContent = li.device || "?";
  if (li.pid) {
    pick(frag, "pidRow").hidden = false;
    pick(frag, "pid").textContent = li.pid;
  }
  if (li.last_error) {
    const err = pick(frag, "lastError");
    err.hidden = false;
    err.textContent = li.last_error;
  }
  if (log.length) {
    const block = pick(frag, "logBlock");
    block.hidden = false;
    block.open = logOpen;
    block.addEventListener("toggle", () => { logOpen = block.open; });
    pick(frag, "logSummary").textContent = `recent log (${log.length} line${log.length === 1 ? "" : "s"})`;
    pick(frag, "log").textContent = log.join("\n");
  }

  mount(bodyEl, frag);

  // Wire actions after mount so #ids resolve against the live DOM.
  bodyEl.querySelector("#liveStartBtn")?.addEventListener("click", onAction.start);
  bodyEl.querySelector("#liveApplyBtn")?.addEventListener("click", onAction.start);
  bodyEl.querySelector("#liveStopBtn")?.addEventListener("click", onAction.stop);
  // Nudge language to "no" when an nb-whisper model is picked and lang is
  // still on the boot default.
  bodyEl.querySelector("#liveModelSelect").addEventListener("change", (e) => {
    if (!e.target.value.startsWith("nb-")) return;
    const li = bodyEl.querySelector("#liveLangInput");
    if (li && (li.value === "en" || li.value === "")) li.value = "no";
  });
}

export const formValues = () => ({
  model: document.getElementById("liveModelSelect")?.value ?? null,
  language: document.getElementById("liveLangInput")?.value.trim() ?? null,
});
