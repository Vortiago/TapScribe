// @ts-check
// TapScribe — operator console.
// Vanilla JS ES module. Polls /api/state every second; full re-render of the
// sessions browser only when something structural changed so user scroll +
// inputs survive across ticks.

import { cssEscape, fmtClock, fmtElapsedShort } from "./formatters.js";
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
/**
 * @param {string} label
 * @param {() => Promise<unknown>} op
 * @param {() => Promise<unknown>} [after]
 */
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
/**
 * @param {Map<string, number>} inflight
 * @param {Map<string, number> | null} done
 * @param {string} key
 * @param {string} label
 * @param {() => Promise<unknown>} op
 */
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

// Throws if the id isn't in the DOM — the dashboard's static HTML is
// authored to a fixed schema, so a missing id is a programmer bug.
// Failing loudly at the call site beats a "null is not an object" three
// stack frames deeper, and matches the contract of pick() in templates.js.
/** @param {string} id */
const $ = (id) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing element: #${id}`);
  return el;
};

// ---- Render state -------------------------------------------------------
/** @type {import('./types.js').AppState | null} */
let lastJson = null;
/** @type {Map<string, number>} */
const wavInflight = new Map();
/** @type {Map<string, number>} */
const wavJustDone = new Map();
/** @type {Map<string, number>} */
const sessJustDone = new Map();
const MIN_VISIBLE_MS = 600;      // keep the spinner visible at least this long so quick completions are perceptible
const FLASH_MS = 1500;           // how long the green "just done" tint stays on a row/button
/** @type {Map<string, number>} */
const sessInflight = new Map();
/** @type {Map<string, number>} */
const sessStripInflight = new Map();
/** @type {Map<string, "original" | "stripped">} */
const sourcePick = new Map();    // session name → "original" | "stripped"
/** @type {string | null} */
let selectedSessionId = null;    // which session is open (null = pick is_current)
/** @type {string | null} */
let expandedWav = null;          // "<session>/<name>" expanded inline transcript
let showAudit = true;            // whether to show the suppressed-audit table
/** @type {Record<string, Record<string, string>>} */
const rangeState = {};           // per-session form state (from/to/prompt/hotwords)
let sessionFilter = "";          // sidebar filter query
let batchModel = "small.en";     // dashboard-wide batch transcribe model (Controls box)
let batchBackend = "auto";       // dashboard-wide backend preference; chips drive this
// Catalog of every model registered server-side, filtered to batch context.
// Loaded once on dashboard boot; `available_backends` tells the chip row
// which backends to gray out. Shape mirrors GET /api/models?context=batch.
/** @type {import('./types.js').ModelCatalog} */
let modelCatalog = { context: "batch", available_backends: [], models: [] };
/** @type {import('./types.js').ModelCatalog} */
let liveModelCatalog = { context: "live", available_backends: [], models: [] };
/** @type {Record<string, import('./types.js').EffectiveMeta>} */
const localMeta = {};            // per-session optimistic meta cache (label + aliases)
const metaSaveTimers = new Map();// debounce timers for PUT /api/session-meta

// Strip-silence tunables (gap/pad/floor). Surfaced as inputs next to the
// strip-silence button on every session and persisted to localStorage so
// the last-used values stick across reloads. Defaults mirror the server-side
// fallbacks in api_session_strip_silence (tapscribe/app.py) and
// SPEECH_RMS_DBFS_FLOOR (tapscribe/strip_silence.py).
const STRIP_OPTS_LS_KEY = "tapscribe.stripOpts.v1";
/** @type {import('./types.js').StripOpts} */
const STRIP_OPT_DEFAULTS = Object.freeze({
  min_silence_ms: 500,
  pad_ms: 200,
  speech_floor_db: -45,
});
/** @returns {import('./types.js').StripOpts} */
function loadStripOpts() {
  try {
    const raw = localStorage.getItem(STRIP_OPTS_LS_KEY);
    if (!raw) return { ...STRIP_OPT_DEFAULTS };
    const parsed = JSON.parse(raw);
    return { ...STRIP_OPT_DEFAULTS, ...parsed };
  } catch {
    // localStorage unavailable (private mode) or corrupt JSON — fall back
    // to defaults rather than blowing up the dashboard boot.
    return { ...STRIP_OPT_DEFAULTS };
  }
}
function saveStripOpts() {
  try { localStorage.setItem(STRIP_OPTS_LS_KEY, JSON.stringify(stripOpts)); }
  catch {
    // localStorage write quota / private-mode failure is best-effort —
    // the in-memory values still drive the next /strip-silence POST.
  }
}
let stripOpts = loadStripOpts();
let rxPattern = "";              // regex tester pattern (per-currently-selected-session)
let rxFlags = "i";
let rxOpen = false;
/** @type {string | null} */
let rxOwnerSession = null;       // which session rxPattern was last typed for

