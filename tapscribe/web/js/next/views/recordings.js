// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
// Stages · Recordings (SESSION stage 2). The per-session AUDIO-FILES stage: a
// real waveform (an isolated <canvas> drawn from server-computed peaks)
// sitting above the REAL strip-silence knobs + the per-WAV list (originals +
// indented stripped region clips) with an original/stripped source toggle.
// Transcription (the engine selector, transcribe controls, and per-WAV cache)
// moved to the Transcript stage — Recordings is files + silence-stripping only.
//
// The strip controls + per-WAV list come from the same endpoints (POST
// /api/sessions/{s}/strip-silence, DELETE /api/sessions/{s}/stripped). The
// waveform fetches peaks lazily from /api/wav/{s}/{name}/peaks; the cut
// overlay on top of it lands in a later slice.
//
// The WAV list itself is built for HUGE sessions (hundreds–thousands of
// files). Two pieces keep it snappy:
//   1. The file listing is NOT on /api/state — it's fetched lazily through
//      next/session-files.js, which owns the in-flight dedupe and the
//      cold-vs-stale sentinel, so it crosses the wire once per change, not
//      every poll. `currentFiles` holds the resolved list for the focused
//      session.
//   2. The list is a KEYED LIST rendered through `renderList` (templates.js —
//      keyed, in-place, never replaceChildren) and each row carries
//      `content-visibility: auto` (next.css `.wavrow`), so the browser skips
//      layout/paint of off-screen rows and a selection / expand / poll tick
//      never rebuilds thousands of nodes. Selection is a term in the row's
//      itemSig, so picking a WAV repaints two rows; the per-row transcript
//      expand is a native <details> that lazy-fetches its body on first open.
//
// Built once for the page; `update(j, session)` refreshes stats / strip-job
// progress each tick and hands the list to `renderList`, which reconciles only
// when the file set or selection changes (files_sig / source / selected name)
// and defers while text is selected inside it.

import { tpl, mount, pick, renderList, markListStale } from "../../templates.js";
import { postJson, del, wavTranscript, wavePeaks, wavStripMeta, fetchStripPreview, wavUrl, errText } from "../../api.js";
import { createFilesSource, listState } from "../session-files.js";
import { fmtBytes, fmtDur, fmtClock, fmtMs, fmtMmSs, truncMid } from "../../formatters.js";
import { header, strong, inline, buildSourceToggle, renderJobBar, effectiveSource, setSourcePick, clearSourcePick, sessionLabel } from "../shell.js";
import { setDimmable } from "../ui.js";
import { createWaveform } from "../components/waveform.js";

/** Strip-silence knob defaults — mirror STRIP_OPT_DEFAULTS / the server-side
 * fallbacks in api_session_strip_silence (tapscribe/routes/strip.py). */
const STRIP_DEFAULTS = Object.freeze({ min_silence_ms: 500, pad_ms: 200, speech_floor_db: -45 });

/** @typedef {{ min_silence_ms: number, pad_ms: number, speech_floor_db: number }} StripKnobs */

