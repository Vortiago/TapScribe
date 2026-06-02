// @ts-check
// TapScribe · Stages — Phase-1 entry point for the /next dashboard.
//
// Boots a slim left SPINE (two groups: GLOBAL Taps·People·Settings, pinned;
// THIS SESSION Capture·Recordings·Transcript, numbered) + one dense view at a
// time in the view container. Polls /api/state every 500ms (same `api.js` as
// the classic dashboard), re-renders the spine each tick, and runs the active
// view's per-tick update. Views are BUILT once (or once per session, for
// Transcript) and cached so the reused dashboard components keep their
// scroll/focus/signature state across ticks.
//
// Client-side view routing only — clicking a spine item sets currentView and
// re-renders; we mirror it into location.hash so a reload lands on the same
// view. window.gotoView(name) is exposed for screenshot/automation driving.

import { fetchState, postJson } from "../api.js";
import { loadTemplates } from "../templates.js";
import { ALL_VIEWS, resolveSession, placeholderView } from "./shell.js";
import * as spine from "./components/spine.js";
import * as engine from "./components/engine.js";
import * as captureView from "./views/capture.js";
import * as transcriptView from "./views/transcript.js";
import * as settingsView from "./views/settings.js";
import * as recordingsView from "./views/recordings.js";
import * as tapsView from "./views/taps.js";
import * as peopleView from "./views/people.js";

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

// Engine states: Settings holds the global batch DEFAULT, Transcript holds the
// engine for the open session. Phase 1 keeps both client-side (the transcribe
// wiring lands with Recordings in a later phase); they default to the first
// catalog model once it loads.
/** @type {import('./components/engine.js').EngineState} */
let defaultEngine = { backend: "auto", model: "" };
/** @type {import('./components/engine.js').EngineState} */
let overrideEngine = { backend: "auto", model: "" };
// Recordings holds the engine for the open session's transcribe jobs (one WAV
// / session range). Kept client-side like the others; seeded from the catalog.
/** @type {import('./components/engine.js').EngineState} */
let recordingsEngine = { backend: "auto", model: "" };

// Built-view cache. Capture + Settings are page-singletons; Transcript is
// keyed by session id so a new session rebuilds its merged transcript.
/** @typedef {{ node: DocumentFragment | Node, host?: HTMLElement, update: (j: import('../types.js').AppState, session: import('../types.js').Session | null) => void, rebuildEngine?: () => void, key: string }} BuiltView */
/** @type {Map<string, BuiltView>} */
const viewCache = new Map();
/** @type {string | null} */
let mountedKey = null;

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
  if (!recordingsEngine.model) recordingsEngine = { ...recordingsEngine, model: first.model_id };
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
    },
  });
}

/**
 * Render the session-override (Transcript) engine selector into a host.
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

/**
 * Render the Recordings engine selector into a host. Drives the one-WAV /
 * session-range transcribe jobs for the open session.
 * @param {Element} host
 */
function renderRecordingsEngine(host) {
  engine.render(host, {
    state: recordingsEngine,
    catalog: modelCatalog,
    onChange: (next) => {
      recordingsEngine = next;
      viewCache.get("recordings")?.rebuildEngine?.();
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
    const b = settingsView.build({ rebuildEngine: renderDefaultEngine });
    return { ...b, update: (j) => b.update(j), key: "settings" };
  }
  if (view === "transcript") {
    const b = transcriptView.build({ metaFor, rebuildEngine: renderOverrideEngine });
    return { ...b, key: viewKey("transcript", session) };
  }
  if (view === "recordings") {
    const b = recordingsView.build({
      metaFor,
      engineState: () => recordingsEngine,
      rebuildEngine: renderRecordingsEngine,
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

/** @param {import('../types.js').AppState} j @param {import('../types.js').Session | null} session */
function renderSpine(j, session) {
  spine.render($("spine"), j, {
    currentView,
    session,
    metaFor,
    onSelectView: gotoView,
    onSelectSession: (id) => {
      selectedSessionId = id;
      // A fresh session pick lands you on the live stage if it's recording,
      // else the transcript — matching the prototype's session-switch.
      const picked = (lastJson?.sessions || []).find((s) => s.session === id);
      currentView = picked?.is_current ? "capture" : "transcript";
      syncHash();
      if (lastJson) renderAll(lastJson);
    },
    onNewSession: async () => {
      if (!confirm("Start a new recording session?\n\nNew utterances land in a fresh folder; in-progress ones finish in their current folder.")) return;
      try { await postJson("/api/new-session"); }
      catch (e) { alert(`Failed to start new session: ${e}`); return; }
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

async function refresh() {
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
