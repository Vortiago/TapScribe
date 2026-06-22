// @ts-check
// Popup for the SpatialChat Bridge — a vanilla-web ES-module app.
//
// The DOM shell only: it owns no "what should show" decisions. The meeting
// lifecycle is derived by the pure presenter (popup-presenter.js) and applied
// here; Start/End/Dismiss effects go through popup-actions.js; the poll → card
// view-model through pipeline-view.js. Buttons / pills / the card panel / the
// tap table come from the vendored vanilla-components. control-client.js stays
// a classic global (the content script needs it in both worlds), read here as
// the `TapscribeControlClient` global (see types.d.ts).

import { tpl, pick, renderRegion } from "./lib/templates.js";
import { warmButton, createButtonSync } from "./components/button/button.js";
import { warmStatusDot, createStatusDotSync } from "./components/status-dot/status-dot.js";
import { warmPanel, createPanelSync } from "./components/panel/panel.js";
import { warmTableShell, createTableShellSync } from "./components/table-shell/table-shell.js";
import { warmEmptyState, createEmptyStateSync } from "./components/empty-state/empty-state.js";
import { map as mapPipeline } from "./pipeline-view.js";
import { meetingView, shouldKeepPolling } from "./popup-presenter.js";
import { snapshotIsLive, tapStateLabel } from "./taps-view.js";
import {
  startMeeting as actStart,
  requestEndMeeting as actEnd,
  dismissMeeting as actDismiss,
} from "./popup-actions.js";

const control = TapscribeControlClient;
const storage = chrome.storage.local;
// Popup-lifetime signal for component onClick wiring; aborted on unload.
const ac = new AbortController();

/** @param {string} id @returns {HTMLElement} */
function el(id) {
  const node = document.getElementById(id);
  if (!node) throw new Error("missing #" + id);
  return node;
}
/** @param {string} id @returns {HTMLInputElement} */
function input(id) { return /** @type {HTMLInputElement} */ (el(id)); }

/** @param {string} id @param {string} text @param {"ok" | "err" | ""} [tone] */
function setStatus(id, text, tone) {
  const node = el(id);
  node.textContent = text;
  node.className = "status " + (tone || "");
}

// ---- recorder config (what the control client takes) ----------------------

let currentHost = "localhost";
let currentPort = 8001;
let currentTapToken = "";
let currentUseTls = false;
/** @returns {RecorderCfg} */
function cfg() {
  return { host: currentHost, port: currentPort, useTls: currentUseTls, token: currentTapToken };
}

// ---- meeting state (fed to the presenter) ---------------------------------

/** @type {string | null} */ let currentMeetingSessionId = null;
let currentMeetingActive = false;
/** @type {{ phase: string, error?: string | null } | null} */ let lastMeetingEnd = null;
/** @type {import("./pipeline-view.js").PipelineView | null} */ let latestPollView = null;
/** @type {any} */ let latestStatus = null;

// ---- meeting card render state --------------------------------------------

/** @type {ReturnType<typeof setTimeout> | null} */ let cardTimer = null;
/** @type {string | null} */ let summaryRenderedFor = null;
let currentSummaryText = "";

// ---- component handles (built once at mount) ------------------------------

/** @type {ReturnType<typeof createButtonSync>} */ let btnStart;
/** @type {ReturnType<typeof createButtonSync>} */ let btnEnd;
/** @type {ReturnType<typeof createButtonSync>} */ let btnCopy;
/** @type {ReturnType<typeof createButtonSync>} */ let btnDismiss;
/** @type {ReturnType<typeof createPanelSync>} */ let cardPanel;
/** @type {HTMLElement} */ let cardProgress;
/** @type {HTMLElement} */ let cardFailure;
/** @type {HTMLElement} */ let cardSummaryPane;
/** @type {HTMLElement} */ let cardSummaryText;
/** @type {HTMLElement} */ let cardMeta;
/** @type {{ el: HTMLElement, dot: ReturnType<typeof createStatusDotSync>, verdict: HTMLElement }} */ let recorderPill;
/** @type {{ el: HTMLElement, dot: ReturnType<typeof createStatusDotSync>, verdict: HTMLElement }} */ let tokenPill;

// ---- mount ----------------------------------------------------------------

