// @ts-check
// TapScribe · Stages — Phase-1 entry point for the /next dashboard.
//
// Boots a slim left SPINE (two groups: GLOBAL Taps·People·Settings, pinned;
// THIS SESSION Capture·Recordings·Transcript·Summary, numbered) + one dense
// view at a time in the view container. Polls /api/state every 500ms (same
// `api.js` as the classic dashboard), re-renders the spine each tick, and runs
// the active view's per-tick update. Views are BUILT once (or once per session,
// for Transcript) and cached so the reused dashboard components keep their
// scroll/focus/signature state across ticks.
//
// Client-side view routing only — clicking a spine item sets currentView and
// re-renders; we mirror it into location.hash so a reload lands on the same
// view. window.gotoView(name) is exposed for screenshot/automation driving.

import { fetchState, postJson, putJson, del } from "../api.js";
import { loadTemplates, pick } from "../templates.js";
import { ALL_VIEWS, resolveSession, placeholderView } from "./shell.js";
import * as spine from "./components/spine.js";
import * as engine from "./components/engine.js";
import * as activeTaps from "../components/active-taps.js";
import * as captureView from "./views/capture.js";
import * as transcriptView from "./views/transcript.js";
import * as summaryView from "./views/summary.js";
import * as settingsView from "./views/settings.js";
import * as recordingsView from "./views/recordings.js";
import * as tapsView from "./views/taps.js";
import * as peopleView from "./views/people.js";
import * as sessionsView from "./views/sessions.js";

/** @param {string} id */
const $ = (id) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing element: #${id}`);
  return el;
};

// ---- Render state -----------------------------------------------------------
/** @type {import('../types.js').AppState | null} */
let lastJson = null;
/** @type {import('./shell.js').ViewId} */
let currentView = "capture";
/** @type {string | null} */
let selectedSessionId = null;

// Model catalog (batch context) — drives the engine selectors + spine. Loaded
// once at boot; the registry is static server-side so it never changes.
/** @type {import('../types.js').ModelCatalog} */
let modelCatalog = { context: "batch", available_backends: [], models: [] };
/** @type {import('../types.js').ModelCatalog} */
let liveModelCatalog = { context: "live", available_backends: [], models: [] };

// Engine states: Settings holds the global batch DEFAULT; Transcript holds the
// engine for the open session AND drives its transcribe jobs (one WAV / session
// range). Both are kept client-side and seeded from the first catalog model
// once it loads. (Recordings no longer has its own engine — transcription moved
// to the Transcript stage, so one engine state covers Transcript's selector +
// transcribe.)
/** @type {import('./components/engine.js').EngineState} */
let defaultEngine = { backend: "auto", model: "" };
/** @type {import('./components/engine.js').EngineState} */
let overrideEngine = { backend: "auto", model: "" };

// Built-view cache. Capture + Settings are page-singletons; Transcript is
// keyed by session id so a new session rebuilds its merged transcript.
/** @typedef {{ node: DocumentFragment | Node, host?: HTMLElement, update: (j: import('../types.js').AppState, session: import('../types.js').Session | null) => void, rebuildEngine?: () => void, key: string }} BuiltView */
/** @type {Map<string, BuiltView>} */
const viewCache = new Map();
/** @type {string | null} */
let mountedKey = null;

// Only transcript:* keys are unbounded (one per visited session); the other
// views are page-singletons (≤ 7 keys total). An always-open operator tab
// that focuses many sessions over days would otherwise retain every cached
// transcript view's DOM + listeners forever. Keep the most recent few — the
// Map is maintained in LRU order for these keys (re-set on access).
const MAX_CACHED_TRANSCRIPT_VIEWS = 6;
function evictStaleTranscriptViews() {
  const txKeys = [...viewCache.keys()].filter((k) => k.startsWith("transcript:"));
  let excess = txKeys.length - MAX_CACHED_TRANSCRIPT_VIEWS;
  for (const key of txKeys) {
    if (excess <= 0) break;
    if (key === mountedKey) continue; // never evict the mounted view; evict the next-oldest instead
    viewCache.delete(key);
    excess--;
  }
}

