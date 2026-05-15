// Popup for the SpatialChat Bridge.
// - Reads/writes recorderHost + recorderPort in chrome.storage.local
// - Probes /health on the Recorder
// - Shows the latest status snapshot pushed by content.js
// - Auto-refreshes while open

const $ = (id) => document.getElementById(id);

let currentHost = "localhost";
let currentPort = 8001;
let pollTimer = null;

async function load() {
  const { recorderHost, recorderPort } = await chrome.storage.local.get(["recorderHost", "recorderPort"]);
  currentHost = (recorderHost || "localhost").trim();
  currentPort = Number(recorderPort) || 8001;
  $("host").value = currentHost;
  $("port").value = String(currentPort);
  await refresh();
}

function setSaveStatus(text, kind) {
  const el = $("saveStatus");
  el.textContent = text;
  el.className = "status " + (kind || "");
}

function setPill(id, ok, label) {
  const el = $(id);
  el.textContent = label;
  el.className = "pill " + (ok === true ? "ok" : ok === false ? "err" : "wait");
}

async function probeHealth(host, port, signal) {
  const url = "http://" + host + ":" + port + "/health";
  try {
    const r = await fetch(url, { method: "GET", signal });
    if (!r.ok) return { ok: false, status: r.status, url };
    const body = await r.json().catch(() => ({}));
    return { ok: true, body, url };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e), url };
  }
}

async function probeAll() {
  setPill("recorderStatus", null, "checking…");
  $("probeMeta").textContent = "Probing " + currentHost + ":" + currentPort + " …";
  const ctrl = new AbortController();
  const tmo = setTimeout(() => ctrl.abort(), 4000);
  const rec = await probeHealth(currentHost, currentPort, ctrl.signal);
  clearTimeout(tmo);
  setPill("recorderStatus", rec.ok, rec.ok ? "reachable" : "unreachable");
  const detail = rec.ok ? "ok" : (rec.error || ("HTTP " + rec.status));
  $("probeMeta").textContent = "recorder: " + detail;
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
  if (!status.channels || status.channels.length === 0) {
    let msg = "Content script is loaded";
    if (!status.settingsReady) msg += " but still loading settings";
    msg += ". No taps yet — either nobody is speaking nearby or the SpatialChat room hasn't connected.";
    el.innerHTML = '<div>' + escapeHtml(msg) + '</div><div class="meta">last update ' + age + 's ago (recorder: <code>' + escapeHtml(hostLabel) + '</code>)' + (stale ? ' — STALE' : '') + '</div>';
    el.className = "small muted";
    return;
  }
  let h = '<table><thead><tr><th>Speaker</th><th>/tap</th><th>frames</th><th>state</th></tr></thead><tbody>';
  for (const c of status.channels) {
    const who = c.name || c.identity.slice(0, 8);
    const tapPill = wsPill(c.tapWs);
    let stateLabel = "";
    if (c.error) stateLabel = '<span class="pill err">' + escapeHtml(c.error) + '</span>';
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
  await chrome.storage.local.set({ recorderHost: cleanHost, recorderPort: cleanPort });
  $("host").value = cleanHost;
  $("port").value = String(cleanPort);
  currentHost = cleanHost;
  currentPort = cleanPort;
  setSaveStatus("Saved (" + cleanHost + ":" + cleanPort + "). Reload the SpatialChat tab.", "ok");
  await probeAll();
});

$("recheck").addEventListener("click", probeAll);

$("openDash").addEventListener("click", (ev) => {
  ev.preventDefault();
  chrome.tabs.create({ url: "http://" + currentHost + ":" + currentPort + "/" });
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