async function mount() {
  await Promise.all([warmButton(), warmStatusDot(), warmPanel(), warmTableShell(), warmEmptyState()]);

  el("settingsActions").append(
    createButtonSync({ label: "Save", variant: "primary", onClick: onSave }, ac.signal).el,
    createButtonSync({ label: "Test connection", onClick: () => probeAll() }, ac.signal).el,
    createButtonSync({ label: "Open dashboard ↗", variant: "ghost", onClick: openDash }, ac.signal).el,
  );

  btnStart = createButtonSync({ label: "Start meeting", onClick: onStart }, ac.signal);
  btnEnd = createButtonSync({ label: "End meeting", onClick: onEnd, disabled: true }, ac.signal);
  el("meetingActions").append(btnStart.el, btnEnd.el);

  recorderPill = pill("Recorder", "checking…");
  tokenPill = pill("Tap token", "not tested");
  el("reachability").append(recorderPill.el, tokenPill.el);

  buildCard();
  await load();
}

/** A "Label <dot> verdict" reachability cell: status-dot for colour, a span we
 * update for the verdict (status-dot's label is set at create only).
 * @param {string} label @param {string} verdictText */
function pill(label, verdictText) {
  const wrap = document.createElement("div");
  const dot = createStatusDotSync({ tone: "neutral" });
  const verdict = document.createElement("span");
  verdict.textContent = verdictText;
  wrap.append(label + " ", dot.el, " ", verdict);
  return { el: wrap, dot, verdict };
}

function buildCard() {
  const frag = tpl("tpl-meeting-card");
  cardProgress = pick(frag, "progress");
  cardFailure = pick(frag, "failure");
  cardSummaryPane = pick(frag, "summary");
  cardSummaryText = pick(frag, "summaryText");
  cardMeta = pick(frag, "meta");
  const copySlot = pick(frag, "copy");
  const dismissSlot = pick(frag, "dismiss");

  btnCopy = createButtonSync({ label: "Copy", size: "sm", onClick: copySummary }, ac.signal);
  copySlot.replaceWith(btnCopy.el);
  btnDismiss = createButtonSync({ label: "Dismiss", size: "sm", variant: "ghost", onClick: onDismiss }, ac.signal);
  dismissSlot.append(btnDismiss.el);

  const body = /** @type {HTMLElement} */ (frag.firstElementChild);
  cardPanel = createPanelSync({ head: "Meeting", body });
  cardPanel.el.hidden = true;
  el("meetingCardHost").append(cardPanel.el);
}

// ---- load + settings ------------------------------------------------------

async function load() {
  const s = await storage.get([
    "recorderHost", "recorderPort", "tapToken", "useTls", "meetingSessionId", "meetingActive", "meetingEnd",
  ]);
  currentHost = (s.recorderHost || "localhost").trim();
  currentPort = Number(s.recorderPort) || 8001;
  currentTapToken = (s.tapToken || "").trim();
  currentUseTls = !!s.useTls;
  currentMeetingSessionId =
    typeof s.meetingSessionId === "string" && s.meetingSessionId ? s.meetingSessionId : null;
  currentMeetingActive =
    typeof s.meetingActive === "boolean" ? s.meetingActive : !!currentMeetingSessionId;
  lastMeetingEnd = s.meetingEnd || null;
  input("host").value = currentHost;
  input("port").value = String(currentPort);
  input("tapToken").value = currentTapToken;
  input("useTls").checked = currentUseTls;
  applyMeeting();
  if (currentMeetingSessionId) pollCardOnce();
  await refresh();
}

