// TapScribe — operator console.
// Vanilla JS ES module. Polls /api/state every second; full re-render of the
// sessions browser only when something structural changed so user scroll +
// inputs survive across ticks.

import { cssEscape, fmtClock } from "./formatters.js";
import { fetchState, postJson, putJson, del } from "./api.js";
import { loadTemplates } from "./templates.js";
import { aliasOf } from "./speakers.js";
import * as liveFeed from "./components/live-feed.js";
import * as activeTaps from "./components/active-taps.js";
import * as liveChannel from "./components/live-channel.js";
import * as configCard from "./components/config-card.js";
import * as ribbon from "./components/ribbon.js";
import * as mergedTranscript from "./components/merged-transcript.js";
import * as sessionSidebar from "./components/session-sidebar.js";
import * as sessionDetail from "./components/session-detail.js";

// Convenience: run an async op behind an alert("X failed: …") wrapper and
// then re-poll. Returns whether the op succeeded.
async function tryThen(label, op, after = tick) {
  try { await op(); }
  catch (e) { alert(`${label} failed: ${e}`); return false; }
  finally { await after(); }
  return true;
}

// Spinner + min-visible + just-done dance shared by transcribeWav,
// transcribeSession, and stripSession. Marks `key` busy in `inflight`,
// guarantees the spinner stays visible at least MIN_VISIBLE_MS, then sets
// `done.set(key, …)` (with FLASH_MS auto-clear) when `op()` resolves cleanly.
async function withInflight(inflight, done, key, label, op) {
  if (inflight.has(key)) return;
  const startMs = Date.now();
  inflight.set(key, startMs);
  lastSessionsSig = "";
  tick();
  let failed = false;
  try { await op(); }
  catch (e) { failed = true; alert(`${label} failed: ${e}`); }
  finally {
    const held = Date.now() - startMs;
    if (held < MIN_VISIBLE_MS) await new Promise((r) => setTimeout(r, MIN_VISIBLE_MS - held));
    inflight.delete(key);
    if (!failed && done) {
      done.set(key, Date.now() + FLASH_MS);
      setTimeout(() => { done.delete(key); lastSessionsSig = ""; tick(); }, FLASH_MS + 100);
    }
    await refresh();
  }
  return !failed;
}

