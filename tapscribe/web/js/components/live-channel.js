// @ts-check
// Live channel panel — model/lang form + start/stop/apply controls + recent
// log. Body rebuild is skipped while the user is editing the form or the
// payload hasn't actually changed, so open <details>/<select> stay open.

import { tpl, mount, pick } from "../templates.js";
import { wireConfigSave } from "../api.js";

// Display labels for model families — used as <optgroup> labels in the live
// model select. Mirrors session-detail.js's FAMILY_LABELS but trimmed to
// the families that have live-eligible models today.
/** @type {[string, string][]} */
const LIVE_FAMILY_LABELS = [
  ["whisper", "Whisper"],
  ["nb-whisper", "NB-Whisper (Norwegian)"],
  ["voxtral", "Voxtral (Mistral)"],
];

let lastSig = "";
/** @type {ReturnType<typeof setInterval> | null} */
let logDialogPoll = null;

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').LiveChannelCtx} ctx
 */
export function render(j, { stateEl, mlxEl, bodyEl, mlxAvail, onAction, liveCatalog }) {
  const li = j.live_info || {};
  const log = j.live_log || [];
  const state = li.state || "stopped";
  const supportsNativeVad = j.live_supports_native_vad !== false;

  stateEl.textContent = state;
  mlxEl.textContent = mlxAvail ? "mlx available" : "cpu only";

  // Don't rebuild while the user is editing — would close their <select>,
  // wipe a slider value they're typing, wipe their in-progress
  // init-prompt edit, or (if an init-prompt save is in flight) detach
  // the status element the awaiting putJson will try to write to.
  // The dataset.cfgKey check covers the init-prompt textarea AND its
  // save button + status span so a click-then-poll-tick race can't
  // tear the DOM out from under the save handler.
  const focused = /** @type {HTMLElement | null} */ (document.activeElement);
  const editableIds = new Set([
    "liveModelSelect", "liveLangInput",
    "liveGateKindSelect",
    "liveGateThreshold", "liveGateHangover", "liveGatePreRoll",
  ]);
  if (focused) {
    if (editableIds.has(focused.id)) return;
    if (focused.dataset && focused.dataset.cfgKey && bodyEl.contains(focused)) return;
  }

  const lp = j.live_prompt || {};
  const sup = j.inputs_support || { live_prompt: true };

  const sig = [
    state, li.model || "", li.language || "", li.pid || "", li.host || "",
    li.port || "", li.backend || "", li.device || "", li.last_error || "",
    li.gate_kind || "", li.gate_speech_threshold || "",
    li.gate_hangover_ms || "", li.gate_pre_roll_ms || "",
    supportsNativeVad ? "1" : "0",
    log.length, log.length ? log[log.length - 1] : "",
    (liveCatalog?.models || []).length,
    sup.live_prompt ? 1 : 0, lp.length || 0, lp.content || "",
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

  // Speech-gate form: kind selector + three knob inputs. The "backend"
  // option is greyed out (disabled) when the current LiveChannel has
  // no native VAD — picking it would be a no-op since there's nothing
  // backend-side to defer gating to.
  const gateSel = frag.querySelector("#liveGateKindSelect");
  if (gateSel) {
    gateSel.value = li.gate_kind || "tapscribe";
    const backendOpt = gateSel.querySelector('option[value="backend"]');
    if (backendOpt) {
      backendOpt.disabled = !supportsNativeVad;
      backendOpt.textContent = supportsNativeVad
        ? "Backend native VAD"
        : "Backend native VAD (not supported)";
    }
  }
  const threshEl = frag.querySelector("#liveGateThreshold");
  if (threshEl) threshEl.value = li.gate_speech_threshold || "0.50";
  const hangEl = frag.querySelector("#liveGateHangover");
  if (hangEl) hangEl.value = li.gate_hangover_ms || "400";
  const prerollEl = frag.querySelector("#liveGatePreRoll");
  if (prerollEl) prerollEl.value = li.gate_pre_roll_ms || "300";

  const starting = state === "starting";
  const running = starting || state === "running";
  // While starting, lock the form so the user can't queue another change.
  if (starting) {
    sel.disabled = true;
    langInput.disabled = true;
    if (gateSel) gateSel.disabled = true;
    if (threshEl) threshEl.disabled = true;
    if (hangEl) hangEl.disabled = true;
    if (prerollEl) prerollEl.disabled = true;
  }

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

  // Init-prompt expandable. Hidden when no installed live model supports
  // initial_prompt (registry-driven via inputs_support.live_prompt).
  const initRow = pick(frag, "initPromptRow");
  if (sup.live_prompt) {
    initRow.hidden = false;
    pick(frag, "initPromptCount").textContent = lp.length ? `· ${lp.length} chars` : "";
    frag.querySelector("#liveInitPromptText").value = lp.content || "";
    // Default-open the editor when populated so the operator sees what's
    // in effect; collapsed when empty to keep the panel compact.
    initRow.open = !!lp.length;
  }

  mount(bodyEl, frag);

  // Wire actions after mount so #ids resolve against the live DOM.
  bodyEl.querySelector("#liveStartBtn")?.addEventListener("click", onAction.start);
  bodyEl.querySelector("#liveApplyBtn")?.addEventListener("click", onAction.start);
  bodyEl.querySelector("#liveStopBtn")?.addEventListener("click", onAction.stop);
  bodyEl.querySelector("#liveLogBtn")?.addEventListener("click", openLogDialog);
  // Nudge language to "no" when an nb-whisper model is picked and lang is
  // still on the boot default.
  bodyEl.querySelector("#liveModelSelect")?.addEventListener("change", (e) => {
    const value = /** @type {HTMLSelectElement} */ (e.target).value;
    if (!value.startsWith("nb-")) return;
    const li = /** @type {HTMLInputElement | null} */ (bodyEl.querySelector("#liveLangInput"));
    if (li && (li.value === "en" || li.value === "")) li.value = "no";
  });

  const initBtn = /** @type {HTMLButtonElement | null} */ (bodyEl.querySelector("#liveInitPromptSave"));
  if (initBtn) {
    wireConfigSave({
      key: "live-prompt",
      btn: initBtn,
      textarea: bodyEl.querySelector("#liveInitPromptText"),
      status: bodyEl.querySelector('[data-slot="initPromptStatus"]'),
      onSuccess: undefined,
    });
  }
}

export const formValues = () => {
  // Read the gate knobs only when they have a value (so "Apply" with
  // untouched sliders doesn't force a restart over identical numbers).
  // The server-side `matches()` check uses the same null-means-unchanged
  // semantics for these fields.
  /** @param {string} id */
  const numOrNull = (id) => {
    const el = /** @type {HTMLInputElement | null} */ (document.getElementById(id));
    if (!el || el.value === "") return null;
    const n = Number(el.value);
    return Number.isFinite(n) ? n : null;
  };
  return {
    model: /** @type {HTMLSelectElement | null} */ (document.getElementById("liveModelSelect"))?.value ?? null,
    language: /** @type {HTMLInputElement | null} */ (document.getElementById("liveLangInput"))?.value.trim() ?? null,
    gate_kind: /** @type {HTMLSelectElement | null} */ (document.getElementById("liveGateKindSelect"))?.value ?? null,
    gate_speech_threshold: numOrNull("liveGateThreshold"),
    gate_hangover_ms: numOrNull("liveGateHangover"),
    gate_pre_roll_ms: numOrNull("liveGatePreRoll"),
  };
};


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

/**
 * @param {HTMLDialogElement} dlg
 * @param {{ log?: string[], state?: string } | null} payload
 */
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
  let dlg = /** @type {HTMLDialogElement | null} */ (document.getElementById("liveLogDialog"));
  if (!dlg) {
    const frag = tpl("tpl-live-log-dialog");
    document.body.appendChild(frag);
    dlg = /** @type {HTMLDialogElement} */ (document.getElementById("liveLogDialog"));
    const closeBtn = dlg.querySelector("#liveLogCloseBtn");
    closeBtn?.addEventListener("click", () => dlg?.close());
    dlg.querySelector("#liveLogRefreshBtn")?.addEventListener("click", async () => {
      if (dlg) renderLogInto(dlg, await fetchLog());
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
