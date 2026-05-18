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
let logDialogPoll = null;

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
  // The /api/state payload only carries a tail preview (up to 30 lines);
  // the dialog fetches the full 200-line deque from /api/live/log on
  // demand. The button shows the preview count as a hint that there's
  // something to look at.
  if (log.length) {
    pick(frag, "logRow").hidden = false;
    pick(frag, "logCount").textContent = `(${log.length}+)`;
  }

  mount(bodyEl, frag);

  // Wire actions after mount so #ids resolve against the live DOM.
  bodyEl.querySelector("#liveStartBtn")?.addEventListener("click", onAction.start);
  bodyEl.querySelector("#liveApplyBtn")?.addEventListener("click", onAction.start);
  bodyEl.querySelector("#liveStopBtn")?.addEventListener("click", onAction.stop);
  bodyEl.querySelector("#liveLogBtn")?.addEventListener("click", openLogDialog);
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


// ── Log dialog ──────────────────────────────────────────────────────
//
// The live-channel panel previously inlined a tail of the WlK log in a
// <details> block, which competed with everything else in the column.
// We've moved it behind a "view logs" button that opens this <dialog>,
// fetching the full deque on demand and polling once a second while the
// dialog is open.

async function fetchLog() {
  try {
    const r = await fetch("/api/live/log", { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

function renderLogInto(dlg, payload) {
  const pre = pick(dlg, "pre");
  const status = pick(dlg, "status");
  if (!payload) {
    pre.textContent = "(failed to load logs)";
    status.textContent = "";
    return;
  }
  const log = payload.log || [];
  pre.textContent = log.length ? log.join("\n") : "(no log entries yet)";
  status.textContent = `state: ${payload.state || "stopped"} · ${log.length} line${log.length === 1 ? "" : "s"}`;
  // Auto-scroll to bottom so new lines are visible on each refresh.
  pre.scrollTop = pre.scrollHeight;
}

async function openLogDialog() {
  // Reuse the existing dialog if it's already in the DOM — avoids
  // multiple ids and lets polling state be tied to one element.
  let dlg = document.getElementById("liveLogDialog");
  if (!dlg) {
    const frag = tpl("tpl-live-log-dialog");
    document.body.appendChild(frag);
    dlg = document.getElementById("liveLogDialog");
    dlg.querySelector("#liveLogCloseBtn").addEventListener("click", () => dlg.close());
    dlg.querySelector("#liveLogRefreshBtn").addEventListener("click", async () => {
      renderLogInto(dlg, await fetchLog());
    });
    dlg.addEventListener("close", () => {
      if (logDialogPoll !== null) {
        clearInterval(logDialogPoll);
        logDialogPoll = null;
      }
    });
  }
  renderLogInto(dlg, await fetchLog());
  if (!dlg.open) dlg.showModal();
  if (logDialogPoll === null) {
    logDialogPoll = setInterval(async () => {
      if (!dlg.open) return;
      renderLogInto(dlg, await fetchLog());
    }, 1000);
  }
}
