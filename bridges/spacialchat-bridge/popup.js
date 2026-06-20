// Popup for the SpatialChat Bridge.
// - Reads/writes recorderHost + recorderPort in chrome.storage.local
// - Probes /health on the Recorder
// - Shows the latest status snapshot pushed by content.js
// - Auto-refreshes while open

const $ = (id) => document.getElementById(id);

let currentHost = "localhost";
let currentPort = 8001;
let currentTapToken = "";
let currentUseTls = false;
// The bracketed meeting's detached Session id (or null). Mirrors the durable
// `meetingSessionId` stored in chrome.storage.local. It is the popup's poll
// target for the meeting card and persists across End — the content script
// keeps it stored after the meeting ends so a re-opened (ephemeral) popup can
// still re-derive progress/summary. It is cleared only on the next "Start
// meeting" or an explicit "Dismiss".
let currentMeetingSessionId = null;
// Whether a meeting is actively *recording* (taps routing into it). Distinct
// from `currentMeetingSessionId` being set: after End the id lingers for the
// card while `meetingActive` is false (capture has fallen back to the global
// Session). Drives the Start/End button enable/disable.
let currentMeetingActive = false;
// The latest End-meeting outcome the content script published (phase: ending
// / started / busy / failed). Used as the card's "ending" lifecycle hint and
// to kick off polling the instant the pipeline is triggered.
let lastMeetingEnd = null;
let pollTimer = null;

// --- Meeting card state (module ④) ----------------------------------------
// The card polls the recorder for the stored Session id and renders through
// the pure mapper (pipeline-view.js). It holds NO local summary cache — every
// open re-derives from the poll, so a finished summary survives a popup close
// or a Recorder restart. Interaction-hold by hand: progress text updates in
// place and the summary pane is built ONCE on the transition to done, so a
// poll tick can never clobber a mid-copy text selection.
let cardTimer = null;             // setTimeout id for the next running-state poll
let summaryRenderedFor = null;    // Session id whose summary pane is already built
let currentSummaryText = "";      // text the Copy button writes to the clipboard

// The recorder config the shared control-client takes (control-client.js,
// loaded ahead of us by popup.html). It owns the bearer header, scheme
// derivation, response parsing, and timeouts for every control call.
function cfg() {
  return { host: currentHost, port: currentPort, useTls: currentUseTls, token: currentTapToken };
}

async function load() {
  const { recorderHost, recorderPort, tapToken, useTls, meetingSessionId, meetingActive, meetingEnd } =
    await chrome.storage.local.get(
      ["recorderHost", "recorderPort", "tapToken", "useTls", "meetingSessionId", "meetingActive", "meetingEnd"],
    );
  currentHost = (recorderHost || "localhost").trim();
  currentPort = Number(recorderPort) || 8001;
  currentTapToken = (tapToken || "").trim();
  currentUseTls = !!useTls;
  currentMeetingSessionId =
    typeof meetingSessionId === "string" && meetingSessionId ? meetingSessionId : null;
  // The id can outlive the active meeting (kept for the card after End), so
  // read `meetingActive` explicitly; default to "active when an id is present"
  // for the bring-up case where only the id was ever written.
  currentMeetingActive =
    typeof meetingActive === "boolean" ? meetingActive : !!currentMeetingSessionId;
  lastMeetingEnd = meetingEnd || null;
  $("host").value = currentHost;
  $("port").value = String(currentPort);
  $("tapToken").value = currentTapToken;
  $("useTls").checked = currentUseTls;
  renderMeeting();
  // Re-derive the card from the recorder on every open: a stored Session id
  // means there may be a meeting in flight (or a finished summary) to show.
  if (currentMeetingSessionId) pollCardOnce();
  await refresh();
}

function setStatus(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = "status " + (kind || "");
}

function setSaveStatus(text, kind) { setStatus("saveStatus", text, kind); }

function setPill(id, ok, label) {
  const el = $(id);
  el.textContent = label;
  el.className = "pill " + (ok === true ? "ok" : ok === false ? "err" : "wait");
}

