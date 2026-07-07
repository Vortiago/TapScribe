// @ts-check
// Stages · Capture (SESSION stage 1). Live IRC captions + the live-channel
// control + capture health + the per-session recording/prompt/hotwords
// overrides. (The active taps that used to sit in the aside now live in the
// global #tapsRail — see js/next/main.js — so they're visible on every view.)
//
// REUSES the classic dashboard components verbatim (imported, not copied):
//   - live-feed.js      → the IRC captions stream
//   - live-channel.js   → the live-channel model/lang/gate form + start/stop
//
// The view is BUILT ONCE for the page (its component host elements, esp.
// live-channel's bodyEl, then persist), and `update(j, session)` re-runs the
// live-data components + refreshes the session-dependent header/overrides each
// poll tick. This mirrors main.js's mount-once / render-per-tick model so
// live-channel's own editing + signature guards keep working and the captions
// stream survives scroll across ticks.

import { tpl, pick } from "../../templates.js";
import { putJson, postJson, del } from "../../api.js";
import { header, strong, inline } from "../shell.js";
import * as liveFeed from "../../components/live-feed.js";
import * as liveChannel from "../../components/live-channel.js";
import { fillLanguageOptions, setSelectedLanguages, selectedLanguages } from "../components/language-picker.js";

/** @param {string} s */
const clip = (s) => (s.length > 60 ? s.slice(0, 60) + "…" : s);

/** Set a capture-health tile value, dimming empty/em-dash placeholders so a
 * missing metric recedes while real values stay bright. */
/** @param {HTMLElement} el @param {string} value */
const setHealth = (el, value) => {
  el.textContent = value;
  el.classList.toggle("is-empty", value === "" || value === "—");
};