// ---- Helpers ----------------------------------------------------------------

/**
 * Effective per-session meta — reads the server's session_meta directly.
 * (Phase 1 has no optimistic local layer; override saves re-poll via refresh.)
 * @param {import('../types.js').Session | null} s
 * @returns {import('../types.js').EffectiveMeta}
 */
function metaFor(s) {
  const m = (s && s.session_meta) || {};
  return {
    label: m.label || "",
    aliases: m.aliases || {},
    prompt: m.prompt || "",
    hotwords: m.hotwords || "",
  };
}

/** Seed both engine model ids from the catalog once it loads. */
function seedEngineModels() {
  const first = modelCatalog.models[0];
  if (!first) return;
  if (!defaultEngine.model) defaultEngine = { ...defaultEngine, model: first.model_id };
  if (!overrideEngine.model) overrideEngine = { ...overrideEngine, model: first.model_id };
}

/**
 * Per-(selected default model) input support — mirrors the server's
 * _compute_inputs_support, but for the ONE model chosen in Settings' "Default
 * engine" selector, so the prompt/hotwords editors gate on the picked model
 * (Whisper declares both `initial_prompt` + `hotwords`; Parakeet/Voxtral
 * declare neither). Returns null when the model isn't in the catalog yet, so
 * config-card falls back to the registry-wide inputs_support.
 * @returns {{ batch_prompt: boolean, batch_hotwords: boolean } | null}
 */
function defaultEngineSupport() {
  const m = modelCatalog.models.find((x) => x.model_id === defaultEngine.model);
  if (!m) return null;
  const names = new Set((m.inputs || []).map((i) => i.name));
  return { batch_prompt: names.has("initial_prompt"), batch_hotwords: names.has("hotwords") };
}

/**
 * Render the DEFAULT (Settings) engine selector into a host.
 * @param {Element} host
 */
function renderDefaultEngine(host) {
  engine.render(host, {
    state: defaultEngine,
    catalog: modelCatalog,
    onChange: (next) => {
      defaultEngine = next;
      const v = viewCache.get("settings");
      v?.rebuildEngine?.();
      // Re-gate the prompt/hotwords editors on the newly-picked model right
      // away (don't wait for the next poll tick). config-card's signature
      // includes the support flags, so this is a no-op repaint when the new
      // model has the same prompt/hotwords support as the old one. (Settings'
      // update ignores the session arg — it renders global defaults.)
      if (lastJson) v?.update?.(lastJson, null);
    },
  });
}

/**
 * Render the session (Transcript) engine selector into a host. This engine
 * state also drives the Transcript stage's transcribe jobs (one WAV / session
 * range).
 * @param {Element} host
 */
function renderOverrideEngine(host) {
  engine.render(host, {
    state: overrideEngine,
    catalog: modelCatalog,
    onChange: (next) => {
      overrideEngine = next;
      const v = viewCache.get(`transcript:${selectedSessionId || ""}`);
      v?.rebuildEngine?.();
    },
  });
}

const liveStart = async () => {
  try {
    const { formValues } = await import("../components/live-channel.js");
    await postJson("/api/live/start", formValues());
  } catch (e) { alert(`Live start/apply failed: ${e}`); }
  finally { await refresh(); }
};
const liveStop = async () => {
  try { await postJson("/api/live/stop"); }
  catch (e) { alert(`Live stop failed: ${e}`); }
  finally { await refresh(); }
};
// Restart the live channel with a specific model — the Settings Live card's
// "apply (restart)". Sends ONLY the model; the server keeps the rest of the
// live config (omitted fields = "unchanged"), matching the classic apply.
/** @param {string} model */
const applyLiveModel = async (model) => {
  try { await postJson("/api/live/start", { model }); }
  catch (e) { alert(`Live start/restart failed: ${e}`); }
  finally { await refresh(); }
};

// ---- Active-taps rail -------------------------------------------------------
// The global, collapsible right rail. Hosts the reused active-taps component
// on every view (Capture used to own it; now it lives here so the operator
// sees live taps everywhere). The delegated rec/live toggle handler is bound
// ONCE on the rail body — the body re-renders each tick, but a delegated click
// on the parent survives every swap (same contract as the classic dashboard's
// #activeTapsBody handler and the one Capture previously carried).

