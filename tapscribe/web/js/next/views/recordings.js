// @ts-check
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
//   1. The file listing is NOT on /api/state — it's fetched lazily via
//      fetchSessionFiles(sid, files_sig) and cached, so it crosses the wire
//      once per change, not every poll. `currentFiles` holds the resolved
//      list for the focused session.
//   2. The list is rendered through `reconcileList` (keyed, in-place — never
//      replaceChildren) and each row carries `content-visibility: auto`
//      (next.css `.wavrow`), so the browser skips layout/paint of off-screen
//      rows and a selection / expand / poll tick never rebuilds thousands of
//      nodes. Selection is an in-place `.is-sel` toggle; the per-row
//      transcript expand is a native <details> that lazy-fetches its body on
//      first open.
//
// Built once for the page; `update(j, session)` refreshes stats / strip-job
// progress each tick and reconciles the list only when the file set changes
// (files_sig / source), deferring while a control is focused or text is being
// selected inside the list.

import { tpl, pick, selectionInside, reconcileList } from "../../templates.js";
import { postJson, del, loadSessionFiles, fetchWavTranscript, peekWavTranscript, fetchWavePeaks, peekWavePeaks, fetchWavStripMeta, peekWavStripMeta, fetchStripPreview } from "../../api.js";
import { fmtBytes, fmtDur, fmtClock, fmtMs, fmtMmSs, truncMid } from "../../formatters.js";
import { header, strong, inline, buildSourceToggle, renderJobBar } from "../shell.js";
import { createWaveform } from "../components/waveform.js";

/** Strip-silence knob defaults — mirror STRIP_OPT_DEFAULTS / the server-side
 * fallbacks in api_session_strip_silence (tapscribe/app.py). */
const STRIP_DEFAULTS = Object.freeze({ min_silence_ms: 500, pad_ms: 200, speech_floor_db: -45 });

/** @typedef {{ min_silence_ms: number, pad_ms: number, speech_floor_db: number }} StripKnobs */