/**
 * @param {{
 *   liveCatalog: import('../../types.js').ModelCatalog,
 *   languageCatalog: import('../../types.js').LanguageCatalog,
 *   metaFor: (s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta,
 *   onLiveStart: () => void,
 *   onLiveStop: () => void,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { liveCatalog, languageCatalog, metaFor, onLiveStart, onLiveStop, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-capture");

  const headHost = pick(frag, "head");
  const liveFeedCtx = {
    countEl: pick(frag, "liveFeedCount"),
    shell: pick(frag, "liveFeedShell"),
    autoscrollEl: pick(frag, "liveAutoScroll"),
  };
  const liveChannelHosts = {
    stateEl: pick(frag, "liveStateBadge"),
    mlxEl: pick(frag, "liveMlxNote"),
    bodyEl: pick(frag, "liveChannelBody"),
  };
  const healthHosts = {
    hLive: pick(frag, "hLive"),
    hRec: pick(frag, "hRec"),
    hLag: pick(frag, "hLag"),
    hChan: pick(frag, "hChan"),
  };
  const recPill = /** @type {HTMLButtonElement} */ (pick(frag, "recPill"));
  const liveClear = /** @type {HTMLButtonElement} */ (pick(frag, "liveClear"));
  const promptTa = /** @type {HTMLTextAreaElement} */ (pick(frag, "capPrompt"));
  const hotwordsTa = /** @type {HTMLTextAreaElement} */ (pick(frag, "capHotwords"));
  const promptSave = /** @type {HTMLButtonElement} */ (pick(frag, "capPromptSave"));
  const hotwordsSave = /** @type {HTMLButtonElement} */ (pick(frag, "capHotwordsSave"));
  const promptStatus = pick(frag, "capPromptStatus");
  const hotwordsStatus = pick(frag, "capHotwordsStatus");
  const languagesSel = /** @type {HTMLSelectElement} */ (pick(frag, "capLanguages"));
  const languagesSave = /** @type {HTMLButtonElement} */ (pick(frag, "capLanguagesSave"));
  const languagesStatus = pick(frag, "capLanguagesStatus");
  const ovrNote = pick(frag, "ovrNote");

  // The candidate-language options are a static catalog — fill once at build
  // (the per-tick update only re-seeds the SELECTION on a session change).
  fillLanguageOptions(languagesSel, languageCatalog);

  // Latest poll snapshot + focused session, refreshed by update(). The wired
  // handlers below read these so a click always uses the current state.
  /** @type {import('../../types.js').AppState | null} */
  let latest = null;
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  /** @type {string | null} */
  let lastSessionId = null; // null = not seeded yet, so the first update always seeds overrides

  recPill.addEventListener("click", async () => {
    const enabled = !((latest?.recording_enabled ?? true) !== false);
    recPill.disabled = true;
    try { await postJson("/api/recording/toggle", { enabled }); }
    catch (e) { alert(`Recording toggle failed: ${e}`); }
    finally { recPill.disabled = false; afterMutate(); }
  });

  // Clear the live captions → DELETE /api/live-transcript (the classic
  // dashboard's "clear"). invalidate() forces live-feed to repaint the now-
  // empty shell on the next tick.
  liveClear.addEventListener("click", async () => {
    liveClear.disabled = true;
    try { await del("/api/live-transcript"); }
    catch (e) { alert(`Clear captions failed: ${e}`); }
    finally { liveClear.disabled = false; liveFeed.invalidate(); afterMutate(); }
  });

  // Per-session override saves → PUT /api/session-meta/{id}. Empty value clears
  // the override (falls back to the global default). Bound once; the target
  // session id is read from the `session` closure var at click time. `getValue`
  // returns the field's payload — a textarea string for prompt/hotwords, the
  // selected codes array for languages (write_session_meta accepts both).
  /** @param {"prompt"|"hotwords"|"languages"} key @param {() => unknown} getValue @param {HTMLButtonElement} btn @param {HTMLElement} status */
  const wireOverride = (key, getValue, btn, status) => {
    btn.addEventListener("click", async () => {
      if (!session) return;
      const sid = session.session;
      btn.disabled = true;
      status.textContent = "saving…";
      try {
        await putJson(`/api/session-meta/${encodeURIComponent(sid)}`, { [key]: getValue() });
        status.textContent = "saved";
        setTimeout(() => { if (status.textContent === "saved") status.textContent = ""; }, 1500);
      } catch (e) {
        status.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
      } finally { btn.disabled = false; afterMutate(); }
    });
  };
  wireOverride("prompt", () => promptTa.value, promptSave, promptStatus);
  wireOverride("hotwords", () => hotwordsTa.value, hotwordsSave, hotwordsStatus);
  wireOverride("languages", () => selectedLanguages(languagesSel), languagesSave, languagesStatus);

  // live-feed has a module-level signature cache; clear it once at build so
  // the first update populates this view's fresh (empty) shell.
  liveFeed.invalidate();

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    latest = j;
    session = sess;
    const active = j.active || [];
    const liveCount = active.filter((a) => a.live !== false).length;
    const recEnabled = j.recording_enabled !== false;
    const li = j.live_info || {};

    header(headHost, {
      eyebrow: "Session · 1 Capture",
      title: "Capture",
      sub: {
        // Read session_meta.label directly (== metaFor(sess).label) so the
        // per-tick sig doesn't allocate a throwaway EffectiveMeta; build() still
        // uses metaFor(), but that only runs past the gate on a real change.
        sig: `${recEnabled ? 1 : 0}§${sess ? sess.session_meta?.label || sess.session : ""}`,
        build: () =>
          inline(
            "live IRC captions · recorder ",
            strong(recEnabled ? "armed" : "paused"),
            sess ? " · " : "",
            sess ? strong(metaFor(sess).label || sess.session) : "",
          ),
      },
    });

    // Reused components — each touches only its passed hosts. (The active-taps
    // rows moved to the global #tapsRail, rendered by main.js every tick.) The
    // live captions are scoped to the focused session: the deque is global, but
    // each line carries its session, so an archived session shows only its own
    // (aged-out → empty) lines, never the live session's captions.
    liveFeed.render(j, {
      ...liveFeedCtx,
      sessionId: sess?.session || "",
      isCurrent: !!sess?.is_current,
    });
    liveChannel.render(j, {
      ...liveChannelHosts,
      liveCatalog,
      onAction: { start: onLiveStart, stop: onLiveStop },
    });

    setHealth(healthHosts.hLive, `${liveCount} / ${active.length}`);
    setHealth(healthHosts.hRec, `${active.filter((a) => a.record !== false).length} / ${active.length}`);
    const lags = /** @type {number[]} */ (active.map((a) => a.lag_s).filter((l) => typeof l === "number"));
    setHealth(healthHosts.hLag, lags.length ? `${Math.max(...lags).toFixed(1)}s` : "—");
    setHealth(healthHosts.hChan, li.state || "stopped");

    recPill.textContent = recEnabled ? "● recording" : "⏸ paused";
    recPill.classList.toggle("is-on", recEnabled);
    recPill.classList.toggle("is-paused", !recEnabled);

    // Clear wipes the GLOBAL live-caption deque, so only the live session owns
    // it. Off the current session the panel shows another session's lines (or
    // none), where a Clear would surprisingly nuke the live captions — disable
    // and hide it there.
    liveClear.disabled = liveClear.hidden = !sess?.is_current;

    // Override fields — re-seed when the focused session changes (but never
    // clobber an in-progress edit on the same session).
    const sid = sess?.session || "";
    const gPrompt = j.prompt?.content || "";
    const gHotwords = j.hotwords?.content || "";
    promptTa.placeholder = gPrompt ? `inherits global (${clip(gPrompt)})` : "no global default";
    hotwordsTa.placeholder = gHotwords ? `inherits global (${clip(gHotwords)})` : "no global default";
    if (sid !== lastSessionId) {
      lastSessionId = sid;
      const meta = sess ? metaFor(sess) : null;
      promptTa.value = meta?.prompt || "";
      hotwordsTa.value = meta?.hotwords || "";
      // Seed the candidate-language override from this session's meta (empty =
      // no override → inherits the global default). Re-seeded only on a session
      // switch, so a poll never clobbers an in-progress selection edit.
      setSelectedLanguages(languagesSel, meta?.languages || []);
      const disabled = !sess;
      promptTa.disabled = hotwordsTa.disabled = disabled;
      promptSave.disabled = hotwordsSave.disabled = disabled;
      languagesSel.disabled = languagesSave.disabled = disabled;
      ovrNote.textContent = sess
        ? "blank → inherits the global Settings default"
        : "no session selected — overrides need a session";
    }
  };

  return { node: frag, update };
}
