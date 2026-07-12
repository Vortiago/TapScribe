// @ts-check
// gate-allow: signal-listener — handlers ride nodes this component builds; replaced subtrees take their listeners with them, and the few persistent targets are wired exactly once per page.
// Live channel panel — model/lang form + start/stop/apply controls + recent
// log. Body rebuild is skipped while the user is editing the form or the
// payload hasn't actually changed, so open <details>/<select> stay open.

import { tpl, pick, renderRegion, selectionInside } from "../templates.js";
import { wireConfigSave } from "../api.js";
import { LIVE_FAMILY_LABELS, buildModelSelect } from "../model-select.js";

/** @type {ReturnType<typeof setInterval> | null} */
let logDialogPoll = null;

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').LiveChannelCtx} ctx
 */
export function render(j, ctx) {
  const { stateEl, mlxEl, bodyEl, liveCatalog } = ctx;
  const li = j.live_info || {};
  const log = j.live_log || [];
  const state = li.state || "stopped";
  const supportsNativeVad = j.live_supports_native_vad !== false;
  const lp = j.live_prompt || {};
  const sup = j.inputs_support || { live_prompt: true };

  stateEl.textContent = state;
  // Acceleration note (the element keeps its historical mlxEl name): derived
  // from the server's available_backends probe, NOT just MLX-or-nothing —
  // a CUDA box used to read "cpu only" here while the live child was happily
  // on the GPU.
  const accel = j.available_backends || [];
  mlxEl.textContent = accel.includes("mlx")
    ? "mlx available"
    : accel.includes("cuda")
      ? "cuda available"
      : "cpu only";

  // The body swap goes through renderRegion (focus-guarded + per-host sig):
  // it skips while any <select>/<input>/<textarea> inside the body is focused,
  // so an open dropdown or mid-edit gate knob survives the poll tick. One case
  // it deliberately can't see: BUTTONS. While an init-prompt save is in
  // flight, focus sits on its [data-cfg-key] save button — swapping then would
  // detach the status span the awaiting putJson writes to. Hold the body for
  // that one case here.
  const focused = /** @type {HTMLElement | null} */ (document.activeElement);
  if (focused && focused.dataset && focused.dataset.cfgKey && bodyEl.contains(focused)) return;

  // NOTE: the log tail is deliberately NOT in this sig. Folding it in
  // rebuilt the whole body on every WlK log line — snapping shut an
  // operator-opened init-prompt <details> and churning the form for a
  // value that only feeds the small "(N+)" log-count hint. Per the
  // render-signature hygiene rule, that hint updates IN PLACE below.
  const sig = [
    state, li.model || "", li.language || "", li.pid || "", li.host || "",
    li.port || "", li.backend || "", li.device || "", li.last_error || "",
    li.gate_kind || "", li.gate_speech_threshold || "",
    li.gate_hangover_ms || "", li.gate_pre_roll_ms || "",
    li.gate_min_speech_ms || "",
    supportsNativeVad ? "1" : "0",
    (liveCatalog?.models || []).length,
    sup.live_prompt ? 1 : 0, lp.length || 0, lp.content || "",
  ].join("§");
  renderRegion(bodyEl, () => buildBody(j, ctx), { sig });

  // Log-count hint — in-place per render (the template ships the row hidden
  // with the button pre-wired, so unhiding later needs no rebuild).
  const logRow = /** @type {HTMLElement | null} */ (bodyEl.querySelector('[data-slot="logRow"]'));
  if (logRow) {
    logRow.hidden = log.length === 0;
    const cnt = logRow.querySelector('[data-slot="logCount"]');
    if (cnt) cnt.textContent = log.length ? `(${log.length}+)` : "";
  }
}

/**
 * Build the live-channel body for the current state — model picker, gate
 * form, actions, info rows, init-prompt editor — fully wired. Listeners are
 * attached to the fragment's nodes at build time; the nodes survive the
 * renderRegion swap into bodyEl, so no post-mount wiring pass is needed.
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').LiveChannelCtx} ctx
 * @returns {DocumentFragment}
 */