let lastSessionsSig = "";        // structural signature; re-renders sessions only when changed

  // Pull effective meta for a session: optimistic local override beats server.
  /**
   * @param {import('./types.js').Session | null} s
   * @returns {import('./types.js').EffectiveMeta}
   */
  function effectiveMeta(s) {
    const local = /** @type {Record<string, unknown> | undefined} */ (s ? localMeta[s.session] : undefined);
    const server = /** @type {Record<string, unknown>} */ ((s && s.session_meta) || {});
    const pick = /** @param {string} k @param {unknown} dflt */ (k, dflt) => (local && k in local ? local[k] : (server[k] || dflt));
    return {
      label: /** @type {string} */ (pick("label", "")),
      aliases: /** @type {Record<string, string>} */ (pick("aliases", {})),
      prompt: /** @type {string} */ (pick("prompt", "")),
      hotwords: /** @type {string} */ (pick("hotwords", "")),
    };
  }

  // Derive the set of speaker keys for which we should show an alias editor.
  // Prefer the merged transcript's speakers list; fall back to per-WAV speaker_name.
  /**
   * @param {import('./types.js').Session | null} s
   * @returns {string[]}
   */
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

  // Debounced PUT /api/session-meta. The server merges partial payloads
  // so we send only the fields we know locally.
  /** @param {string} sessId */
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
  /** @type {import('./types.js').LiveFeedCtx} */
  let liveFeedCtx;
  /** @type {import('./types.js').ActiveTapsCtx} */
  let activeTapsCtx;
  /** @type {Omit<import('./types.js').LiveChannelCtx, 'mlxAvail' | 'liveCatalog'>} */
  let liveChannelCtx;
  /** @type {import('./types.js').ConfigCardCtx} */
  let configCardCtx;
  /** @type {import('./types.js').RibbonCtx} */
  let ribbonCtx;
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
    configCardCtx = { gridEl: $("configGrid"), headerNoteEl: $("configHeaderNote") };
    ribbonCtx = { statusEl: $("sessionStatus"), pillEl: $("recordingPill") };
  }

  async function tick() {
    try {
      const j = await fetchState();
      lastJson = j;
      ribbon.renderStatus(j, ribbonCtx);
      ribbon.renderRecPill(ribbonCtx, j.recording_enabled !== false);
      liveChannel.render(j, { ...liveChannelCtx, mlxAvail: !!j.mlx_available, liveCatalog: liveModelCatalog });
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
  /**
   * @param {string} identity
   * @param {string} which
   * @param {boolean} enabled
   */
  const setTapPref = (identity, which, enabled) =>
    tryThen("Tap setting toggle", () => putJson("/api/tap-settings", { identity, [which]: enabled }));

  // ---- Sessions: tabs + detail --------------------------------------------

  /**
   * @param {import('./types.js').Session[]} sessions
   * @returns {string}
   */
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
          (s.files || []).map((f) => {
            // Include each region's transcript stamp so a region transcribe
            // re-flows the row (its "took Xms" cell + has-tx marker).
            const regionSig = (f.regions || [])
              .map((r) => r.name + "@" + (r.transcript ? r.transcript.transcribed_at : ""))
              .join("|");
            return f.name
              + ":" + (f.transcript ? f.transcript.transcribed_at : "")
              + "::" + regionSig;
          }).join(","),
          meta.label || "",
          aliasSig,
          // Per-session prompt/hotwords overrides feed the badged rows
          // in session controls and the "N sessions override this"
          // footer on the default config panel. Multi-tab editing and
          // external session-meta.json writes are only visible to the
          // dashboard if these are part of the signature. Capped on
          // the server at MAX_CONFIG_TEXT_LEN so the join stays cheap.
          meta.prompt || "",
          meta.hotwords || "",
          stripSig,
          srcPick,
          stripping,
        ].join("|");
      })
      .join("§");
  }

  /**
   * @param {import('./types.js').Session[]} sessions
   * @returns {string | null}
   */
  function pickSelectedSession(sessions) {
    if (!sessions.length) return null;
    if (selectedSessionId && sessions.find((s) => s.session === selectedSessionId)) return selectedSessionId;
    const cur = sessions.find((s) => s.is_current);
    // sessions[0] exists: length was checked above
    return cur ? cur.session : /** @type {import('./types.js').Session} */ (sessions[0]).session;
  }

  /** @param {import('./types.js').AppState} j */
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
      + "::" + batchModel
      + "::" + batchBackend;
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

  // Re-render the sessions pane from the last polled state — no network
  // round trip. Click handlers that only change local UI state use this
  // instead of tick() so the click lands instantly; the 500ms poll loop
  // keeps the underlying data fresh.
  function rerenderFromCache() {
    if (lastJson) renderSessionsIfChanged(lastJson);
  }

  /**
   * @param {import('./types.js').Session[]} sessions
   * @param {string | null} selectedId
   */
  function renderSessionSidebar(sessions, selectedId) {
    sessionSidebar.render(sessions, {
      listEl: $("sessList"),
      selectedId,
      filter: sessionFilter,
      metaFor: effectiveMeta,
      onSelect: (/** @type {string} */ id) => { selectedSessionId = id; lastSessionsSig = ""; rerenderFromCache(); },
      onDelete: (/** @type {string} */ id) => deleteSession(id),
    });
  }

  /** @param {string} sessId */
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
  /** @param {string} sessId */
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
  /** @param {import('./types.js').Session | undefined} s */
  function renderSessionDetail(s) {
    if (!s) return;
    sessionDetail.render(s, $("sessDetailRoot"), /** @type {import('./types.js').SessionDetailCtx} */ ({
      // state
      lastJson,
      batchModel,
      batchBackend,
      modelCatalog,
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
      // Global batch defaults — shown as the placeholder preview in
      // per-session override rows when no override is set.
      defaults: {
        prompt: (lastJson && lastJson.prompt && lastJson.prompt.content) || "",
        hotwords: (lastJson && lastJson.hotwords && lastJson.hotwords.content) || "",
      },
      // sub-component
      renderMerged: (t, meta) => mergedTranscript.render(t, meta, { showAudit }),
      // callbacks
      onTranscribeSession: transcribeSession,
      onCopyMerged: copyMerged,
      onTranscribeWav: transcribeWav,
      onToggleWav: (wk, sess) => {
        // Stripped region sub-row keys carry "@stripped"; the base key is
        // "<session>/<name>" where <name> is the region's own (unique)
        // filename. Originals: same shape, no suffix.
        const stripped = wk.endsWith("@stripped");
        const baseKey = stripped ? wk.slice(0, -"@stripped".length) : wk;
        const idx = baseKey.indexOf("/");
        const targetName = idx >= 0 ? baseKey.slice(idx + 1) : baseKey;
        let tx = null;
        if (stripped) {
          for (const ff of (sess.files || [])) {
            const r = (ff.regions || []).find((rr) => rr.name === targetName);
            if (r) { tx = r.transcript; break; }
          }
        } else {
          const f = (sess.files || []).find((ff) => ff.name === targetName);
          tx = f?.transcript || null;
        }
        if (!tx) return;
        expandedWav = expandedWav === wk ? null : wk;
        lastSessionsSig = "";
        rerenderFromCache();
      },
      onRangeEdit: (sk, k, v) => {
        rangeState[sk] = rangeState[sk] || {};
        rangeState[sk][k] = v;
      },
      onModelChange: (v) => {
        batchModel = v;
        lastSessionsSig = "";
        // The change fires from the focused <select>, which trips the
        // focused-input guard in renderSessionsIfChanged. Blur it so the
        // re-render runs — the pane is rebuilt anyway, so losing focus on
        // the (recreated) select is harmless.
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        rerenderFromCache();
      },
      onBackendChange: (v) => {
        batchBackend = v;
        // The selected model may not be valid on the new backend — leave
        // it; the model select will filter on next render and the user
        // can pick a compatible one. Force a re-render so the chip
        // active-state updates immediately.
        lastSessionsSig = "";
        rerenderFromCache();
      },
      onSourcePick: (sk, v) => { sourcePick.set(sk, v); },
      onStripRun: stripSession,
      onStripRemove: removeStripped,
      // Strip-silence parameter inputs. Edits don't tick — re-rendering on
      // every keystroke would steal focus and the focused-input guard in
      // renderSessionsIfChanged already protects the live inputs anyway.
      // Reset *does* tick so every session's row picks up the new defaults.
      stripOpts,
      onStripOptEdit: (k, v) => {
        if (k === "speech_floor_db") {
          stripOpts[k] = v === "" ? STRIP_OPT_DEFAULTS[k] : Number(v);
        } else {
          stripOpts[k] = v === "" ? STRIP_OPT_DEFAULTS[k] : Math.max(0, parseInt(v, 10) || 0);
        }
        saveStripOpts();
      },
      onStripOptReset: () => {
        stripOpts = { ...STRIP_OPT_DEFAULTS };
        saveStripOpts();
        lastSessionsSig = "";
        rerenderFromCache();
      },
      onNameEdit: (sk, value) => {
        localMeta[sk] = { ...effectiveMeta(s), label: value };
        persistSessionMeta(sk);
      },
      onAliasEdit: (sk, key, value) => {
        const cur = effectiveMeta(s);
        const aliases = { ...(cur.aliases || {}) };
        if (value) aliases[key] = value;
        else delete aliases[key];
        localMeta[sk] = { ...cur, aliases };
        persistSessionMeta(sk);
      },
      onMetaOverrideEdit: (sk, metaKey, value) => {
        // effectiveMeta resolves local-over-server for every field, so we
        // can rebuild localMeta from it without separately re-spreading
        // the prior local entry.
        localMeta[sk] = { ...effectiveMeta(s), [metaKey]: value };
        persistSessionMeta(sk);
        // Flip the badge style on the next tick without waiting for the
        // PUT to round-trip via /api/state.
        lastSessionsSig = "";
      },
      onAbsorbSession: (target, source) => absorbSession(target, source),
      onRxToggle: (sk) => { rxOpen = !rxOpen; rxOwnerSession = sk; lastSessionsSig = ""; rerenderFromCache(); },
      onRxPatternInput: (sk, v) => { rxPattern = v; rxOwnerSession = sk; updateRegexResult(s); },
      onRxFlagsInput: (sk, v) => { rxFlags = v; rxOwnerSession = sk; updateRegexResult(s); },
      onRxSeed: (sk, seed) => {
        rxPattern = seed;
        rxOwnerSession = sk;
        const inp = /** @type {HTMLInputElement | null} */ ($("sessDetailRoot").querySelector("[data-rx-pattern]"));
        if (inp) inp.value = seed;
        updateRegexResult(s);
      },
      onAuditToggle: () => { showAudit = !showAudit; lastSessionsSig = ""; rerenderFromCache(); },
    }));
  }


  /** @param {import('./types.js').Session} s */
  function updateRegexResult(s) {
    // Surgical update: don't re-render the whole detail (would lose input focus).
    const out = $("sessDetailRoot").querySelector(".rx-result");
    if (!out) return;
    const segs = s.session_transcript?.segments || [];
    out.replaceChildren(sessionDetail.renderRegexHits(segs, { rxPattern, rxFlags }));
  }

  function captureRangeState() {
    for (const el of /** @type {NodeListOf<HTMLInputElement>} */ (document.querySelectorAll("[data-range-key]"))) {
      const sk = el.dataset.sessId;
      const k = el.dataset.rangeKey;
      if (!sk || !k) continue;
      const entry = rangeState[sk] ?? {};
      rangeState[sk] = entry;
      entry[k] = el.value;
    }
    // Dynamic per-model inputs use [data-input-name] (the registry
    // input's name — `source_lang`, `target_lang`). `initial_prompt`
    // and `hotwords` are persisted via session-meta directly (see
    // [data-meta-key]), not via the ephemeral rangeState.
    for (const el of /** @type {NodeListOf<HTMLInputElement>} */ (document.querySelectorAll("[data-input-name]"))) {
      const sk = el.dataset.sessId;
      if (!sk) continue;
      const k = el.dataset.inputName;
      if (!k) continue;
      const entry = rangeState[sk] ?? {};
      rangeState[sk] = entry;
      entry[k] = el.value;
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
  /** @param {import('./types.js').AppState} j */
  function updateSessionProgressInPlace(j) {
    if (!j.sessions) return;
    for (const s of j.sessions) {
      const btn = /** @type {HTMLButtonElement | null} */ (document.querySelector(`[data-tx-sess="${cssEscape(s.session)}"]`));
      if (!btn) continue;
      const { node, busy } = sessionDetail.sessionProgressInner(s, sessInflight);
      btn.replaceChildren(node);
      btn.disabled = busy;
    }
  }

  // ---- Mutations ----------------------------------------------------------

  // Resolve the effective source for a session: a stripped pick falls back
  // to "original" when no stripped/ folder exists.
  /**
   * @param {string} session
   * @param {string | null | undefined} [override]
   * @returns {"original" | "stripped"}
   */
  function effectiveSource(session, override) {
    if (override) return /** @type {"original" | "stripped"} */ (override);
    const s = lastJson?.sessions?.find((x) => x.session === session);
    const want = sourcePick.get(session) || "original";
    return (want === "stripped" && !s?.stripped) ? "original" : want;
  }

  /**
   * @param {string} session
   * @param {string} name
   * @param {string | null | undefined} [sourceOverride]
   */
  function transcribeWav(session, name, sourceOverride) {
    const source = effectiveSource(session, sourceOverride);
    // Key inflight/justDone by source so original and stripped sub-rows can
    // each show a spinner without colliding.
    const key = `${session}/${name}${source === "stripped" ? "@stripped" : ""}`;
    const rng = rangeState[session] || {};
    return withInflight(wavInflight, wavJustDone, key, "Transcribe",
      () => postJson("/api/transcribe", {
        session, name, source,
        model: batchModel,
        backend: batchBackend,
        // Prompt and hotwords resolve server-side from session-meta →
        // global defaults; the dashboard edits session-meta directly.
        source_lang: rng.source_lang || "",
        target_lang: rng.target_lang || "",
      }));
  }

  /** @param {string} session */
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
      backend: batchBackend,
      from_iso: (rng.from || "").trim(),
      to_iso: (rng.to || "").trim(),
      source_lang: rng.source_lang || "",
      target_lang: rng.target_lang || "",
      source: effectiveSource(session),
      force: !!s?.session_transcript,
    };
    await withInflight(sessInflight, sessJustDone, session, "Session transcribe",
      () => postJson("/api/transcribe-session", payload));
  }

  /** @param {string} session */
  async function stripSession(session) {
    let _summary = null;
    // Snapshot the current operator-tuned params so the POST body matches
    // exactly what the inputs show — protects against the user nudging an
    // input mid-flight from accidentally changing semantics.
    const body = { ...stripOpts };
    await withInflight(sessStripInflight, null, session, "Strip silence", async () => {
      _summary = await postJson(`/api/sessions/${encodeURIComponent(session)}/strip-silence`, body);
    });
    // tsc can't track variable mutation through an async closure; snapshot with cast.
    const summary = /** @type {import('./types.js').StripSilenceResult | null} */ (_summary);
    // Auto-flip source to stripped on success so the user can immediately
    // transcribe the cleaned audio. Skip when no files were written — an
    // all-silent session produces no stripped/ folder.
    if (summary && summary.files_written > 0) sourcePick.set(session, "stripped");
    if (summary) {
      const pct = summary.in_seconds > 0 ? Math.round(100 * summary.speech_seconds / summary.in_seconds) : 0;
      console.log(`[strip-silence] ${session}:`, summary, "params:", body);
      const regions = (summary.files || []).reduce((n, r) => n + (r.segments || 0), 0);
      const params = `gap=${body.min_silence_ms}ms pad=${body.pad_ms}ms floor=${body.speech_floor_db}dB`;
      alert(
        `Stripped ${summary.files_written}/${summary.files_processed} WAVs → ${regions} regions · `
        + `${Math.round(summary.speech_seconds)}s speech of ${Math.round(summary.in_seconds)}s (${pct}%)\n${params}`
      );
    }
  }

  /**
   * @param {string} target
   * @param {string} source
   */
  async function absorbSession(target, source) {
    const sessions = lastJson?.sessions || [];
    /** @param {string} id */
    const labelOf = (id) => {
      const s = sessions.find((x) => x.session === id);
      const lbl = s ? effectiveMeta(s).label : "";
      return lbl ? `"${lbl}" (${id})` : id;
    };
    const srcSess = sessions.find((x) => x.session === source);
    const wavCount = srcSess ? srcSess.wav_count : 0;
    const msg = `Move all ${wavCount} WAV${wavCount === 1 ? "" : "s"} from ${labelOf(source)} into ${labelOf(target)}?

The source folder will be deleted. The target's merged transcript (if any) will be cleared so you can re-run it on the combined audio. Speaker aliases on the target are kept; source aliases fill in any names the target doesn't already have.`;
    if (!confirm(msg)) return;
    try { await postJson(`/api/sessions/${encodeURIComponent(target)}/absorb`, { source }); }
    catch (e) { alert(`Merge failed: ${e}`); return; }
    forgetSession(source);
    // The selected session is the *target*; ensure we stay on it.
    selectedSessionId = target;
    await refresh();
  }

  /** @param {string} session */
  async function removeStripped(session) {
    if (!confirm("Delete the stripped/ folder for this session?\n\nOriginals are kept. You can rerun strip silence later.")) return;
    try { await del(`/api/sessions/${encodeURIComponent(session)}/stripped`); }
    catch (e) { alert(`Remove stripped failed: ${e}`); return; }
    if (sourcePick.get(session) === "stripped") sourcePick.delete(session);
    lastSessionsSig = "";
    await refresh();
  }

  /**
   * @param {string} session
   * @param {HTMLButtonElement} btn
   */
  async function copyMerged(session, btn) {
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

    // Non-secure context (LAN http://): `navigator.clipboard` is gated, so
    // the await would reject and any `window.open` in the catch is past the
    // user-gesture window and gets popup-blocked. Open the fallback tab
    // synchronously inside the click handler instead.
    const haveClipboard = window.isSecureContext
      && typeof navigator.clipboard?.writeText === "function";
    if (!haveClipboard) {
      openTranscriptTab(out, btn);
      return;
    }

    try {
      await navigator.clipboard.writeText(out);
      flashButton(btn, "✓ copied");
    } catch (e) {
      // Past the user gesture — popup will likely be blocked. Try once,
      // then fall back to a prompt() the user can select-copy from.
      const w = window.open("", "_blank");
      if (w) {
        populateTranscriptTab(w, out);
        flashButton(btn, "↗ opened in new tab");
      } else {
        window.prompt("Copy the merged transcript (Ctrl/Cmd-C, Enter):", out);
      }
    }
  }

  /**
   * @param {string} text
   * @param {HTMLButtonElement} btn
   */
  function openTranscriptTab(text, btn) {
    const w = window.open("", "_blank");
    if (w) {
      populateTranscriptTab(w, text);
      flashButton(btn, "↗ opened in new tab");
    } else {
      window.prompt("Copy the merged transcript (Ctrl/Cmd-C, Enter):", text);
    }
  }

  /**
   * @param {Window} w
   * @param {string} text
   */
  function populateTranscriptTab(w, text) {
    w.document.body.style.font = "12px ui-monospace, Menlo, Consolas, monospace";
    w.document.body.style.whiteSpace = "pre-wrap";
    w.document.body.textContent = text;
  }

  /**
   * @param {HTMLButtonElement | null} btn
   * @param {string} label
   */
  function flashButton(btn, label) {
    if (!btn) return;
    const prev = btn.textContent;
    btn.textContent = label;
    btn.classList.add("just-completed");
    setTimeout(() => {
      btn.classList.remove("just-completed");
      // Only restore if the button hasn't been re-rendered to something else.
      if (btn.textContent === label) btn.textContent = prev;
    }, 1500);
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
    const btn = /** @type {HTMLButtonElement | null} */ (/** @type {Element | null} */ (ev.target)?.closest(".tap-toggle"));
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
    sessionFilter = /** @type {HTMLInputElement} */ ($("sessFilter")).value || "";
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

// Fetch the model catalog once at boot for the batch + live pickers.
// The catalog only changes on a server restart (TranscriberRegistry is
// static at boot), so re-fetching every tick would be wasteful. If a
// future feature adds dynamic model installation we'll add a refresh.
//
// Non-blocking: dashboard initialises immediately with the empty default
// catalog so the poll loop starts running. Once the fetch resolves the
// dropdowns get populated on the next render tick. This matters for the
// Playwright e2e tests, where blocking on a fetch at module-import time
// would deadlock dashboard boot against the test driver's first action.
async function loadModelCatalogs() {
  try {
    const [batchRes, liveRes] = await Promise.all([
      fetch("/api/models?context=batch", { cache: "no-store" }),
      fetch("/api/models?context=live", { cache: "no-store" }),
    ]);
    if (batchRes.ok) modelCatalog = await batchRes.json();
    if (liveRes.ok) liveModelCatalog = await liveRes.json();
    lastSessionsSig = "";  // force the next tick to re-render with real models
  } catch (e) {
    console.error("Failed to load model catalogs:", e);
  }
}
initComponentCtx();

// Serialised poll loop — awaiting tick() inline ends the setInterval-style
// re-entrancy that the signature/focus guards exist to paper over, and
// skipping ticks while hidden avoids needless /api/state calls.
//
// We fire `loadModelCatalogs()` from INSIDE the loop's first iteration
// instead of at module top-level. Reason: in the Playwright e2e tests
// the dashboard waits for the static empty state to be visible before
// running any user actions; firing the catalog fetches at module load
// triggers extra parallel HTTP requests that delay the first
// `/api/state` tick enough that Playwright's `wait_for_selector("...
// .empty")` times out (the active-taps render re-mounts the .empty div
// faster than Playwright can confirm visibility). Lazy-firing from the
// loop keeps the empty state stable long enough for the wait to pass,
// then loads the catalog in the background while polling continues.
let _catalogLoaded = false;
(async () => {
  for (;;) {
    if (document.visibilityState === "visible") {
      if (!_catalogLoaded) {
        _catalogLoaded = true;
        loadModelCatalogs();
      }
      await tick();
    }
    await new Promise((r) => setTimeout(r, 500));
  }
})();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") tick();
});