// Reflect the bracketed-meeting state in the popup. While a meeting is
// actively recording, "Start meeting" is disabled (so a second start can't
// orphan the first), "End meeting" is enabled, and the active Session id is
// shown. Once the meeting ends the buttons flip (Start usable again) even
// though the Session id lingers for the card; any prior status line (e.g. a
// failure / end outcome) is left untouched.
function renderMeeting() {
  const start = $("startMeeting");
  const end = $("endMeeting");
  const active = currentMeetingActive;
  if (start) start.disabled = active;
  if (end) end.disabled = !active;
  if (active) {
    setStatus("meetingStatus", "Meeting active — capturing into " + currentMeetingSessionId + ".", "ok");
  }
}

// "End meeting": ask the content script (which owns the /tap WebSockets and
// outlives this ephemeral popup) to drain + close every tap and trigger the
// end-of-meeting pipeline. We only signal via storage; the content script
// publishes the outcome back via `meetingEnd`, rendered in renderMeetingEnd.
function endMeeting() {
  setStatus("meetingStatus", "Ending meeting…", "");
  if ($("startMeeting")) $("startMeeting").disabled = true;
  if ($("endMeeting")) $("endMeeting").disabled = true;
  // A timestamp nonce so a repeat End fires storage.onChanged again.
  chrome.storage.local.set({ meetingEndRequestedAt: Date.now() }).catch(() => {
    setStatus("meetingStatus", "Couldn't request End meeting — try again.", "err");
    renderMeeting();
  });
}

// Render the End-meeting outcome the content script published to storage and
// hand off to the meeting card. The content script owns the drain → close-all
// → trigger sequence (it outlives the popup); once it reports the pipeline is
// in flight (started / busy) the card starts polling for live progress and the
// finished summary.
function renderMeetingEnd(end) {
  if (!end || !end.phase) return;
  lastMeetingEnd = end;
  if (end.phase === "ending") {
    setStatus("meetingStatus", "Ending meeting…", "");
    pollCardOnce(); // surface the "ending" lifecycle in the card too
  } else if (end.phase === "started") {
    setStatus("meetingStatus", "Meeting ended — processing started on the recorder.", "ok");
    pollCardOnce(); // the pipeline is now running — begin tracking progress
  } else if (end.phase === "busy") {
    setStatus("meetingStatus", "Recorder busy — another job is already running on this session.", "err");
    // A 409 means a job is already on this Session; it may be the pipeline
    // itself (re-trigger) or another job. Poll: if it reaches done, the card
    // simply shows the summary.
    pollCardOnce();
  } else if (end.phase === "failed") {
    setStatus("meetingStatus", "End meeting failed: " + (end.error || "unknown error") + ".", "err");
  }
}

// "Start meeting": mint a fresh DETACHED session via the shared
// control-client and persist its server-minted id to chrome.storage.local.
// The content script reads `meetingSessionId` (live, via storage.onChanged)
// and routes every /tap into it — no SpatialChat tab reload needed. On
// failure the button is re-enabled so the operator can retry.
async function startMeeting() {
  const btn = $("startMeeting");
  if (btn) btn.disabled = true; // optimistic: block a double-click mid-flight
  setStatus("meetingStatus", "Starting meeting…", "");
  try {
    const res = await TapscribeControlClient.createDetachedSession(cfg(), { timeoutMs: 6000 });
    // Persist FIRST: the stored id is what the content script routes on, so
    // it is the real "meeting started" signal. Only mirror it into local
    // state once the write lands — if the set rejects, the catch below
    // leaves currentMeetingSessionId null and re-enables the button rather
    // than showing a green "Meeting active" the content script never saw.
    // `meetingActive: true` marks routing live; clearing `meetingEnd` drops
    // any previous meeting's outcome so the card starts fresh (#26).
    await chrome.storage.local.set({
      meetingSessionId: res.sessionId,
      meetingActive: true,
      meetingEnd: null,
    });
    currentMeetingSessionId = res.sessionId;
    currentMeetingActive = true;
    lastMeetingEnd = null;
    resetCard();
    renderMeeting();
  } catch (e) {
    const why = e && e.kind === "mixed-content-blocked"
      ? "recorder is http:// on a non-trustworthy host — enable TLS, or run it on localhost"
      : String((e && e.message) || e);
    setStatus("meetingStatus", "Start meeting failed: " + why, "err");
    // No meeting was started, so re-enable via the single enable/disable
    // rule in renderMeeting (which leaves the error status above intact).
    renderMeeting();
  }
}

