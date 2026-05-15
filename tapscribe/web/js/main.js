// TapScribe — operator console.
// Vanilla JS ES module. Polls /api/state every second; full re-render of the
// sessions browser only when something structural changed so user scroll +
// inputs survive across ticks.

import {
  cssEscape,
  escapeHtml,
  fmtBytes,
  fmtClock,
  fmtDur,
  fmtElapsed,
  fmtElapsedShort,
  fmtMs,
  fmtSessionLabel,
  truncMid,
} from "./formatters.js";

const $ = (id) => document.getElementById(id);
const LIVE_MODELS = [
  "tiny.en", "base.en", "small.en", "medium.en",
  "large-v3", "large-v3-turbo",
  "nb-whisper-medium", "nb-whisper-large",
];

// ---- Render state -------------------------------------------------------
let lastJson = null;
const wavInflight = new Map();
const wavJustDone = new Map();
const sessJustDone = new Map();
const MIN_VISIBLE_MS = 600;      // keep the spinner visible at least this long so quick completions are perceptible
const FLASH_MS = 1500;           // how long the green "just done" tint stays on a row/button
const sessInflight = new Map();
const sessStripInflight = new Map();
const sourcePick = new Map();    // session name → "original" | "stripped"
let selectedSessionId = null;    // which session is open (null = pick is_current)
let expandedWav = null;          // "<session>/<name>" expanded inline transcript
let showAudit = true;            // whether to show the suppressed-audit table
const rangeState = {};           // per-session form state (from/to/prompt/hotwords)
let sessionFilter = "";          // sidebar filter query
let batchModel = "small.en";     // dashboard-wide batch transcribe model (Controls box)
const localMeta = {};            // per-session optimistic meta cache (label + aliases)
const metaSaveTimers = new Map();// debounce timers for PUT /api/session-meta
let rxPattern = "";              // regex tester pattern (per-currently-selected-session)
let rxFlags = "i";
let rxOpen = false;
let rxOwnerSession = null;       // which session rxPattern was last typed for

