// @ts-check
// gate-allow: signal-listener — boot-time wiring on page-lifetime singletons (rail buttons, document visibility, window wake); main.js runs once per page, so these listeners are deliberately permanent.
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

import { fetchState, postJson, putJson, errText } from "../api.js";
import { loadTemplates, mount, pick, consumeDeferredRender, interactionHeld, wireErrorBar } from "../templates.js";
import { warmProgress } from "../vc/components/progress/progress.js";
import { warmEmptyState } from "../vc/components/empty-state/empty-state.js";
import { ALL_VIEWS, resolveSession, placeholderView } from "./shell.js";
import { createPollPacer, FAST_MS } from "./poll-pacer.js";
import { createPlayerHost } from "./player-host.js";
import { dropCaughtUpSessionLabels, setSessionLabelRepaint } from "./session-labels.js";
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

// Adaptive /api/state cadence (issue #247, ADR-0013): fast while anything moves,
// back off to 2s when idle-and-unchanged, snap back on change/interaction.
// Declared here (module init) so refresh() and the visibility handler — both
// defined before the poll loop — reference an already-constructed pacer.
const pacer = createPollPacer();

// A poll is "active" (never back off) when a job, tap, or the live channel is
// in-flight.
/** @param {import('../types.js').AppState | null} j */
function stateHasActivity(j) {
  if (!j) return false;
  if (Array.isArray(j.active) && j.active.length > 0) return true;
  // The live channel can be mid-startup ("starting" — a silent model load) or
  // streaming ("running") with no entry in j.active and no session job; keep
  // polling fast so its ready/error transition and captions don't lag a
  // backoff interval behind.
  const live = j.live_info && j.live_info.state;
  if (live === "starting" || live === "running") return true;
  return Array.isArray(j.sessions) && j.sessions.some((s) => s.progress != null);
}

// Resolver for the in-flight BACKOFF sleep, so wake() can cut a 2s idle wait
// short and poll NOW. Null whenever no interruptible sleep is pending.
/** @type {(() => void) | null} */
let _interruptSleep = null;
/** @param {number} ms @returns {Promise<void>} */
function pacedSleep(ms) {
  return new Promise((resolve) => {
    const t = setTimeout(() => {
      _interruptSleep = null;
      resolve();
    }, ms);
    // Only a backed-off (slow) sleep is worth interrupting. Interrupting a fast
    // sleep would let a burst of interaction events (key auto-repeat, typing)
    // poll /api/state once per keystroke — the opposite of the backoff's point.
    // A fast sleep already resolves within FAST_MS, and pacer.wake() has reset
    // the streak so the next sleep stays fast regardless.
    _interruptSleep = ms > FAST_MS
      ? () => {
          clearTimeout(t);
          _interruptSleep = null;
          resolve();
        }
      : null;
  });
}
// Operator activity (click, keypress, focusing a control, tab re-show, a
// mutation) resets the pacer to fast AND interrupts any in-flight backoff so
// the next poll fires promptly — the interaction hold defers renders while a
// control is focused, and a stale hold would apply up to a full idle interval
// late on release.
function wake() {
  pacer.wake();
  if (_interruptSleep) _interruptSleep();
}
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
// Candidate-language catalog (ADR-0010) — the selectable languages for the
// per-meeting + global pickers. Loaded once at boot alongside the models.
/** @type {import('../types.js').LanguageCatalog} */
let languageCatalog = { languages: [], default: [], specialists: {} };

// Engine state: Settings holds the global batch DEFAULT (the ADR-0010
// generalist, batch-model.txt). The Transcript stage no longer has its own
// engine selector — the operator declares LANGUAGES there, not a model
// (ADR-0011), and its transcribe jobs resolve the generalist server-side. Kept
// client-side and seeded from the first catalog model once it loads.
/** @type {import('./components/engine.js').EngineState} */
let defaultEngine = { backend: "auto", model: "" };

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
 * Effective per-session meta. `aliases` is the SERVER-RESOLVED speaker-name map
 * (`session.names`, ADR-0009): the slug → display name the server already
 * resolved through per-session Override > Person name > bridge/roster default.
 * Layering it over the raw `session_meta.aliases` means a global Person rename
 * propagates to this session's transcript with no client-side join — and an old
 * rosterless session (empty `names`) still renders via its retained aliases.
 * @param {import('../types.js').Session | null} s
 * @returns {import('../types.js').EffectiveMeta}
 */
function metaFor(s) {
  const m = (s && s.session_meta) || {};
  return {
    label: m.label || "",
    aliases: { ...(m.aliases || {}), ...((s && s.names) || {}) },
    prompt: m.prompt || "",
    hotwords: m.hotwords || "",
    languages: m.languages || [],
  };
}