const RAIL_HIDDEN_KEY = "tapscribe.next.tapsRailHidden";

/** @type {import('../types.js').ActiveTapsCtx} */
let railCtx;

/** Lazily resolve the rail host slots (the shell is static, so once is fine). */
function railContext() {
  if (!railCtx) {
    railCtx = {
      countEl: $("tapsRailCount"),
      badgeEl: pick($("tapsRail"), "activeTapsBadge"),
      bodyEl: $("tapsRailBody"),
    };
    // Delegated rec/live toggle → PUT /api/tap-settings, then refresh(). Flip
    // the visual state immediately so the click feels responsive; the next
    // poll repaints from the authoritative state.
    railCtx.bodyEl.addEventListener("click", async (ev) => {
      const btn = /** @type {HTMLButtonElement | null} */ (
        /** @type {Element | null} */ (ev.target)?.closest(".tap-toggle"));
      if (!btn || btn.disabled) return;
      const identity = btn.dataset.identity;
      const which = btn.dataset.toggle;
      if (!identity || !which) return;
      const next = btn.dataset.state !== "1";
      btn.dataset.state = next ? "1" : "0";
      btn.classList.toggle("on", next);
      btn.disabled = true;
      try { await putJson("/api/tap-settings", { identity, [which]: next }); }
      catch (e) { alert(`Tap setting toggle failed: ${e}`); }
      finally { btn.disabled = false; await refresh(); }
    });
  }
  return railCtx;
}

/** @param {boolean} hidden */
function setRailHidden(hidden) {
  $("next-app").classList.toggle("rail-hidden", hidden);
  try { localStorage.setItem(RAIL_HIDDEN_KEY, hidden ? "1" : "0"); }
  catch { /* private-mode / quota — the rail just won't persist, not fatal. */ }
}

/** Apply the saved (or narrow-screen default) collapse state + wire the toggles. */
function initRail() {
  let saved = null;
  try { saved = localStorage.getItem(RAIL_HIDDEN_KEY); }
  catch { /* storage unavailable — fall through to the responsive default. */ }
  // No explicit preference yet → default hidden on narrow viewports so the
  // rail doesn't crush the workspace; visible on wide ones.
  const hidden = saved != null ? saved === "1" : window.matchMedia("(max-width: 1100px)").matches;
  $("next-app").classList.toggle("rail-hidden", hidden);

  $("tapsRailHide").addEventListener("click", () => setRailHidden(true));
  $("tapsRailShow").addEventListener("click", () => setRailHidden(false));
}

/** @param {import('../types.js').AppState} j */
function renderRail(j) {
  activeTaps.render(j, railContext());
}

// ---- View mounting ----------------------------------------------------------

/**
 * Build (or fetch from cache) the BuiltView for `currentView`, mount it into
 * the view root if it isn't already mounted, and run its per-tick update.
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').Session | null} session
 */
function renderView(j, session) {
  const root = $("viewRoot");
  const key = viewKey(currentView, session);

  let built = viewCache.get(key) ?? null;
  if (built && key.startsWith("transcript:")) {
    // Refresh LRU position so eviction drops the least-recently-VIEWED one.
    viewCache.delete(key);
    viewCache.set(key, built);
  }
  if (!built) {
    built = buildView(currentView, session);
    if (built) {
      // Wrap the view's fragment in a STABLE, layout-transparent host element.
      // A DocumentFragment empties when its children are moved into the DOM, so
      // caching + re-mounting the fragment itself goes blank on the second
      // visit; a host element can be detached and re-appended freely.
      const host = document.createElement("div");
      host.style.display = "contents";
      host.appendChild(built.node);
      built.host = host;
      viewCache.set(key, built);
      evictStaleTranscriptViews();
    }
  }

  if (!built) {
    // All six views now build a real BuiltView, so this is a defensive
    // fallback only — a future view that returns null from buildView lands
    // here and renders a fresh "coming later" card with no live state.
    mountedKey = null;
    renderPlaceholder(root, currentView, session);
    return;
  }

  if (mountedKey !== key) {
    root.replaceChildren(built.host ?? built.node);
    mountedKey = key;
  }
  built.update(j, session);
}

