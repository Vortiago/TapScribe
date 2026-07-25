// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
// Stages · Transcript (SESSION stage 3). The merged transcript for the open
// session (main/left) + a transcription CONTROL COLUMN (right): the meeting
// LANGUAGES control (a candidate-language <select multiple> + a readout of the
// effective set and the models that will run), the transcribe controls (session
// range from/to + force + a Transcribe action, plus a per-WAV re-transcribe
// picker), and the per-WAV transcript cache (set-primary).
//
// The operator declares LANGUAGES here, not a model (ADR-0011): the generalist
// is the global default (Settings / batch-model.txt) and the transcribe routes
// resolve it server-side, so both actions POST without a model/backend. REUSES
// merged-transcript.js verbatim for the merged result and language-picker.js for
// the candidate-language <select>. The REAL transcribe wiring (POST
// /api/transcribe, POST /api/transcribe-session, PUT /api/wav/{s}/{name}/primary,
// job progress) was moved here from recordings.js — Transcript drives the
// transcribe jobs now; Recordings is files + silence-stripping only. No mock
// data here.
//
// Built once for the page (per session id, like the rest of the SESSION
// stages); `update(j, session)` re-renders the merged transcript + the
// control column (signature-gated so an in-progress range edit isn't
// clobbered) and repaints the languages readout in place.

import { tpl, pick, renderRegion, markRegionStale, renderList, markListStale, selectionInside } from "../../templates.js";
import { createEmptyStateSync } from "../../vc/components/empty-state/empty-state.js";
import {
  postJson, putJson, sessionTranscript, createLastGoodHold, errText,
} from "../../api.js";
import { createFilesSource, listState } from "../session-files.js";
import { wireSave } from "../../save-status.js";
import { fmtBytes, fmtClock, fmtDur, fmtMs, truncMid } from "../../formatters.js";
import { aliasOf } from "../../speakers.js";
import { header, strong, inline, buildSourceToggle, renderJobBar, effectiveSource, sessionLabel } from "../shell.js";
import { makeStatusFlasher, copyToClipboard } from "../ui.js";
import * as mergedTranscript from "../../components/merged-transcript.js";
import { fillLanguageOptions, setSelectedLanguages, selectedLanguages } from "../components/language-picker.js";

/**
 * The recording (original WAV) a selected file belongs to. `sel` may be an
 * original WAV (returned as-is) or one of its silence-stripped region clips —
 * then we return the original whose `regions[]` contains it. Falls back to
 * `sel` when no parent is found (e.g. an orphaned clip). Pure.
 * @param {import('../../types.js').WavFile | import('../../types.js').WavRegion | null} sel
 * @param {import('../../types.js').WavFile[]} files
 * @returns {import('../../types.js').WavFile | import('../../types.js').WavRegion | null}
 */
export function recordingFor(sel, files) {
  if (!sel) return null;
  for (const f of files) {
    if (f.name === sel.name) return f;
    if ((f.regions || []).some((r) => r.name === sel.name)) return f;
  }
  return sel;
}

/**
 * Every cached transcript for a recording as ONE tagged list: the original
 * WAV's own variants, then each silence-stripped region clip's variants. Each
 * row carries the `file` it lives on (so set-primary resolves the right path)
 * and the cache_listing `source` ("original"|"stripped") that drives its tag.
 * Independent of the Original/Stripped transcribe toggle — the toggle chooses
 * what to transcribe, not what the cache shows (the operator asked the list not
 * to change on toggle, since each row is already source-tagged). Pure.
 * @param {import('../../types.js').WavFile | import('../../types.js').WavRegion | null} rec
 * @returns {(import('../../types.js').WavTranscriptVariant & { file: string })[]}
 */
export function recordingVariants(rec) {
  if (!rec) return [];
  /** @type {(import('../../types.js').WavTranscriptVariant & { file: string })[]} */
  const out = [];
  for (const v of rec.transcripts || []) out.push({ ...v, file: rec.name });
  const regions = /** @type {import('../../types.js').WavFile} */ (rec).regions || [];
  for (const r of regions) {
    for (const v of r.transcripts || []) out.push({ ...v, file: r.name });
  }
  return out;
}