// --- Meeting card: poll the pipeline + render progress / summary ----------

function stopCardPolling() {
  if (cardTimer != null) {
    clearTimeout(cardTimer);
    cardTimer = null;
  }
}

// Schedule the next poll. Only running / ending are live states worth a
// timer; done / failed / idle / recording are steady states the next
// popup-open re-derives, so we stop polling there (and never busy-loop).
function scheduleNextPoll() {
  stopCardPolling();
  cardTimer = setTimeout(pollCardOnce, 1500);
}

// One poll of the recorder for the stored Session id, mapped through the pure
// view-model mapper and rendered. No local summary cache — this is the only
// source of truth, so a finished summary survives a popup close / Recorder
// restart (the recorder's done branch serves the persisted summary).
async function pollCardOnce() {
  const sid = currentMeetingSessionId;
  if (!sid) {
    hideCard();
    return;
  }
  let raw;
  try {
    raw = await TapscribeControlClient.pollPipeline(cfg(), sid, { timeoutMs: 6000 });
  } catch (e) {
    // A transient poll failure (network blip, a Recorder mid-restart) leaves
    // the meeting state and any rendered summary intact — don't tear them
    // down over one failed poll. Self-heal: keep retrying at the poll cadence
    // while the meeting id is still live, so a Recorder that comes back up
    // resumes progress / serves the persisted summary without a reopen.
    if (currentMeetingSessionId) scheduleNextPoll();
    return;
  }
  const view = TapscribePipelineView.map(raw, {
    meetingActive: currentMeetingActive,
    ending: !!(lastMeetingEnd && lastMeetingEnd.phase === "ending"),
  });
  renderCard(view);
  if (view.phase === "running" || view.phase === "ending") scheduleNextPoll();
  else stopCardPolling();
}

// Render the card from a view-model. Progress text is updated IN PLACE (never
// a node rebuild) and the summary pane is built once on the transition to
// done, so a poll tick can't clobber a mid-copy text selection.
function renderCard(view) {
  const card = $("meetingCard");
  if (!card) return;
  const phaseEl = $("meetingProgress");
  const failEl = $("meetingFailure");
  const pane = $("meetingSummaryPane");
  const dismiss = $("meetingDismiss");

  // The card only has something to show once the pipeline is in flight or
  // finished (or while ending). Recording / idle is covered by the status line.
  const show =
    view.phase === "running" || view.phase === "done" ||
    view.phase === "failed" || view.phase === "ending";
  card.hidden = !show;
  if (!show) return;

  // Progress line (in place).
  if (view.phase === "running") {
    phaseEl.hidden = false;
    phaseEl.className = "status";
    phaseEl.textContent =
      view.progress + (view.currentFile ? " — " + view.currentFile : "");
  } else if (view.phase === "ending") {
    phaseEl.hidden = false;
    phaseEl.className = "status";
    phaseEl.textContent = "Ending meeting — flushing audio, then processing…";
  } else if (view.phase === "done") {
    phaseEl.hidden = false;
    phaseEl.className = "status ok";
    phaseEl.textContent = "Summary ready.";
  } else {
    phaseEl.hidden = true;
  }

  // Failure line.
  if (view.phase === "failed") {
    failEl.hidden = false;
    failEl.textContent =
      "Failed" + (view.failureStage ? " during " + view.failureStage : "") +
      ": " + view.failureReason;
  } else {
    failEl.hidden = true;
  }

  // Summary pane — built once per Session (render-once guard).
  if (view.phase === "done") {
    renderSummaryOnce(view);
  } else if (pane) {
    pane.hidden = true;
  }

  // Dismiss is offered once the meeting is over (not while still recording).
  if (dismiss) dismiss.hidden = currentMeetingActive;
}