/**
 * @param {import('./shell.js').ViewId} view
 * @param {import('../types.js').Session | null} session
 */
function viewKey(view, session) {
  // Transcript is per-session (its merged transcript differs); the rest are
  // page-singletons keyed by view alone.
  if (view === "transcript") return `transcript:${session?.session || ""}`;
  return view;
}

/**
 * @param {import('./shell.js').ViewId} view
 * @param {import('../types.js').Session | null} session
 * @returns {BuiltView | null}
 */
function buildView(view, session) {
  if (view === "capture") {
    const b = captureView.build({
      liveCatalog: liveModelCatalog,
      metaFor,
      onLiveStart: liveStart,
      onLiveStop: liveStop,
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: "capture" };
  }
  if (view === "settings") {
    const b = settingsView.build({
      rebuildEngine: renderDefaultEngine,
      selectedSupport: defaultEngineSupport,
      liveCatalog: liveModelCatalog,
      applyLiveModel,
      afterMutate: () => { refresh(); },
    });
    return { ...b, update: (j) => b.update(j), key: "settings" };
  }
  if (view === "transcript") {
    const b = transcriptView.build({
      metaFor,
      engineState: () => overrideEngine,
      rebuildEngine: renderOverrideEngine,
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: viewKey("transcript", session) };
  }
  if (view === "summary") {
    const b = summaryView.build({
      metaFor,
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: "summary" };
  }
  if (view === "recordings") {
    const b = recordingsView.build({
      metaFor,
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: "recordings" };
  }
  if (view === "taps") {
    const b = tapsView.build({
      liveCatalog: liveModelCatalog,
      onLiveStart: liveStart,
      onLiveStop: liveStop,
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: "taps" };
  }
  if (view === "people") {
    const b = peopleView.build({
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: "people" };
  }
  if (view === "sessions") {
    const b = sessionsView.build({
      metaFor,
      // Reuse the spine's session-switch: focus the id, then route into its
      // Capture (if recording) / Transcript view, so the row "Open" button and
      // the spine picker drive the exact same flow.
      onSelectSession,
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: "sessions" };
  }
  return null;
}

/**
 * Generic "coming in a later phase" fallback. No view falls here today (all
 * six build a real BuiltView); kept so a future null-returning view degrades
 * gracefully instead of mounting nothing.
 * @param {Element} root
 * @param {import('./shell.js').ViewId} view
 * @param {import('../types.js').Session | null} _session
 */
function renderPlaceholder(root, view, _session) {
  const title = view.charAt(0).toUpperCase() + view.slice(1);
  placeholderView(root, {
    eyebrow: "Stages", title, icon: "🚧",
    heading: `${title} view`,
    detail: "This view is not built yet.",
  });
}

// ---- Spine ------------------------------------------------------------------

/**
 * Focus a session id and route into it: the live (Capture) stage if it's
 * recording, else its Transcript. Shared by the spine session picker AND the
 * global Sessions list's per-row "Open" action so both switch identically.
 * @param {string} id
 */
function onSelectSession(id) {
  selectedSessionId = id;
  // A fresh session pick lands you on the live stage if it's recording,
  // else the transcript — matching the prototype's session-switch.
  const picked = (lastJson?.sessions || []).find((s) => s.session === id);
  currentView = picked?.is_current ? "capture" : "transcript";
  syncHash();
  if (lastJson) renderAll(lastJson);
}

/** @param {import('../types.js').AppState} j @param {import('../types.js').Session | null} session */
function renderSpine(j, session) {
  spine.render($("spine"), j, {
    currentView,
    session,
    metaFor,
    onSelectView: gotoView,
    onSelectSession,
    onNewSession: async () => {
      if (!confirm("Start a new recording session?\n\nNew utterances land in a fresh folder; in-progress ones finish in their current folder.")) return;
      try { await postJson("/api/new-session"); }
      catch (e) { alert(`Failed to start new session: ${e}`); return; }
      // Fresh session → clear the live captions. The feed is the live channel's
      // transcript buffer (not session-scoped), so it would otherwise keep
      // showing the previous session's lines. Best-effort.
      await del("/api/live-transcript").catch(() => {});
      selectedSessionId = null;
      currentView = "capture";
      syncHash();
      await refresh();
    },
  });
}

// ---- Routing ----------------------------------------------------------------

/** @param {import('./shell.js').ViewId} id */
function gotoView(id) {
  if (!ALL_VIEWS.includes(id)) return;
  currentView = id;
  syncHash();
  window.scrollTo(0, 0);
  if (lastJson) renderAll(lastJson);
}

function syncHash() {
  if (location.hash.slice(1) !== currentView) {
    history.replaceState(null, "", `#${currentView}`);
  }
}

function viewFromHash() {
  const h = /** @type {import('./shell.js').ViewId} */ (location.hash.slice(1));
  if (ALL_VIEWS.includes(h)) currentView = h;
}

// ---- Tick -------------------------------------------------------------------

/** @param {import('../types.js').AppState} j */
function renderAll(j) {
  const sessions = j.sessions || [];
  const session = resolveSession(sessions, selectedSessionId);
  // Keep selectedSessionId honest so the spine select reflects the resolved
  // session even before the operator explicitly picks one.
  if (session && selectedSessionId == null) selectedSessionId = session.session;
  renderSpine(j, session);
  renderView(j, session);
  // The active-taps rail is global — render it every tick regardless of the
  // active view. active-taps holds only buttons (no focus state to clobber),
  // so renderRegion's focus-guard isn't needed here.
  renderRail(j);
}

async function tick() {
  try {
    const j = await fetchState();
    lastJson = j;
    renderAll(j);
  } catch (e) {
    const spineEl = $("spine");
    if (!spineEl.querySelector(".spine__head")) {
      spineEl.textContent = `state error: ${e}`;
    }
    console.error("Stages tick failed:", e);
  }
}

// Mutation-driven re-render. Paint from the last known state FIRST so a
// UI-only click (expand a transcript, pick a WAV, toggle a row) applies
// instantly from cache instead of stalling on — or dying with — the
// /api/state round-trip, then poll for the authoritative state. The classic
// dashboard had the same contract ("I click and nothing happens until I
// wait" was a real bug report); test_ui_only_click_updates_dom_without_a_
// fresh_poll pins it.
async function refresh() {
  if (lastJson) renderAll(lastJson);
  await tick();
}

// ---- Boot -------------------------------------------------------------------

await loadTemplates(
  // Existing component templates the REUSED components need:
  "/web/components/live-feed.html",
  "/web/components/active-taps.html",
  "/web/components/live-channel.html",
  "/web/components/merged-transcript.html",
  "/web/components/config-card.html",
  // New Stages templates:
  "/web/components/next/spine.html",
  "/web/components/next/views.html",
  "/web/components/next/recordings.html",
  "/web/components/next/taps.html",
  "/web/components/next/people.html",
  "/web/components/next/sessions.html",
  "/web/components/next/summary.html",
);

async function loadModelCatalogs() {
  try {
    const [batchRes, liveRes] = await Promise.all([
      fetch("/api/models?context=batch", { cache: "no-store" }),
      fetch("/api/models?context=live", { cache: "no-store" }),
    ]);
    if (batchRes.ok) modelCatalog = await batchRes.json();
    if (liveRes.ok) liveModelCatalog = await liveRes.json();
    seedEngineModels();
    // Drop any built views that captured the empty catalog so they rebuild
    // with real models on the next render.
    viewCache.clear();
    mountedKey = null;
    if (lastJson) renderAll(lastJson);
  } catch (e) {
    console.error("Failed to load model catalogs:", e);
  }
}

viewFromHash();
initRail();

// Expose a screenshot/automation hook (parity with the prototype's gotoView).
/** @type {any} */ (window).gotoView = gotoView;

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