/**
 * @param {{
 *   metaFor: (s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta,
 *   languageCatalog: import('../../types.js').LanguageCatalog,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { metaFor, languageCatalog, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-transcript");

  const headHost = pick(frag, "head");
  const txHint = pick(frag, "txHint");
  const txCopyBtn = /** @type {HTMLButtonElement} */ (pick(frag, "txCopyBtn"));
  const txCopyStatus = pick(frag, "txCopyStatus");
  const mergedHost = pick(frag, "mergedHost");
  // Meeting-languages control (ADR-0011): declare the candidate languages; the
  // generalist model is the global default, resolved server-side.
  const txLanguages = /** @type {HTMLSelectElement} */ (pick(frag, "txLanguages"));
  const txLanguagesSave = /** @type {HTMLButtonElement} */ (pick(frag, "txLanguagesSave"));
  const txLanguagesStatus = pick(frag, "txLanguagesStatus");
  const txLangEffective = pick(frag, "txLangEffective");
  const txLangModels = pick(frag, "txLangModels");
  // Transcribe controls (moved from recordings.js).
  const txSelLabel = pick(frag, "txSelLabel");
  const txOneBtn = /** @type {HTMLButtonElement} */ (pick(frag, "txOneBtn"));
  const rangeFrom = /** @type {HTMLInputElement} */ (pick(frag, "rangeFrom"));
  const rangeTo = /** @type {HTMLInputElement} */ (pick(frag, "rangeTo"));
  const forceBox = /** @type {HTMLInputElement} */ (pick(frag, "forceBox"));
  const txRangeBtn = /** @type {HTMLButtonElement} */ (pick(frag, "txRangeBtn"));
  const txNote = pick(frag, "txNote");
  const srcSwHost = pick(frag, "srcSwHost");
  const wavList = pick(frag, "wavList");
  /** The empty/loading placeholder — a hidden-toggled SIBLING of `wavList`, so
   * the rows host's children belong to renderList alone (views.html). */
  const wavEmpty = /** @type {HTMLElement} */ (pick(frag, "wavEmpty"));
  const jobBar = pick(frag, "jobBar");
  const jobLabel = pick(frag, "jobLabel");
  const jobCount = pick(frag, "jobCount");
  const jobProgress = /** @type {HTMLElement} */ (pick(frag, "jobProgress"));
  const jobWav = pick(frag, "jobWav");
  const cacheHint = pick(frag, "cacheHint");
  const cacheBody = pick(frag, "cacheBody");

  // Candidate-language options are a static catalog — fill once at build. A
  // per-code display-name map drives the readout below (code → "Norwegian").
  fillLanguageOptions(txLanguages, languageCatalog);
  /** @type {Map<string, string>} */
  const langNames = new Map((languageCatalog.languages || []).map((l) => [l.code, l.name]));

  // ---- View-local state -----------------------------------------------------
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  // The generalist that will ACTUALLY run — the RESOLVED batch model
  // (batch_model_effective: batch-model.txt validated + defaulted server-side),
  // NOT the raw batch_model_default (empty/stale when unset). Refreshed each
  // update() so the "models that will run" readout names the real model, and
  // stays in step with a Settings edit. "" only until the first poll lands.
  let generalist = "";
  // The LIVE global candidate-language default (what an empty selection inherits),
  // refreshed each update() from /api/state's `languages.default` — NOT the
  // boot-frozen /api/languages catalog default, which would go stale the moment
  // the operator edits the global default in Settings and reintroduce the exact
  // surprise-specialist the readout exists to prevent.
  /** @type {string[]} */
  let inheritedDefault = [];
  // Seed the language selection from session-meta exactly once per built view
  // (the view is rebuilt per session id), so a poll — or a save-on-transcribe
  // re-poll — never clobbers an unsaved in-progress selection (Interaction hold).
  let langSeeded = false;
  // The merged body + meta currently rendered in the pane, captured inside the
  // merged-pane renderRegion build. The copy handler reads THESE (no re-fetch)
  // so the copied text is exactly what's on screen — same alias set, same loaded
  // body. null until a body has loaded; the copy button's disabled state mirrors it.
  /** @type {import('../../types.js').MergedTranscript | null} */
  let copyTxFull = null;
  /** @type {import('../../types.js').EffectiveMeta | null} */
  let copyMeta = null;
  /** Selected WAV/clip name, per session id (drives re-transcribe + cache). */
  /** @type {Map<string, string>} */
  const selectedWav = new Map();
  /** Source toggle (original / stripped) per session id — which audio the
   * transcribe actions + the per-WAV picker operate on. Mirrors Recordings. */
  /** @type {Map<string, "original" | "stripped">} */
  const sourcePick = new Map();
  /** wavKey ("session/name[@stripped]") currently transcribing optimistically. */
  /** @type {Set<string>} */
  const txInflight = new Set();
  // TWO render signatures, deliberately split. The merged transcript is
  // O(segments) to rebuild — a long session's is a 100-200 ms synchronous
  // stall — so it must NOT share a signature with things that change every
  // second (job progress) or per utterance (the WAV list). Sharing one sig
  // was exactly the "/next freezes while transcribing" bug: each job tick
  // invalidated the combined sig and re-rendered thousands of segment rows
  // (see test_next_perf_soak.py::test_soak_transcript_heavy). The merged pane's
  // own sig (txSig) is held by `renderRegion(mergedHost, …)`; only the control
  // column keeps a closure sig here.
  let lastCtlSig = " "; // control column chrome: toggle/range/note/selected/cache
  // Keys (session@stamp) we've already scheduled a re-render for after the
  // lazy merged-transcript fetch lands — dedupes repeated misses.
  /** @type {Set<string>} */
  const txRerenderPending = new Set();
  /** Keys (session@stamp) whose merged-body fetch REJECTED — the failure
   * memory that paces retries at the poll cadence (same discipline as
   * api.js loadSessionFiles / recordings.js failedWave). A rejection evicts
   * the resource cache key, so without the memory the next resolve would
   * refetch immediately; a remembered key skips exactly one resolve (this
   * runs once per poll tick) so the retry fires on a later tick. A
   * re-transcribe changes the stamp — a different key — and fetches at once.
   * @type {Set<string>} */
  const failedMerged = new Set();
  /** Per-session last-good merged body — the shared bounded
   * stale-while-revalidate hold (api.js `createLastGoodHold`), which keeps a
   * re-transcribe (a new transcribed_at) from blanking the merged pane to
   * "loading transcript…" while the new body refetches. Show the previous merged
   * transcript in place until the fresh one lands (markRegionStale on the fetch
   * forces the swap), instead of wiping the transcript the operator is reading.
   * `get` returns the cold-load sentinel (null) only when this session never
   * resolved a body.
   * @type {import('../../api.js').LastGoodHold<import('../../types.js').MergedTranscript>} */
  const lastGoodMerged = createLastGoodHold();
  /** The lazily-fetched WAV listing for the FOCUSED session — the array
   * /api/state no longer embeds. Refreshed at the top of update() from the
   * (sid, files_sig) client cache; sourceFiles()/recordingFor read it. */
  /** @type {import('../../types.js').WavFile[]} */
  let currentFiles = [];
  /** The lazily-fetched WAV listing for the focused session — owns the
   * in-flight dedupe and the cold-vs-stale sentinel (next/session-files.js). */
  const filesSource = createFilesSource({
    onLoaded: () => {
      lastCtlSig = " ";
      markListStale(wavList);
      afterMutate();
    },
  });

  // ---- Helpers --------------------------------------------------------------

  // Resolve the OPEN session's FULL merged transcript from the lazy cache.
  // /api/state ships only a slim marker; on a cache miss this fires the fetch
  // once and re-renders (via afterMutate) when it lands. Returns null until
  // then. Keyed by (session, transcribed_at) so a re-transcribe re-fetches.
  /**
   * @param {import('../../types.js').MergedTranscriptMarker | null} marker
   * @param {string} sid
   * @returns {import('../../types.js').MergedTranscript | null}
   */
  const resolveMerged = (marker, sid) => {
    if (!marker || !marker.transcribed_at || !sid) return null;
    const stamp = marker.transcribed_at;
    const cached = sessionTranscript.peek(sid, stamp);
    if (cached !== undefined) {
      if (cached) lastGoodMerged.hold(sid, cached);
      return cached;
    }
    const key = `${sid}@${stamp}`;
    // `failedMerged.delete(key)` is check-AND-consume: a key whose last fetch
    // failed skips this one resolve, and the next poll tick's resolve retries.
    if (!txRerenderPending.has(key) && !failedMerged.delete(key)) {
      txRerenderPending.add(key);
      sessionTranscript.fetch(sid, stamp)
        .then(
          // Landed: force the merged pane past its sig gate and re-render now.
          () => { markRegionStale(mergedHost); afterMutate(); },
          // Failed: remember it and stay quiet — no afterMutate (nothing
          // changed to render, and the failure's own synchronous re-render
          // refiring the evicted fetch was the unpaced retry storm). The
          // remembered key defers the retry to a later poll tick.
          () => { failedMerged.add(key); },
        )
        .finally(() => { txRerenderPending.delete(key); });
    }
    // Stale-while-revalidate: hold the previous merged body during the refetch
    // so a re-transcribe refreshes the pane in place instead of blanking it to
    // "loading transcript…". null (→ cold-load placeholder) only when this
    // session never resolved a body. markRegionStale (above) forces the swap to
    // the fresh body once the fetch lands.
    return lastGoodMerged.get(sid);
  };

  /** The WAVs the picker + per-WAV transcribe operate on: the originals, or the
   * flattened silence-stripped region clips when the source toggle is stripped
   * (effectiveSource — shared with Recordings — falls back to original when no
   * stripped/ folder exists, so a stale toggle can't transcribe nothing).
   * Reads the lazily-fetched `currentFiles`, not session.files (which /api/state
   * no longer ships). */
  /** @returns {(import('../../types.js').WavFile | import('../../types.js').WavRegion)[]} */
  const sourceFiles = () =>
    effectiveSource(session, sourcePick) === "stripped" ? currentFiles.flatMap((f) => f.regions || []) : currentFiles;

  /** In-flight key for a (name, source) — matches transcribeWav's key shape so
   * the row "⟳ tx" busy state lines up with the optimistic set. */
  /** @param {string} name @param {"original"|"stripped"} src */
  const wavKey = (name, src) => `${session?.session || ""}/${name}${src === "stripped" ? "@stripped" : ""}`;

  /** Resolve the selected WAV/clip for the focused session (first if unset).
   * Takes the already-resolved source files so the per-tick path doesn't
   * rebuild the stripped-region flatMap a second time; defaults to computing
   * them for event-time callers. */
  /** @param {(import('../../types.js').WavFile | import('../../types.js').WavRegion)[]} [files] */
  const selectedFor = (files = sourceFiles()) => {
    if (!session || !files.length) return null;
    const want = selectedWav.get(session.session);
    return files.find((f) => f.name === want) ?? files[0] ?? null;
  };

  // ---- Meeting languages (ADR-0011) -----------------------------------------

  /** The effective candidate set = the current selection, or the global default
   * when nothing is picked (inherit). Drives both the readout and what the
   * server will run (it reads the same session-meta the save writes). */
  const effectiveLanguages = () => {
    const picked = selectedLanguages(txLanguages);
    return { codes: picked.length ? picked : inheritedDefault, inherited: picked.length === 0 };
  };

  /** @param {string} c */
  const langName = (c) => langNames.get(c) || c;

  /** Set an element's text in place, but ONLY when it changed AND no text
   * selection is being made inside it — the Interaction hold for per-tick in-place
   * text updaters (CLAUDE.md: apply `selectionInside`, don't clobber a mid-copy
   * selection). The equality check also skips the common no-change tick, so the
   * readout never churns a text node when nothing moved. Deferral advances no
   * signature, so the held-back text lands on the first tick after the selection
   * clears. @param {HTMLElement} el @param {string} val */
  const setReadoutText = (el, val) => {
    if (el.textContent !== val && !selectionInside(el)) el.textContent = val;
  };

  /** Repaint the "in effect" + "models that will run" lines. Derived and
   * non-interactive, updated in place each tick / on selection change — never a
   * sig-gated region (CLAUDE.md render hygiene). This is the antidote to a
   * surprise specialist: the Norwegian nb-whisper pass is named before you click
   * transcribe. */
  const renderLangReadout = () => {
    const { codes, inherited } = effectiveLanguages();
    setReadoutText(
      txLangEffective,
      codes.length
        ? `in effect: ${codes.map(langName).join(", ")}${inherited ? " — inherited from global default" : ""}`
        : "no languages set",
    );
    // Models the cover will load = generalist ∪ specialists for the effective
    // languages (mirrors catalog.cover_models; the server run is authoritative).
    const specialists = languageCatalog.specialists || {};
    const gen = generalist || "the global default model";
    const extras = [];
    const seen = new Set([generalist]);
    for (const c of codes) {
      const m = specialists[c];
      if (m && !seen.has(m)) { seen.add(m); extras.push(`${m} (${langName(c)})`); }
    }
    setReadoutText(
      txLangModels,
      extras.length
        ? `will run: ${gen} (generalist) + ${extras.join(" + ")}`
        : `will run: ${gen} (generalist) only`,
    );
  };
  txLanguages.addEventListener("change", renderLangReadout);

  /** Persist the current selection to session-meta (empty = inherit the global
   * default). The transcribe actions call this first (save-on-transcribe) so
   * what the readout shows is exactly what runs; the Save button reuses it. */
  const saveLanguages = async () => {
    if (!session) return;
    await putJson(`/api/session-meta/${encodeURIComponent(session.session)}`, {
      languages: selectedLanguages(txLanguages),
    });
  };

  /** Save-on-transcribe guard shared by both transcribe actions: persist the
   * languages FIRST and, if that fails, tell the operator it was the SAVE that
   * failed (the transcribe never ran) rather than mislabelling it a transcribe
   * failure. Returns false when the caller should abort. */
  const saveLanguagesOrAlert = async () => {
    try {
      await saveLanguages();
      return true;
    } catch (e) {
      alert(`Saving languages failed: ${errText(e)}`);
      return false;
    }
  };

  // The Save button (set-languages-without-transcribing) reuses the shared
  // save-button lifecycle (disable → "saving…" → "saved"/"failed" → re-enable);
  // the button is disabled whenever there's no session, so no guard is needed.
  wireSave({ btn: txLanguagesSave, status: txLanguagesStatus, put: saveLanguages, onSuccess: afterMutate });

  // ---- Transcribe (REAL — moved from recordings.js) -------------------------
  // Both actions are LANGUAGE-driven (ADR-0011): save the meeting's languages
  // first (WYSIWYG), then POST WITHOUT a model/backend — the server resolves the
  // generalist (batch-model.txt) + the specialists for those languages.

  /** @param {string} name @param {"original"|"stripped"} src */
  const transcribeWav = async (name, src) => {
    if (!session) return;
    const sid = session.session;
    const key = wavKey(name, src);
    txInflight.add(key);
    lastCtlSig = " ";
    afterMutate();
    try {
      if (!(await saveLanguagesOrAlert())) return;
      await postJson("/api/transcribe", { session: sid, name, source: src });
    } catch (e) {
      alert(`Transcribe failed: ${errText(e)}`);
    } finally {
      txInflight.delete(key);
      afterMutate();
    }
  };

  txOneBtn.addEventListener("click", () => {
    const sel = selectedFor();
    if (sel) transcribeWav(sel.name, effectiveSource(session, sourcePick));
  });

  txRangeBtn.addEventListener("click", async () => {
    if (!session) return;
    const sid = session.session;
    txRangeBtn.disabled = true;
    try {
      if (!(await saveLanguagesOrAlert())) return;
      await postJson("/api/transcribe-session", {
        session: sid,
        source: effectiveSource(session, sourcePick),
        from_iso: rangeFrom.value.trim(),
        to_iso: rangeTo.value.trim(),
        force: forceBox.checked,
      });
    } catch (e) {
      alert(`Session transcribe failed: ${errText(e)}`);
    } finally {
      txRangeBtn.disabled = false;
      afterMutate();
    }
  });

  // ---- Copy merged transcript (ported from classic main.js onCopyMerged) ----

  // Rebuild the export text from segments so display-name aliases match what
  // the user sees — the backend's `plain_text` uses raw speaker keys. One line
  // per non-suppressed segment ("[hh:mm:ss] Alias: text", "[uncertain]" suffix
  // on low-confidence lines); suppressed segments (full.suppressed) are never
  // included. Falls back to plain_text when no segments produced a line.
  /**
   * @param {import('../../types.js').MergedTranscript} full
   * @param {import('../../types.js').EffectiveMeta} meta
   * @returns {string}
   */
  const buildCopyText = (full, meta) => {
    const aliases = meta.aliases || {};
    const lines = [];
    for (const seg of full.segments || []) {
      const text = seg.text || "";
      if (!text) continue;
      const speaker = aliasOf(seg.speaker || "", aliases);
      let line = `[${fmtClock(seg.abs_start)}] ${speaker}: ${text}`;
      if (seg.low_confidence) line += " [uncertain]";
      lines.push(line);
    }
    return lines.join("\n") || full.plain_text || "";
  };

  const flashCopyStatus = makeStatusFlasher(txCopyStatus);

  /** Render the transcript text into a blank tab for manual select-copy.
   * @param {Window} w @param {string} text */
  const populateTranscriptTab = (w, text) => {
    w.document.body.style.font = "12px ui-monospace, Menlo, Consolas, monospace";
    w.document.body.style.whiteSpace = "pre-wrap";
    w.document.body.textContent = text;
  };

  // Bound ONCE at build time; reads the captured copyTxFull/copyMeta (the body
  // currently in the pane), not per-tick DOM. Disabled until a body has loaded.
  // The copy flow is the shared copyToClipboard (ui.js); this view's fallback
  // is the styled new-tab variant — window.open succeeds when the fallback
  // runs synchronously in the gesture (non-secure context), and degrades to a
  // prompt() when blocked (post-await clipboard rejection) — same design as
  // the classic dashboard's copy.
  txCopyBtn.addEventListener("click", async () => {
    if (!copyTxFull || !copyMeta) return;
    const out = buildCopyText(copyTxFull, copyMeta);
    if (!out) { flashCopyStatus("nothing to copy"); return; }
    await copyToClipboard(out, {
      onOk: () => flashCopyStatus("✓ copied"),
      onFallback: () => {
        const w = window.open("", "_blank");
        if (w) {
          populateTranscriptTab(w, out);
          flashCopyStatus("↗ opened in new tab");
        } else {
          window.prompt("Copy the merged transcript (Ctrl/Cmd-C, Enter):", out);
        }
      },
    });
  });

  // ---- Set primary (REAL — moved from recordings.js) ------------------------

  /** @param {string} name @param {string} backend @param {string} model @param {"original"|"stripped"} src */
  const setPrimary = async (name, backend, model, src) => {
    if (!session) return;
    const sid = session.session;
    try {
      await putJson(`/api/wav/${encodeURIComponent(sid)}/${encodeURIComponent(name)}/primary`,
        { backend, model, source: src });
    } catch (e) {
      alert(`Set primary failed: ${errText(e)}`);
    } finally {
      markRegionStale(mergedHost);
      lastCtlSig = " ";
      afterMutate();
    }
  };

  // ---- Per-WAV picker (drives re-transcribe + the cache panel) --------------

  /** @typedef {{ file: import('../../types.js').WavFile | import('../../types.js').WavRegion, src: "original"|"stripped" }} PickRow */

  /** Build one picker row (a <button class="wavrow">). Selection (`.is-sel`) is
   * NOT set here — it is renderList's `update`, gated on the row's own itemSig,
   * so picking a WAV repaints two rows instead of rebuilding the list. */
  /** @param {PickRow} m @returns {HTMLElement} */
  const buildPickRow = (m) => {
    const f = m.file;
    const node = tpl("tpl-next-txwavrow");
    const btn = /** @type {HTMLButtonElement} */ (node.firstElementChild);
    btn.dataset.wav = f.name;
    pick(node, "name").textContent = truncMid(f.name, 30);
    const who = f.speaker_name ? `${f.speaker_name} · ` : "";
    pick(node, "sub").textContent = `${who}${fmtBytes(f.size)}`;
    pick(node, "dur").textContent = fmtDur(f.duration_s);
    const tag = pick(node, "txTag");
    const inflight = txInflight.has(wavKey(f.name, m.src));
    if (inflight) { tag.textContent = "⟳ tx"; tag.className = "wavrow__tx is-busy"; }
    else if (f.transcript) { tag.textContent = "✓ tx"; tag.className = "wavrow__tx is-done"; }
    else { tag.textContent = "no tx"; tag.className = "wavrow__tx is-none"; }
    btn.addEventListener("click", () => {
      if (session) { selectedWav.set(session.session, f.name); lastCtlSig = " "; afterMutate(); }
    });
    return btn;
  };

  /** Reconcile key for a picker row — folds everything buildPickRow renders
   * EXCEPT selection (which is the row's itemSig instead), including the
   * inflight flag so a "⟳ tx" busy state recreates that one row. A changed key
   * rebuilds the row; unchanged rows keep their key (and state) across the
   * moveBefore reconcile. */
  /** @param {PickRow} m */
  const pickKey = (m) =>
    [
      m.file.name, m.file.size, m.file.duration_s, m.file.speaker_name || "",
      m.file.transcript?.transcribed_at || "", m.file.transcript ? 1 : 0,
      txInflight.has(wavKey(m.file.name, m.src)) ? 1 : 0,
    ].join("|");

  // ---- Transcript cache (REAL — moved from recordings.js) -------------------

  /** @param {import('../../types.js').WavFile | import('../../types.js').WavRegion | null} sel */
  const renderCache = (sel) => {
    // Show the whole RECORDING's cache (its original variants + every stripped
    // region clip's variants), not just the toggle-selected file's — so the
    // list doesn't change when you flip Original/Stripped; each row's source
    // tag distinguishes them. recordingFor maps a selected region back to its
    // parent original so either selection lands on the same list.
    const rec = recordingFor(sel, currentFiles);
    cacheBody.replaceChildren(); // static-render — user picked a row; one-shot rebuild of the cache list follows
    cacheHint.textContent = rec ? truncMid(rec.name, 30) : "no WAV";
    if (!rec) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Pick a WAV to see its cached transcripts.";
      cacheBody.appendChild(empty);
      return;
    }
    const variants = recordingVariants(rec);
    if (!variants.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No cached transcripts yet — transcribe this WAV first.";
      cacheBody.appendChild(empty);
      return;
    }
    for (const v of variants) {
      const row = tpl("tpl-next-cacherow");
      pick(row, "id").textContent = `${v.backend || "?"} · ${v.model || "?"}`;
      const srcTag = pick(row, "src");
      srcTag.textContent = v.source;
      srcTag.classList.add(v.source === "stripped" ? "is-stripped" : "is-original");
      const wordCount = (v.text || "").trim() ? (v.text || "").trim().split(/\s+/).length : 0;
      pick(row, "meta").textContent = `${wordCount} w · ${fmtMs(v.transcribe_ms)}`;
      // is_primary is per-file (the original has one, each region has one); the
      // merge reads the relevant files' primaries by source, so showing each
      // file's primary is correct. set-primary targets the row's own file.
      const pbtn = /** @type {HTMLButtonElement} */ (pick(row, "primary"));
      pbtn.textContent = v.is_primary ? "● primary" : "set";
      if (v.is_primary) pbtn.classList.add("is-primary");
      else pbtn.addEventListener("click", () => setPrimary(v.file, v.backend, v.model, v.source));
      cacheBody.appendChild(row);
    }
  };

  // ---- Per-tick update ------------------------------------------------------

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    session = sess;
    const tx = sess?.session_transcript || null;
    const sid = sess?.session || "";
    const job = sess?.progress || null;
    const filesSig = sess?.files_sig || "";

    // ---- Meeting languages (ADR-0011). Track the operator's generalist for the
    // readout, seed the selection once per session-view (never per tick — an
    // unsaved in-progress selection must survive a poll / a save-on-transcribe
    // re-poll, Interaction hold), and repaint the derived readout in place.
    generalist = j?.batch_model_effective || "";
    inheritedDefault = j?.languages?.default || [];
    if (!langSeeded && sess && document.activeElement !== txLanguages) {
      setSelectedLanguages(txLanguages, metaFor(sess).languages || []);
      langSeeded = true;
    }
    txLanguages.disabled = txLanguagesSave.disabled = !sess;
    if (sess) renderLangReadout();
    else { setReadoutText(txLangEffective, ""); setReadoutText(txLangModels, ""); }

    // Resolve the focused session's WAV listing — the array /api/state no
    // longer ships, fetched once per (sid, files_sig) and client-cached. `null`
    // → a fetch is in flight; `[]` → nothing to fetch (empty files_sig).
    const { files: fetchedFiles, loading: filesLoading } = filesSource.resolve(sid, filesSig);
    currentFiles = fetchedFiles;

    // ---- Job progress (one job per session). renderJobBar does in-place writes
    // on prebuilt nodes, EVERY tick — deliberately outside both signature gates.
    // Sharing a signature with the O(segments) merged transcript was the "/next
    // freezes while transcribing" bug (one rebuild per job tick).
    renderJobBar({ jobBar, jobLabel, jobCount, jobProgress, jobWav }, job);

    // ---- Merged transcript + header — rendered through renderRegion on the
    // mergedHost: it gates on txSig (session, marker stamp, loaded-ness, and the
    // label/aliases the rendered lines show) AND defers, without advancing, while
    // a selection is active inside the pane — so a per-WAV sidecar landing mid-job
    // (or an alias edit) can't dissolve a selection the operator is copying. The
    // header + copy-button capture + hint all live INSIDE the build closure so
    // they stay in sync with the body actually rendered (a deferred render must
    // not desync copy ↔ pane); resolveMerged marks the pane stale when the lazy
    // body lands, so "loading… → loaded" re-crosses the gate without a marker change.
    const txFull = sess ? resolveMerged(tx, sid) : null;
    const meta = sess ? metaFor(sess) : null;
    const aliasSig = meta ? Object.entries(meta.aliases).map(([k, v]) => `${k}=${v}`).join(",") : "";
    const txSig = [sid, tx?.transcribed_at || "", txFull ? 1 : 0, meta?.label || "", aliasSig].join("§");
    renderRegion(
      mergedHost,
      () => {
        header(headHost, {
          eyebrow: "Session · 3 Transcript",
          title: "Transcript",
          sub: tx && sess
            ? inline("merged result for ", strong(sessionLabel(sess)))
            : (sess ? "not transcribed yet — declare languages and transcribe below" : "no session selected — pick one from the spine"),
        });

        // Copy button: enabled only once the FULL merged body has loaded (the
        // slim marker alone can't produce alias-applied lines). Captured here,
        // inside the build, so the click handler copies exactly what's shown.
        if (sess && txFull && meta) {
          copyTxFull = txFull;
          copyMeta = meta;
          txCopyBtn.disabled = false;
        } else {
          copyTxFull = null;
          copyMeta = null;
          txCopyBtn.disabled = true;
        }

        // Merged transcript (main/left). `tx` is the slim marker; the body comes
        // from the lazy cache. While it loads, the marker still drives the "has a
        // transcript" branch so the hint shows the marker's segment count.
        if (sess && tx) {
          const segCount = txFull ? (txFull.segments || []).length : (tx.segment_count || 0);
          const model = txFull?.model || "?";
          txHint.textContent = `${segCount} seg · model ${model} · took ${fmtMs(txFull?.transcribe_ms)}`;
          if (txFull) return mergedTranscript.render(txFull, metaFor(sess));
          const loading = document.createElement("div");
          loading.className = "empty";
          loading.textContent = "loading transcript…";
          return loading;
        }
        txHint.textContent = "not run";
        // vc empty-state (js/vc, warmed at boot) — .work .empty-state in
        // next.css keeps the old .empty metrics.
        return createEmptyStateSync({
          title: sess ? "Not transcribed yet" : "No session selected",
          detail: sess
            ? "Declare the meeting's languages, then transcribe the session range (or a single WAV) to produce the merged transcript here."
            : "Pick a session from the spine to view its merged transcript.",
        }).el;
      },
      { sig: txSig },
    );

    // ---- Control-column CHROME (source toggle / range / note / selected-WAV
    // button / cache) — own signature, selection-inclusive. The per-WAV picker
    // LIST is split out below with its own files-set gate so a thousands-row
    // session doesn't replaceChildren on every selection / poll tick. Skip the
    // chrome rebuild when nothing it depends on changed, or while a range box is
    // mid-edit (so an in-progress ISO edit isn't wiped).
    const src = effectiveSource(session, sourcePick);
    const srcFiles = sourceFiles();
    const sel = selectedFor(srcFiles);
    const ctlSig = [
      sid,
      src,
      sess?.stripped ? "S" : "",
      filesLoading ? "L" : "",
      sel?.name || "",
      sel ? (txInflight.has(wavKey(sel.name, src)) ? "B" : "") : "",
      sel?.transcript?.transcribed_at || "",
      srcFiles.length,
      sess?.earliest_iso || "",
      sess?.latest_iso || "",
    ].join("§");
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    const editing = !!focused && (focused === rangeFrom || focused === rangeTo);
    if (!(ctlSig === lastCtlSig || editing)) {
      lastCtlSig = ctlSig;

      // Source toggle (original / stripped) — drives the range transcribe AND
      // the per-WAV picker below.
      srcSwHost.replaceChildren(buildSourceToggle({ // gate-allow: raw-swap — ctlSig-gated swap of the buttons-only source toggle
        active: src,
        hasStripped: !!sess?.stripped,
        onPick: (which) => {
          if (!session) return;
          sourcePick.set(session.session, which);
          lastCtlSig = " ";
          markListStale(wavList);
          afterMutate();
        },
      }));

      // Range placeholders + note
      if (!rangeFrom.value) rangeFrom.placeholder = sess?.earliest_iso || "ISO";
      if (!rangeTo.value) rangeTo.placeholder = sess?.latest_iso || "ISO";
      const srcWord = src === "stripped" ? "stripped clip" : "WAV";
      txRangeBtn.disabled = !sess || !srcFiles.length;
      txNote.textContent = sess
        ? (srcFiles.length ? `transcribes every ${srcWord} in the range` : `no ${srcWord}s to transcribe yet`)
        : "pick a session first";

      // Per-WAV re-transcribe selected-WAV button
      txSelLabel.textContent = sel ? `Selected: ${truncMid(sel.name, 22)} · ${src}` : "Selected WAV";
      const oneBusy = !!sel && txInflight.has(wavKey(sel.name, src));
      txOneBtn.disabled = !sel || oneBusy;
      txOneBtn.textContent = oneBusy ? "⟳ transcribing" : (sel?.transcript ? "re-transcribe" : "transcribe");

      // Transcript cache for the selected WAV/clip
      renderCache(sel);
    }

    // ---- Per-WAV picker LIST ------------------------------------------------
    // One `renderList` call for both states: rows when there are any, an empty
    // `items` when there aren't. `wavList` holds ONLY keyed rows — the
    // empty/loading placeholder is a hidden-toggled SIBLING (views.html) — so
    // the seam owns the host outright.
    //
    // The seam owns the gate (sig skip · selection defer without advancing ·
    // per-row itemSig). Everything a picker row DISPLAYS is folded into
    // `pickKey`, so `update` has one job: the selection highlight. The selected
    // name is therefore a term in BOTH signatures — the list sig so a click gets
    // past the skip at all, the item sig so only the two rows that flipped
    // repaint rather than every row (issue #213). `auditRows` is ON: these rows
    // are plain buttons whose whole content comes from create + update, so a
    // probe row is a sound comparison (unlike the Recordings <details> rows,
    // whose bodies lazy-load).
    const inflightSig = [...txInflight].filter((k) => k.startsWith(`${sid}/`)).sort().join(",");
    const pickState = listState({ hasSession: !!sess, loading: filesLoading, count: srcFiles.length });
    const pickSelName = sel?.name || "";
    const pickRendered = renderList(
      wavList,
      pickState === "rows" ? srcFiles.map((f) => ({ file: f, src })) : [],
      {
        key: pickKey,
        create: buildPickRow,
        update: (node, m) => { node.classList.toggle("is-sel", m.file.name === pickSelName); },
        itemSig: (m) => (m.file.name === pickSelName ? "sel" : ""),
        sig: [pickState, sid, src, filesSig, inflightSig, pickSelName].join("§"),
        auditRows: true,
      },
    );
    if (pickRendered) {
      wavEmpty.hidden = pickState === "rows";
      if (pickState !== "rows") {
        wavEmpty.textContent = filesLoading
          ? "loading recordings…"
          : sess
            ? (src === "stripped" ? "No stripped clips — strip silence in Recordings first." : "No WAVs recorded yet.")
            : "Pick a session from the spine.";
      }
    }
  };

  return { node: frag, update };
}