// Build the summary pane exactly once for the current Session. A later poll
// tick (or a reopen) for the same Session must NOT rewrite the text node —
// that would clobber a mid-copy selection. `currentSummaryText` is refreshed
// each call so the Copy button always copies the latest text even when the
// DOM is left untouched.
function renderSummaryOnce(view) {
  const pane = $("meetingSummaryPane");
  if (!pane) return;
  pane.hidden = false;
  currentSummaryText = view.summaryText || "";
  if (summaryRenderedFor === currentMeetingSessionId) return;
  summaryRenderedFor = currentMeetingSessionId;
  $("meetingSummaryText").textContent = currentSummaryText;
  const s = view.summary || {};
  const bits = [];
  if (s.model) bits.push("model: " + s.model);
  if (s.source) bits.push("source: " + s.source);
  $("meetingSummaryMeta").textContent = bits.join(" · ");
}

function hideCard() {
  stopCardPolling();
  const card = $("meetingCard");
  if (card) card.hidden = true;
}

// Reset the transient card render state for a fresh meeting (without touching
// storage). Called on Start so a new meeting never shows the previous one's
// summary even for one frame.
function resetCard() {
  stopCardPolling();
  summaryRenderedFor = null;
  currentSummaryText = "";
  const pane = $("meetingSummaryPane");
  if (pane) pane.hidden = true;
  const fail = $("meetingFailure");
  if (fail) fail.hidden = true;
  hideCard();
}

// Copy the finished summary to the clipboard. Only ever called from the
// card's Copy button (a user gesture), so navigator.clipboard is permitted.
async function copySummary() {
  const btn = $("meetingCopy");
  try {
    await navigator.clipboard.writeText(currentSummaryText);
    if (btn) {
      btn.textContent = "Copied!";
      setTimeout(() => { btn.textContent = "Copy"; }, 1200);
    }
  } catch (e) {
    if (btn) btn.textContent = "Copy failed";
  }
}

// "Dismiss" a finished/failed meeting: clear the durable Session id and the
// end outcome so the card stops re-deriving last meeting's result on every
// open. Routing already fell back to the global Session at End, so this is
// purely the popup forgetting a finished meeting.
async function dismissMeeting() {
  stopCardPolling();
  try {
    await chrome.storage.local.set({ meetingSessionId: null, meetingActive: false, meetingEnd: null });
  } catch (e) {
    // best-effort: even if the write fails, drop the local card below
  }
  currentMeetingSessionId = null;
  currentMeetingActive = false;
  lastMeetingEnd = null;
  resetCard();
  setStatus("meetingStatus", "", "");
  renderMeeting();
}

function renderMixedContentWarning() {
  const el = $("mixedContentWarn");
  if (!el) return;
  // The content script's ws:// to a non-trustworthy host from the https
  // SpatialChat page is mixed-content-blocked. We can't probe that from the
  // popup's own chrome-extension:// origin, so we evaluate the host against
  // the control-client's shared trustworthy-host allowlist directly.
  const risky = !currentUseTls && !TapscribeControlClient.isTrustworthyHost(currentHost);
  if (risky) {
    el.innerHTML =
      "<strong>Mixed-content blocked:</strong> the bridge runs inside " +
      "<code>https://app.spatial.chat</code>, so plain <code>ws://</code> " +
      "to <code>" + escapeHtml(currentHost) + "</code> will be refused by " +
      "the browser. Enable TLS on the recorder and tick “Use TLS”, " +
      "or run the recorder on <code>localhost</code>. The popup's own probe " +
      "below uses the extension origin and can still say “ok”.";
    el.className = "status err";
  } else {
    el.textContent = "";
    el.className = "";
  }
}

async function probeAll() {
  renderMixedContentWarning();
  setPill("recorderStatus", null, "checking…");
  setPill("tokenStatus", null, "checking…");
  $("probeMeta").textContent = "Probing " + currentHost + ":" + currentPort + " …";
  // Both probes route through the shared control-client, which owns the
  // /health reachability check (no token) and the /tap WS token probe,
  // plus the 4 s abort timeout.
  const rec = await TapscribeControlClient.checkHealth(cfg(), { timeoutMs: 4000 });
  setPill("recorderStatus", rec.ok, rec.ok ? "reachable" : "unreachable");
  const detail = rec.ok ? "ok" : (rec.error || ("HTTP " + rec.status));

  // Token probe only runs when the recorder is reachable, else the
  // failure is just the same network error twice.
  let tokenDetail = "n/a";
  if (rec.ok) {
    const tok = await TapscribeControlClient.probeTapToken(cfg(), { timeoutMs: 4000 });
    setPill("tokenStatus", tok.ok, tok.ok ? "accepted" : "rejected");
    tokenDetail = tok.ok ? "ok" : (tok.error || "rejected");
  } else {
    setPill("tokenStatus", null, "skipped");
  }
  $("probeMeta").textContent = "recorder: " + detail + " · tap-token: " + tokenDetail;
}

