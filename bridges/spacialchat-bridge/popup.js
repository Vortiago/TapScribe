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
let pollTimer = null;

// The recorder config the shared control-client takes (control-client.js,
// loaded ahead of us by popup.html). It owns the bearer header, scheme
// derivation, response parsing, and timeouts for every control call.
function cfg() {
  return { host: currentHost, port: currentPort, useTls: currentUseTls, token: currentTapToken };
}

async function load() {
  const { recorderHost, recorderPort, tapToken, useTls, autoNewSessionOnRoomChange } =
    await chrome.storage.local.get(
      ["recorderHost", "recorderPort", "tapToken", "useTls", "autoNewSessionOnRoomChange"],
    );
  currentHost = (recorderHost || "localhost").trim();
  currentPort = Number(recorderPort) || 8001;
  currentTapToken = (tapToken || "").trim();
  currentUseTls = !!useTls;
  $("host").value = currentHost;
  $("port").value = String(currentPort);
  $("tapToken").value = currentTapToken;
  $("useTls").checked = currentUseTls;
  $("autoNewSessionOnRoomChange").checked = !!autoNewSessionOnRoomChange;
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

// POST /api/tap/new-session with no body — the legacy GLOBAL rotate.
// Routed through the shared control-client, which owns the bearer header
// and scheme derivation; the popup keeps the status-label rendering. The
// recorder rotates to a fresh session folder and prunes empty ones.
async function postNewSession() {
  setStatus("newSessionStatus", "Starting new session…", "");
  try {
    const res = await TapscribeControlClient.rotateSession(cfg(), { timeoutMs: 4000 });
    if (!res.ok) {
      setStatus("newSessionStatus", "New session failed (HTTP " + res.status + ").", "err");
      return;
    }
    const body = res.body || {};
    const label = body.rotated === false
      ? "Already on a fresh session — nothing to rotate."
      : "New session started" + (body.current ? " (" + body.current + ")" : "") + ".";
    setStatus("newSessionStatus", label, "ok");
  } catch (e) {
    setStatus("newSessionStatus", "New session failed: " + String(e && e.message || e), "err");
  }
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

$("newSession").addEventListener("click", postNewSession);

// Persist the room-change toggle immediately (not gated on Save) so
// content.js picks it up via chrome.storage.onChanged without a tab reload.
$("autoNewSessionOnRoomChange").addEventListener("change", () => {
  chrome.storage.local.set({ autoNewSessionOnRoomChange: $("autoNewSessionOnRoomChange").checked });
});

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
  try { chrome.storage.onChanged.removeListener(onStorageChanged); } catch (e) {}
});