const $ = (id) => document.getElementById(id);

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

  // Debounced PUT /api/session-meta. The caller passes the FULL meta object
  // (label + aliases); we serialise it as-is.
  function persistSessionMeta(sessId) {
    clearTimeout(metaSaveTimers.get(sessId));
    metaSaveTimers.set(sessId, setTimeout(async () => {
      metaSaveTimers.delete(sessId);
      const meta = localMeta[sessId];
      if (!meta) return;
      try { await putJson(`/api/session-meta/${encodeURIComponent(sessId)}`, meta); }
      catch (e) { console.error("session-meta save failed", e); }
    }, 500));
  }

  // ---- Polling ------------------------------------------------------------

  // DOM handles for the components — looked up once at boot, not per-tick.
  let liveFeedCtx, activeTapsCtx, liveChannelCtx, configCardCtx, ribbonCtx;
  function initComponentCtx() {
    liveFeedCtx = {
      countEl: $("liveFeedCount"),
      shell: $("liveFeedShell"),
      autoscrollEl: $("liveAutoScroll"),
    };
    activeTapsCtx = {
      countEl: $("activeCount"),
      badgeEl: $("activeTapsBadge"),
      bodyEl: $("activeTapsBody"),
    };
    liveChannelCtx = {
      stateEl: $("liveStateBadge"),
      mlxEl: $("liveMlxNote"),
      bodyEl: $("liveChannelBody"),
      onAction: { start: liveStartOrApply, stop: liveStop },
    };
    configCardCtx = { gridEl: $("configGrid") };
    ribbonCtx = { statusEl: $("sessionStatus"), pillEl: $("recordingPill") };
  }

  async function tick() {
    try {
      const j = await fetchState();
      lastJson = j;
      ribbon.renderStatus(j, ribbonCtx);
      ribbon.renderRecPill(ribbonCtx, j.recording_enabled !== false);
      liveChannel.render(j, { ...liveChannelCtx, mlxAvail: !!j.mlx_available });
      activeTaps.render(j, activeTapsCtx);
      liveFeed.render(j, liveFeedCtx);
      configCard.render(j, configCardCtx);
      renderSessionsIfChanged(j);
      updateSessionProgressInPlace(j);
      updateWavInflightInPlace();
    } catch (e) {
      ribbon.renderError(ribbonCtx.statusEl, String(e));
    }
  }

  async function refresh() {
    lastSessionsSig = "";
    configCard.invalidate();
    await tick();
  }

  // ---- Live channel mutations ---------------------------------------------

  function liveStartOrApply() {
    return tryThen("Live start/apply", () => postJson("/api/live/start", liveChannel.formValues()));
  }
  const liveStop = () => tryThen("Live stop", () => postJson("/api/live/stop"));
  const setTapPref = (identity, which, enabled) =>
    tryThen("Tap setting toggle", () => putJson("/api/tap-settings", { identity, [which]: enabled }));

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
      const empty = document.createElement("div");
      empty.className = "dim small";
      empty.style.padding = "12px";
      empty.textContent = "No sessions on disk yet.";
      $("sessList").replaceChildren(empty);
      $("sessDetailRoot").replaceChildren();
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
    sessionSidebar.render(sessions, {
      listEl: $("sessList"),
      selectedId,
      filter: sessionFilter,
      metaFor: effectiveMeta,
      onSelect: (id) => { selectedSessionId = id; lastSessionsSig = ""; tick(); },
      onDelete: (id) => deleteSession(id),
    });
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
    try { await del(`/api/sessions/${encodeURIComponent(sessId)}`); }
    catch (e) { alert(`Delete failed: ${e}`); return; }
    forgetSession(sessId);
    await refresh();
  }

  // Drop every per-session cache for a gone session. Without this, debounced
  // /api/session-meta PUTs would 404 after delete and stale wav-inflight keys
  // would leak. wavInflight/wavJustDone keys are prefixed "session/…" so we
  // sweep them by prefix.
  function forgetSession(sessId) {
    delete localMeta[sessId];
    delete rangeState[sessId];
    sourcePick.delete(sessId);
    sessStripInflight.delete(sessId);
    sessInflight.delete(sessId);
    sessJustDone.delete(sessId);
    clearTimeout(metaSaveTimers.get(sessId));
    metaSaveTimers.delete(sessId);
    const prefix = `${sessId}/`;
    for (const k of wavInflight.keys()) if (k.startsWith(prefix)) wavInflight.delete(k);
    for (const k of wavJustDone.keys()) if (k.startsWith(prefix)) wavJustDone.delete(k);
    if (selectedSessionId === sessId) selectedSessionId = null;
    if (expandedWav?.startsWith(prefix)) expandedWav = null;
  }

  // Per-session detail render — delegates to the session-detail component
  // with all the state + callbacks it needs to render and wire events.
  function renderSessionDetail(s) {
    sessionDetail.render(s, $("sessDetailRoot"), {
      // state
      lastJson,
      batchModel,
      sourcePick,
      sessInflight,
      sessJustDone,
      sessStripInflight,
      wavInflight,
      wavJustDone,
      expandedWav,
      rangeState,
      rxOpen,
      rxPattern,
      rxFlags,
      effectiveMeta,
      deriveSpeakerKeys,
      // sub-component
      renderMerged: (t, meta) => mergedTranscript.render(t, meta, { showAudit }),
      // callbacks
      onTranscribeSession: transcribeSession,
      onCopyMerged: copyMerged,
      onTranscribeWav: transcribeWav,
      onToggleWav: (wk, sess) => {
        // Stripped sub-row keys carry "@stripped" so they don't collide.
        const stripped = wk.endsWith("@stripped");
        const baseKey = stripped ? wk.slice(0, -"@stripped".length) : wk;
        const f = (sess.files || []).find((ff) => sess.session + "/" + ff.name === baseKey);
        const tx = stripped ? f?.stripped?.transcript : f?.transcript;
        if (!tx) return;
        expandedWav = expandedWav === wk ? null : wk;
        lastSessionsSig = "";
        tick();
      },
      onRangeEdit: (sk, k, v) => {
        rangeState[sk] = rangeState[sk] || {};
        rangeState[sk][k] = v;
      },
      onModelChange: (v) => { batchModel = v; },
      onSourcePick: (sk, v) => { sourcePick.set(sk, v); },
      onStripRun: stripSession,
      onStripRemove: removeStripped,
      onNameEdit: (sk, value) => {
        const cur = effectiveMeta(s);
        localMeta[sk] = { label: value, aliases: cur.aliases || {} };
        persistSessionMeta(sk);
      },
      onAliasEdit: (sk, key, value) => {
        const cur = effectiveMeta(s);
        const aliases = { ...(cur.aliases || {}) };
        if (value) aliases[key] = value;
        else delete aliases[key];
        localMeta[sk] = { label: cur.label || "", aliases };
        persistSessionMeta(sk);
      },
      onRxToggle: (sk) => { rxOpen = !rxOpen; rxOwnerSession = sk; lastSessionsSig = ""; tick(); },
      onRxPatternInput: (sk, v) => { rxPattern = v; rxOwnerSession = sk; updateRegexResult(s); },
      onRxFlagsInput: (sk, v) => { rxFlags = v; rxOwnerSession = sk; updateRegexResult(s); },
      onRxSeed: (sk, seed) => {
        rxPattern = seed;
        rxOwnerSession = sk;
        const inp = $("sessDetailRoot").querySelector("[data-rx-pattern]");
        if (inp) inp.value = seed;
        updateRegexResult(s);
      },
      onAuditToggle: () => { showAudit = !showAudit; lastSessionsSig = ""; tick(); },
    });
  }


  function updateRegexResult(s) {
    // Surgical update: don't re-render the whole detail (would lose input focus).
    const out = $("sessDetailRoot").querySelector(".rx-result");
    if (!out) return;
    const segs = s.session_transcript?.segments || [];
    out.replaceChildren(sessionDetail.renderRegexHits(segs, { rxPattern, rxFlags }));
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
    for (const s of j.sessions) {
      const btn = document.querySelector(`[data-tx-sess="${cssEscape(s.session)}"]`);
      if (!btn) continue;
      const { node, busy } = sessionDetail.sessionProgressInner(s, sessInflight);
      btn.replaceChildren(node);
      btn.disabled = busy;
    }
  }

  // ---- Mutations ----------------------------------------------------------

  // Resolve the effective source for a session: a stripped pick falls back
  // to "original" when no stripped/ folder exists.
  function effectiveSource(session, override) {
    if (override) return override;
    const s = lastJson?.sessions?.find((x) => x.session === session);
    const want = sourcePick.get(session) || "original";
    return (want === "stripped" && !s?.stripped) ? "original" : want;
  }

  function transcribeWav(session, name, sourceOverride) {
    const source = effectiveSource(session, sourceOverride);
    // Key inflight/justDone by source so original and stripped sub-rows can
    // each show a spinner without colliding.
    const key = `${session}/${name}${source === "stripped" ? "@stripped" : ""}`;
    return withInflight(wavInflight, wavJustDone, key, "Transcribe",
      () => postJson("/api/transcribe", { session, name, model: batchModel, source }));
  }

  async function transcribeSession(session) {
    captureRangeState();
    if (lastJson) updateSessionProgressInPlace(lastJson);
    const rng = rangeState[session] || {};
    const s = lastJson?.sessions?.find((x) => x.session === session);
    // Re-transcribe means "do the work again" — bypass the per-WAV JSON cache
    // that would otherwise short-circuit the merge in milliseconds.
    const payload = {
      session,
      model: batchModel,
      from_iso: (rng.from || "").trim(),
      to_iso: (rng.to || "").trim(),
      prompt: rng.prompt || "",
      hotwords: rng.hotwords || "",
      source: effectiveSource(session),
      force: !!s?.session_transcript,
    };
    await withInflight(sessInflight, sessJustDone, session, "Session transcribe",
      () => postJson("/api/transcribe-session", payload));
  }

  async function stripSession(session) {
    let summary = null;
    await withInflight(sessStripInflight, null, session, "Strip silence", async () => {
      summary = await postJson(`/api/sessions/${encodeURIComponent(session)}/strip-silence`);
    });
    // Auto-flip source to stripped on success so the user can immediately
    // transcribe the cleaned audio. Skip when no files were written — an
    // all-silent session produces no stripped/ folder.
    if (summary?.files_written > 0) sourcePick.set(session, "stripped");
    if (summary) {
      const pct = summary.in_seconds > 0 ? Math.round(100 * summary.speech_seconds / summary.in_seconds) : 0;
      console.log(`[strip-silence] ${session}:`, summary);
      const detector = Array.isArray(summary.detector) ? summary.detector.join(", ") : summary.detector;
      alert(`Stripped ${summary.files_written}/${summary.files_processed} WAVs · ${Math.round(summary.speech_seconds)}s speech of ${Math.round(summary.in_seconds)}s (${pct}%) · detector ${detector}`);
    }
  }

  async function removeStripped(session) {
    if (!confirm("Delete the stripped/ folder for this session?\n\nOriginals are kept. You can rerun strip silence later.")) return;
    try { await del(`/api/sessions/${encodeURIComponent(session)}/stripped`); }
    catch (e) { alert(`Remove stripped failed: ${e}`); return; }
    if (sourcePick.get(session) === "stripped") sourcePick.delete(session);
    lastSessionsSig = "";
    await refresh();
  }

  async function copyMerged(session) {
    if (!lastJson) return;
    const s = lastJson.sessions.find((x) => x.session === session);
    if (!s || !s.session_transcript) {
      alert("No merged transcript yet for this session.");
      return;
    }
    // Rebuild the text from segments so display-name aliases match what the
    // user sees on screen — the backend's `plain_text` uses raw speaker keys.
    const aliases = effectiveMeta(s).aliases || {};
    const segs = s.session_transcript.segments || [];
    const lines = [];
    for (const seg of segs) {
      const text = seg.text || "";
      if (!text) continue;
      const speaker = aliasOf(seg.speaker || "", aliases);
      let line = `[${fmtClock(seg.abs_start)}] ${speaker}: ${text}`;
      if (seg.low_confidence) line += " [uncertain]";
      lines.push(line);
    }
    const out = lines.join("\n") || s.session_transcript.plain_text || "";
    if (!out) {
      alert("No merged transcript yet for this session.");
      return;
    }
    try {
      await navigator.clipboard.writeText(out);
    } catch (e) {
      const w = window.open("", "_blank");
      if (w) {
        w.document.body.style.font = "12px ui-monospace, Menlo, Consolas, monospace";
        w.document.body.style.whiteSpace = "pre-wrap";
        w.document.body.textContent = out;
      } else {
        alert("Copy failed (clipboard blocked).");
      }
    }
  }

  // ---- Top-bar actions ----------------------------------------------------

  $("refreshBtn").addEventListener("click", () => { refresh(); });

  $("newSessionBtn").addEventListener("click", () => {
    if (!confirm("Start a new recording session?\n\nWAVs from new utterances will land in a fresh folder. In-progress utterances finish in their current folder.")) return;
    tryThen("Failed to start new session", () => postJson("/api/new-session"), refresh);
  });

  $("liveClearBtn").addEventListener("click", async () => {
    try { await del("/api/live-transcript"); } catch { /* idempotent */ }
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
  $("recordingPill").addEventListener("click", () => {
    const enabled = !(lastJson?.recording_enabled !== false);
    tryThen("Recording toggle", () => postJson("/api/recording/toggle", { enabled }));
  });

  // Bulk-delete every session with 0 WAVs, no merged transcript, no label.
  $("pruneEmptyBtn").addEventListener("click", async () => {
    if (!confirm("Delete every session that has 0 WAVs, no merged transcript, and no label?\n\nThe current session is always kept. Cannot be undone.")) return;
    let j;
    try { j = await postJson("/api/sessions/prune-empty"); }
    catch (e) { alert(`Clear empty failed: ${e}`); return; }
    // Drop all per-session caches for the gone sessions (mirrors deleteSession).
    for (const id of j.pruned || []) forgetSession(id);
    await refresh();
    alert(`Removed ${j.count || 0} empty session${j.count === 1 ? "" : "s"}.`);
  });

  // Sidebar filter — bound once on boot; input is static in the HTML shell.
  // Triggering a re-render of the sidebar (via signature reset) is enough.
  $("sessFilter").addEventListener("input", () => {
    sessionFilter = $("sessFilter").value || "";
    lastSessionsSig = "";
    if (lastJson) renderSessionsIfChanged(lastJson);
  });

// ---- Boot ---------------------------------------------------------------

await loadTemplates(
  "/web/components/ribbon.html",
  "/web/components/live-channel.html",
  "/web/components/active-taps.html",
  "/web/components/live-feed.html",
  "/web/components/config-card.html",
  "/web/components/merged-transcript.html",
  "/web/components/session-sidebar.html",
  "/web/components/session-detail.html",
);
initComponentCtx();

// Serialised poll loop — awaiting tick() inline ends the setInterval-style
// re-entrancy that the signature/focus guards exist to paper over, and
// skipping ticks while hidden avoids needless /api/state calls.
(async () => {
  for (;;) {
    if (document.visibilityState === "visible") await tick();
    await new Promise((r) => setTimeout(r, 500));
  }
})();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") tick();
});