/**
 * @param {{
 *   metaFor: (s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { metaFor, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-recordings");

  const headHost = pick(frag, "head");
  const waveName = pick(frag, "waveName");
  // Isolated canvas waveform — mounted once into the hero; update() feeds it
  // the selected WAV's peaks (lazy + client-cached) as the selection changes.
  const waveform = createWaveform();
  pick(frag, "waveHost").appendChild(waveform.node);
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
  const stripBtn = /** @type {HTMLButtonElement} */ (pick(frag, "stripBtn"));
  const clearBtn = /** @type {HTMLButtonElement} */ (pick(frag, "clearBtn"));
  const jobBar = pick(frag, "jobBar");
  const jobLabel = pick(frag, "jobLabel");
  const jobCount = pick(frag, "jobCount");
  const jobFill = /** @type {HTMLElement} */ (pick(frag, "jobFill"));
  const jobWav = pick(frag, "jobWav");
  const wavHint = pick(frag, "wavHint");
  const wavList = pick(frag, "wavList");

  // ---- View-local state -----------------------------------------------------
  /** @type {import('../../types.js').AppState | null} */
  let latest = null;
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  /** @type {StripKnobs} */
  const knobs = { ...STRIP_DEFAULTS };
  /** Source toggle, per session id. */
  /** @type {Map<string, "original" | "stripped">} */
  const sourcePick = new Map();
  /** Selected original WAV name, per session id (drives the waveform header). */
  /** @type {Map<string, string>} */
  const selectedWav = new Map();
  /** The lazily-fetched WAV listing for the FOCUSED session — the array
   * /api/state no longer embeds. Refreshed at the top of update() from the
   * (sid, files_sig)-keyed client cache; [] until the first fetch lands. Every
   * helper that used to read session.files reads this instead. */
  /** @type {import('../../types.js').WavFile[]} */
  let currentFiles = [];
  /** (sid@files_sig) fetches in flight — dedupes the lazy files fetch across
   * the ticks before it lands (the api.js cache dedupes the request itself). */
  /** @type {Set<string>} */
  const pendingFiles = new Set();
  /** Last (sid · source · files_sig) the WAV list was reconciled for — the
   * list-level gate so reconcileList runs only when the file SET changes, not
   * every poll tick. NOT advanced on a deferred reconcile (selection/focus
   * hold) so the held-back render lands once the interaction clears. */
  let lastListSig = " ";
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
  // canvas (render-signature hygiene). `pendingWave` stops a fresh re-render
  // callback being chained on every tick while one fetch is in flight (the
  // api.js cache already dedupes the network request itself); `failedWave`
  // remembers an unreadable WAV so it shows a message instead of refetching
  // every tick.
  let lastWaveSig = " ";
  /** @type {Set<string>} */
  const pendingWave = new Set();
  /** @type {Map<string, string>} */
  const failedWave = new Map();
  /** Committed-cut (strip-meta) fetches in flight / failed, keyed
   * `sid/name@strippedAt` — same dedupe + no-retry-loop discipline as the
   * peaks cache above. A failed key never refetches; a re-strip changes the
   * stamp and therefore the key. */
  /** @type {Set<string>} */
  const pendingCutMeta = new Set();
  /** @type {Set<string>} */
  const failedCutMeta = new Set();

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
  /** @param {string} sid @param {string} name @param {string} src @param {number} size */
  const waveKey = (sid, name, src, size) => `${sid}/${name}@${src}@${size}`;

  /** @param {string} sid @returns {"original" | "stripped"} */
  const effectiveSource = (sid) => {
    const s = latest?.sessions?.find((x) => x.session === sid) || null;
    const want = sourcePick.get(sid) || "original";
    return (want === "stripped" && !s?.stripped) ? "original" : want;
  };

  /** Set a wave-stat value, dimming empty/em-dash placeholders so they recede
   * while real numbers (clips/speech accent + good) stay bright. */
  /** @param {HTMLElement} el @param {string} value */
  const setStat = (el, value) => {
    el.textContent = value;
    el.classList.toggle("is-empty", value === "" || value === "—");
  };

  /** Resolve the selected original WAV for the focused session (first if
   * unset). Reads the lazily-fetched `currentFiles`, not session.files (which
   * /api/state no longer ships). */
  const selectedFor = () => {
    if (!session || !currentFiles.length) return null;
    const want = selectedWav.get(session.session);
    return currentFiles.find((f) => f.name === want) ?? currentFiles[0] ?? null;
  };

  /** Resolve + draw the selected WAV's waveform. Peaks are fetched lazily
   * (once per WAV+source, client-cached on the file's byte size so the poll
   * never refetches) and drawn when they land. Guarded by `lastWaveSig` so the
   * canvas only redraws when the selection / source / load-state actually
   * changes — not on every body re-render. */
  /** @param {import('../../types.js').WavFile | null} sel @param {"original"|"stripped"} src */
  const drawWaveform = (sel, src) => {
    const sid = session?.session || "";
    if (!sel || !sid) {
      if (livePreview) {
        livePreview = null;
        waveform.setPreview(null);
      }
      const wsig = `none:${session ? "nofiles" : "nosession"}`;
      if (wsig === lastWaveSig) return;
      lastWaveSig = wsig;
      waveform.showMessage(session ? "no WAVs recorded yet" : "no session selected");
      return;
    }
    const fileSig = String(sel.size);
    const key = waveKey(sid, sel.name, src, sel.size);
    // A live strip-preview belongs to ONE (wav, source, size) — drop it the
    // moment the waveform moves elsewhere so stale spans never overlay a
    // different recording.
    if (livePreview && livePreview.key !== key) {
      livePreview = null;
      waveform.setPreview(null);
    }
    /** @type {"ok" | "loading" | "error"} */
    let state;
    /** @type {import('../../types.js').WavePeaks | undefined} */
    let data;
    let message = "";
    if (failedWave.has(key)) {
      state = "error";
      message = failedWave.get(key) || "could not read waveform";
    } else {
      data = peekWavePeaks(sid, sel.name, src, fileSig);
      if (data !== undefined) {
        state = "ok";
      } else {
        state = "loading";
        if (!pendingWave.has(key)) {
          pendingWave.add(key);
          fetchWavePeaks(sid, sel.name, src, fileSig)
            .then(() => { failedWave.delete(key); })
            .catch((e) => { failedWave.set(key, String(e).replace(/^Error:\s*/, "")); })
            .finally(() => {
              pendingWave.delete(key);
              // Redraw the canvas ONLY (the body didn't change) now that the
              // peaks are cached or the fetch failed. Re-resolve the current
              // selection in case it moved while the fetch was in flight, and
              // reset just the wave sig so this redraw isn't skipped — no
              // full body rebuild and no extra /api/state poll.
              lastWaveSig = " ";
              drawWaveform(selectedFor(), session ? effectiveSource(session.session) : "original");
            });
        }
      }
    }

    // Committed strip cut (#90): only the ORIGINAL's waveform carries the
    // overlay (the stripped source IS the cut result). Resolved lazily from
    // /strip-meta, cached on the stripped_at stamp so a re-strip refetches;
    // spans come from the persisted sidecar — never reconstructed from
    // region filenames.
    const stripped = session?.stripped || null;
    const cutStamp = src === "original" && stripped ? stripped.stripped_at || "" : "";
    /** @type {import('../../types.js').CutSpan[] | null} */
    let cut = null;
    if (src === "original" && stripped) {
      const mkey = `${sid}/${sel.name}@${cutStamp}`;
      const meta = peekWavStripMeta(sid, sel.name, cutStamp);
      if (meta !== undefined) {
        cut = meta && meta.spans && meta.spans.length ? meta.spans : null;
      } else if (!pendingCutMeta.has(mkey) && !failedCutMeta.has(mkey)) {
        pendingCutMeta.add(mkey);
        fetchWavStripMeta(sid, sel.name, cutStamp)
          .catch(() => { failedCutMeta.add(mkey); })
          .finally(() => {
            pendingCutMeta.delete(mkey);
            // Same redraw-only contract as the peaks fetch above: reset just
            // the wave sig and re-resolve the current selection.
            lastWaveSig = " ";
            drawWaveform(selectedFor(), session ? effectiveSource(session.session) : "original");
          });
      }
    }

    const wsig = `${key}@${state}@cut:${cutStamp}:${cut ? cut.length : 0}`;
    if (wsig === lastWaveSig) return;
    lastWaveSig = wsig;
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
    setStat(stats.clips, String(clips));
    setStat(stats.speech, `${Math.round(speechS)}s`);
    setStat(stats.in, `${Math.round(inS)}s`);
    setStat(stats.kept, `${kept}%`);
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
    if (!sel || effectiveSource(sid) !== "original") return;
    const token = ++previewToken;
    const key = waveKey(sid, sel.name, "original", sel.size);
    fetchStripPreview(sid, sel.name, { ...knobs })
      .then((p) => {
        if (token !== previewToken) return; // superseded by a newer drag
        // Identity at land time, not just ordering: the selection/source/
        // session may have moved while the fetch was in flight — never
        // paint another WAV's preview (the next drawWaveform tick would
        // only reconcile it up to a poll later).
        const cur = selectedFor();
        if (!session || !cur || effectiveSource(session.session) !== "original") return;
        if (waveKey(session.session, cur.name, "original", cur.size) !== key) return;
        livePreview = { key, p };
        waveform.setPreview({ spans: p.spans, speech_floor_db: p.knobs.speech_floor_db });
        paintPreviewStats(p);
      })
      .catch(() => { /* transient — the next knob input refires */ });
  };

  const schedulePreview = () => {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => { previewTimer = null; firePreview(); }, PREVIEW_DEBOUNCE_MS);
  };

  /** Drop the live preview entirely — overlay, stats, AND any debounce still
   * pending, so a drag scheduled just before a ✂ strip / clear doesn't
   * re-create the preview ~300ms after it was deliberately dropped. */
  const dropPreview = () => {
    if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
    livePreview = null;
    waveform.setPreview(null);
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
      // The committed cut now reflects these knobs — drop the live preview.
      dropPreview();
      // Flip to the cleaned audio on success so the operator can act on it.
      if ((res.files_written || 0) > 0) sourcePick.set(sid, "stripped");
    } catch (e) {
      alert(`Strip silence failed: ${String(e).replace(/^Error:\s*/, "")}`);
    } finally {
      stripInflight.delete(sid);
      stripBtn.disabled = false;
      // Force the next tick to repaint chrome + reconcile the list with the new
      // stripped clips (the new files_sig will refetch the listing).
      lastChromeSig = " ";
      lastListSig = " ";
      afterMutate();
    }
  });

  clearBtn.addEventListener("click", async () => {
    if (!session) return;
    const sid = session.session;
    if (!confirm("Delete the stripped/ folder for this session?\n\nOriginals are kept; you can rerun strip silence later.")) return;
    try { await del(`/api/sessions/${encodeURIComponent(sid)}/stripped`); }
    catch (e) { alert(`Clear stripped failed: ${String(e).replace(/^Error:\s*/, "")}`); return; }
    lastStrip.delete(sid);
    dropPreview();
    if (sourcePick.get(sid) === "stripped") sourcePick.delete(sid);
    lastChromeSig = " ";
    lastListSig = " ";
    afterMutate();
  });

  // ---- Per-WAV transcript expand (native <details>, lazy body) --------------

  /** Render the message-only body (no cached transcript / load error) into an
   * expand host. */
  /** @param {HTMLElement} host @param {string} msg */
  const fillExpandMessage = (host, msg) => {
    const el = document.createElement("div");
    el.className = "expand-tx-loading dim small";
    el.textContent = msg;
    host.replaceChildren(el);
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
    const cached = peekWavTranscript(sid, name, src, stamp);
    if (cached !== undefined) {
      if (cached) host.replaceChildren(buildExpand(cached, speakerName));
      else fillExpandMessage(host, "no transcript body on disk");
      return;
    }
    host.replaceChildren(tpl("tpl-next-txloading"));
    // Tag the host with the stamp we're fetching so a re-transcribe that
    // recreates the row (new marker) doesn't get an older body painted in.
    host.dataset.txStamp = stamp;
    fetchWavTranscript(sid, name, src, stamp)
      .then((full) => {
        if (host.dataset.txStamp !== stamp || !host.isConnected) return;
        if (full) host.replaceChildren(buildExpand(full, speakerName));
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
      alert(`Delete failed: ${String(e).replace(/^Error:\s*/, "")}`);
      return;
    }
    // The next /api/state poll carries a new files_sig (the digest drops the
    // deleted WAV), so currentFiles refetches and the reconcile removes the
    // row. Force both gates so that lands on the first tick.
    lastChromeSig = " ";
    lastListSig = " ";
    afterMutate();
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

  /** Reflect the current selection on the originals in place — `.is-sel` drives
   * the "🌊 viewing" badge + highlight (pure CSS), `aria-pressed` the name
   * button. No row rebuild, so selecting in a thousands-row list is O(rows) DOM
   * attribute flips, not a reconcile. */
  /** @param {string} selName */
  const applySelection = (selName) => {
    for (const row of /** @type {NodeListOf<HTMLElement>} */ (wavList.querySelectorAll(".wavrow:not(.is-clip)"))) {
      const isSel = row.dataset.wav === selName;
      row.classList.toggle("is-sel", isSel);
      const selEl = row.querySelector("[data-wav-select]");
      if (selEl) selEl.setAttribute("aria-pressed", String(isSel));
    }
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
   * followed by its indented stripped region clips when the stripped source is
   * active (matches the classic layout). */
  /**
   * @param {import('../../types.js').WavFile[]} files
   * @param {"original"|"stripped"} src
   * @param {boolean} isCurrent
   * @returns {RowModel[]}
   */
  const buildRowModels = (files, src, isCurrent) => {
    /** @type {RowModel[]} */
    const models = [];
    for (const f of files) {
      models.push({ kind: "wav", file: f, src, isCurrent });
      if (src === "stripped") {
        for (const r of f.regions || []) models.push({ kind: "clip", file: r, src: "stripped", isCurrent });
      }
    }
    return models;
  };

  /** Build one WAV-list row as a native <details>: the <summary> is the always-
   * visible row (name selects the waveform; the rest of the summary toggles
   * expand), the body lazy-fetches the transcript on first open. Returns the
   * <details> Element so `reconcileList` can key + reuse it. */
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
        applySelection(f.name);
        // paintWaveHeader (not just waveName + drawWaveform) so the clips /
        // speech / in / kept stat quartet is repainted too — otherwise selecting
        // away from a WAV that had a live strip preview leaves its stale preview
        // numbers in the stat row until the next poll tick.
        paintWaveHeader(selectedFor(), effectiveSource(sid2), session.stripped || null);
      };
      selectEl.addEventListener("click", (e) => { e.preventDefault(); select(); });
      selectEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); }
      });
    }

    // Download — preventDefault stops the summary toggle; triggerDownload
    // navigates via a synthetic anchor outside the <summary>.
    const dl = /** @type {HTMLAnchorElement} */ (pick(node, "download"));
    const dlQs = src === "stripped" ? "?source=stripped" : "";
    dl.href = `/api/wav/${encodeURIComponent(sid)}/${encodeURIComponent(f.name)}${dlQs}`;
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
   * selection (applied in place via applySelection) — a changed key drops the
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
   * @param {"original"|"stripped"} src
   * @param {import('../../types.js').StrippedStats | null} stripped
   */
  const paintWaveHeader = (sel, src, stripped) => {
    const sid = session?.session || "";
    waveName.textContent = sel
      ? `🌊 ${truncMid(sel.name, 40)} · ${fmtDur(sel.duration_s)} · ${src}`
      : "no WAV selected";
    drawWaveform(sel, src);
    // drawWaveform already dropped a preview that no longer matches the shown
    // WAV, so a surviving livePreview is this session's by construction.
    const pv = livePreview && livePreview.key.startsWith(`${sid}/`) ? livePreview.p : null;
    const ls = lastStrip.get(sid);
    if (pv) {
      paintPreviewStats(pv);
    } else if (ls) {
      paintCutStats(ls.files_written ?? 0, ls.speech_seconds, ls.in_seconds);
    } else if (stripped) {
      setStat(stats.clips, String(stripped.count));
      setStat(stats.speech, `${Math.round(stripped.speech_seconds)}s`);
      setStat(stats.in, "—");
      setStat(stats.kept, "—");
    } else {
      for (const v of Object.values(stats)) setStat(v, "—");
    }
  };

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    latest = j;
    session = sess;
    const sid = sess?.session || "";
    const src = effectiveSource(sid);
    const filesSig = sess?.files_sig || "";
    const stripped = sess?.stripped || null;
    const job = sess?.progress || null;
    const isCurrent = (j.current_session || "") === sid;

    // Resolve the focused session's WAV listing — the array /api/state no
    // longer ships, fetched once per (sid, files_sig) and client-cached. `null`
    // → a fetch is in flight (show a loading placeholder); `[]` → nothing to
    // fetch (empty files_sig = no folder / no WAVs yet).
    const fetched = loadSessionFiles(sid, filesSig, pendingFiles, () => {
      lastChromeSig = " ";
      lastListSig = " ";
      afterMutate();
    });
    const filesLoading = fetched === null;
    currentFiles = fetched || [];
    const files = currentFiles;
    const sel = selectedFor();

    // ---- Chrome (header + waveform header + stats + strip buttons) ----------
    // Gated on a SMALL signature. The WAV list has its own files-set gate and
    // the job bar repaints in place each tick (render-signature hygiene), so a
    // per-second strip/transcribe job tick never rebuilds the O(files) list or
    // churns the chrome. Selection is NOT in the sig — select() repaints the
    // wave header in place, so picking a WAV never rebuilds the source toggle.
    const chromeSig = [
      sid, src, filesSig, filesLoading ? "L" : "",
      stripped ? `${stripped.count}:${stripped.stripped_at}` : "",
      stripInflight.has(sid) ? "S" : "",
      job?.kind === "strip" ? "J" : "",
      livePreview && livePreview.key.startsWith(`${sid}/`) ? "P" : "",
      lastStrip.has(sid) ? "R" : "",
      isCurrent ? "CUR" : "",
    ].join("§");
    if (chromeSig !== lastChromeSig) {
      lastChromeSig = chromeSig;
      header(headHost, {
        eyebrow: "Session · 2 Recordings",
        title: "Recordings",
        sub: sess
          ? inline(`${files.length} WAV${files.length === 1 ? "" : "s"} in `, strong(metaFor(sess).label || sess.session), " · strip silence, then transcribe")
          : "no session selected — pick one from the spine",
        actions: sess && files.length ? buildSourceToggle({
          active: src,
          hasStripped: !!stripped,
          onPick: (which) => {
            if (!session) return;
            sourcePick.set(session.session, which);
            lastChromeSig = " ";
            lastListSig = " ";
            afterMutate();
          },
        }) : undefined,
      });

      if (!sess || (!files.length && !filesLoading)) {
        waveName.textContent = sess ? "no WAVs recorded yet" : "no session selected";
        for (const v of Object.values(stats)) setStat(v, "—");
        wavHint.textContent = "0 files";
        stripBtn.disabled = !sess;
        stripBtn.textContent = "✂ strip all";
        clearBtn.disabled = !stripped;
        drawWaveform(null, src);
      } else if (filesLoading) {
        // files_sig is set but the listing fetch hasn't landed yet.
        waveName.textContent = "loading…";
        for (const v of Object.values(stats)) setStat(v, "—");
        wavHint.textContent = "loading…";
        stripBtn.disabled = true;
        stripBtn.textContent = "✂ strip all";
        clearBtn.disabled = true;
        drawWaveform(null, src);
      } else {
        paintWaveHeader(sel, src, stripped);
        const stripBusy = stripInflight.has(sid) || job?.kind === "strip";
        stripBtn.disabled = stripBusy;
        stripBtn.textContent = stripBusy ? "⟳ stripping…" : "✂ strip all";
        clearBtn.disabled = !stripped || stripBusy;
        wavHint.textContent = `${files.length} original${files.length === 1 ? "" : "s"}`;
      }
    }

    // ---- Job progress bar (in place, every tick — render-signature hygiene) -
    renderJobBar({ jobBar, jobLabel, jobCount, jobFill, jobWav }, job);

    // ---- WAV list (own gate) ------------------------------------------------
    // The list owns its host's content: a placeholder when there's nothing to
    // reconcile, else the keyed reconcile. Each row carries content-visibility
    // (next.css .wavrow) so the browser skips off-screen layout/paint, and the
    // reconcile only runs when the file SET changes (files_sig / source) — never
    // on a poll tick, a job tick, or a selection (selection is applied in
    // place). It's deferred while a control is focused or text is selected
    // inside the list (don't advance the gate), so a mid-copy selection is
    // never clobbered — the held render lands on the first tick after it clears.
    const listState = !sess ? "none" : filesLoading ? "loading" : files.length ? "rows" : "empty";
    const listSig = `${sid}§${src}§${filesSig}§${listState}`;
    if (listState === "rows") {
      if (listSig !== lastListSig && !selectionInside(wavList)) {
        // Clear any leftover empty/loading placeholder (a non-reconcile child)
        // so reconcileList owns the host's children outright.
        if (!wavList.querySelector(".wavrow")) wavList.replaceChildren();
        reconcileList(wavList, buildRowModels(files, src, isCurrent), rowKey, buildRow);
        lastListSig = listSig;
      }
      // Keep the selection highlight correct across reconciles + idle ticks.
      applySelection(sel?.name || "");
    } else if (listSig !== lastListSig) {
      lastListSig = listSig;
      const ph = document.createElement("div");
      ph.className = listState === "loading" ? "empty dim" : "empty";
      ph.textContent =
        listState === "loading"
          ? "loading recordings…"
          : listState === "empty"
            ? "No recordings yet. Once taps record into this session, each WAV appears here."
            : "Pick a session from the spine to manage its recordings.";
      wavList.replaceChildren(ph);
    }
  };

  return { node: frag, update };
}