function buildBody(j, { onAction, liveCatalog }) {
  const li = j.live_info || {};
  const state = li.state || "stopped";
  const supportsNativeVad = j.live_supports_native_vad !== false;
  const lp = j.live_prompt || {};
  const sup = j.inputs_support || { live_prompt: true };

  const frag = tpl("tpl-live-channel");
  const sel = /** @type {HTMLSelectElement} */ (frag.querySelector("#liveModelSelect"));
  const langInput = /** @type {HTMLInputElement} */ (frag.querySelector("#liveLangInput"));
  const currentModel = li.model || "tiny.en";

  // Group live-eligible models by family (Whisper / NB-Whisper / …), shared
  // with the Stages engine/settings pickers — see #225. If the currently-
  // running model isn't in the catalog (operator pinned an unrecognised name
  // via --live-model), buildModelSelect's unregisteredFallback keeps it
  // visible so the dropdown still reflects what's actually running.
  const models = liveCatalog?.models || [];
  buildModelSelect(sel, models, {
    selected: currentModel,
    familyLabels: LIVE_FAMILY_LABELS,
    unregisteredFallback: true,
  });
  langInput.value = li.language || "en";

  // Speech-gate form: kind selector + three knob inputs. The "backend"
  // option is greyed out (disabled) when the current LiveChannel has
  // no native VAD — picking it would be a no-op since there's nothing
  // backend-side to defer gating to.
  const gateSel = /** @type {HTMLSelectElement | null} */ (frag.querySelector("#liveGateKindSelect"));
  if (gateSel) {
    gateSel.value = li.gate_kind || "tapscribe";
    const backendOpt = /** @type {HTMLOptionElement | null} */ (gateSel.querySelector('option[value="backend"]'));
    if (backendOpt) {
      backendOpt.disabled = !supportsNativeVad;
      backendOpt.textContent = supportsNativeVad
        ? "Backend native VAD"
        : "Backend native VAD (not supported)";
    }
  }
  const threshEl = /** @type {HTMLInputElement | null} */ (frag.querySelector("#liveGateThreshold"));
  if (threshEl) threshEl.value = li.gate_speech_threshold || "0.50";
  const hangEl = /** @type {HTMLInputElement | null} */ (frag.querySelector("#liveGateHangover"));
  if (hangEl) hangEl.value = li.gate_hangover_ms || "400";
  const prerollEl = /** @type {HTMLInputElement | null} */ (frag.querySelector("#liveGatePreRoll"));
  if (prerollEl) prerollEl.value = li.gate_pre_roll_ms || "300";
  const minSpeechEl = /** @type {HTMLInputElement | null} */ (frag.querySelector("#liveGateMinSpeech"));
  if (minSpeechEl) minSpeechEl.value = li.gate_min_speech_ms || "0";

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
    if (minSpeechEl) minSpeechEl.disabled = true;
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
  // The log-count row is filled IN PLACE by render() after the mount (the
  // /api/state payload only carries a tail preview; the dialog fetches the
  // full deque from /api/live/log on demand). Keeping the volatile count out
  // of this build path is what lets the body sig ignore log churn.

  // Init-prompt expandable. Hidden when no installed live model supports
  // initial_prompt (registry-driven via inputs_support.live_prompt).
  const initRow = /** @type {HTMLDetailsElement} */ (pick(frag, "initPromptRow"));
  if (sup.live_prompt) {
    initRow.hidden = false;
    pick(frag, "initPromptCount").textContent = lp.length ? `· ${lp.length} chars` : "";
    /** @type {HTMLTextAreaElement} */ (frag.querySelector("#liveInitPromptText")).value = lp.content || "";
    // Default-open the editor when populated so the operator sees what's
    // in effect; collapsed when empty to keep the panel compact.
    initRow.open = !!lp.length;
  }

  // Wire actions against the fragment's nodes (they survive the mount swap).
  // The handlers capture element references at build time — querying at event
  // time would miss, because mounting empties the fragment.
  frag.querySelector("#liveStartBtn")?.addEventListener("click", onAction.start);
  frag.querySelector("#liveApplyBtn")?.addEventListener("click", onAction.start);
  frag.querySelector("#liveStopBtn")?.addEventListener("click", onAction.stop);
  frag.querySelector("#liveLogBtn")?.addEventListener("click", openLogDialog);
  // Nudge language to "no" when an nb-whisper model is picked and lang is
  // still on the boot default. `sel`/`langInput` come from the always-present
  // top section of the live-channel template (not from any of the
  // state-specific action templates), so they're already hard references —
  // unlike the start/stop/apply buttons above, which use `?.` because each
  // only appears in one of the three state templates.
  sel.addEventListener("change", () => {
    if (!sel.value.startsWith("nb-")) return;
    if (langInput.value === "en" || langInput.value === "") langInput.value = "no";
  });

  const initBtn = /** @type {HTMLButtonElement | null} */ (frag.querySelector("#liveInitPromptSave"));
  if (initBtn) {
    wireConfigSave({
      key: "live-prompt",
      btn: initBtn,
      textarea: frag.querySelector("#liveInitPromptText"),
      status: frag.querySelector('[data-slot="initPromptStatus"]'),
      onSuccess: undefined,
    });
  }
  return frag;
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
    gate_min_speech_ms: numOrNull("liveGateMinSpeech"),
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
  // The dialog refreshes once a second while open; rewriting the <pre> (and
  // autoscrolling) while the operator is select-copying log lines would
  // dissolve the selection on every tick. Same interaction-state rule as
  // renderRegion's guards — checked here directly because this updater
  // writes textContent in place rather than swapping a region.
  if (selectionInside(dlg)) return;
  const pre = pick(dlg, "pre");
  const status = pick(dlg, "status");
  if (!payload) {
    pre.textContent = "(failed to load logs)";
    status.textContent = "";
    return;
  }
  const log = payload.log || [];
  // Sticky-scroll (same rule as live-feed): only follow the tail when the
  // operator was already AT the tail. An unconditional scroll-to-bottom
  // yanked them back down every refresh while they were reading older lines.
  // Geometry is read BEFORE the rewrite — the new content changes it.
  const wasAtBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 10;
  pre.textContent = log.length ? log.join("\n") : "(no log entries yet)";
  status.textContent = `state: ${payload.state || "stopped"} · ${log.length} line${log.length === 1 ? "" : "s"}`;
  if (wasAtBottom) pre.scrollTop = pre.scrollHeight;
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