function renderTaps(status) {
  const el = $("tapState");
  if (!status) {
    el.textContent = "No status from the SpatialChat tab. Open https://app.spatial.chat/* with this extension installed.";
    el.className = "small muted";
    return;
  }
  const age = Math.round((Date.now() - status.ts) / 1000);
  const stale = age > 5;
  const hostLabel = (status.recorderHost || "?") + ":" + (status.recorderPort || "?");
  // Surface a non-running AudioContext at the top so the operator sees
  // it before reading the per-channel rows. Suspended/interrupted is
  // the common case after a tab background — clears on the next click
  // in the SpatialChat tab. "failed" means tap setup threw (CSP,
  // addModule reject, ...); no click will fix it, point at DevTools.
  const ctxState = status.audioContextState;
  const ctxHint = ctxState === "failed"
    ? "see DevTools console on the SpatialChat tab for the error"
    : "click anywhere in the SpatialChat tab to resume";
  const ctxBanner = (ctxState && ctxState !== "running")
    ? '<div class="status err" style="margin-bottom:6px;">Audio capture paused — AudioContext is <code>' +
      escapeHtml(ctxState) + '</code>. ' + ctxHint + '.</div>'
    : '';
  if (!status.channels || status.channels.length === 0) {
    let msg = "Content script is loaded";
    if (!status.settingsReady) msg += " but still loading settings";
    msg += ". No taps yet — either nobody is speaking nearby or the SpatialChat room hasn't connected.";
    el.innerHTML = ctxBanner + '<div>' + escapeHtml(msg) + '</div><div class="meta">last update ' + age + 's ago (recorder: <code>' + escapeHtml(hostLabel) + '</code>)' + (stale ? ' — STALE' : '') + '</div>';
    el.className = "small muted";
    return;
  }
  let h = ctxBanner + '<table><thead><tr><th>Speaker</th><th>/tap</th><th>frames</th><th>state</th></tr></thead><tbody>';
  for (const c of status.channels) {
    const who = c.name || c.identity.slice(0, 8);
    const tapPill = wsPill(c.tapWs);
    let stateLabel = "";
    if (c.error) stateLabel = '<span class="pill err">' + escapeHtml(c.error) + '</span>';
    else if (c.draining) stateLabel = '<span class="pill wait">draining</span>';
    else if (c.muted) stateLabel = '<span class="pill wait">muted</span>';
    else stateLabel = '<span class="pill ok">active</span>';
    h += '<tr>';
    h += '<td>' + escapeHtml(who) + '</td>';
    h += '<td>' + tapPill + '</td>';
    h += '<td>' + (c.framesSent || 0) + '</td>';
    h += '<td>' + stateLabel + '</td>';
    h += '</tr>';
  }
  h += '</tbody></table>';
  h += '<div class="meta">last update ' + age + 's ago (recorder: <code>' + escapeHtml(hostLabel) + '</code>)' + (stale ? ' — STALE, SpatialChat tab may be closed' : '') + '</div>';
  el.innerHTML = h;
  el.className = "";
}

function wsPill(state) {
  if (!state) return '<span class="pill wait">idle</span>';
  if (state === "OPEN") return '<span class="pill ok">OPEN</span>';
  if (state === "CONNECTING") return '<span class="pill wait">conn…</span>';
  if (state === "CLOSED") return '<span class="pill err">CLOSED</span>';
  return '<span class="pill err">' + escapeHtml(state) + '</span>';
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}

async function refresh() {
  const { bridgeStatus } = await chrome.storage.local.get(["bridgeStatus"]);
  renderTaps(bridgeStatus);
  await probeAll();
}

// --- Wiring ---------------------------------------------------------------