/**
 * @param {{
 *   afterMutate: () => void,
 *   player: ReturnType<typeof import('../components/player.js').createPlayer>,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { afterMutate, player } = ctx;
  const frag = tpl("tpl-next-view-recordings");

  const headHost = pick(frag, "head");
  const waveName = pick(frag, "waveName");
  // Isolated canvas waveform — mounted once into the hero; update() feeds it
  // the selected WAV's peaks (lazy + client-cached) as the selection changes.
  const waveform = createWaveform();
  const waveHost = pick(frag, "waveHost");
  waveHost.appendChild(waveform.node);

  // The waveform is a CONTROL surface too (#191): a click on it is a seek target
  // on the ORIGINAL the hero is showing — the hero shows an original in both
  // toggle states, so that is the only file a click here can mean.
  waveform.onSeek((offsetS) => {
    const sel = selectedFor();
    if (!session || !sel) return;
    const loaded = player.loaded();
    const sid = session.session;
    // Already playing this exact file? Then a click is a SEEK, not a reload —
    // reloading would drop the buffer and restart the fetch mid-listen.
    if (loaded && loaded.session === sid && loaded.name === sel.name && loaded.source === "original") {
      player.seek(offsetS);
      return;
    }
    player.load({ session: sid, name: sel.name, source: "original", offsetS });
  });

  // STRICT IDENTITY: draw a position only while the Player holds the very file
  // the canvas is drawing (always an original of the focused session). Playing a
  // stripped clip, another WAV, or another session's audio draws nothing rather
  // than a position on a timeline that isn't running (ADR-0017). Driven by the
  // Player's frame loop, never by the poll — so it costs nothing when idle.
  player.onTick((loaded, currentTime) => {
    // SKIP while this view's host is off-document — do NOT unsubscribe. Views are
    // CACHED and re-mounted (main.js keeps this instance under "recordings"), so
    // "detached" means "the operator is on another stage", not "dead". Retiring
    // the subscription here killed the playhead permanently for the rest of the
    // page — on the very stage walk ADR-0017 exists to support. Staying
    // subscribed costs one bounded closure per evicted view.
    if (!waveHost.isConnected) return;
    // Cheap identity terms FIRST, and compare against `shownWav` — the name
    // `drawWaveform` recorded — rather than re-deriving the selection. This runs
    // once per animation frame, and `selectedFor()` is a linear scan of the
    // session's WAV listing; the canvas already knows what it drew.
    const shown =
      !!loaded
      && loaded.source === "original"
      && !!session
      && loaded.session === session.session
      && loaded.name === shownWav;
    waveform.setPlayhead(shown ? currentTime : null);
  });
  const stats = {
    clips: pick(frag, "sClips"),
    speech: pick(frag, "sSpeech"),
    in: pick(frag, "sIn"),
    kept: pick(frag, "sKept"),
  };
  const knobVals = {
    min_silence_ms: pick(frag, "kvMinSilence"),
    pad_ms: pick(frag, "kvPad"),
    speech_floor_db: pick(frag, "kvFloor"),
  };
  const playKeptBtn = /** @type {HTMLButtonElement} */ (pick(frag, "playKeptBtn"));
  const stripBtn = /** @type {HTMLButtonElement} */ (pick(frag, "stripBtn"));
  const clearBtn = /** @type {HTMLButtonElement} */ (pick(frag, "clearBtn"));
  const jobBar = pick(frag, "jobBar");
  const jobLabel = pick(frag, "jobLabel");
  const jobCount = pick(frag, "jobCount");
  const jobProgress = /** @type {HTMLElement} */ (pick(frag, "jobProgress"));
  const jobWav = pick(frag, "jobWav");
  const wavHint = pick(frag, "wavHint");
  const wavList = pick(frag, "wavList");
  /** The empty/loading placeholder — a hidden-toggled SIBLING of `wavList`, so
   * the rows host's children belong to renderList alone (recordings.html). */
  const wavEmpty = /** @type {HTMLElement} */ (pick(frag, "wavEmpty"));

  /** Invalidate BOTH of this view's gates and repaint. One owner for "what a
   * mutate invalidates", so a new gate is added here rather than at five call
   * sites. Used where the server-side `files_sig` may still lag the mutation
   * (a strip, a clear, a delete, a landed listing fetch) — never where the
   * changed value is already a term in the list's `sig`. */
  const repaintAfterMutate = () => {
    lastChromeSig = " ";
    markListStale(wavList);
    afterMutate();
  };

  // ---- View-local state -----------------------------------------------------
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  /** @type {StripKnobs} */
  const knobs = { ...STRIP_DEFAULTS };
  /** Selected original WAV name, per session id (drives the waveform header). */
  /** @type {Map<string, string>} */
  const selectedWav = new Map();
  /** The lazily-fetched WAV listing for the FOCUSED session — the array
   * /api/state no longer embeds. Refreshed at the top of update() from the
   * (sid, files_sig)-keyed client cache; [] until the first fetch lands. Every
   * helper that used to read session.files reads this instead. */
  /** @type {import('../../types.js').WavFile[]} */
  let currentFiles = [];
  /** The lazily-fetched WAV listing for the focused session — owns the
   * in-flight dedupe and the cold-vs-stale sentinel (next/session-files.js). */
  const filesSource = createFilesSource({
    onLoaded: () => {
      repaintAfterMutate();
    },
  });
  /** Last strip-silence response stats, per session id (overlay on s.stripped). */
  /** @type {Map<string, import('../../types.js').StripSilenceResult>} */
  const lastStrip = new Map();
  /** Sessions with a strip POST in flight (the job snapshot also flags this). */
  /** @type {Set<string>} */
  const stripInflight = new Set();
  let lastChromeSig = " "; // sentinel so the first update always paints the chrome
  // Waveform render state. `lastWaveSig` is the canvas's OWN small signature
  // (selected WAV · source · size · load-state) so a per-second strip/transcribe
  // job tick — which churns the body's signature — never rebuilds the O(bins)
  // canvas (render-signature hygiene). The fetch-once / no-refetch-after-failure
  // bookkeeping for both lazy bodies the canvas needs (peaks, committed cut) is
  // the `remember-error` policy on their resources (api.js) — the canvas only
  // has to say what to do when one lands.
  let lastWaveSig = " ";
  /** The ORIGINAL WAV name the canvas is currently drawing, recorded by
   * `drawWaveform` — the one owner of what the canvas shows. The playhead's
   * strict-identity rule compares against this instead of re-resolving the
   * selection on every animation frame. */
  /** @type {string | null} */
  let shownWav = null;
  /** The COMMITTED cut spans the canvas is drawing, or null. The live preview
   * (`livePreview`) takes precedence over this when one is up — together they
   * answer "which cut is on screen right now", which is what ▶ kept plays. */
  /** @type {import('../../types.js').CutSpan[] | null} */
  let shownCut = null;

  /** Either lazy body the canvas draws from landed — OR failed, which is equally
   * something to show. Redraw the canvas ONLY (the body didn't change):
   * re-resolve the current selection in case it moved while the fetch was in
   * flight, and reset just the wave sig so this redraw isn't skipped — no full
   * body rebuild and no extra /api/state poll. Both watchers share it because the
   * canvas is what either body feeds. (`drawWaveform`/`selectedFor` are consts
   * declared further down; this only runs on a landed fetch, never at build.) */
  const redrawCanvas = () => {
    lastWaveSig = " ";
    drawWaveform(selectedFor());
  };
  /** The two lazy bodies the hero canvas draws from, watched for this view. */
  const heroPeaks = wavePeaks.watch(redrawCanvas);
  const heroCut = wavStripMeta.watch(redrawCanvas);

  /** Live strip-preview bookkeeping (#89). At most ONE preview is live at a
   * time: `livePreview` pins the latest response to the exact waveKey it was
   * computed for, so a selection/source/session change drops it instead of
   * overlaying (or re-stating stats for) the wrong WAV; `previewToken` makes
   * the debounced fetches latest-wins. */
  /** @type {{ key: string, p: import('../../types.js').StripPreview } | null} */
  let livePreview = null;
  let previewToken = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let previewTimer = null;

  // ---- Helpers --------------------------------------------------------------

  /** Identity of what the canvas shows — shared by the draw guard and the
   * preview fire/land/pin checks, which must agree byte-for-byte. */
  /** @param {string} sid @param {string} name @param {number} size */
  const waveKey = (sid, name, size) => `${sid}/${name}@original@${size}`;

  /** The live knob preview IF it belongs to the focused session, else null.
   * `livePreview` is pinned to the exact waveKey it was computed for, so this
   * one predicate is what stops a stale preview being read against the wrong
   * session — spelled once here rather than at each reader. */
  const sessionPreview = () => {
    const sid = session?.session || "";
    return livePreview && sid && livePreview.key.startsWith(`${sid}/`) ? livePreview.p : null;
  };

  /** The cut spans the canvas is CURRENTLY showing: a live knob preview when
   * one is up, else the committed cut, else none. One resolver so ▶ kept's
   * enablement and its click can never disagree about what would play. */
  const shownSpans = () => sessionPreview()?.spans ?? shownCut;

  /** ▶ kept is offered only when there IS a cut on screen and a WAV to play it
   * from — with nothing drawn there is no "kept audio" to mean anything. */
  const syncPlayKept = () => {
    const spans = shownSpans();
    // The cut on screen just changed. If kept playback is live on the WAV this
    // canvas is drawing, re-aim it: otherwise the audio keeps hopping the
    // PREVIOUS cut's gaps while the overlay shows the new one, so the operator
    // isn't hearing the cut they're judging. Inert when not playing kept audio.
    const loaded = player.loaded();
    if (loaded && loaded.name === shownWav && loaded.source === "original") {
      player.setKeptSpans(spans);
    }
    const enabled = !!shownWav && !!spans && spans.length > 0;
    playKeptBtn.disabled = !enabled;
    playKeptBtn.title = enabled
      ? "Play only the audio this cut would keep"
      : "Drag a knob (or strip this session) to preview a cut first";
  };

  playKeptBtn.addEventListener("click", () => {
    const spans = shownSpans();
    if (!session || !shownWav || !spans || !spans.length) return;
    // The ORIGINAL is what the cut is measured against — the hero always draws
    // the original, and the kept spans are offsets into it.
    player.load({ session: session.session, name: shownWav, source: "original", keptSpans: spans });
  });

  /** Resolve the selected original WAV for the focused session (first if
   * unset). Reads the lazily-fetched `currentFiles`, not session.files (which
   * /api/state no longer ships). */
  const selectedFor = () => {
    if (!session || !currentFiles.length) return null;
    const want = selectedWav.get(session.session);
    return currentFiles.find((f) => f.name === want) ?? currentFiles[0] ?? null;
  };

  /** Resolve + draw the selected tap's waveform. The hero ALWAYS shows the
   * selected ORIGINAL WAV (peaks fetched from the original source) plus the
   * committed strip-cut overlay when the session is stripped — in BOTH toggle
   * states, since an original only ever lives in `<session>/` (a stripped clip
   * has a different name under `stripped/`, so there is no "original name in
   * stripped/" to fetch). The source toggle switches the list below, not the
   * hero. Peaks are fetched lazily (once per WAV, client-cached on the file's
   * byte size so the poll never refetches) and drawn when they land. Guarded by
   * `lastWaveSig` so the canvas only redraws when the selection / load-state /
   * committed cut actually changes — not on every body re-render or a toggle. */
  /** @param {import('../../types.js').WavFile | null} sel */
  const drawWaveform = (sel) => {
    const sid = session?.session || "";
    // What the canvas is (or is about to be) drawing. Recorded here because this
    // function is the single owner of that decision; the playhead's
    // strict-identity rule reads it once per frame instead of re-resolving the
    // selection. Set before the early returns so a sig-unchanged pass keeps it
    // accurate rather than stale.
    shownWav = sel && sid ? sel.name : null;
    if (!sel || !sid) {
      if (livePreview) {
        livePreview = null;
        waveform.setPreview(null);
      }
      // Nothing drawn means nothing to play — and this has to happen BEFORE the
      // sig-unchanged early return below, or ▶ kept keeps a stale enabled state
      // (and a stale tooltip) over an empty canvas.
      shownCut = null;
      syncPlayKept();
      const wsig = `none:${session ? "nofiles" : "nosession"}`;
      if (wsig === lastWaveSig) return;
      lastWaveSig = wsig;
      waveform.showMessage(session ? "no WAVs recorded yet" : "no session selected");
      return;
    }
    const fileSig = String(sel.size);
    const key = waveKey(sid, sel.name, sel.size);
    // A live strip-preview belongs to ONE (wav, source, size) — drop it the
    // moment the waveform moves elsewhere so stale spans never overlay a
    // different recording.
    if (livePreview && livePreview.key !== key) {
      livePreview = null;
      waveform.setPreview(null);
    }
    // Peaks: `remember-error`, so an unreadable WAV shows the reason instead of
    // being re-asked every poll tick (api.js declares that; the key changes with
    // the WAV's byte size, which is the only thing that could change the answer).
    const peaks = heroPeaks.resolve([sid, sel.name, "original", fileSig]);
    /** @type {"ok" | "loading" | "error"} */
    let state = "loading";
    /** @type {import('../../types.js').WavePeaks | undefined} */
    let data;
    let message = "";
    if (peaks.error) {
      state = "error";
      message = errText(peaks.error) || "could not read waveform";
    } else if (peaks.value) {
      state = "ok";
      data = peaks.value;
    }

    // Committed strip cut (#90): the hero always carries the overlay when the
    // session is stripped — it IS the "entire sound of the tap, kept vs
    // stripped, in one line" view, shown in both toggle states (the toggle
    // only switches the list below). Resolved lazily from /strip-meta, cached
    // on the stripped_at stamp so a re-strip refetches; spans come from the
    // persisted sidecar — never reconstructed from region filenames.
    const stripped = session?.stripped || null;
    const cutStamp = stripped ? stripped.stripped_at || "" : "";
    /** @type {import('../../types.js').CutSpan[] | null} */
    let cut = null;
    if (stripped) {
      // Same `remember-error` policy as the peaks above, and the same
      // redraw-on-land: an unparseable sidecar draws no overlay rather than
      // re-asking forever. Still resolving → no spans yet, so no overlay either.
      const meta = heroCut.resolve([sid, sel.name, cutStamp]).value;
      cut = meta && meta.spans && meta.spans.length ? meta.spans : null;
    }

    const wsig = `${key}@${state}@cut:${cutStamp}:${cut ? cut.length : 0}`;
    if (wsig === lastWaveSig) return;
    lastWaveSig = wsig;
    shownCut = cut && cut.length ? cut : null;
    syncPlayKept();
    if (state === "ok" && data) waveform.showWaveform(data.peaks, data.duration_s, cut);
    else if (state === "loading") waveform.showMessage("loading waveform…");
    else waveform.showMessage(message);
  };

  // ---- Knobs ----------------------------------------------------------------

  /** @param {keyof StripKnobs} key */
  const knobUnit = (key) => (key === "speech_floor_db" ? "dB" : "ms");
  /** @param {keyof StripKnobs} key */
  const paintKnob = (key) => { knobVals[key].textContent = `${knobs[key]} ${knobUnit(key)}`; };

  /** Debounce so a knob drag fires one strip-preview per pause, not one per
   * pixel — silero on the worker thread is O(samples) per call. */
  const PREVIEW_DEBOUNCE_MS = 300;

  /** Paint the clips / speech / in / kept stats row — one painter for every
   * source shape (live preview, last strip response), so the kept% formula
   * and the stat quartet can't diverge between them. */
  /** @param {number} clips @param {number} speechS @param {number} inS */
  const paintCutStats = (clips, speechS, inS) => {
    const kept = inS > 0 ? Math.round(100 * speechS / inS) : 0;
    setDimmable(stats.clips, String(clips));
    setDimmable(stats.speech, `${Math.round(speechS)}s`);
    setDimmable(stats.in, `${Math.round(inS)}s`);
    setDimmable(stats.kept, `${kept}%`);
  };

  /** @param {import('../../types.js').StripPreview} p */
  const paintPreviewStats = (p) => paintCutStats(p.segments, p.speech_seconds, p.in_seconds);

  /** Fire the strip-preview for the CURRENT knobs + selection. Only the
   * latest response lands (token check), and only while the original
   * source is shown — the stripped waveform IS a cut result already. */
  const firePreview = () => {
    if (!session) return;
    const sid = session.session;
    const sel = selectedFor();
    if (!sel || effectiveSource(session) !== "original") return;
    const token = ++previewToken;
    const key = waveKey(sid, sel.name, sel.size);
    fetchStripPreview(sid, sel.name, { ...knobs })
      .then((p) => {
        if (token !== previewToken) return; // superseded by a newer drag
        // Identity at land time, not just ordering: the selection/source/
        // session may have moved while the fetch was in flight — never
        // paint another WAV's preview (the next drawWaveform tick would
        // only reconcile it up to a poll later).
        const cur = selectedFor();
        if (!session || !cur || effectiveSource(session) !== "original") return;
        if (waveKey(session.session, cur.name, cur.size) !== key) return;
        livePreview = { key, p };
        waveform.setPreview({ spans: p.spans, speech_floor_db: p.knobs.speech_floor_db });
        syncPlayKept();
        paintPreviewStats(p);
      })
      .catch(() => { /* transient — the next knob input refires */ });
  };

  const schedulePreview = () => {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => { previewTimer = null; firePreview(); }, PREVIEW_DEBOUNCE_MS);
  };

  /** Drop the live preview entirely — overlay, stats, the debounce still
   * pending, AND any in-flight fetch, so a drag scheduled (or a request
   * already in the air) just before a ✂ strip / clear / source-toggle can't
   * re-create the preview ~300ms later. Bumping `previewToken` supersedes an
   * in-flight `firePreview` fetch (its `.then` bails on a token mismatch) —
   * without it, a preview requested before the drop lands afterwards (e.g.
   * dropped on a toggle to stripped, then the fetch resolves back in the
   * original view). */
  const dropPreview = () => {
    if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
    previewToken++;
    livePreview = null;
    waveform.setPreview(null);
    syncPlayKept();
  };

  for (const inp of /** @type {NodeListOf<HTMLInputElement>} */ (frag.querySelectorAll("[data-strip-knob]"))) {
    const key = /** @type {keyof StripKnobs} */ (inp.dataset.stripKnob);
    inp.value = String(knobs[key]);
    paintKnob(key);
    inp.addEventListener("input", () => {
      const n = Number(inp.value);
      if (Number.isFinite(n)) { knobs[key] = n; paintKnob(key); schedulePreview(); }
    });
  }

  // ---- Strip-silence (REAL) -------------------------------------------------

  stripBtn.addEventListener("click", async () => {
    if (!session) return;
    const sid = session.session;
    stripInflight.add(sid);
    stripBtn.disabled = true;
    try {
      const res = /** @type {import('../../types.js').StripSilenceResult} */ (
        await postJson(`/api/sessions/${encodeURIComponent(sid)}/strip-silence`, { ...knobs }));
      lastStrip.set(sid, res);
      // A re-strip rmtree's stripped/ before rewriting it (batch_strip.py), so a
      // clip playing right now was just deleted — the same eviction `clear` does.
      // This IS the strip-knob-tuning loop: listen to a clip, adjust, re-strip.
      // SUCCESS path only: `strip_session` raises SessionBusy / NoUsableWavs
      // BEFORE touching stripped/ and StrippedDirUnclearable when the rmtree
      // itself failed, so on a failure every clip survives and evicting would
      // stop the audio while blaming a deletion that never happened (the rule
      // sessions.js's delete paths already follow).
      player.forgetWhere((f) => f.session === sid && f.source === "stripped");
      // The committed cut now reflects these knobs — drop the live preview.
      dropPreview();
      // Flip to the cleaned audio on success so the operator can act on it.
      if ((res.files_written || 0) > 0) setSourcePick(sid, "stripped");
    } catch (e) {
      alert(`Strip silence failed: ${errText(e)}`);
    } finally {
      stripInflight.delete(sid);
      stripBtn.disabled = false;
      // Force the next tick to repaint chrome + reconcile the list with the new
      // stripped clips (the new files_sig will refetch the listing).
      repaintAfterMutate();
    }
  });

  clearBtn.addEventListener("click", async () => {
    if (!session) return;
    const sid = session.session;
    if (!confirm("Delete the stripped/ folder for this session?\n\nOriginals are kept; you can rerun strip silence later.")) return;
    try { await del(`/api/sessions/${encodeURIComponent(sid)}/stripped`); }
    catch (e) { alert(`Clear stripped failed: ${errText(e)}`); return; }
    lastStrip.delete(sid);
    dropPreview();
    clearSourcePick(sid);
    // Every stripped clip of this session just went; the originals are kept.
    player.forgetWhere((f) => f.session === sid && f.source === "stripped");
    repaintAfterMutate();
  });

  // ---- Per-WAV transcript expand (native <details>, lazy body) --------------

  /** Render the message-only body (no cached transcript / load error) into an
   * expand host. */
  /** @param {HTMLElement} host @param {string} msg */
  const fillExpandMessage = (host, msg) => {
    const el = document.createElement("div");
    el.className = "expand-tx-loading dim small";
    el.textContent = msg;
    mount(host, el);
  };

  /** Fill one open <details>'s body with the WAV's FULL cached transcript,
   * fetched lazily on first open. /api/state ships only a slim marker on each
   * file row; the body crosses the wire once per (wav, source, transcribed_at)
   * and is client-cached. Self-contained per row — the api.js cache dedupes the
   * request across reopens, so there's no global expanded-set / poll-driven
   * re-render machinery: when the fetch lands we just refill THIS host (if it's
   * still the one we fired for). */
  /**
   * @param {HTMLElement} host
   * @param {string} name
   * @param {"original"|"stripped"} src
   * @param {import('../../types.js').WavTranscriptMarker} marker
   * @param {string} speakerName
   */
  const fillExpand = (host, name, src, marker, speakerName) => {
    const sid = session?.session || "";
    const stamp = marker.transcribed_at || "";
    const cached = wavTranscript.peek(sid, name, src, stamp);
    if (cached !== undefined) {
      if (cached) mount(host, buildExpand(cached, speakerName));
      else fillExpandMessage(host, "no transcript body on disk");
      return;
    }
    mount(host, tpl("tpl-next-txloading"));
    // Tag the host with the stamp we're fetching so a re-transcribe that
    // recreates the row (new marker) doesn't get an older body painted in.
    host.dataset.txStamp = stamp;
    wavTranscript.fetch(sid, name, src, stamp)
      .then((full) => {
        if (host.dataset.txStamp !== stamp || !host.isConnected) return;
        if (full) mount(host, buildExpand(full, speakerName));
        else fillExpandMessage(host, "no transcript body on disk");
      })
      .catch(() => {
        if (host.dataset.txStamp === stamp && host.isConnected) fillExpandMessage(host, "could not load transcript");
      });
  };

  /** Build the inline expanded-transcript node for one WAV. Renders a meta
   * strip + the segment lines (speaker = the WAV's speaker_name, since per-WAV
   * sidecars carry no per-segment speaker) with suppressed hallucination lines
   * struck-through, interleaved in start-time order — the audit lines the
   * classic expand surfaced. */
  /**
   * @param {import('../../types.js').WavTranscript} t
   * @param {string} speakerName
   */
  const buildExpand = (t, speakerName) => {
    const frag = tpl("tpl-next-txexpand");
    const metaHost = pick(frag, "meta");
    /** @type {[string, string][]} */
    const fields = [
      ["device", t.device || "?"],
      ["backend", t.backend || "?"],
      ["model", t.model || "?"],
      ["lang", t.language || "?"],
      ["took", fmtMs(t.transcribe_ms)],
    ];
    if (t.source) fields.push(["source", t.source]);
    for (const [label, value] of fields) {
      const field = tpl("tpl-next-txmeta-field");
      pick(field, "label").textContent = label;
      pick(field, "value").textContent = value;
      metaHost.appendChild(field);
    }

    // Per-WAV sidecars have `segments` (start/end/text) + a parallel
    // `suppressed_hallucinations` list. The WavTranscript type only declares
    // `text` + suppressed, so reach the segments[] off the raw record defensively.
    const raw = /** @type {{ segments?: { start?: number, end?: number, text?: string }[] }} */ (
      /** @type {unknown} */ (t));
    /** @type {{ start: number, text: string, sup: boolean, rule: string }[]} */
    const lines = [];
    for (const seg of raw.segments || []) {
      lines.push({ start: Number(seg.start) || 0, text: seg.text || "", sup: false, rule: "" });
    }
    for (const sup of t.suppressed_hallucinations || []) {
      lines.push({ start: Number(sup.start) || 0, text: sup.text || "", sup: true, rule: sup.matched_rule || "" });
    }
    lines.sort((a, b) => a.start - b.start);

    const linesHost = pick(frag, "lines");
    if (!lines.length) {
      // No segments[] on disk (e.g. older sidecar) — fall back to the joined
      // plain text as a single line, same content the classic body showed.
      const line = tpl("tpl-next-txline");
      pick(line, "ts").textContent = "";
      pick(line, "speaker").textContent = speakerName ? `${speakerName}:` : "";
      pick(line, "body").textContent = t.text || "";
      linesHost.appendChild(line);
    } else {
      for (const ln of lines) {
        const line = tpl("tpl-next-txline");
        pick(line, "ts").textContent = `[${fmtMmSs(ln.start)}]`;
        pick(line, "speaker").textContent = speakerName ? `${speakerName}:` : "";
        const body = pick(line, "body");
        body.textContent = ln.text;
        if (ln.sup) {
          body.className = "seg suppressed";
          body.title = `suppressed · matched: ${ln.rule}`;
        }
        linesHost.appendChild(line);
      }
    }
    return frag;
  };

  // ---- Delete a single WAV --------------------------------------------------

  /** @param {string} name @param {"original"|"stripped"} src */
  const deleteWav = async (name, src) => {
    if (!session) return;
    const sid = session.session;
    if (!confirm(`Delete this ${src === "stripped" ? "stripped clip" : "WAV"}?\n\n${name}\n\nThe audio and its cached transcripts are removed.`)) return;
    const qs = src === "stripped" ? "?source=stripped" : "";
    try {
      await del(`/api/wav/${encodeURIComponent(sid)}/${encodeURIComponent(name)}${qs}`);
    } catch (e) {
      alert(`Delete failed: ${errText(e)}`);
      return;
    }
    // Stop the audio if this is what's playing. The browser has the bytes
    // buffered, so without this the deleted recording keeps talking to the end
    // with no error (ADR-0017) — the row would vanish under a still-playing bar.
    player.forget({ session: sid, name, source: src });
    // The next /api/state poll carries a new files_sig (the digest drops the
    // deleted WAV), so currentFiles refetches and the reconcile removes the
    // row. Force both gates so that lands on the first tick.
    repaintAfterMutate();
  };

  /** Trigger a WAV download WITHOUT toggling the row's <details>: a plain
   * anchor click inside a <summary> both navigates AND toggles, so the row's
   * download handler preventDefaults the toggle and navigates via this synthetic
   * (outside-the-summary) anchor instead. */
  /** @param {string} href */
  const triggerDownload = (href) => {
    const a = document.createElement("a");
    a.href = href;
    a.download = "";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  /** Reflect the selection on ONE original row in place — `.is-sel` drives the
   * "🌊 viewing" badge + highlight (pure CSS), `aria-pressed` the name button.
   * This is `renderList`'s `update` for the list: no row rebuild, and because
   * the selected name is a term in each row's `itemSig`, only the two rows whose
   * state actually flipped are touched rather than all of them.
   * Clips are never the waveform-select target, so they have nothing to reflect.
   * @param {HTMLElement} row @param {RowModel} m @param {string} selName */
  const applyRowSelection = (row, m, selName) => {
    if (m.kind === "clip") return;
    const isSel = m.file.name === selName;
    row.classList.toggle("is-sel", isSel);
    const selEl = row.querySelector("[data-wav-select]");
    if (selEl) selEl.setAttribute("aria-pressed", String(isSel));
  };

  // ---- WAV list -------------------------------------------------------------

  /** One flat row model — an original WAV or an indented stripped clip. The
   * reconcile key + sig live here so the list updates in place. */
  /** @typedef {{
   *   kind: "wav" | "clip",
   *   file: import('../../types.js').WavFile | import('../../types.js').WavRegion,
   *   src: "original" | "stripped",
   *   isCurrent: boolean,
   * }} RowModel */

  /** Flatten the file listing into reconcile row models: every original, each
   * followed by its indented stripped region clips when the stripped view is
   * active (matches the classic layout). `view` decides whether the clips are
   * appended; it is NOT the originals' own source — an original tap-WAV always
   * lives in `<session>/`, so its row is ALWAYS the "original" source (its
   * download / delete / expand target the original, never
   * `<session>/stripped/<originalName>`, which never exists). Only the region
   * CLIPS are the stripped source. */
  /**
   * @param {import('../../types.js').WavFile[]} files
   * @param {"original"|"stripped"} view
   * @param {boolean} isCurrent
   * @returns {RowModel[]}
   */
  const buildRowModels = (files, view, isCurrent) => {
    /** @type {RowModel[]} */
    const models = [];
    for (const f of files) {
      models.push({ kind: "wav", file: f, src: "original", isCurrent });
      if (view === "stripped") {
        for (const r of f.regions || []) models.push({ kind: "clip", file: r, src: "stripped", isCurrent });
      }
    }
    return models;
  };

  /** Build one WAV-list row as a native <details>: the <summary> is the always-
   * visible row (name selects the waveform; the rest of the summary toggles
   * expand), the body lazy-fetches the transcript on first open. Returns the
   * <details> Element so `renderList` can key + reuse it. */
  /** @param {RowModel} m @returns {HTMLElement} */
  const buildRow = (m) => {
    const { kind, file: f, src, isCurrent } = m;
    const sid = session?.session || "";
    const isClip = kind === "clip";
    const node = tpl(isClip ? "tpl-next-wavclip" : "tpl-next-wavrow");
    const row = /** @type {HTMLDetailsElement} */ (pick(node, "row"));
    // Stable per-row hooks (e2e + debugging + selection): name · source · kind.
    row.dataset.wav = f.name;
    row.dataset.src = src;

    if (isClip) {
      pick(node, "name").textContent = `↳ ${truncMid(f.name, 36)}`;
      const r = /** @type {import('../../types.js').WavRegion} */ (f);
      const span = r.wav_start && r.wav_end ? `${fmtClock(r.wav_start)}–${fmtClock(r.wav_end)}` : "stripped region";
      pick(node, "sub").textContent = `${span} · ${fmtBytes(f.size)}`;
    } else {
      pick(node, "name").textContent = truncMid(f.name, 40);
      const who = f.speaker_name ? `${f.speaker_name} · ` : "";
      pick(node, "sub").textContent = `${who}${fmtBytes(f.size)}`;
    }
    pick(node, "dur").textContent = fmtDur(f.duration_s);

    const tag = pick(node, "txTag");
    if (f.transcript) { tag.textContent = "✓ tx"; tag.className = "wavrow__tx is-done"; }
    else { tag.textContent = "no tx"; tag.className = "wavrow__tx is-none"; }

    // Originals are the waveform-select target. The name block selects (and
    // preventDefault cancels the <details> toggle the summary-click would
    // otherwise fire); "the rest of the row toggles" is the native default.
    if (!isClip) {
      const selectEl = /** @type {HTMLElement} */ (node.querySelector("[data-wav-select]"));
      selectEl.title = "Show this WAV in the waveform above";
      const select = () => {
        if (!session) return;
        const sid2 = session.session;
        selectedWav.set(sid2, f.name);
        // paintWaveHeader (not just waveName + drawWaveform) so the clips /
        // speech / in / kept stat quartet is repainted too — otherwise selecting
        // away from a WAV that had a live strip preview leaves its stale preview
        // numbers in the stat row until the next poll tick. The chrome sig
        // deliberately omits the selection, so this stays a direct call.
        paintWaveHeader(selectedFor(), session.stripped || null);
        // The ROW highlight is the seam's job now: afterMutate repaints from the
        // cached state synchronously before it polls (main.js refresh), so the
        // new selection lands through renderList's per-row gate on this click —
        // no second painter, and no O(rows) walk (only the two flipped rows).
        afterMutate();
      };
      selectEl.addEventListener("click", (e) => { e.preventDefault(); select(); });
      selectEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); }
      });
    }

    // Play — hands the shell-owned Player a seek target. preventDefault stops
    // the <summary> toggle a click inside the summary would otherwise fire.
    // An OPEN WAV is refused: its RIFF/data-size header is patched only when
    // the tap closes, so the bytes on disk declare a length that isn't there
    // and the browser would decode ~nothing (ADR-0017). The listing's `open`
    // flag flips exactly once, when the tap closes, so this re-enables itself.
    const playBtn = /** @type {HTMLButtonElement} */ (pick(node, "play"));
    const isOpen = "open" in f && f.open === true;
    playBtn.setAttribute("aria-label", `Play ${f.name}`);
    if (isOpen) {
      playBtn.disabled = true;
      playBtn.title = "still recording — playable once the tap closes";
    } else {
      playBtn.title = "Play this audio";
      playBtn.addEventListener("click", (e) => {
        e.preventDefault();
        player.load({ session: sid, name: f.name, source: src });
      });
    }

    // Download — preventDefault stops the summary toggle; triggerDownload
    // navigates via a synthetic anchor outside the <summary>.
    const dl = /** @type {HTMLAnchorElement} */ (pick(node, "download"));
    dl.href = wavUrl({ session: sid, name: f.name, source: src });
    dl.addEventListener("click", (e) => { e.preventDefault(); triggerDownload(dl.href); });

    // Delete — the backend refuses the current session (409), so hide it there.
    const delBtn = /** @type {HTMLButtonElement} */ (node.querySelector("[data-wav-delete]"));
    if (isCurrent) {
      delBtn.remove();
    } else {
      delBtn.addEventListener("click", (e) => { e.preventDefault(); deleteWav(f.name, src); });
    }

    // Inline transcript — native <details>; the body is filled lazily the first
    // time the row opens (and on reopen, cheaply, from the client cache).
    const expandHost = /** @type {HTMLElement} */ (pick(node, "expandBody"));
    row.addEventListener("toggle", () => {
      if (!row.open) return;
      if (!f.transcript) { fillExpandMessage(expandHost, "no cached transcript — transcribe this WAV first"); return; }
      fillExpand(expandHost, f.name, src, f.transcript, f.speaker_name || "");
    });

    return row;
  };

  /** Reconcile key for a WAV row. It folds EVERYTHING buildRow renders or binds
   * (session, name, size, dur, source, current-ness, transcript stamp) EXCEPT
   * selection (which is the row's itemSig instead) — a changed key drops the
   * old row and builds a fresh one (so its listeners close over the right
   * file), while a truly-unchanged row keeps its key and its open-<details>
   * state across the moveBefore reconcile. The session id is in the key because
   * this view is a SINGLE cached instance reused across sessions (main.js keys
   * it "recordings", not per-session): without it, two sessions whose rows
   * share every other field could reuse a node carrying the wrong session's
   * download href / delete / toggle closures. */
  /** @param {RowModel} m */
  const rowKey = (m) =>
    [
      session?.session || "", m.kind, m.file.name, m.file.size, m.file.duration_s,
      // `open` gates the ▶, so it belongs in the key: without it, re-enabling
      // after a tap closes rides on size/duration changing, which is silent for a
      // recording that captured no audio.
      "open" in m.file && m.file.open ? 1 : 0,
      m.src, m.isCurrent ? 1 : 0, m.file.transcript?.transcribed_at || "", m.file.transcript ? 1 : 0,
      m.kind === "clip"
        ? `${/** @type {import('../../types.js').WavRegion} */ (m.file).wav_start || ""}-${/** @type {import('../../types.js').WavRegion} */ (m.file).wav_end || ""}`
        : m.file.speaker_name || "",
    ].join("|");

  // ---- Per-tick update ------------------------------------------------------

  /** Paint the waveform-header name + redraw the canvas, and the clips / speech
   * / in / kept stats from the live preview → last strip response → on-disk
   * stripped summary → placeholders. Shared by the chrome repaint and the
   * selection-change path so the precedence can't diverge. */
  /**
   * @param {import('../../types.js').WavFile | null} sel
   * @param {import('../../types.js').StrippedStats | null} stripped
   */
  const paintWaveHeader = (sel, stripped) => {
    const sid = session?.session || "";
    // The hero always shows the original tap; a "· ✂ cut" hint flags that the
    // committed strip overlay is on it (kept vs stripped), not the toggle state.
    waveName.textContent = sel
      ? `🌊 ${truncMid(sel.name, 40)} · ${fmtDur(sel.duration_s)}${stripped ? " · ✂ cut" : ""}`
      : "no WAV selected";
    drawWaveform(sel);
    // drawWaveform already dropped a preview that no longer matches the shown
    // WAV, so a surviving livePreview is this session's by construction.
    const pv = sessionPreview();
    const ls = lastStrip.get(sid);
    if (pv) {
      paintPreviewStats(pv);
    } else if (ls) {
      paintCutStats(ls.files_written ?? 0, ls.speech_seconds, ls.in_seconds);
    } else if (stripped) {
      setDimmable(stats.clips, String(stripped.count));
      setDimmable(stats.speech, `${Math.round(stripped.speech_seconds)}s`);
      setDimmable(stats.in, "—");
      setDimmable(stats.kept, "—");
    } else {
      for (const v of Object.values(stats)) setDimmable(v, "—");
    }
  };

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    session = sess;
    const sid = sess?.session || "";
    const src = effectiveSource(sess);
    const filesSig = sess?.files_sig || "";
    const stripped = sess?.stripped || null;
    const job = sess?.progress || null;
    const isCurrent = (j.current_session || "") === sid;

    // Resolve the focused session's WAV listing — the array /api/state no
    // longer ships, fetched once per (sid, files_sig) and client-cached. `null`
    // → a fetch is in flight (show a loading placeholder); `[]` → nothing to
    // fetch (empty files_sig = no folder / no WAVs yet).
    const { files, loading: filesLoading, sigTerm: filesTerm } = filesSource.resolve(sid, filesSig);
    currentFiles = files;
    const sel = selectedFor();

    // ---- Chrome (header + waveform header + stats + strip buttons) ----------
    // Gated on a SMALL signature. The WAV list has its own files-set gate and
    // the job bar repaints in place each tick (render-signature hygiene), so a
    // per-second strip/transcribe job tick never rebuilds the O(files) list or
    // churns the chrome. Selection is NOT in the sig — select() repaints the
    // wave header in place, so picking a WAV never rebuilds the source toggle.
    // `sessionLabel(sess)` is a sig TERM, not a hoisted header() call: header()
    // is passed `actions` (the source toggle), which headerNeedsRender never
    // gates, so calling it unconditionally would rebuild the toggle every tick.
    // Without the term, renaming the session elsewhere left the old label in
    // the header until an unrelated term changed — and this is a BESPOKE gate,
    // so the __TAPSCRIBE_SIG_AUDIT drift audit can't see it.
    const chromeSig = [
      sid, src, filesSig, filesLoading ? "L" : "",
      sess ? sessionLabel(sess) : "",
      stripped ? `${stripped.count}:${stripped.stripped_at}` : "",
      stripInflight.has(sid) ? "S" : "",
      job?.kind === "strip" ? "J" : "",
      sessionPreview() ? "P" : "",
      lastStrip.has(sid) ? "R" : "",
      isCurrent ? "CUR" : "",
    ].join("§");
    if (chromeSig !== lastChromeSig) {
      lastChromeSig = chromeSig;
      header(headHost, {
        eyebrow: "Session · 2 Recordings",
        title: "Recordings",
        sub: sess
          ? inline(`${files.length} WAV${files.length === 1 ? "" : "s"} in `, strong(sessionLabel(sess)), " · strip silence, then transcribe")
          : "no session selected — pick one from the spine",
        actions: sess && files.length ? buildSourceToggle({
          active: src,
          hasStripped: !!stripped,
          onPick: (which) => {
            if (!session) return;
            setSourcePick(session.session, which);
            // The live strip-preview is an original-view tuning artifact; a
            // source switch must clear it. The waveKey is source-independent
            // (the hero is always the original tap), so a lingering preview
            // would otherwise survive the toggle and overlay the stripped
            // view's committed cut instead of being dropped.
            dropPreview();
            // No markListStale: `src` is a term in the list's sig, so the
            // synchronous afterMutate repaint crosses the gate on its own.
            lastChromeSig = " ";
            afterMutate();
          },
        }) : undefined,
      });

      if (!sess || (!files.length && !filesLoading)) {
        waveName.textContent = sess ? "no WAVs recorded yet" : "no session selected";
        for (const v of Object.values(stats)) setDimmable(v, "—");
        wavHint.textContent = "0 files";
        stripBtn.disabled = !sess;
        stripBtn.textContent = "✂ strip all";
        clearBtn.disabled = !stripped;
        drawWaveform(null);
      } else if (filesLoading) {
        // files_sig is set but the listing fetch hasn't landed yet.
        waveName.textContent = "loading…";
        for (const v of Object.values(stats)) setDimmable(v, "—");
        wavHint.textContent = "loading…";
        stripBtn.disabled = true;
        stripBtn.textContent = "✂ strip all";
        clearBtn.disabled = true;
        drawWaveform(null);
      } else {
        paintWaveHeader(sel, stripped);
        const stripBusy = stripInflight.has(sid) || job?.kind === "strip";
        stripBtn.disabled = stripBusy;
        stripBtn.textContent = stripBusy ? "⟳ stripping…" : "✂ strip all";
        clearBtn.disabled = !stripped || stripBusy;
        wavHint.textContent = `${files.length} original${files.length === 1 ? "" : "s"}`;
      }
    }

    // ---- Job progress bar (in place, every tick — render-signature hygiene) -
    renderJobBar({ jobBar, jobLabel, jobCount, jobProgress, jobWav }, job);

    // ---- WAV list -----------------------------------------------------------
    // One `renderList` call for both states: rows when there are any, an empty
    // `items` when there aren't (which removes exactly the rows the seam
    // created). `wavList` holds ONLY keyed rows — the empty/loading placeholder
    // is a hidden-toggled SIBLING (recordings.html) — so the seam owns the host
    // outright and needs no marker to tell rows from placeholders.
    //
    // The seam owns the gate: the list `sig` skips a quiet tick entirely, a
    // selection inside the list defers WITHOUT advancing it, and each row's
    // `itemSig` decides whether `update` runs. Everything a row DISPLAYS is
    // already folded into `rowKey`, so a content change recreates the row and
    // `update` has exactly one job — the selection highlight. That is why the
    // selected name is a term in BOTH signatures: the list sig so a click gets
    // past rule 1 at all, the item sig so only the two affected rows are WRITTEN
    // to instead of all of them (issue #213). Note the reconcile WALK a selection
    // triggers is still O(rows) — that part is a wash against the querySelectorAll
    // walk it replaced; what changed is that it happens per click, never per tick.
    // Rows carry content-visibility
    // (next.css .wavrow) so off-screen layout/paint is skipped either way.
    //
    // `auditRows: false` — the one opt-out: these rows are <details> whose bodies
    // lazy-load a transcript on expand, so a fresh probe row legitimately differs
    // from an expanded one and would report drift that isn't there.
    const state = listState({ hasSession: !!sess, loading: filesLoading, count: files.length });
    const selName = sel?.name || "";
    // A THUNK, not an array: buildRowModels walks every file (plus every stripped
    // region clip), and rule 1 skips before the thunk runs — so a quiet tick on a
    // thousand-WAV session allocates nothing at all.
    const rendered = renderList(wavList, () => buildRowModels(files, src, isCurrent), {
      key: rowKey,
      create: buildRow,
      update: (node, m) => { applyRowSelection(/** @type {HTMLElement} */ (node), m, selName); },
      itemSig: (m) => (m.kind === "clip" ? "" : m.file.name === selName ? "sel" : ""),
      // isCurrent gates the row's Delete button and is folded into `rowKey`, so it
      // must be a sig term or the rows stay keyed on a stale value. `filesTerm`
      // carries the listing's stamp AND its provisional-ness (session-files.js).
      sig: `${sid}§${src}§${filesTerm}§${state}§${selName}§${isCurrent ? 1 : 0}`,
      auditRows: false,
    });
    if (rendered) {
      wavEmpty.hidden = state === "rows";
      if (state !== "rows") {
        wavEmpty.classList.toggle("dim", state === "loading");
        wavEmpty.textContent =
          state === "loading"
            ? "loading recordings…"
            : state === "empty"
              ? "No recordings yet. Once taps record into this session, each WAV appears here."
              : "Pick a session from the spine to manage its recordings.";
      }
    }
  };

  return { node: frag, update };
}