let lastSessionsSig = "";        // structural signature; re-renders sessions only when changed
let lastConfigSig = "";
let lastLiveSig = "";
let liveLogOpen = false;         // persist the "recent log" <details> state across re-renders

  // Aliases: meta.aliases[rawSpeakerName] → display string. Falls back to raw.
  function aliasOf(speaker, aliases) {
    if (!speaker || !aliases) return speaker || "";
    return aliases[speaker] || speaker;
  }

  // Pull effective meta for a session: optimistic local override beats server.
  function effectiveMeta(s) {
    const local = s ? localMeta[s.session] : null;
    const server = (s && s.session_meta) || {};
    return {
      label: local && "label" in local ? local.label : (server.label || ""),
      aliases: local && "aliases" in local ? local.aliases : (server.aliases || {}),
    };
  }

  // Derive the set of speaker keys for which we should show an alias editor.
  // Prefer the merged transcript's speakers list; fall back to per-WAV speaker_name.
  function deriveSpeakerKeys(s) {
    const set = new Set();
    if (s && s.session_transcript && Array.isArray(s.session_transcript.speakers)) {
      for (const sp of s.session_transcript.speakers) if (sp) set.add(sp);
    }
    if (s && Array.isArray(s.files)) {
      for (const f of s.files) if (f.speaker_name) set.add(f.speaker_name);
    }
    return Array.from(set).sort();
  }

  // Group sessions by date relative to "now" (Today / Yesterday / This week / Older).
  function groupSessions(sessions) {
    const now = Date.now();
    const groups = { Today: [], Yesterday: [], "This week": [], Older: [] };
    for (const s of sessions) {
      let day = null;
      const m = /^(\d{4})-(\d{2})-(\d{2})T/.exec(s.session);
      if (m) day = new Date(m[1] + "-" + m[2] + "-" + m[3] + "T00:00:00").getTime();
      if (!day) { groups.Older.push(s); continue; }
      const diff = Math.floor((now - day) / 86400000);
      if (diff <= 0) groups.Today.push(s);
      else if (diff === 1) groups.Yesterday.push(s);
      else if (diff < 7) groups["This week"].push(s);
      else groups.Older.push(s);
    }
    return Object.entries(groups).filter(([, items]) => items.length > 0);
  }

  // Debounced PUT /api/session-meta. The caller passes the FULL meta object
  // (label + aliases); we serialise it as-is. Server returns the persisted
  // shape so the next /api/state poll matches.
  function persistSessionMeta(sessId) {
    const existing = metaSaveTimers.get(sessId);
    if (existing) clearTimeout(existing);
    const t = setTimeout(async () => {
      metaSaveTimers.delete(sessId);
      const meta = localMeta[sessId];
      if (!meta) return;
      try {
        await fetch("/api/session-meta/" + encodeURIComponent(sessId), {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(meta),
        });
      } catch (e) {
        console.error("session-meta save failed", e);
      }
    }, 500);
    metaSaveTimers.set(sessId, t);
  }

  // Stable per-speaker index 0..4 based on speaker name. Used for color.
  const _speakerIdxMap = new Map();
  let _speakerIdxNext = 0;
  function speakerIndex(name) {
    if (!name) return 0;
    if (!_speakerIdxMap.has(name)) {
      _speakerIdxMap.set(name, _speakerIdxNext % 5);
      _speakerIdxNext++;
    }
    return _speakerIdxMap.get(name);
  }

  // ---- Polling ------------------------------------------------------------

  async function fetchState() {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  async function tick() {
    try {
      const j = await fetchState();
      lastJson = j;
      renderRibbonSessionStatus(j);
      renderRecordingPill(j);
      renderLiveChannel(j);
      renderActiveStreams(j);
      renderLiveFeed(j);
      renderConfigIfChanged(j);
      renderSessionsIfChanged(j);
      updateSessionProgressInPlace(j);
      updateWavInflightInPlace();
    } catch (e) {
      $("sessionStatus").innerHTML =
        '<span class="dim tiny">recorder unreachable: ' + escapeHtml(String(e)) + "</span>";
    }
  }

  async function refresh() {
    // Force full re-render of sessions on next tick.
    lastSessionsSig = "";
    lastConfigSig = "";
    await tick();
  }

  // ---- Ribbon: session status ---------------------------------------------

  function renderRibbonSessionStatus(j) {
    const sess = (j.sessions || []).find((s) => s.is_current) || (j.sessions || [])[0];
    let elapsed = null;
    if (sess && sess.earliest_iso) {
      elapsed = Math.max(0, Math.floor((Date.now() - new Date(sess.earliest_iso).getTime()) / 1000));
    }
    const html =
      '<span class="dim tiny">session</span>' +
      ' <span class="fg mono">' + escapeHtml(sess ? sess.session : "—") + "</span>" +
      ' <span class="dim">·</span>' +
      ' <span class="mono tnum">' + fmtElapsed(elapsed) + "</span>" +
      ' <span class="dim">elapsed</span>' +
      ' <span class="dim">·</span>' +
      ' <span class="fg tnum">' + ((sess && sess.wav_count) || 0) + "</span>" +
      ' <span class="dim">wavs</span>';
    $("sessionStatus").innerHTML = html;
  }

  // ---- Recording pill ------------------------------------------------------

  function renderRecordingPill(j) {
    const pill = $("recordingPill");
    if (!pill) return;
    const enabled = j.recording_enabled !== false;  // default-true on older builds
    if (enabled) {
      pill.classList.remove("paused");
      pill.innerHTML = '<span class="dot rec" style="width:7px;height:7px"></span>RECORDING';
      pill.title = "Recording new utterances. Click to pause.";
    } else {
      pill.classList.add("paused");
      pill.innerHTML = '<span class="dot"></span>PAUSED';
      pill.title = "Recording paused — /record WSes are accepted then immediately closed. Click to resume.";
    }
  }

  // ---- Live channel panel --------------------------------------------------

  function renderLiveChannel(j) {
    const li = j.live_info || {};
    const log = j.live_log || [];
    const mlxAvail = !!j.mlx_available;
    const state = li.state || "stopped";

    // Always update the cheap header bits.
    $("liveStateBadge").textContent = state;
    $("liveMlxNote").textContent = mlxAvail ? "mlx available" : "cpu only";

    // Skip the body rebuild when the user is currently editing an input —
    // otherwise we'd close their open <select> dropdown each tick.
    const focused = document.activeElement;
    if (focused && (focused.id === "liveModelSelect" || focused.id === "liveLangInput")) {
      return;
    }

    // Signature gate: skip rebuilding the body when nothing meaningful changed.
    // Without this, every new log line clobbers any open <details> and any
    // unsubmitted form edits.
    const sig = [
      state, li.model || "", li.language || "", li.pid || "", li.host || "",
      li.port || "", li.backend || "", li.device || "", li.last_error || "",
      log.length, log.length ? log[log.length - 1] : "",
    ].join("§");
    if (sig === lastLiveSig) return;
    lastLiveSig = sig;

    const currentModel = li.model || "tiny.en";
    const modelOptions = LIVE_MODELS.slice();
    if (currentModel && !modelOptions.includes(currentModel)) modelOptions.push(currentModel);
    const modelOpts = modelOptions
      .map((m) => '<option value="' + escapeHtml(m) + '"' + (m === currentModel ? " selected" : "") + ">" + escapeHtml(m) + "</option>")
      .join("");

    const running = state === "running" || state === "starting";

    let html = "";
    html +=
      '<div class="live-row">' +
        '<span class="lbl">model</span>' +
        '<select class="select" id="liveModelSelect">' + modelOpts + "</select>" +
      "</div>";
    html +=
      '<div class="live-row">' +
        '<span class="lbl">lang</span>' +
        '<input class="input" id="liveLangInput" value="' + escapeHtml(li.language || "en") + '" size="6">' +
      "</div>";

    html += '<div class="action-row" style="margin-top:6px;">';
    if (running) {
      html += '<button class="btn primary" id="liveApplyBtn" title="Stop and re-spawn WhisperLiveKit with the model/lang above">apply (restart)</button>';
      html += '<button class="btn danger" id="liveStopBtn">stop</button>';
    } else {
      html += '<button class="btn primary" id="liveStartBtn">start</button>';
    }
    html += "</div>";

    html += '<div class="live-meta">';
    html += '<span>port <code>' + escapeHtml(li.port || "?") + "</code></span>";
    html += '<span>backend <code>' + escapeHtml(li.backend || "?") + "</code></span>";
    html += '<span>device <code>' + escapeHtml(li.device || "?") + "</code></span>";
    if (li.pid) html += '<span>pid <code>' + escapeHtml(li.pid) + "</code></span>";
    html += "</div>";

    if (li.last_error) {
      html += '<div class="live-err">' + escapeHtml(li.last_error) + "</div>";
    }
    if (log.length) {
      html +=
        '<details class="live-log"' + (liveLogOpen ? " open" : "") + ">" +
          "<summary>recent log (" + log.length + " line" + (log.length === 1 ? "" : "s") + ")</summary>" +
          "<pre>" + escapeHtml(log.join("\n")) + "</pre>" +
        "</details>";
    }

    $("liveChannelBody").innerHTML = html;

    const startBtn = $("liveStartBtn");
    const applyBtn = $("liveApplyBtn");
    const stopBtn = $("liveStopBtn");
    if (startBtn) startBtn.addEventListener("click", liveStartOrApply);
    if (applyBtn) applyBtn.addEventListener("click", liveStartOrApply);
    if (stopBtn) stopBtn.addEventListener("click", liveStop);
    const det = $("liveChannelBody").querySelector(".live-log");
    if (det) det.addEventListener("toggle", () => { liveLogOpen = det.open; });
    // When user picks an nb-whisper-* model, nudge the language input to "no"
    // if it's still on the boot default ("en" or empty). They can still
    // override it back to anything else.
    const modelSel = $("liveModelSelect");
    if (modelSel) modelSel.addEventListener("change", () => {
      if (!modelSel.value.startsWith("nb-")) return;
      const langInput = $("liveLangInput");
      if (langInput && (langInput.value === "en" || langInput.value === "")) {
        langInput.value = "no";
      }
    });
  }

  async function liveStartOrApply() {
    const modelEl = $("liveModelSelect");
    const langEl = $("liveLangInput");
    const payload = {
      model: modelEl ? modelEl.value : null,
      language: langEl ? langEl.value.trim() : null,
    };
    try {
      const r = await fetch("/api/live/start", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
    } catch (e) {
      alert("Live start/apply failed: " + e);
    }
    await tick();
  }
  async function liveStop() {
    try {
      const r = await fetch("/api/live/stop", { method: "POST" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
    } catch (e) {
      alert("Live stop failed: " + e);
    }
    await tick();
  }

  // ---- Active taps ---------------------------------------------------------

  function renderActiveStreams(j) {
    const list = j.active || [];
    $("activeCount").textContent = String(list.length);
    const badgeEl = $("activeTapsBadge");
    if (list.length === 0) {
      badgeEl.innerHTML = '<span class="dim tiny">idle</span>';
      $("activeTapsBody").innerHTML = '<div class="empty">No taps. Speak in the bridged tab to capture.</div>';
      return;
    }
    badgeEl.innerHTML = '<span class="chip rec" style="padding:1px 6px; font-size:10px;"><span class="dot rec" style="width:6px;height:6px;"></span>capturing</span>';

    let html = "";
    for (const a of list) {
      const dur = (a.bytes_received || 0) / 32000;
      const spk = speakerIndex(a.name || a.identity);
      // Settings default to true when the server didn't include them
      // (older payload, or first-ever sighting of this identity).
      const recOn = a.record !== false;
      const liveOn = a.live !== false;
      const ident = a.identity || "";
      html += '<div class="stream-row">';
      html += '<span class="dot rec" title="receiving"></span>';
      html += '<div class="who">';
      html += '<div class="name"><span data-spk="' + spk + '">●</span> <span class="fg">' + escapeHtml(a.name || "<anon>") + "</span></div>";
      html += '<div class="ident" title="' + escapeHtml(a.filename || "") + '">' + escapeHtml(ident) + " · " + escapeHtml(truncMid(a.filename || "", 30)) + "</div>";
      html += "</div>";
      html += '<div class="tap-toggles">';
      html += '<button class="tap-toggle rec' + (recOn ? " on" : "") + '"'
        + ' data-identity="' + escapeHtml(ident) + '"'
        + ' data-toggle="record"'
        + ' data-state="' + (recOn ? "1" : "0") + '"'
        + ' title="Save this tap to a WAV (applies to next utterance)">rec</button>';
      html += '<button class="tap-toggle live' + (liveOn ? " on" : "") + '"'
        + ' data-identity="' + escapeHtml(ident) + '"'
        + ' data-toggle="live"'
        + ' data-state="' + (liveOn ? "1" : "0") + '"'
        + ' title="Send this tap to the live channel (applies to next utterance)">live</button>';
      html += "</div>";
      html += '<div class="stats">';
      html += '<div><span class="b">' + escapeHtml(fmtBytes(a.bytes_received || 0)) + "</span></div>";
      html += '<div><span class="m">~' + escapeHtml(fmtDur(dur)) + "</span></div>";
      html += "</div>";
      html += "</div>";
    }
    $("activeTapsBody").innerHTML = html;
  }

  async function setTapPref(identity, which, enabled) {
    try {
      const r = await fetch("/api/tap-settings", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ identity, [which]: enabled }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
    } catch (e) {
      alert("Tap setting toggle failed: " + e);
    }
    await tick();
  }

  // ---- Live feed -----------------------------------------------------------

  // Signature of the feed contents — uses the tail entry rather than just
  // length so the renderer keeps updating after the deque saturates at
  // maxlen=200 (length stays 200 but the tail keeps changing).
  let _lastFeedSig = "";
  function renderLiveFeed(j) {
    const feed = j.live_feed || [];
    const shell = $("liveFeedShell");
    $("liveFeedCount").textContent = String(feed.length);

    if (feed.length === 0) {
      shell.innerHTML =
        '<div class="feed-empty">' +
          '<div class="ascii">┌──────────────────────┐\n│   ▁ ▂ ▃ ▄ ▅ ▆ ▇ █    │\n│       awaiting       │\n└──────────────────────┘</div>' +
          "<div>no live transcripts yet</div>" +
          '<div class="dim tiny" style="margin-top:6px;">speak in the bridged tab — settled lines arrive here</div>' +
        "</div>";
      _lastFeedSig = "";
      return;
    }

    let body = shell.querySelector(".feed-body");
    let wasAtBottom = true;
    if (body) {
      wasAtBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 10;
    } else {
      shell.innerHTML = '<div class="feed-body"></div>';
      body = shell.querySelector(".feed-body");
    }

    const tail = feed[feed.length - 1] || {};
    const sig = feed.length + "::" + (tail.ts || "") + "::" + (tail.identity || "") + "::" + (tail.text || "").slice(0, 20);
    if (sig === _lastFeedSig) return;
    _lastFeedSig = sig;

    let html = "";
    for (const e of feed) {
      const hms = (e.ts || "").slice(11, 19);
      const who = e.name || e.identity || "?";
      const spk = speakerIndex(who);
      html += '<div class="line">';
      html += '<span class="ts">[' + escapeHtml(hms) + "]</span>";
      html += '<span class="who" data-i="' + spk + '" title="' + escapeHtml(e.identity || "") + '">' + escapeHtml(who) + "</span>";
      html += '<span class="txt">' + escapeHtml(e.text || "") + "</span>";
      html += "</div>";
    }
    body.innerHTML = html;

    const wantAutoScroll = $("liveAutoScroll").checked;
    if (wantAutoScroll && wasAtBottom) body.scrollTop = body.scrollHeight;
  }

  // ---- Config in effect ----------------------------------------------------

  function renderConfigIfChanged(j) {
    const p = j.prompt || {};
    const h = j.hotwords || {};
    const hl = j.hallucinations || {};
    const sig = [p.length || 0, h.length || 0, hl.count || 0, p.content || "", h.content || "", (hl.rules || []).join("|")].join("§");
    if (sig === lastConfigSig) return;
    lastConfigSig = sig;

    const hotwordList = (h.content || "").split(",").map((s) => s.trim()).filter(Boolean);
    const halRules = hl.rules || [];

    let out = "";
    // initial prompt column
    out += '<div class="cfg-col">';
    out += '<div class="k"><span>initial prompt</span><span class="meta">prompt.txt</span>';
    if (p.length) out += '<span class="meta">· ' + p.length + " chars</span>";
    out += "</div>";
    out += '<div class="v">';
    if (p.length) out += escapeHtml(p.content);
    else out += '<span class="empty">empty — no prose context biasing</span>';
    out += "</div></div>";

    // hotwords column
    out += '<div class="cfg-col">';
    out += '<div class="k"><span>hotwords</span><span class="meta">hotwords.txt</span>';
    if (hotwordList.length) out += '<span class="meta">· ' + hotwordList.length + " terms</span>";
    out += "</div>";
    out += '<div class="v">';
    if (hotwordList.length) out += hotwordList.map((w) => "<code>" + escapeHtml(w) + "</code>").join(" ");
    else out += '<span class="empty">empty — no keyword biasing</span>';
    out += "</div></div>";

    // hallucinations column
    out += '<div class="cfg-col">';
    out += '<div class="k"><span>hallucination filter</span><span class="meta">hallucinations.txt</span>';
    if (halRules.length) out += '<span class="meta">· ' + halRules.length + " rule" + (halRules.length === 1 ? "" : "s") + "</span>";
    out += "</div>";
    out += '<div class="v">';
    if (halRules.length) {
      out += '<div class="dim tiny" style="margin-bottom:4px;">matched segments dropped &amp; stashed in <code style="display:inline;">suppressed_hallucinations</code></div>';
      out += halRules.map((r) => "<code>" + escapeHtml(r) + "</code>").join(" ");
    } else {
      out += '<span class="empty">no patterns — nothing will be suppressed</span>';
    }
    out += "</div></div>";

    $("configGrid").innerHTML = out;
  }

  // ---- Sessions: tabs + detail --------------------------------------------

  function sessionsSignature(sessions) {
    // Cheap signature of structural state that, when changed, should trigger a full re-render.
    return sessions
      .map((s) => {
        const meta = s.session_meta || {};
        const aliasSig = Object.entries(meta.aliases || {}).map(([k, v]) => k + "=" + v).sort().join(";");
        const stripSig = s.stripped ? (s.stripped.count + ":" + s.stripped.stripped_at) : "";
        const srcPick = sourcePick.get(s.session) || "";
        const stripping = sessStripInflight.has(s.session) ? "S" : "";
        return [
          s.session,
          s.is_current ? 1 : 0,
          s.wav_count,
          s.session_transcript ? s.session_transcript.transcribed_at : "",
          (s.files || []).map((f) =>
            f.name
              + ":" + (f.transcript ? f.transcript.transcribed_at : "")
              // include stripped duration AND its transcript stamp so the
              // sub-row's "took X ms" cell refreshes after a transcribe.
              + ":" + (f.stripped ? (f.stripped.duration_s + "/" + (f.stripped.transcript ? f.stripped.transcript.transcribed_at : "")) : "")
          ).join(","),
          meta.label || "",
          aliasSig,
          stripSig,
          srcPick,
          stripping,
        ].join("|");
      })
      .join("§");
  }

  function pickSelectedSession(sessions) {
    if (!sessions.length) return null;
    if (selectedSessionId && sessions.find((s) => s.session === selectedSessionId)) return selectedSessionId;
    const cur = sessions.find((s) => s.is_current);
    return cur ? cur.session : sessions[0].session;
  }

  function renderSessionsIfChanged(j) {
    const sessions = j.sessions || [];
    $("sessCount").textContent = sessions.length + " on disk";
    const sig = sessionsSignature(sessions)
      + "::" + (selectedSessionId || "")
      + "::" + (expandedWav || "")
      + "::" + (showAudit ? "1" : "0")
      + "::" + sessionFilter
      + "::" + (rxOpen ? "1" : "0")
      + "::" + (rxOwnerSession || "")
      + "::" + rxPattern + "::" + rxFlags
      + "::" + batchModel;
    if (sig === lastSessionsSig) return;

    // Don't clobber active text inputs / textareas / selects in the detail
    // pane. Buttons being focused (Chrome focuses on click) must NOT block
    // re-render, otherwise expand/regex-toggle/audit-toggle clicks freeze
    // the UI until the user tabs away.
    const focused = document.activeElement;
    const editing = focused && /^(INPUT|TEXTAREA|SELECT)$/.test(focused.tagName);
    const inDetail = editing && $("sessDetailRoot") && $("sessDetailRoot").contains(focused);
    if (inDetail) {
      return;
    }

    lastSessionsSig = sig;

    if (!sessions.length) {
      $("sessList").innerHTML = '<div class="dim small" style="padding:12px;">No sessions on disk yet.</div>';
      $("sessDetailRoot").innerHTML = "";
      return;
    }

    // Capture in-flight form edits before re-render (mirrored into rangeState).
    captureRangeState();

    const selectedId = pickSelectedSession(sessions);
    if (selectedId !== selectedSessionId) {
      // Reset regex tester when switching session.
      rxOwnerSession = selectedId;
      rxPattern = "";
      rxOpen = false;
    }
    selectedSessionId = selectedId;
    const selected = sessions.find((s) => s.session === selectedId);

    renderSessionSidebar(sessions, selectedId);
    renderSessionDetail(selected);
  }

  function renderSessionSidebar(sessions, selectedId) {
    const q = sessionFilter.trim().toLowerCase();
    const filtered = q
      ? sessions.filter((s) => {
          const meta = effectiveMeta(s);
          return s.session.toLowerCase().includes(q)
            || (meta.label || "").toLowerCase().includes(q);
        })
      : sessions;
    const groups = groupSessions(filtered);

    if (!groups.length) {
      $("sessList").innerHTML = '<div class="dim small" style="padding:12px;">no matches</div>';
      return;
    }

    let html = "";
    for (const [gname, items] of groups) {
      html += '<div class="sess-group-hd">' + escapeHtml(gname) + ' <span class="dim">· ' + items.length + "</span></div>";
      for (const s of items) {
        const meta = effectiveMeta(s);
        const cls = "sess-item" + (s.session === selectedId ? " active" : "") + (s.is_current ? " current" : "");
        const primary = meta.label
          ? escapeHtml(meta.label)
          : '<span class="dim">' + escapeHtml(fmtSessionLabel(s.session)) + "</span>";
        const counter = (s.wav_count || 0) + "w" + (s.stripped ? " · ✂" : "") + (s.session_transcript ? " · tx" : "");
        // The sidebar item is now a <div> (not <button>) because nesting an
        // interactive <button class="del"> inside a <button> is invalid HTML
        // and Chrome's accessibility layer can swallow the inner click.
        html += '<div class="' + cls + '" data-sess-id="' + escapeHtml(s.session) + '" role="button" tabindex="0" title="' + escapeHtml(s.session) + '">';
        html += '<span class="indic"></span>';
        html += '<span>';
        html += '<div class="lbl-1">' + primary + "</div>";
        html += '<div class="lbl-2">' + escapeHtml(s.session) + "</div>";
        html += '</span>';
        html += '<span class="row" style="gap:4px;">';
        html += '<span class="ct">' + escapeHtml(counter) + "</span>";
        if (!s.is_current) {
          html += '<button class="del" data-del-sess="' + escapeHtml(s.session) + '" title="Delete this session folder">×</button>';
        }
        html += '</span>';
        html += "</div>";
      }
    }
    $("sessList").innerHTML = html;
    for (const row of $("sessList").querySelectorAll(".sess-item")) {
      row.addEventListener("click", (e) => {
        // The delete button is a child of sess-item, so its click bubbles up
        // here. Skip selection in that case — the del handler below runs first.
        if (e.target.closest("[data-del-sess]")) return;
        selectedSessionId = row.dataset.sessId;
        lastSessionsSig = "";
        tick();
      });
    }
    for (const del of $("sessList").querySelectorAll("[data-del-sess]")) {
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        e.preventDefault();
        await deleteSession(del.dataset.delSess);
      });
    }
  }

  async function deleteSession(sessId) {
    const sess = (lastJson && lastJson.sessions || []).find((x) => x.session === sessId);
    const wavCount = sess ? sess.wav_count : 0;
    const meta = sess ? effectiveMeta(sess) : { label: "", aliases: {} };
    const label = meta.label || sessId;
    const msg = wavCount > 0
      ? `Delete "${label}" and its ${wavCount} WAV${wavCount === 1 ? "" : "s"}?\n\nThis removes the entire folder from disk. Cannot be undone.`
      : `Delete empty session "${label}"?\n\n(Folder ${sessId})`;
    if (!confirm(msg)) return;
    try {
      const r = await fetch("/api/sessions/" + encodeURIComponent(sessId), { method: "DELETE" });
      if (!r.ok) {
        const j = await r.json().catch(() => ({ detail: r.statusText }));
        // Include the status code so "Not Found" (Starlette generic, route
        // doesn't exist — usually a stale backend) is distinguishable from
        // "session not found" (route exists, folder is missing on disk).
        throw new Error(r.status + " " + (j.detail || r.statusText));
      }
    } catch (e) {
      alert("Delete failed: " + e);
      return;
    }
    // Clear every per-session Map for the gone session so we don't keep
    // references to it. sessInflight/wavInflight self-heal via their async
    // finally blocks, but metaSaveTimers' pending debounced PUT would 404
    // post-delete — cancel it explicitly. wavInflight/wavJustDone keys are
    // prefixed "session/..." so they're swept by prefix scan.
    delete localMeta[sessId];
    delete rangeState[sessId];
    sourcePick.delete(sessId);
    sessStripInflight.delete(sessId);
    sessInflight.delete(sessId);
    sessJustDone.delete(sessId);
    const timer = metaSaveTimers.get(sessId);
    if (timer) clearTimeout(timer);
    metaSaveTimers.delete(sessId);
    for (const k of Array.from(wavInflight.keys())) if (k.startsWith(sessId + "/")) wavInflight.delete(k);
    for (const k of Array.from(wavJustDone.keys())) if (k.startsWith(sessId + "/")) wavJustDone.delete(k);
    if (selectedSessionId === sessId) selectedSessionId = null;
    if (expandedWav && expandedWav.startsWith(sessId + "/")) expandedWav = null;
    await refresh();
  }

  function renderSessionDetail(s) {
    if (!s) {
      $("sessDetailRoot").innerHTML = "";
      return;
    }
    const sessKey = s.session;
    const meta = effectiveMeta(s);
    const aliasKeys = deriveSpeakerKeys(s);
    const sessStartMs = sessInflight.get(sessKey);
    const sessBusy = !!s.progress || sessStartMs != null;
    // Initial render's button content. updateSessionProgressInPlace overrides
    // this once per tick — keep them in sync.
    const sessElapsed = sessStartMs ? fmtElapsedShort((Date.now() - sessStartMs) / 1000) : null;
    let sessBtnInner;
    if (s.progress) {
      const filePart = s.progress.current_file ? " · " + escapeHtml(s.progress.current_file) : "";
      const elapsedPart = sessElapsed ? ' <span class="dim">(' + sessElapsed + ")</span>" : "";
      sessBtnInner = '<span class="spin">⟳</span> transcribing ' + (s.progress.current + 1) + "/" + s.progress.total + filePart + elapsedPart;
    } else if (sessStartMs != null) {
      sessBtnInner = '<span class="spin">⟳</span> transcribing… ' + (sessElapsed || "0:00");
    } else {
      sessBtnInner = s.session_transcript ? "▶ re-transcribe whole session" : "▶ transcribe whole session";
    }
    const rng = rangeState[sessKey] || {};

    let html = '<div style="display:flex; flex-direction:column; min-width:0;">';

    // Editable header row: session name + folder + time-range
    html += '<div class="sess-name-row">';
    html += '<input class="sess-name' + (meta.label ? "" : " unnamed") + '"';
    html += ' data-sess-name="' + escapeHtml(sessKey) + '"';
    html += ' value="' + escapeHtml(meta.label || "") + '"';
    html += ' placeholder="give this session a name…">';
    html += '<span class="sess-folder">' + escapeHtml(sessKey) + "</span>";
    html += '<span class="dim tiny">' + escapeHtml(fmtClock(s.earliest_iso)) + " → " + escapeHtml(fmtClock(s.latest_iso));
    html += " · " + (s.wav_count || 0) + " wavs";
    if (s.is_current) html += ' · <span style="color:var(--rec);">● recording</span>';
    html += "</span>";
    html += "</div>";

    html += '<div class="sess-detail">';

    // Side: controls + aliases + WAVs + regex tester
    html += '<div class="sess-side">';

    // Controls box
    html += '<div class="box"><div class="box-hd">';
    html += "<span>controls</span><span class=\"spacer\"></span>";
    html += '<span class="dim tnum" style="font-size:10px;">' + escapeHtml(fmtClock(s.earliest_iso)) + " → " + escapeHtml(fmtClock(s.latest_iso)) + "</span>";
    html += '</div><div class="box-bd">';
    html += '<div class="ctl-grid">';
    // Batch model picker for this and per-WAV transcribes. Lives here (not the
    // ribbon) because it's a transcribe control. Selection is shared across all
    // sessions for simplicity — change once and it sticks.
    const modelOpts = [
      ["tiny.en", "tiny.en (Whisper, English, fast)"],
      ["small.en", "small.en (Whisper, English)"],
      ["medium.en", "medium.en (Whisper, English, better)"],
      ["large-v3", "large-v3 (Whisper, multilingual incl. Norwegian, slow)"],
      ["nb-whisper-medium", "nb-whisper-medium (NB-AiLab, Norwegian-tuned)"],
      ["nb-whisper-large", "nb-whisper-large (NB-AiLab, Norwegian-tuned, slow)"],
      ["voxtral-mini", "voxtral-mini (Mistral 3B, EN/ES/FR/PT/HI/DE/NL/IT — no Norwegian)"],
    ];
    html += '<span class="lbl">model</span><select class="select" data-model-pick>';
    for (const [v, label] of modelOpts) {
      html += '<option value="' + escapeHtml(v) + '"' + (v === batchModel ? " selected" : "") + ">" + escapeHtml(label) + "</option>";
    }
    html += '</select>';

    // Source picker: originals vs stripped. The stripped option is only
    // available once strip-silence has produced an output folder. If the
    // operator picked "stripped" earlier and then removed the folder, fall
    // back silently to "original" rather than carrying a dead selection.
    const stripped = s.stripped || null;
    const wantSource = sourcePick.get(sessKey) || "original";
    const currentSource = (wantSource === "stripped" && !stripped) ? "original" : wantSource;
    const stripping = sessStripInflight.has(sessKey);
    html += '<span class="lbl">source</span>';
    html += '<div class="src-row" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">';
    html += '<label class="row tiny" style="gap:4px; cursor:pointer;"><input type="radio" name="src-' + escapeHtml(sessKey) + '" data-source-pick="original" data-sess-id="' + escapeHtml(sessKey) + '"' + (currentSource === "original" ? " checked" : "") + '> originals <span class="dim">(' + (s.wav_count || 0) + ')</span></label>';
    if (stripped) {
      html += '<label class="row tiny" style="gap:4px; cursor:pointer;"><input type="radio" name="src-' + escapeHtml(sessKey) + '" data-source-pick="stripped" data-sess-id="' + escapeHtml(sessKey) + '"' + (currentSource === "stripped" ? " checked" : "") + '> stripped <span class="dim">(' + stripped.count + ' · ' + escapeHtml(fmtDur(stripped.speech_seconds)) + ' speech)</span></label>';
    } else {
      html += '<span class="dim tiny" title="Run silence stripping below to enable this source">stripped: <em>none</em></span>';
    }
    html += '</div>';

    // Silence-strip controls. Originals are NEVER touched — output lands in
    // <session>/stripped/ and can be deleted from here too.
    html += '<span class="lbl">silence</span>';
    html += '<div class="silence-ctl" style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">';
    if (stripping) {
      html += '<button class="btn tiny" disabled><span class="spin">⟳</span> stripping…</button>';
    } else if (stripped) {
      html += '<button class="btn tiny" data-strip-run="' + escapeHtml(sessKey) + '" title="Re-run silence stripping on the originals">↻ re-strip</button>';
      html += '<button class="btn tiny ghost" data-strip-remove="' + escapeHtml(sessKey) + '" title="Delete the stripped/ folder (originals are kept)">× remove</button>';
      html += '<span class="dim tiny">stripped ' + escapeHtml(fmtClock(stripped.stripped_at)) + '</span>';
    } else {
      html += '<button class="btn tiny" data-strip-run="' + escapeHtml(sessKey) + '" title="Detect silence and write trimmed copies under stripped/. Originals untouched.">✂ strip silence</button>';
      html += '<span class="dim tiny">copies to stripped/, originals untouched</span>';
    }
    html += '</div>';

    html += '<span class="lbl">from</span><input class="input" data-range-key="from" data-sess-id="' + escapeHtml(sessKey) + '" placeholder="' + escapeHtml(s.earliest_iso || "optional ISO timestamp") + '" value="' + escapeHtml(rng.from || "") + '">';
    html += '<span class="lbl">to</span><input class="input" data-range-key="to" data-sess-id="' + escapeHtml(sessKey) + '" placeholder="' + escapeHtml(s.latest_iso || "optional ISO timestamp") + '" value="' + escapeHtml(rng.to || "") + '">';
    html += '<span class="lbl">prompt</span><textarea class="textarea" rows="2" data-range-key="prompt" data-sess-id="' + escapeHtml(sessKey) + '" placeholder="meeting context — overrides prompt.txt for this job">' + escapeHtml(rng.prompt || "") + "</textarea>";
    html += '<span class="lbl">hotwords</span><input class="input" data-range-key="hotwords" data-sess-id="' + escapeHtml(sessKey) + '" placeholder="e.g. Acme Inc., Patricia Lin" value="' + escapeHtml(rng.hotwords || "") + '">';
    html += "</div>";
    html += '<div class="action-row" style="margin-top:10px;">';
    const sessJustDoneFlag = sessJustDone.has(sessKey);
    html += '<button class="btn ' + (s.session_transcript ? "" : "primary") + (sessJustDoneFlag ? " just-completed" : "") + '" data-tx-sess="' + escapeHtml(sessKey) + '"' + (sessBusy ? " disabled" : "") + ">" + sessBtnInner + "</button>";
    if (s.session_transcript) {
      html += '<button class="btn ghost" data-copy-sess="' + escapeHtml(sessKey) + '">⎘ copy merged</button>';
    }
    html += "</div>";
    html += "</div></div>";

    // Speaker aliases box (shown only when there's at least one identifiable speaker)
    if (aliasKeys.length) {
      html += '<div class="box"><div class="box-hd"><span>speaker aliases</span><span class="spacer"></span><span class="dim tiny">slug → display name</span></div>';
      html += '<div class="box-bd">';
      for (const k of aliasKeys) {
        const v = meta.aliases[k] || "";
        const placeholder = k.replace(/[_-]+/g, " ");
        html += '<div class="alias-row">';
        html += '<code title="' + escapeHtml(k) + '">' + escapeHtml(k) + "</code>";
        html += '<input class="input" data-alias-key="' + escapeHtml(k) + '" data-alias-sess="' + escapeHtml(sessKey) + '" placeholder="' + escapeHtml(placeholder) + '" value="' + escapeHtml(v) + '">';
        html += "</div>";
      }
      html += "</div></div>";
    }

    // WAVs box
    html += '<div class="box"><div class="box-hd"><span>wavs</span><span class="spacer"></span><span class="dim tnum tiny">' + (s.files || []).length + " file" + ((s.files || []).length === 1 ? "" : "s") + "</span></div>";
    html += '<div class="box-bd flush wav-list">';
    if (!(s.files || []).length) {
      html += '<div class="dim small" style="padding:10px;">no wavs yet</div>';
    } else {
      // Inline transcript renderer shared by the original row and the
      // stripped sub-row's expand state.
      const renderInlineTx = (t) => {
        let out = '<div class="expand-tx">';
        out += '<div class="meta">';
        out += '<span>device <span class="fg">' + escapeHtml(t.device || "?") + "</span></span>";
        out += '<span>backend <span class="fg">' + escapeHtml(t.backend || "?") + "</span></span>";
        out += '<span>model <span class="fg">' + escapeHtml(t.model || "?") + "</span></span>";
        out += '<span>lang <span class="fg">' + escapeHtml(t.language || "?") + "</span></span>";
        out += '<span>took <span class="fg">' + escapeHtml(fmtMs(t.transcribe_ms)) + "</span></span>";
        if (t.source) out += '<span>source <span class="fg">' + escapeHtml(t.source) + "</span></span>";
        out += "</div>";
        out += '<div class="body">' + escapeHtml(t.text || "") + "</div>";
        const sup = (t.suppressed_hallucinations || []);
        if (sup.length) {
          out += '<details style="margin-top:6px;"><summary class="dim tiny">' + sup.length + " suppressed segment" + (sup.length === 1 ? "" : "s") + "</summary>";
          out += '<table class="tbl audit-tbl" style="margin-top:4px;"><thead><tr><th>time (s)</th><th>text</th><th>matched rule</th></tr></thead><tbody>';
          for (const it of sup) {
            const start = it.start != null ? Number(it.start).toFixed(2) : "?";
            const end = it.end != null ? Number(it.end).toFixed(2) : "?";
            out += '<tr><td class="muted tnum">' + escapeHtml(start + "–" + end) + '</td><td class="wrap"><code>' + escapeHtml(it.text || "") + '</code></td><td class="muted"><code>' + escapeHtml(it.matched_rule || "") + "</code></td></tr>";
          }
          out += "</tbody></table></details>";
        }
        out += "</div>";
        return out;
      };

      for (const f of s.files) {
        // Original row
        const wavKey = sessKey + "/" + f.name;
        const busy = wavInflight.has(wavKey);
        const open = expandedWav === wavKey;
        const dlHref = "/api/wav/" + encodeURIComponent(sessKey) + "/" + encodeURIComponent(f.name);
        const justDone = wavJustDone.has(wavKey);
        html += '<div class="wav-row' + (busy ? " in-flight" : "") + (justDone ? " just-completed" : "") + '">';
        // Filename click = toggle inline transcript (when one exists). Files
        // without a transcript no-op on click. Download is a separate icon
        // button in the action group on the right.
        html += '<a class="wav-name' + (f.transcript ? " has-tx" : "") + '" href="#" data-toggle-wav="' + escapeHtml(wavKey) + '" title="' + escapeHtml(f.name) + (f.transcript ? "\n\nClick to expand the transcript." : "") + '">' + escapeHtml(truncMid(f.name, 42)) + "</a>";
        html += '<span class="wav-num">' + escapeHtml(fmtDur(f.duration_s)) + "</span>";
        if (busy) {
          // Replace the size/took cell with a live elapsed timer. The actual
          // text gets bumped by updateWavInflightInPlace() each tick.
          const startMs = wavInflight.get(wavKey) || Date.now();
          html += '<span class="wav-num in-flight" data-elapsed-for="' + escapeHtml(wavKey) + '">transcribing… ' + escapeHtml(fmtElapsedShort((Date.now() - startMs) / 1000)) + "</span>";
        } else {
          html += '<span class="wav-num"><span class="m">' + escapeHtml(fmtBytes(f.size));
          if (f.transcript && f.transcript.transcribe_ms != null) {
            html += " · took " + escapeHtml(fmtMs(f.transcript.transcribe_ms));
          }
          html += "</span></span>";
        }
        html += '<span class="row" style="gap:4px;">';
        html += '<a class="btn tiny ghost" href="' + escapeHtml(dlHref) + '" download title="Download WAV">⬇</a>';
        // Each row is explicit about which audio it transcribes — original
        // here, stripped on the sub-row below. Otherwise a session-level
        // sourcePick of "stripped" would route this click to a stripped
        // sibling that doesn't exist (silent originals never get a stripped
        // sibling) and the backend 404s.
        if (busy) {
          html += '<button class="btn tiny" data-tx-wav="' + escapeHtml(wavKey) + '" data-tx-source="original" disabled><span class="spin">⟳</span> transcribing…</button>';
        } else {
          html += '<button class="btn tiny" data-tx-wav="' + escapeHtml(wavKey) + '" data-tx-source="original">' + (f.transcript ? "re-tx" : "transcribe") + "</button>";
        }
        html += "</span>";
        html += "</div>";
        if (open && f.transcript) {
          html += renderInlineTx(f.transcript);
        }

        // Stripped sub-row. Renders only when strip-silence has produced a
        // sibling under <session>/stripped/<same-name>. Its in-flight and
        // expanded state is keyed by `<wavKey>@stripped` so it doesn't
        // collide with the original row.
        if (f.stripped) {
          const stripKey = wavKey + "@stripped";
          const sBusy = wavInflight.has(stripKey);
          const sOpen = expandedWav === stripKey;
          const sJustDone = wavJustDone.has(stripKey);
          const sDl = dlHref + "?source=stripped";
          const sTx = f.stripped.transcript;
          html += '<div class="wav-row strip-sub' + (sBusy ? " in-flight" : "") + (sJustDone ? " just-completed" : "") + '" style="padding-left:18px; opacity:0.85;">';
          html += '<a class="wav-name' + (sTx ? " has-tx" : "") + '" href="#" data-toggle-wav="' + escapeHtml(stripKey) + '" title="' + escapeHtml(f.name) + " (stripped)" + (sTx ? "\n\nClick to expand the transcript." : "") + '"><span class="dim">↳</span> stripped</a>';
          html += '<span class="wav-num">' + escapeHtml(fmtDur(f.stripped.duration_s)) + "</span>";
          if (sBusy) {
            const startMs = wavInflight.get(stripKey) || Date.now();
            html += '<span class="wav-num in-flight" data-elapsed-for="' + escapeHtml(stripKey) + '">transcribing… ' + escapeHtml(fmtElapsedShort((Date.now() - startMs) / 1000)) + "</span>";
          } else {
            html += '<span class="wav-num"><span class="m">' + escapeHtml(fmtBytes(f.stripped.size));
            if (sTx && sTx.transcribe_ms != null) {
              html += " · took " + escapeHtml(fmtMs(sTx.transcribe_ms));
            }
            html += "</span></span>";
          }
          html += '<span class="row" style="gap:4px;">';
          html += '<a class="btn tiny ghost" href="' + escapeHtml(sDl) + '" download title="Download stripped WAV">⬇</a>';
          if (sBusy) {
            html += '<button class="btn tiny" data-tx-wav="' + escapeHtml(wavKey) + '" data-tx-source="stripped" disabled><span class="spin">⟳</span> transcribing…</button>';
          } else {
            html += '<button class="btn tiny" data-tx-wav="' + escapeHtml(wavKey) + '" data-tx-source="stripped">' + (sTx ? "re-tx" : "transcribe") + "</button>";
          }
          html += "</span>";
          html += "</div>";
          if (sOpen && sTx) {
            html += renderInlineTx(sTx);
          }
        }
      }
    }
    html += "</div></div>";  // /wavs box

    // Regex tester (collapsed by default)
    {
      const segs = (s.session_transcript && s.session_transcript.segments) || [];
      const existingRules = (lastJson && lastJson.hallucinations && lastJson.hallucinations.rules) || [];
      html += '<div class="box">';
      html += '<button class="box-hd" data-rx-toggle style="width:100%; background:transparent; border:0; border-bottom:1px solid var(--hairline-2); cursor:pointer; font-family:inherit; text-align:left;">';
      html += "<span>" + (rxOpen ? "▾" : "▸") + " regex tester</span>";
      html += '<span class="spacer"></span>';
      html += '<span class="dim tiny">try a candidate hallucination rule against this session</span>';
      html += "</button>";
      if (rxOpen) {
        html += '<div class="rx-tester">';
        html += '<div class="rx-input">';
        html += '<input class="input" data-rx-pattern placeholder="^thanks for watching" value="' + escapeHtml(rxPattern) + '">';
        html += '<input class="input" data-rx-flags placeholder="flags" value="' + escapeHtml(rxFlags) + '">';
        html += "</div>";
        if (existingRules.length) {
          html += '<div class="rx-seed dim tiny" style="margin-top:6px;">existing rules: ';
          for (const r of existingRules) {
            // Rules are raw strings like "amara.org" or "re:..." or "exact:...". Strip
            // the prefix so the regex tester gets a workable starting point.
            let seed = r;
            const lower = r.toLowerCase();
            if (lower.startsWith("re:")) seed = r.slice(3).trim();
            else if (lower.startsWith("exact:")) seed = "^" + r.slice(6).trim() + "$";
            html += '<code data-rx-seed="' + escapeHtml(seed) + '" title="click to try">' + escapeHtml(r) + "</code>";
          }
          html += "</div>";
        }
        html += '<div class="rx-result">' + renderRegexHits(segs) + "</div>";
        html += "</div>";
      }
      html += "</div>";  // /rx box
    }

    html += "</div>";  // /sess-side

    // Main: merged transcript
    html += '<div class="sess-main">';
    if (s.session_transcript) {
      html += renderMergedTranscript(s.session_transcript, meta);
    } else {
      html += '<div class="box"><div class="box-hd"><span>merged transcript</span><span class="spacer"></span><span class="dim tiny">not yet merged</span></div>';
      html += '<div class="box-bd"><div class="empty" style="padding:12px 0;">No merged transcript yet — run <span class="fg">▶ transcribe whole session</span> to merge all WAVs into one chronological timeline.</div></div></div>';
    }
    html += "</div>";  // /sess-main

    html += "</div>";  // /sess-detail
    html += "</div>";  // /outer name+detail wrapper

    $("sessDetailRoot").innerHTML = html;

    // Wire up buttons
    for (const btn of $("sessDetailRoot").querySelectorAll("[data-tx-sess]")) {
      btn.addEventListener("click", () => transcribeSession(btn.dataset.txSess));
    }
    for (const btn of $("sessDetailRoot").querySelectorAll("[data-copy-sess]")) {
      btn.addEventListener("click", () => copyMerged(btn.dataset.copySess));
    }
    for (const btn of $("sessDetailRoot").querySelectorAll("[data-tx-wav]")) {
      btn.addEventListener("click", (e) => {
        // Immediate visual feedback — the next tick will reskin properly.
        const target = e.currentTarget;
        if (target && !target.disabled) {
          target.disabled = true;
          target.innerHTML = '<span class="spin">⟳</span> transcribing…';
          target.closest(".wav-row")?.classList.add("in-flight");
        }
        const wk = btn.dataset.txWav;
        const sourceOverride = btn.dataset.txSource || null;
        const idx = wk.indexOf("/");
        transcribeWav(wk.slice(0, idx), wk.slice(idx + 1), sourceOverride);
      });
    }
    for (const a of $("sessDetailRoot").querySelectorAll("[data-toggle-wav]")) {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        const wk = a.dataset.toggleWav;
        // Stripped sub-row keys carry an "@stripped" suffix so they don't
        // collide with the original row's expanded/inflight state.
        const stripped = wk.endsWith("@stripped");
        const baseKey = stripped ? wk.slice(0, -"@stripped".length) : wk;
        const f = (s.files || []).find((ff) => sessKey + "/" + ff.name === baseKey);
        if (!f) return;
        const tx = stripped ? (f.stripped && f.stripped.transcript) : f.transcript;
        if (!tx) return;
        expandedWav = expandedWav === wk ? null : wk;
        lastSessionsSig = "";
        tick();
      });
    }
    for (const el of $("sessDetailRoot").querySelectorAll("[data-range-key]")) {
      el.addEventListener("input", () => {
        const sk = el.dataset.sessId;
        const k = el.dataset.rangeKey;
        rangeState[sk] = rangeState[sk] || {};
        rangeState[sk][k] = el.value;
      });
    }

    // Batch model picker — change handler updates the shared global.
    const modelPick = $("sessDetailRoot").querySelector("[data-model-pick]");
    if (modelPick) modelPick.addEventListener("change", () => {
      batchModel = modelPick.value;
    });

    // Source picker — sticks per session. A re-render keeps the checked radio
    // because renderSessionDetail reads from sourcePick before rendering.
    for (const r of $("sessDetailRoot").querySelectorAll("[data-source-pick]")) {
      r.addEventListener("change", () => {
        if (!r.checked) return;
        const sk = r.dataset.sessId;
        sourcePick.set(sk, r.dataset.sourcePick);
      });
    }

    // Strip silence — POST to backend; spinner via sessStripInflight + tick.
    for (const btn of $("sessDetailRoot").querySelectorAll("[data-strip-run]")) {
      btn.addEventListener("click", () => stripSession(btn.dataset.stripRun));
    }
    for (const btn of $("sessDetailRoot").querySelectorAll("[data-strip-remove]")) {
      btn.addEventListener("click", () => removeStripped(btn.dataset.stripRemove));
    }

    // Session name (label) — debounced PUT to /api/session-meta.
    const nameInput = $("sessDetailRoot").querySelector("[data-sess-name]");
    if (nameInput) {
      nameInput.addEventListener("input", () => {
        const sk = nameInput.dataset.sessName;
        const cur = effectiveMeta(s);
        localMeta[sk] = { label: nameInput.value, aliases: cur.aliases || {} };
        // Reflect "unnamed" italic state immediately.
        if (nameInput.value) nameInput.classList.remove("unnamed");
        else nameInput.classList.add("unnamed");
        persistSessionMeta(sk);
      });
    }

    // Speaker aliases — also debounced PUT.
    for (const el of $("sessDetailRoot").querySelectorAll("[data-alias-key]")) {
      el.addEventListener("input", () => {
        const sk = el.dataset.aliasSess;
        const key = el.dataset.aliasKey;
        const cur = effectiveMeta(s);
        const aliases = Object.assign({}, cur.aliases || {});
        if (el.value) aliases[key] = el.value;
        else delete aliases[key];
        localMeta[sk] = { label: cur.label || "", aliases };
        persistSessionMeta(sk);
      });
    }

    // Regex tester
    const rxToggle = $("sessDetailRoot").querySelector("[data-rx-toggle]");
    if (rxToggle) rxToggle.addEventListener("click", () => {
      rxOpen = !rxOpen;
      rxOwnerSession = sessKey;
      lastSessionsSig = "";
      tick();
    });
    const rxPat = $("sessDetailRoot").querySelector("[data-rx-pattern]");
    if (rxPat) rxPat.addEventListener("input", () => {
      rxPattern = rxPat.value;
      rxOwnerSession = sessKey;
      updateRegexResult(s);
    });
    const rxFl = $("sessDetailRoot").querySelector("[data-rx-flags]");
    if (rxFl) rxFl.addEventListener("input", () => {
      rxFlags = rxFl.value;
      rxOwnerSession = sessKey;
      updateRegexResult(s);
    });
    for (const seed of $("sessDetailRoot").querySelectorAll("[data-rx-seed]")) {
      seed.addEventListener("click", () => {
        rxPattern = seed.dataset.rxSeed;
        rxOwnerSession = sessKey;
        const inp = $("sessDetailRoot").querySelector("[data-rx-pattern]");
        if (inp) inp.value = rxPattern;
        updateRegexResult(s);
      });
    }

    const auditBtn = $("sessDetailRoot").querySelector("[data-toggle-audit]");
    if (auditBtn) auditBtn.addEventListener("click", () => {
      showAudit = !showAudit;
      lastSessionsSig = "";
      tick();
    });
  }

  function renderRegexHits(segs) {
    if (!rxPattern) return '<span class="rx-empty">enter a regex to test against ' + segs.length + " segments</span>";
    let rx;
    try { rx = new RegExp(rxPattern, rxFlags); }
    catch (e) { return '<span class="rx-error">' + escapeHtml(String(e.message || e)) + "</span>"; }
    const hits = [];
    for (const seg of segs) if (seg && seg.text && rx.test(seg.text)) hits.push(seg);
    if (!hits.length) return '<span class="rx-empty">no matches in ' + segs.length + " segments</span>";
    let html = '<div class="dim tiny" style="margin-bottom:6px;"><span style="color:var(--warn);">' + hits.length + "</span> match" + (hits.length === 1 ? "" : "es") + " in " + segs.length + " segments</div>";
    for (const h of hits) {
      html += '<span class="rx-hit">' + escapeHtml(h.text || "");
      html += '<span class="ctx">[' + escapeHtml(fmtClock(h.abs_start)) + "] " + escapeHtml(h.speaker || "") + "</span>";
      html += "</span>";
    }
    return html;
  }

  function updateRegexResult(s) {
    // Surgical update: don't re-render the whole detail (would lose input focus).
    const out = $("sessDetailRoot").querySelector(".rx-result");
    if (!out) return;
    const segs = (s.session_transcript && s.session_transcript.segments) || [];
    out.innerHTML = renderRegexHits(segs);
  }

  function renderMergedTranscript(t, meta) {
    // Build a single chronological list of (segment | suppressed-segment) so
    // suppressed lines render inline with strikethrough. abs_hms is no
    // longer on the wire — derive HH:MM:SS from abs_start with fmtClock.
    const items = [];
    for (const seg of t.segments || []) {
      items.push({
        kind: "ok",
        ts: seg.abs_start || "",
        hms: fmtClock(seg.abs_start),
        speaker: seg.speaker || "",
        text: seg.text || "",
        lowConf: !!seg.low_confidence,
        confidence: typeof seg.avg_logprob === "number" ? seg.avg_logprob : null,
      });
    }
    for (const sup of t.suppressed || []) {
      items.push({ kind: "sup", ts: sup.abs_start || "", hms: fmtClock(sup.abs_start), speaker: sup.speaker || "", text: sup.text || "", rule: sup.matched_rule || "" });
    }
    items.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));

    const speakers = t.speakers || [];
    const aliases = (meta && meta.aliases) || {};
    function spkClass(rawSpeaker) {
      const i = speakers.indexOf(rawSpeaker);
      return "who-" + (i >= 0 ? i % 5 : 0);
    }

    const lowCount = typeof t.low_confidence_count === "number"
      ? t.low_confidence_count
      : items.filter((it) => it.lowConf).length;

    let html = '<div class="box">';
    html += '<div class="box-hd"><span>merged transcript</span><span class="spacer"></span>';
    html += '<span class="dim tnum tiny">' + (t.wav_count || 0) + " wavs · " + (t.segments || []).length + " seg · took " + escapeHtml(fmtMs(t.transcribe_ms)) + " · model " + escapeHtml(t.model || "?") + "</span></div>";
    html += '<div class="box-bd" style="padding:10px;">';

    // Speaking-time bar — speaking_seconds is now a dict keyed by speaker
    // (replaces the prior parallel arrays). Look up each speaker's seconds
    // by name; defaults to 0 if the speaker didn't actually contribute.
    const speakingByName = (t.speaking_seconds && typeof t.speaking_seconds === "object" && !Array.isArray(t.speaking_seconds))
      ? t.speaking_seconds
      : {};
    if (speakers.length && Object.keys(speakingByName).length) {
      const totalRaw = speakers.reduce((acc, name) => acc + (speakingByName[name] || 0), 0);
      const total = totalRaw || 1;
      html += '<div class="spk-bar">';
      for (let i = 0; i < speakers.length; i++) {
        const sec = speakingByName[speakers[i]] || 0;
        const pct = ((sec / total) * 100).toFixed(2);
        const display = aliasOf(speakers[i], aliases);
        html += '<span data-spk="' + (i % 5) + '" style="width:' + pct + '%;" title="' + escapeHtml(display) + " · " + escapeHtml(fmtDur(sec)) + '"></span>';
      }
      html += "</div>";
      html += '<div class="spk-legend">';
      for (let i = 0; i < speakers.length; i++) {
        const sec = speakingByName[speakers[i]] || 0;
        const pct = totalRaw > 0 ? ((sec / total) * 100).toFixed(0) : "0";
        const display = aliasOf(speakers[i], aliases);
        html += "<span>";
        html += '<span class="sw" data-spk="' + (i % 5) + '"></span>';
        html += '<span data-spk="' + (i % 5) + '">' + escapeHtml(display) + "</span>";
        html += '<span class="pct">' + pct + "%</span>";
        html += "</span>";
      }
      html += "</div>";
    }

    // Meta strip
    html += '<div class="dim tiny" style="margin-top:6px;">';
    html += "merged at <span class=\"fg\">" + escapeHtml(fmtClock(t.transcribed_at)) + "</span>";
    html += " · via <span class=\"fg\">" + escapeHtml(t.transcriber || t.backend || "faster-whisper") + "</span>";
    html += " on <span class=\"fg\">" + escapeHtml(t.device || "CPU") + "</span>";
    if (lowCount > 0) {
      html += ' · <span style="color:var(--warn);">' + lowCount + " low-confidence</span>";
    }
    if (t.suppressed_count > 0) {
      html += ' · <span style="color:var(--rec);">' + t.suppressed_count + " suppressed</span>";
    }
    html += "</div>";

    html += "</div>";  // /box-bd
    html += '<div style="border-top:1px solid var(--hairline-2);">';

    // Inline transcript
    html += '<div class="transcript" style="border:0; border-radius:0; max-height:380px;">';
    for (const it of items) {
      const displaySpk = aliasOf(it.speaker, aliases);
      html += "<div>";
      html += '<span class="ts">[' + escapeHtml(it.hms) + "]</span> ";
      html += '<span class="' + spkClass(it.speaker) + '">' + escapeHtml(displaySpk) + ":</span> ";
      if (it.kind === "sup") {
        html += '<span class="seg suppressed" title="suppressed · matched: ' + escapeHtml(it.rule) + '">' + escapeHtml(it.text) + "</span>";
      } else if (it.lowConf) {
        const confLabel = it.confidence != null ? it.confidence.toFixed(2) : "?";
        // Show a "≈XX%" pseudo-percent from the avg_logprob: exp(avg_logprob)
        // is the geometric mean per-token probability; reasonable proxy.
        const pct = it.confidence != null ? (Math.exp(it.confidence) * 100).toFixed(0) : "?";
        html += '<span class="seg lowconf" title="low confidence · avg_logprob ' + confLabel + '">' + escapeHtml(it.text);
        html += '<span class="conf-chip">⚑ ' + pct + "%</span></span>";
      } else {
        html += "<span>" + escapeHtml(it.text) + "</span>";
      }
      html += "</div>";
    }
    html += "</div>";

    // Audit table (collapsible)
    if (t.suppressed_count > 0 && Array.isArray(t.suppressed)) {
      html += '<div style="border-top:1px solid var(--hairline-2);">';
      html += '<button class="btn ghost tiny" data-toggle-audit style="width:100%; border:0; border-radius:0; justify-content:flex-start; padding:8px 12px; color:var(--fg-2);">';
      html += (showAudit ? "▾" : "▸") + " hallucination audit · " + t.suppressed_count + " segment" + (t.suppressed_count === 1 ? "" : "s") + " dropped";
      html += "</button>";
      if (showAudit) {
        html += '<table class="tbl audit-tbl" style="border-top:1px solid var(--hairline-2);"><thead><tr><th>time</th><th>speaker</th><th>text</th><th>matched rule</th><th>from</th></tr></thead><tbody>';
        for (const sup of t.suppressed) {
          html += "<tr>";
          html += '<td class="muted tnum">' + escapeHtml(fmtClock(sup.abs_start)) + "</td>";
          html += "<td>" + escapeHtml(sup.speaker || "") + "</td>";
          html += '<td class="wrap"><code>' + escapeHtml(sup.text || "") + "</code></td>";
          html += '<td class="muted"><code>' + escapeHtml(sup.matched_rule || "") + "</code></td>";
          html += '<td class="muted">' + escapeHtml(truncMid(sup.source_wav || "", 28)) + "</td>";
          html += "</tr>";
        }
        html += "</tbody></table>";
      }
      html += "</div>";
    }

    html += "</div></div>";  // /transcript+audit wrapper, /box
    return html;
  }

  function captureRangeState() {
    for (const el of document.querySelectorAll("[data-range-key]")) {
      const sk = el.dataset.sessId;
      const k = el.dataset.rangeKey;
      rangeState[sk] = rangeState[sk] || {};
      rangeState[sk][k] = el.value;
    }
  }

  // Lightweight per-tick update: bump the elapsed timer on each in-flight
  // wav row's status cell, surgically (no full re-render).
  function updateWavInflightInPlace() {
    const now = Date.now();
    for (const [key, startMs] of wavInflight) {
      const cell = document.querySelector('[data-elapsed-for="' + cssEscape(key) + '"]');
      if (!cell) continue;
      cell.textContent = "transcribing… " + fmtElapsedShort((now - startMs) / 1000);
    }
  }

  // Lightweight per-tick update: refresh the session-transcribe button's
  // label, busy state, and elapsed timer — surgically (no full re-render).
  function updateSessionProgressInPlace(j) {
    if (!j.sessions) return;
    const now = Date.now();
    for (const s of j.sessions) {
      const btn = document.querySelector('[data-tx-sess="' + cssEscape(s.session) + '"]');
      if (!btn) continue;
      const startMs = sessInflight.get(s.session);
      const elapsed = startMs ? fmtElapsedShort((now - startMs) / 1000) : null;
      let inner, busy;
      if (s.progress) {
        const filePart = s.progress.current_file ? " · " + escapeHtml(s.progress.current_file) : "";
        const elapsedPart = elapsed ? ' <span class="dim">(' + elapsed + ")</span>" : "";
        inner = '<span class="spin">⟳</span> transcribing ' + (s.progress.current + 1) + "/" + s.progress.total + filePart + elapsedPart;
        busy = true;
      } else if (startMs != null) {
        inner = '<span class="spin">⟳</span> transcribing… ' + (elapsed || "0:00");
        busy = true;
      } else {
        inner = (s.session_transcript ? "▶ re-transcribe whole session" : "▶ transcribe whole session");
        busy = false;
      }
      btn.innerHTML = inner;
      btn.disabled = busy;
    }
  }

  // ---- Mutations ----------------------------------------------------------

  async function transcribeWav(session, name, sourceOverride) {
    // sourceOverride wins over the per-session source pick. Used by the
    // stripped sub-row's own "transcribe" button so the user can transcribe
    // both sources for the same recording without flipping the radio.
    const s = lastJson && (lastJson.sessions || []).find((x) => x.session === session);
    let source;
    if (sourceOverride) {
      source = sourceOverride;
    } else {
      const wantSource = sourcePick.get(session) || "original";
      source = (wantSource === "stripped" && !(s && s.stripped)) ? "original" : wantSource;
    }
    // Key inflight/justDone state by source so the original and stripped
    // sub-rows can each show a spinner without colliding.
    const key = source === "stripped"
      ? session + "/" + name + "@stripped"
      : session + "/" + name;
    if (wavInflight.has(key)) return;
    const startMs = Date.now();
    wavInflight.set(key, startMs);
    lastSessionsSig = "";
    tick();
    const model = batchModel;
    let failed = false;
    try {
      const r = await fetch("/api/transcribe", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ session, name, model, source }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
    } catch (e) {
      failed = true;
      alert("Transcribe failed: " + e);
    } finally {
      // Hold the spinner visible for at least MIN_VISIBLE_MS so genuinely
      // quick completions don't flash imperceptibly.
      const elapsed = Date.now() - startMs;
      if (elapsed < MIN_VISIBLE_MS) {
        await new Promise((r) => setTimeout(r, MIN_VISIBLE_MS - elapsed));
      }
      wavInflight.delete(key);
      if (!failed) {
        wavJustDone.set(key, Date.now() + FLASH_MS);
        setTimeout(() => {
          wavJustDone.delete(key);
          lastSessionsSig = "";
          tick();
        }, FLASH_MS + 100);
      }
      await refresh();
    }
  }

  async function transcribeSession(session) {
    if (sessInflight.has(session)) return;
    captureRangeState();
    sessInflight.set(session, Date.now());
    if (lastJson) updateSessionProgressInPlace(lastJson);
    const model = batchModel;
    const rng = rangeState[session] || {};
    // When the user clicks "re-transcribe whole session" (i.e., a merged
    // transcript already exists), they clearly mean "do the work again". The
    // recorder's per-WAV JSON cache would otherwise return the existing
    // result in milliseconds. First-time "transcribe whole session" runs let
    // the cache work, which is what makes incremental per-WAV transcribe
    // followed by session-merge fast.
    const s = lastJson && (lastJson.sessions || []).find((x) => x.session === session);
    const force = !!(s && s.session_transcript);
    const wantSource = sourcePick.get(session) || "original";
    const source = (wantSource === "stripped" && !(s && s.stripped)) ? "original" : wantSource;
    const payload = {
      session,
      model,
      from_iso: (rng.from || "").trim(),
      to_iso: (rng.to || "").trim(),
      prompt: rng.prompt || "",
      hotwords: rng.hotwords || "",
      source,
      force,
    };
    const startMs = Date.now();
    let failed = false;
    try {
      const r = await fetch("/api/transcribe-session", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
    } catch (e) {
      failed = true;
      alert("Session transcribe failed: " + e);
    } finally {
      const elapsed = Date.now() - startMs;
      if (elapsed < MIN_VISIBLE_MS) {
        await new Promise((r) => setTimeout(r, MIN_VISIBLE_MS - elapsed));
      }
      sessInflight.delete(session);
      if (!failed) {
        sessJustDone.set(session, Date.now() + FLASH_MS);
        setTimeout(() => {
          sessJustDone.delete(session);
          lastSessionsSig = "";
          tick();
        }, FLASH_MS + 100);
      }
      await refresh();
    }
  }

  async function stripSession(session) {
    if (sessStripInflight.has(session)) return;
    sessStripInflight.set(session, Date.now());
    lastSessionsSig = "";
    tick();
    const startMs = Date.now();
    let failed = false;
    let summary = null;
    try {
      const r = await fetch("/api/sessions/" + encodeURIComponent(session) + "/strip-silence", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
      summary = j;
    } catch (e) {
      failed = true;
      alert("Strip silence failed: " + e);
    } finally {
      const elapsed = Date.now() - startMs;
      if (elapsed < MIN_VISIBLE_MS) {
        await new Promise((r) => setTimeout(r, MIN_VISIBLE_MS - elapsed));
      }
      sessStripInflight.delete(session);
      // Auto-flip source to stripped on success so the user can immediately
      // transcribe the cleaned audio without clicking the radio. Only flip
      // when files actually got written — a session of all-silent originals
      // produces files_written=0 and no stripped/ folder, so flipping would
      // be a stale selection (re-renders would fall back to "original"
      // anyway, but better not to set it).
      if (!failed && summary && (summary.files_written || 0) > 0) {
        sourcePick.set(session, "stripped");
      }
      lastSessionsSig = "";
      await refresh();
    }
    if (summary) {
      const pct = summary.in_seconds > 0 ? Math.round(100 * summary.speech_seconds / summary.in_seconds) : 0;
      console.log("[strip-silence] " + session + ":", summary);
      // A small toast-style alert is louder than ideal but matches the prune
      // handler's pattern and gives a clear confirmation that something
      // actually happened.
      alert("Stripped " + summary.files_written + "/" + summary.files_processed + " WAVs · "
        + Math.round(summary.speech_seconds) + "s speech of " + Math.round(summary.in_seconds) + "s ("
        + pct + "%) · detector " + (Array.isArray(summary.detector) ? summary.detector.join(", ") : summary.detector));
    }
  }

  async function removeStripped(session) {
    if (!confirm("Delete the stripped/ folder for this session?\n\nOriginals are kept. You can rerun strip silence later.")) return;
    try {
      const r = await fetch("/api/sessions/" + encodeURIComponent(session) + "/stripped", {
        method: "DELETE",
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
    } catch (e) {
      alert("Remove stripped failed: " + e);
      return;
    }
    // Reset the source pick so we don't ask for a folder that's gone.
    if (sourcePick.get(session) === "stripped") sourcePick.delete(session);
    lastSessionsSig = "";
    await refresh();
  }

  async function copyMerged(session) {
    if (!lastJson) return;
    const s = lastJson.sessions.find((x) => x.session === session);
    if (!s || !s.session_transcript || !s.session_transcript.plain_text) {
      alert("No merged transcript yet for this session.");
      return;
    }
    try {
      await navigator.clipboard.writeText(s.session_transcript.plain_text);
    } catch (e) {
      const w = window.open("", "_blank");
      if (w) {
        w.document.body.style.font = "12px ui-monospace, Menlo, Consolas, monospace";
        w.document.body.style.whiteSpace = "pre-wrap";
        w.document.body.textContent = s.session_transcript.plain_text;
      } else {
        alert("Copy failed (clipboard blocked).");
      }
    }
  }

  // ---- Top-bar actions ----------------------------------------------------

  $("refreshBtn").addEventListener("click", () => { refresh(); });

  $("newSessionBtn").addEventListener("click", async () => {
    if (!confirm("Start a new recording session?\n\nWAVs from new utterances will land in a fresh folder. In-progress utterances finish in their current folder.")) return;
    try {
      const r = await fetch("/api/new-session", { method: "POST" });
      if (!r.ok) throw new Error(r.status);
      await refresh();
    } catch (e) {
      alert("Failed to start new session: " + e);
    }
  });

  $("liveClearBtn").addEventListener("click", async () => {
    await fetch("/api/live-transcript", { method: "DELETE" });
    await tick();
  });

  // Delegated click for the per-tap rec/live toggles. The body re-renders
  // every tick, so binding once on the panel survives all re-renders.
  // data-state is the CURRENT value; we PUT the inverse. We flip the
  // visual state immediately so the click feels responsive — the poll
  // tick after setTapPref() will re-paint from the authoritative state.
  $("activeTapsBody").addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".tap-toggle");
    if (!btn) return;
    if (btn.disabled) return;
    const identity = btn.dataset.identity;
    const which = btn.dataset.toggle;
    if (!identity || !which) return;
    const next = btn.dataset.state !== "1";
    btn.dataset.state = next ? "1" : "0";
    btn.classList.toggle("on", next);
    btn.disabled = true;
    try {
      await setTapPref(identity, which, next);
    } finally {
      btn.disabled = false;
    }
  });

  // Toggle recording (pause / resume). Pass explicit `enabled` so a
  // simultaneous click in another tab doesn't desync us.
  $("recordingPill").addEventListener("click", async () => {
    const currentlyEnabled = !lastJson || lastJson.recording_enabled !== false;
    try {
      const r = await fetch("/api/recording/toggle", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ enabled: !currentlyEnabled }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
    } catch (e) {
      alert("Recording toggle failed: " + e);
    }
    await tick();
  });

  // Bulk-delete every session that has zero WAVs, no merged transcript,
  // and no label — the leftovers from "recorder was running between
  // meetings" days. Confirms first.
  $("pruneEmptyBtn").addEventListener("click", async () => {
    if (!confirm("Delete every session that has 0 WAVs, no merged transcript, and no label?\n\nThe current session is always kept. Cannot be undone.")) return;
    try {
      const r = await fetch("/api/sessions/prune-empty", { method: "POST" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
      // Reset local caches for the gone sessions so nothing references them.
      // Symmetric with deleteSession's cleanup — see the comment there.
      for (const id of j.pruned || []) {
        delete localMeta[id];
        delete rangeState[id];
        sourcePick.delete(id);
        sessStripInflight.delete(id);
        sessInflight.delete(id);
        sessJustDone.delete(id);
        const timer = metaSaveTimers.get(id);
        if (timer) clearTimeout(timer);
        metaSaveTimers.delete(id);
        for (const k of Array.from(wavInflight.keys())) if (k.startsWith(id + "/")) wavInflight.delete(k);
        for (const k of Array.from(wavJustDone.keys())) if (k.startsWith(id + "/")) wavJustDone.delete(k);
        if (selectedSessionId === id) selectedSessionId = null;
        if (expandedWav && expandedWav.startsWith(id + "/")) expandedWav = null;
      }
      await refresh();
      alert("Removed " + (j.count || 0) + " empty session" + ((j.count || 0) === 1 ? "" : "s") + ".");
    } catch (e) {
      alert("Clear empty failed: " + e);
    }
  });

  // Sidebar filter — bound once on boot; input is static in the HTML shell.
  // Triggering a re-render of the sidebar (via signature reset) is enough.
  $("sessFilter").addEventListener("input", () => {
    sessionFilter = $("sessFilter").value || "";
    lastSessionsSig = "";
    if (lastJson) renderSessionsIfChanged(lastJson);
  });

// ---- Boot ---------------------------------------------------------------

tick();
// 500ms is the sweet spot — fast enough that "active taps" / live-channel
// state badge changes feel near-instant, slow enough that /api/state isn't
// doing meaningful CPU work between ticks. Most renders short-circuit on
// unchanged signatures, so we're not actually re-painting twice per second.
setInterval(tick, 500);