async function onSave() {
  const rawHost = input("host").value.trim();
  const rawPort = input("port").value.trim();
  const rawToken = input("tapToken").value.trim();
  const rawUseTls = !!input("useTls").checked;
  if (!rawHost) { setStatus("saveStatus", "Host cannot be empty.", "err"); return; }
  const cleanHost = rawHost
    .replace(/^https?:\/\//i, "").replace(/^wss?:\/\//i, "")
    .replace(/\/.*/, "").replace(/:.*$/, "");
  const cleanPort = Number(rawPort) || 8001;
  if (cleanPort < 1 || cleanPort > 65535) {
    setStatus("saveStatus", "Port must be between 1 and 65535.", "err");
    return;
  }
  await storage.set({ recorderHost: cleanHost, recorderPort: cleanPort, tapToken: rawToken, useTls: rawUseTls });
  input("host").value = cleanHost;
  input("port").value = String(cleanPort);
  currentHost = cleanHost;
  currentPort = cleanPort;
  currentTapToken = rawToken;
  currentUseTls = rawUseTls;
  setStatus("saveStatus",
    "Saved (" + (rawUseTls ? "wss" : "ws") + "://" + cleanHost + ":" + cleanPort + "). Reload the SpatialChat tab.",
    "ok");
  await probeAll();
}

function openDash() { chrome.tabs.create({ url: control.httpBase(cfg()) + "/" }); }

// ---- reachability probes --------------------------------------------------

async function probeAll() {
  renderMixedContentWarning();
  recorderPill.dot.setTone("neutral");
  recorderPill.verdict.textContent = "checking…";
  tokenPill.dot.setTone("neutral");
  tokenPill.verdict.textContent = "checking…";
  el("probeMeta").textContent = "Probing " + currentHost + ":" + currentPort + " …";

  const rec = await control.checkHealth(cfg(), { timeoutMs: 4000 });
  recorderPill.dot.setTone(rec.ok ? "ok" : "bad");
  recorderPill.verdict.textContent = rec.ok ? "reachable" : "unreachable";
  const detail = rec.ok ? "ok" : (rec.error || ("HTTP " + rec.status));

  let tokenDetail = "n/a";
  if (rec.ok) {
    const tok = await control.probeTapToken(cfg(), { timeoutMs: 4000 });
    tokenPill.dot.setTone(tok.ok ? "ok" : "bad");
    tokenPill.verdict.textContent = tok.ok ? "accepted" : "rejected";
    tokenDetail = tok.ok ? "ok" : (tok.error || "rejected");
  } else {
    tokenPill.dot.setTone("neutral");
    tokenPill.verdict.textContent = "skipped";
  }
  el("probeMeta").textContent = "recorder: " + detail + " · tap-token: " + tokenDetail;
}

/** @param {string} t */
function code(t) { const c = document.createElement("code"); c.textContent = t; return c; }

function renderMixedContentWarning() {
  const node = el("mixedContentWarn");
  node.textContent = "";
  const risky = !currentUseTls && !control.isTrustworthyHost(currentHost);
  if (!risky) { node.className = ""; return; }
  node.className = "status err";
  const strong = document.createElement("strong");
  strong.textContent = "Mixed-content blocked: ";
  node.append(
    strong,
    "the bridge runs inside https://app.spatial.chat, so plain ws:// to ",
    code(currentHost),
    " will be refused by the browser. Enable TLS on the recorder and tick “Use TLS”, or run it on localhost.",
  );
}

// ---- active taps ----------------------------------------------------------
// tapStateLabel + the staleness rule (snapshotIsLive) live in taps-view.js so
// they're unit-testable DOM-free; this shell only renders their verdict.

function tapSig() {
  const st = latestStatus;
  if (!st) return "none";
  // A snapshot whose writer (the SpatialChat tab's content script) has gone
  // away renders as the no-tab empty state regardless of its frozen channels;
  // collapse every stale snapshot to one sig so renderRegion swaps to the
  // empty state exactly once when liveness flips, then holds.
  if (!snapshotIsLive(st, Date.now())) return "stale";
  const ctx = st.audioContextState || "";
  if (!st.channels || st.channels.length === 0) return "empty:" + ctx + ":" + !!st.settingsReady;
  return ctx + "|" + st.channels
    .map((/** @type {any} */ c) =>
      c.identity + ":" + (c.tapWs || "") + ":" + (c.framesSent || 0) + ":" + tapStateLabel(c))
    .join("|");
}

function renderTaps() {
  renderRegion(el("tapState"), buildTaps, { sig: tapSig() });
}

/** @returns {Node} */
function buildTaps() {
  const wrap = document.createElement("div");
  const st = latestStatus;
  if (!st) {
    wrap.append(createEmptyStateSync({
      title: "No status from the SpatialChat tab",
      detail: "Open https://app.spatial.chat/* with this extension installed.",
    }).el);
    return wrap;
  }
  // A stale snapshot is a closed/crashed tab's leftover (content.js stops
  // refreshing `ts` once the tab goes away — see taps-view.js): show the
  // no-tab empty state, not its frozen roster of now-departed speakers.
  if (!snapshotIsLive(st, Date.now())) {
    wrap.append(createEmptyStateSync({
      title: "No active SpatialChat tab",
      detail: "The SpatialChat tab was closed. Open https://app.spatial.chat/* and join a room to resume tapping.",
    }).el);
    return wrap;
  }
  const ctx = st.audioContextState;
  if (ctx && ctx !== "running") {
    const banner = document.createElement("div");
    banner.className = "status err";
    banner.textContent = ctx === "failed"
      ? "Audio capture failed — see the SpatialChat tab's DevTools console."
      : "Audio capture paused (AudioContext " + ctx + ") — click in the SpatialChat tab to resume.";
    wrap.append(banner);
  }
  if (!st.channels || st.channels.length === 0) {
    let detail = "Nobody is speaking nearby, or the SpatialChat room hasn't connected.";
    if (!st.settingsReady) detail = "Content script is still loading settings. " + detail;
    wrap.append(createEmptyStateSync({ title: "No taps yet", detail }).el);
    return wrap;
  }
  const table = createTableShellSync({
    columns: [
      { key: "who", label: "Speaker" },
      { key: "tap", label: "/tap" },
      { key: "frames", label: "frames", align: "end" },
      { key: "state", label: "state" },
    ],
    rows: st.channels.map((/** @type {any} */ c) => [
      c.name || String(c.identity).slice(0, 8),
      c.tapWs || "idle",
      c.framesSent || 0,
      tapStateLabel(c),
    ]),
  });
  wrap.append(table.el);
  return wrap;
}

async function refresh() {
  const { bridgeStatus } = await storage.get(["bridgeStatus"]);
  latestStatus = bridgeStatus || null;
  renderTaps();
  await probeAll();
}

// ---- meeting: apply the presenter's view-model ----------------------------

function applyMeeting() {
  const view = meetingView({
    meetingSessionId: currentMeetingSessionId,
    meetingActive: currentMeetingActive,
    lastEnd: lastMeetingEnd,
    pollView: latestPollView,
  });
  btnStart.setDisabled(view.startDisabled);
  btnEnd.setDisabled(view.endDisabled);
  // Only write the headline for a derivable steady state; transient action
  // feedback ("Starting meeting…") set by the handlers is left otherwise.
  if (view.status) setStatus("meetingStatus", view.status.text, view.status.tone);
  applyCard(view.card);
}

/** @param {import("./popup-presenter.js").MeetingView["card"]} card */
function applyCard(card) {
  cardPanel.el.hidden = !card.visible;
  if (!card.visible) return;

  cardProgress.hidden = !card.progress;
  cardProgress.textContent = card.progress || "";
  cardProgress.className = "status" + (card.summary ? " ok" : "");

  cardFailure.hidden = !card.failure;
  cardFailure.textContent = card.failure || "";

  if (card.summary) {
    cardSummaryPane.hidden = false;
    currentSummaryText = card.summary.text;
    // Render the summary pane ONCE per session — a later poll tick must not
    // rewrite the text node and clobber a mid-copy selection.
    if (summaryRenderedFor !== currentMeetingSessionId) {
      summaryRenderedFor = currentMeetingSessionId;
      cardSummaryText.textContent = card.summary.text;
      cardMeta.textContent = card.summary.meta;
      cardMeta.className = "meta";
    }
  } else {
    cardSummaryPane.hidden = true;
  }

  btnDismiss.el.hidden = card.dismissHidden;
}

function resetCard() {
  stopCardPolling();
  summaryRenderedFor = null;
  currentSummaryText = "";
  latestPollView = null;
  cardSummaryPane.hidden = true;
  cardFailure.hidden = true;
  cardPanel.el.hidden = true;
}

// ---- meeting: poll the pipeline -------------------------------------------

function stopCardPolling() {
  if (cardTimer != null) { clearTimeout(cardTimer); cardTimer = null; }
}
function scheduleNextPoll() {
  stopCardPolling();
  cardTimer = setTimeout(pollCardOnce, 1500);
}

async function pollCardOnce() {
  const sid = currentMeetingSessionId;
  if (!sid) { latestPollView = null; applyMeeting(); return; }
  let raw;
  try {
    raw = await control.pollPipeline(cfg(), sid, { timeoutMs: 6000 });
  } catch (e) {
    // Transient failure (network blip / Recorder mid-restart): keep the
    // rendered state, self-heal at the poll cadence while the id is live.
    if (currentMeetingSessionId) scheduleNextPoll();
    return;
  }
  latestPollView = mapPipeline(raw, {
    meetingActive: currentMeetingActive,
    ending: !!(lastMeetingEnd && lastMeetingEnd.phase === "ending"),
  });
  applyMeeting();
  if (shouldKeepPolling(latestPollView.phase)) scheduleNextPoll();
  else stopCardPolling();
}

// ---- meeting: actions -----------------------------------------------------

async function onStart() {
  btnStart.setDisabled(true); // optimistic: block a double-click mid-flight
  setStatus("meetingStatus", "Starting meeting…", "");
  const out = await actStart({ control, storage, cfg: cfg() });
  if (out.ok) {
    currentMeetingSessionId = out.sessionId;
    currentMeetingActive = true;
    lastMeetingEnd = null;
    resetCard();
    applyMeeting();
  } else {
    const why = out.kind === "mixed-content-blocked"
      ? "recorder is http:// on a non-trustworthy host — enable TLS, or run it on localhost"
      : out.message;
    setStatus("meetingStatus", "Start meeting failed: " + why, "err");
    applyMeeting();
  }
}

async function onEnd() {
  setStatus("meetingStatus", "Ending meeting…", "");
  btnStart.setDisabled(true);
  btnEnd.setDisabled(true);
  try {
    await actEnd({ storage, now: Date.now() });
  } catch (e) {
    setStatus("meetingStatus", "Couldn't request End meeting — try again.", "err");
    applyMeeting();
  }
}

async function onDismiss() {
  stopCardPolling();
  try { await actDismiss({ storage }); } catch (e) { /* best-effort; drop the card below */ }
  currentMeetingSessionId = null;
  currentMeetingActive = false;
  lastMeetingEnd = null;
  resetCard();
  setStatus("meetingStatus", "", "");
  applyMeeting();
}

async function copySummary() {
  try {
    await navigator.clipboard.writeText(currentSummaryText);
    btnCopy.setLabel("Copied!");
    setTimeout(() => btnCopy.setLabel("Copy"), 1200);
  } catch (e) {
    btnCopy.setLabel("Copy failed");
  }
}

// ---- storage-driven updates -----------------------------------------------

/** @type {StorageListener} */
const onStorageChanged = (changes, area) => {
  if (area !== "local") return;
  if (changes.bridgeStatus && changes.bridgeStatus.newValue) {
    latestStatus = changes.bridgeStatus.newValue;
    renderTaps();
  }
  if (changes.meetingActive) {
    currentMeetingActive = !!changes.meetingActive.newValue;
    if (!currentMeetingActive && currentMeetingSessionId) pollCardOnce();
    applyMeeting();
  }
  if (changes.meetingSessionId) {
    const v = changes.meetingSessionId.newValue;
    currentMeetingSessionId = (typeof v === "string" && v) ? v : null;
    if (!currentMeetingSessionId) {
      currentMeetingActive = false;
      resetCard();
    } else {
      resetCard();
      pollCardOnce();
    }
    applyMeeting();
  }
  if (changes.meetingEnd && changes.meetingEnd.newValue) {
    lastMeetingEnd = changes.meetingEnd.newValue;
    const phase = lastMeetingEnd ? lastMeetingEnd.phase : null;
    if (phase === "ending" || phase === "started" || phase === "busy") pollCardOnce();
    applyMeeting();
  }
};
chrome.storage.onChanged.addListener(onStorageChanged);

// Keep the tap region fresh even if a push was missed (cheap; renderRegion
// skips when the sig is unchanged or an interaction is live).
const pollTimer = setInterval(async () => {
  const { bridgeStatus } = await storage.get(["bridgeStatus"]);
  latestStatus = bridgeStatus || null;
  renderTaps();
}, 1500);

window.addEventListener("unload", () => {
  clearInterval(pollTimer);
  stopCardPolling();
  ac.abort();
  try { chrome.storage.onChanged.removeListener(onStorageChanged); } catch (e) { /* ignore */ }
});

mount();
