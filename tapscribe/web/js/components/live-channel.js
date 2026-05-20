// Live channel panel — model/lang form + start/stop/apply controls + recent
// log. Body rebuild is skipped while the user is editing the form or the
// payload hasn't actually changed, so open <details>/<select> stay open.

import { tpl, mount, pick } from "../templates.js";

// Display labels for model families — used as <optgroup> labels in the live
// model select. Mirrors session-detail.js's FAMILY_LABELS but trimmed to
// the families that have live-eligible models today.
const LIVE_FAMILY_LABELS = [
  ["whisper", "Whisper"],
  ["nb-whisper", "NB-Whisper (Norwegian)"],
  ["voxtral", "Voxtral (Mistral)"],
];

let lastSig = "";
let logDialogPoll = null;

export function render(j, { stateEl, mlxEl, bodyEl, mlxAvail, onAction, liveCatalog }) {
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
    // include catalog length so a server-restart-driven catalog refresh
    // forces a re-render of the model options.
    (liveCatalog?.models || []).length,
  ].join("§");
  if (sig === lastSig) return;
  lastSig = sig;

  const frag = tpl("tpl-live-channel");
  const sel = frag.querySelector("#liveModelSelect");
  const langInput = frag.querySelector("#liveLangInput");
  const currentModel = li.model || "tiny.en";

  // Group live-eligible models by family (Whisper / NB-Whisper / …). If
  // the currently-running model isn't in the catalog (operator pinned an
  // unrecognised name via --live-model), surface it as an "Other" entry
  // so the dropdown still reflects what's actually running.
  const models = liveCatalog?.models || [];
  const byFamily = new Map();
  for (const m of models) {
    if (!byFamily.has(m.family)) byFamily.set(m.family, []);
    byFamily.get(m.family).push(m);
  }
  let foundCurrent = false;
  for (const [fam, label] of LIVE_FAMILY_LABELS) {
    const entries = byFamily.get(fam);
    if (!entries?.length) continue;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const m of entries) {
      const opt = new Option(m.display_name, m.model_id, false, m.model_id === currentModel);
      group.appendChild(opt);
      if (m.model_id === currentModel) foundCurrent = true;
    }
    sel.appendChild(group);
    byFamily.delete(fam);
  }
  if (byFamily.size) {
    const group = document.createElement("optgroup");
    group.label = "Other";
    for (const [, entries] of byFamily) {
      for (const m of entries) {
        group.appendChild(new Option(m.display_name, m.model_id, false, m.model_id === currentModel));
        if (m.model_id === currentModel) foundCurrent = true;
      }
    }
    sel.appendChild(group);
  }
  if (!foundCurrent && currentModel) {
    // Operator-pinned model not in the catalog — keep it visible so they
    // see what's actually running, prefixed to make the gap obvious.
    sel.add(new Option(`${currentModel} (unregistered)`, currentModel, false, true));
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