$("save").addEventListener("click", async () => {
  const rawHost = $("host").value.trim();
  const rawPort = $("port").value.trim();
  const rawToken = $("tapToken").value.trim();
  const rawUseTls = !!$("useTls").checked;
  if (!rawHost) {
    setSaveStatus("Host cannot be empty.", "err");
    return;
  }
  const cleanHost = rawHost
    .replace(/^https?:\/\//i, "")
    .replace(/^wss?:\/\//i, "")
    .replace(/\/.*/, "")
    .replace(/:.*$/, "");
  const cleanPort = Number(rawPort) || 8001;
  if (cleanPort < 1 || cleanPort > 65535) {
    setSaveStatus("Port must be between 1 and 65535.", "err");
    return;
  }
  await chrome.storage.local.set({
    recorderHost: cleanHost,
    recorderPort: cleanPort,
    tapToken: rawToken,
    useTls: rawUseTls,
  });
  $("host").value = cleanHost;
  $("port").value = String(cleanPort);
  currentHost = cleanHost;
  currentPort = cleanPort;
  currentTapToken = rawToken;
  currentUseTls = rawUseTls;
  setSaveStatus("Saved (" + (rawUseTls ? "wss" : "ws") + "://" + cleanHost + ":" + cleanPort
                + "). Reload the SpatialChat tab.", "ok");
  await probeAll();
});

$("recheck").addEventListener("click", probeAll);

$("startMeeting").addEventListener("click", startMeeting);

$("endMeeting").addEventListener("click", endMeeting);

$("meetingCopy").addEventListener("click", copySummary);

$("meetingDismiss").addEventListener("click", dismissMeeting);

$("openDash").addEventListener("click", (ev) => {
  ev.preventDefault();
  chrome.tabs.create({ url: TapscribeControlClient.httpBase(cfg()) + "/" });
});

load();

// Push-based updates: content.js calls publishStatus() on every state
// change (tap-start/stop, mute, WS open/close). chrome.storage.onChanged
// fires in the popup within ~50 ms, so "active taps" reacts essentially
// instantly.
const onStorageChanged = (changes, areaName) => {
  if (areaName !== "local") return;
  if (changes.bridgeStatus && changes.bridgeStatus.newValue) {
    renderTaps(changes.bridgeStatus.newValue);
  }
  // The content script (or another popup) drives the meeting lifecycle via
  // two keys: `meetingSessionId` (the durable id — set on Start, replaced on
  // the next Start, cleared on Dismiss) and `meetingActive` (routing live;
  // flipped false on End while the id lingers for the card). Re-derive button
  // state + the card live so this popup never shows a stale state once it
  // loses ownership.
  if (changes.meetingActive) {
    currentMeetingActive = !!changes.meetingActive.newValue;
    renderMeeting();
    // The meeting just ended (active → false) but its Session id is still
    // stored: poll so the card picks up the pipeline the End just triggered.
    if (!currentMeetingActive && currentMeetingSessionId) pollCardOnce();
  }
  if (changes.meetingSessionId) {
    const v = changes.meetingSessionId.newValue;
    currentMeetingSessionId = (typeof v === "string" && v) ? v : null;
    if (!currentMeetingSessionId) {
      // Dismissed / cleared: nothing left to record into or to show.
      currentMeetingActive = false;
      resetCard();
    } else {
      // A fresh Start (possibly from another popup) — reset the card and
      // re-derive from the new Session.
      resetCard();
      pollCardOnce();
    }
    renderMeeting();
  }
  // The content script publishes the End-meeting outcome here; renderMeetingEnd
  // both shows the headline and hands off to the card (begins polling once the
  // pipeline is in flight).
  if (changes.meetingEnd && changes.meetingEnd.newValue) {
    renderMeetingEnd(changes.meetingEnd.newValue);
  }
};
chrome.storage.onChanged.addListener(onStorageChanged);

// Periodic fallback poll, mostly to keep the "last update Ns ago" label
// fresh and to catch any state push we somehow missed. 1500ms is fine
// because the push path above handles real state changes.
pollTimer = setInterval(async () => {
  const { bridgeStatus } = await chrome.storage.local.get(["bridgeStatus"]);
  renderTaps(bridgeStatus);
}, 1500);
window.addEventListener("unload", () => {
  if (pollTimer) clearInterval(pollTimer);
  stopCardPolling();
  try { chrome.storage.onChanged.removeListener(onStorageChanged); } catch (e) {}
});