/** Seed the Settings engine model id from the catalog once it loads. */
function seedEngineModels() {
  const first = modelCatalog.models[0];
  if (!first) return;
  if (!defaultEngine.model) defaultEngine = { ...defaultEngine, model: first.model_id };
}

// One-shot adoption of the operator's persisted batch default (batch-model.txt,
// surfaced as batch_model_default in /api/state). Runs once on the first poll —
// never per tick, so the poll can't clobber a Settings select the operator has
// open (Interaction hold) — and a user change flips the flag too, so a slow
// first poll can't overwrite a pick made before it landed.
let defaultEngineAdoptedSaved = false;
/** @param {import('../types.js').AppState} j */
function adoptSavedBatchModel(j) {
  if (defaultEngineAdoptedSaved) return;
  defaultEngineAdoptedSaved = true;
  const saved = j.batch_model_default || "";
  if (!saved || saved === defaultEngine.model) return;
  defaultEngine = { ...defaultEngine, model: saved };
  viewCache.get("settings")?.rebuildEngine?.();
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
      // The user's pick wins over a not-yet-landed first poll's saved value…
      defaultEngineAdoptedSaved = true;
      // …and persists as the operator default (batch-model.txt) — the same
      // value the end-of-meeting pipeline resolves its transcribe stage from.
      putJson("/api/config/batch-model", { content: next.model }).catch((e) => {
        alert(`Save batch model failed: ${errText(e)}`);
      });
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

// `bodyEl` is the specific live-channel host (Capture's or Taps') the
// start/apply click came from — see live-channel.js's formValues(host) and
// #254: reading the form via a global document lookup instead of the
// instance that was actually clicked broke as soon as more than one view's
// live-channel body could be alive at once.
/** @param {HTMLElement} bodyEl */
const liveStart = async (bodyEl) => {
  try {
    const { formValues } = await import("../components/live-channel.js");
    await postJson("/api/live/start", formValues(bodyEl));
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
// sees live taps everywhere). The rec/live toggle click delegation is wired
// via activeTaps.wireToggles (bound ONCE on the rail body — see its own
// docs), the same helper the Taps view's own row list uses for its host.

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
    activeTaps.wireToggles(railCtx.bodyEl, { afterMutate: () => { refresh(); } });
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
    // Deliberately a PLAIN synchronous mount, not withTransition: the poll
    // loop's next tick (and the rest of THIS tick's built.update() call,
    // immediately below) assumes the new view is fully attached the instant
    // this line returns. withTransition can't give that — its mutation only
    // runs inside document.startViewTransition's callback, which the browser
    // may defer past the current microtask; under load, a poll tick or an
    // interaction-hold sweep can observe the OLD view still attached (its
    // childElementCount > 0 check passes vacuously) and tag/act on stale
    // nodes that the deferred mount then rips out from under it — exactly
    // the "don't read the new DOM synchronously after the call" trap the
    // canon docs warn about (render.js). Tried and reverted (#310): see
    // git history for the withTransition attempt this replaced.
    mount(root, built.host ?? built.node);
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
      languageCatalog,
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
      languageCatalog,
      applyLiveModel,
      afterMutate: () => { refresh(); },
    });
    return { ...b, update: (j) => b.update(j), key: "settings" };
  }
  if (view === "transcript") {
    const b = transcriptView.build({
      metaFor,
      languageCatalog,
      afterMutate: () => { refresh(); },
      player,
    });
    return { ...b, key: viewKey("transcript", session) };
  }
  if (view === "summary") {
    const b = summaryView.build({
      afterMutate: () => { refresh(); },
    });
    return { ...b, key: "summary" };
  }
  if (view === "recordings") {
    const b = recordingsView.build({
      afterMutate: () => { refresh(); },
      player,
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
      player,
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
      // The live captions are session-scoped now (live-feed filters by the
      // focused session), so the previous session's lines never leak into the
      // new session's view — no global clear needed. The just-rotated session
      // keeps its trailing captions until they age out of the deque.
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
  // Dev/test-only counter (types.d.ts declares the global): how many times
  // renderAll has actually run, so an e2e test can prove idle 304 ticks stop
  // re-running the whole spine/view sig pipeline (issue #245). Never read by
  // production code.
  globalThis.__TAPSCRIBE_RENDER_ALL_COUNT = (globalThis.__TAPSCRIBE_RENDER_ALL_COUNT || 0) + 1;
  const sessions = j.sessions || [];
  const session = resolveSession(sessions, selectedSessionId);
  // Keep selectedSessionId honest so the spine select reflects the resolved
  // session even before the operator explicitly picks one.
  if (session && selectedSessionId == null) selectedSessionId = session.session;
  adoptSavedBatchModel(j);
  // Drop pending renames the server has caught up to, ONCE per tick and BEFORE
  // anything computes a label sig — two views read the same overlay (#355), so
  // the sweep belongs to the tick's owner rather than to whichever of them
  // happens to render first (ADR-0004: the hold lives at shared seams, not per
  // view). Its doc carries the why.
  dropCaughtUpSessionLabels(sessions);
  renderSpine(j, session);
  renderView(j, session);
  // The active-taps rail is global — render it every tick regardless of the
  // active view. active-taps holds only buttons (no focus state to clobber),
  // so renderRegion's focus-guard isn't needed here.
  renderRail(j);
}

/**
 * Poll /api/state once and render if it changed. Returns the pacer signal for
 * the poll loop ({changed, active}), or null if the poll threw.
 * @returns {Promise<import('./poll-pacer.js').PollSignal | null>}
 */
async function tick() {
  try {
    const j = await fetchState();
    // fetchState() reuses the SAME object on a 304 (api.js). When the poll
    // is a genuine no-op, skip the whole renderAll pass — the spine's
    // O(sessions) signature, the active view's own signature, and the
    // active-taps rail all get recomputed for nothing otherwise (issue
    // #245). consumeDeferredRender() is read (and cleared) unconditionally,
    // BEFORE the render, so a render that a guard deferred last pass gets one
    // retry this pass regardless of branch — leaving it set across a
    // real-change tick would strand a stale "retry owed" past the point the
    // interaction actually cleared. If it's still blocked, the guard
    // re-marks it for the pass after.
    const unchanged = j === lastJson;
    lastJson = j;
    const wasDeferred = consumeDeferredRender();
    const signal = { changed: !unchanged, active: stateHasActivity(j) };
    if (unchanged && !wasDeferred) return signal;
    renderAll(j);
    return signal;
  } catch (e) {
    const spineEl = $("spine");
    if (!spineEl.querySelector(".spine__head")) {
      spineEl.textContent = `state error: ${e}`;
    }
    console.error("Stages tick failed:", e);
    return null;
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
  wake();
  if (lastJson) renderAll(lastJson);
  await tick();
}

// ---- Boot -------------------------------------------------------------------

// Surface listener exceptions / unhandled rejections in #errbar and beacon a
// truncated copy to POST /api/client-errors — the one place an LLM session
// maintaining the dashboard can actually read a browser-side failure.
wireErrorBar();

/** The dashboard's ONE Player, bound to the shell's static audio element (it is
 * declared in next.html, outside #viewRoot, precisely so no render can detach
 * it — ADR-0017). Views receive it through their build ctx. */
const player = createPlayerHost({
  bar: $("playerBar"),
  media: pick(document, "player"),
  name: pick(document, "playerName"),
  msg: pick(document, "playerMsg"),
});

await Promise.all([
  loadTemplates(
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
  ),
  // vc atoms the shell/views build synchronously (createXSync): warm once
  // here so the sync path is safe everywhere after boot.
  warmProgress(),
  warmEmptyState(),
]);

async function loadModelCatalogs() {
  try {
    const [batchRes, liveRes, langRes] = await Promise.all([
      fetch("/api/models?context=batch", { cache: "no-store" }),
      fetch("/api/models?context=live", { cache: "no-store" }),
      fetch("/api/languages", { cache: "no-store" }),
    ]);
    if (batchRes.ok) modelCatalog = await batchRes.json();
    if (liveRes.ok) liveModelCatalog = await liveRes.json();
    if (langRes.ok) languageCatalog = await langRes.json();
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
// The session-label saver's repaint kick — the same `afterMutate` every view
// build gets, wired once here because the saver is a module singleton shared by
// the Sessions list and the spine's rename card (#355). Without it a settled
// rename would wait out the poll backoff in one of the two places.
setSessionLabelRepaint(() => { refresh(); });

// Expose a screenshot/automation hook (parity with the prototype's gotoView).
/** @type {any} */ (window).gotoView = gotoView;

let _catalogLoaded = false;
(async () => {
  for (;;) {
    let delay = FAST_MS;
    if (document.visibilityState === "visible") {
      if (!_catalogLoaded) {
        _catalogLoaded = true;
        loadModelCatalogs();
      }
      const signal = await tick();
      // No signal = the poll threw; retry at the fast cadence rather than
      // letting a transient error stall the loop into a backoff. Fold in
      // interactionHeld(): never back off while the operator holds a focused
      // control or a text selection, so a render the interaction hold deferred
      // catches up on the next fast tick after release, not a backoff interval
      // later (ADR-0004).
      delay = signal
        ? pacer.record({ changed: signal.changed, active: signal.active || interactionHeld() })
        : FAST_MS;
    }
    await pacedSleep(delay);
  }
})();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    wake();
    tick();
  }
});
// Operator interaction snaps the poll back to the fast cadence: a click,
// keypress, or focusing a control means they're working, so live awareness
// (and the interaction hold's next-tick catch-up) must not lag on a backoff.
for (const ev of ["pointerdown", "keydown", "focusin"]) {
  window.addEventListener(ev, wake, { passive: true });
}
