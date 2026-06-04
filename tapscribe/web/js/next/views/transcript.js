// @ts-check
// Stages · Transcript (SESSION stage 3). The merged transcript for the open
// session (main/left) + a transcription CONTROL COLUMN (right): the engine
// selector (backend chips + compact model dropdown + Canary source/target),
// the transcribe controls (session range from/to + force + a Transcribe
// action, plus a per-WAV re-transcribe picker), and the per-WAV transcript
// cache (set-primary).
//
// REUSES merged-transcript.js verbatim for the IRC merged result, and the new
// Stages engine.js (which mirrors session-detail's engine controls) for the
// engine panel. The REAL transcribe wiring (POST /api/transcribe, POST
// /api/transcribe-session, PUT /api/wav/{s}/{name}/primary, job progress) was
// moved here from recordings.js — Transcript drives the transcribe jobs now;
// Recordings is files + silence-stripping only. No mock data here.
//
// Built once for the page (per session id, like the rest of the SESSION
// stages); `update(j, session)` re-renders the merged transcript + the
// control column (signature-gated so an in-progress range edit isn't
// clobbered). The engine panel is rebuilt by main on engine state changes
// (rebuildEngine).

import { tpl, pick } from "../../templates.js";
import { postJson, putJson, fetchSessionTranscript, peekSessionTranscript } from "../../api.js";
import { fmtBytes, fmtClock, fmtDur, fmtMs, truncMid } from "../../formatters.js";
import { aliasOf } from "../../speakers.js";
import { header, strong, inline, buildSourceToggle } from "../shell.js";
import * as mergedTranscript from "../../components/merged-transcript.js";

/**
 * @param {{
 *   metaFor: (s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta,
 *   engineState: () => import('../components/engine.js').EngineState,
 *   rebuildEngine: (host: Element) => void,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void, rebuildEngine: () => void }}
 */
export function build(ctx) {
  const { metaFor, engineState, rebuildEngine, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-transcript");

  const headHost = pick(frag, "head");
  const txHint = pick(frag, "txHint");
  const txCopyBtn = /** @type {HTMLButtonElement} */ (pick(frag, "txCopyBtn"));
  const txCopyStatus = pick(frag, "txCopyStatus");
  const mergedHost = pick(frag, "mergedHost");
  const engineHost = pick(frag, "engineHost");
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
  const jobBar = pick(frag, "jobBar");
  const jobLabel = pick(frag, "jobLabel");
  const jobCount = pick(frag, "jobCount");
  const jobFill = /** @type {HTMLElement} */ (pick(frag, "jobFill"));
  const jobWav = pick(frag, "jobWav");
  const cacheHint = pick(frag, "cacheHint");
  const cacheBody = pick(frag, "cacheBody");

  rebuildEngine(engineHost);

  // ---- View-local state -----------------------------------------------------
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  // The merged body + meta currently rendered in the pane, captured inside the
  // lastTxSig gate. The copy handler reads THESE (no re-fetch) so the copied
  // text is exactly what's on screen — same alias set, same loaded body. null
  // until a body has loaded; the copy button's disabled state mirrors it.
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
  // (see test_next_perf_soak.py::test_soak_transcript_heavy).
  let lastTxSig = " "; // merged transcript + header (sentinel: first update renders)
  let lastCtlSig = " "; // control column: range/note/WAV picker/cache
  // Keys (session@stamp) we've already scheduled a re-render for after the
  // lazy merged-transcript fetch lands — dedupes repeated misses.
  /** @type {Set<string>} */
  const txRerenderPending = new Set();

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
    const cached = peekSessionTranscript(sid, stamp);
    if (cached !== undefined) return cached;
    const key = `${sid}@${stamp}`;
    if (!txRerenderPending.has(key)) {
      txRerenderPending.add(key);
      fetchSessionTranscript(sid, stamp)
        .catch(() => { /* transient failure — next poll retries */ })
        .finally(() => { txRerenderPending.delete(key); lastTxSig = " "; afterMutate(); });
    }
    return null;
  };

  /** Effective source for the focused session — falls back to original when no
   * stripped/ folder exists, so a stale "stripped" toggle can't transcribe
   * nothing after the clips were cleared. */
  const effectiveSource = () => {
    const want = sourcePick.get(session?.session || "") || "original";
    return (want === "stripped" && !session?.stripped) ? "original" : want;
  };

  /** The WAVs the picker + per-WAV transcribe operate on: the originals, or the
   * flattened silence-stripped region clips when the source toggle is stripped. */
  /** @returns {(import('../../types.js').WavFile | import('../../types.js').WavRegion)[]} */
  const sourceFiles = () => {
    const files = session?.files || [];
    return effectiveSource() === "stripped" ? files.flatMap((f) => f.regions || []) : files;
  };

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

  /** Read the Canary source/target lang from the engine panel's selects. */
  const langValues = () => {
    /** @param {string} name */
    const valOf = (name) => /** @type {HTMLSelectElement | null} */ (
      engineHost.querySelector(`select[data-input-name="${name}"]`))?.value || "";
    return { source_lang: valOf("source_lang"), target_lang: valOf("target_lang") };
  };

  // ---- Transcribe (REAL — moved from recordings.js) -------------------------

  /** @param {string} name @param {"original"|"stripped"} src */
  const transcribeWav = async (name, src) => {
    if (!session) return;
    const sid = session.session;
    const eng = engineState();
    const key = wavKey(name, src);
    txInflight.add(key);
    lastCtlSig = " ";
    afterMutate();
    try {
      await postJson("/api/transcribe", {
        session: sid, name, source: src,
        model: eng.model, backend: eng.backend, ...langValues(),
      });
    } catch (e) {
      alert(`Transcribe failed: ${String(e).replace(/^Error:\s*/, "")}`);
    } finally {
      txInflight.delete(key);
      afterMutate();
    }
  };

  txOneBtn.addEventListener("click", () => {
    const sel = selectedFor();
    if (sel) transcribeWav(sel.name, effectiveSource());
  });

  txRangeBtn.addEventListener("click", async () => {
    if (!session) return;
    const sid = session.session;
    const eng = engineState();
    txRangeBtn.disabled = true;
    try {
      await postJson("/api/transcribe-session", {
        session: sid,
        source: effectiveSource(),
        model: eng.model, backend: eng.backend,
        from_iso: rangeFrom.value.trim(),
        to_iso: rangeTo.value.trim(),
        force: forceBox.checked,
        ...langValues(),
      });
    } catch (e) {
      alert(`Session transcribe failed: ${String(e).replace(/^Error:\s*/, "")}`);
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

  /** @type {ReturnType<typeof setTimeout> | null} */
  let copyStatusTimer = null;
  /** @param {string} msg */
  const flashCopyStatus = (msg) => {
    if (copyStatusTimer != null) clearTimeout(copyStatusTimer);
    txCopyStatus.textContent = msg;
    copyStatusTimer = setTimeout(() => {
      if (txCopyStatus.textContent === msg) txCopyStatus.textContent = "";
      copyStatusTimer = null;
    }, 1500);
  };

  /** Render the transcript text into a blank tab for manual select-copy.
   * @param {Window} w @param {string} text */
  const populateTranscriptTab = (w, text) => {
    w.document.body.style.font = "12px ui-monospace, Menlo, Consolas, monospace";
    w.document.body.style.whiteSpace = "pre-wrap";
    w.document.body.textContent = text;
  };

  // Bound ONCE at build time; reads the captured copyTxFull/copyMeta (the body
  // currently in the pane), not per-tick DOM. Disabled until a body has loaded.
  txCopyBtn.addEventListener("click", async () => {
    if (!copyTxFull || !copyMeta) return;
    const out = buildCopyText(copyTxFull, copyMeta);
    if (!out) { flashCopyStatus("nothing to copy"); return; }
    // TapScribe's documented multi-machine mode is plain http over LAN
    // (start.sh --lan; TLS is opt-in) — a NON-SECURE context where
    // navigator.clipboard doesn't exist. The await below would reject and a
    // window.open in the catch would be past the user-gesture window (popup
    // blocked), so open the fallback tab SYNCHRONOUSLY inside the click
    // handler instead — same design as the classic dashboard's copy.
    const haveClipboard = window.isSecureContext
      && typeof navigator.clipboard?.writeText === "function";
    if (!haveClipboard) {
      const w = window.open("", "_blank");
      if (w) {
        populateTranscriptTab(w, out);
        flashCopyStatus("↗ opened in new tab");
      } else {
        window.prompt("Copy the merged transcript (Ctrl/Cmd-C, Enter):", out);
      }
      return;
    }
    try {
      await navigator.clipboard.writeText(out);
      flashCopyStatus("✓ copied");
    } catch {
      // Clipboard write rejected (permission denied). Past the user gesture —
      // a popup will likely be blocked; try once, then fall back to a
      // prompt() the operator can select-copy from.
      const w = window.open("", "_blank");
      if (w) {
        populateTranscriptTab(w, out);
        flashCopyStatus("↗ opened in new tab");
      } else {
        window.prompt("Copy the merged transcript (Ctrl/Cmd-C, Enter):", out);
      }
    }
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
      alert(`Set primary failed: ${String(e).replace(/^Error:\s*/, "")}`);
    } finally {
      lastTxSig = " ";
      lastCtlSig = " ";
      afterMutate();
    }
  };

  // ---- Per-WAV picker (drives re-transcribe + the cache panel) --------------

  /** @param {import('../../types.js').WavFile | import('../../types.js').WavRegion} f @param {boolean} selected */
  const wavRow = (f, selected) => {
    const node = tpl("tpl-next-txwavrow");
    const btn = /** @type {HTMLButtonElement} */ (node.firstElementChild);
    if (selected) btn.classList.add("is-sel");
    pick(node, "name").textContent = truncMid(f.name, 30);
    const who = f.speaker_name ? `${f.speaker_name} · ` : "";
    pick(node, "sub").textContent = `${who}${fmtBytes(f.size)}`;
    pick(node, "dur").textContent = fmtDur(f.duration_s);
    const tag = pick(node, "txTag");
    const inflight = txInflight.has(wavKey(f.name, effectiveSource()));
    if (inflight) { tag.textContent = "⟳ tx"; tag.className = "wavrow__tx is-busy"; }
    else if (f.transcript) { tag.textContent = "✓ tx"; tag.className = "wavrow__tx is-done"; }
    else { tag.textContent = "no tx"; tag.className = "wavrow__tx is-none"; }
    btn.addEventListener("click", () => {
      if (session) { selectedWav.set(session.session, f.name); lastCtlSig = " "; afterMutate(); }
    });
    return node;
  };

  // ---- Transcript cache (REAL — moved from recordings.js) -------------------

  /** @param {import('../../types.js').WavFile | import('../../types.js').WavRegion | null} sel */
  const renderCache = (sel) => {
    cacheBody.replaceChildren();
    cacheHint.textContent = sel ? truncMid(sel.name, 30) : "no WAV";
    const variants = sel?.transcripts || [];
    if (!sel) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Pick a WAV to see its cached transcripts.";
      cacheBody.appendChild(empty);
      return;
    }
    if (!variants.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No cached transcripts yet — transcribe this WAV first.";
      cacheBody.appendChild(empty);
      return;
    }
    // The primary is whichever variant matches sel.transcript's backend+model.
    const primary = sel.transcript;
    for (const v of variants) {
      const row = tpl("tpl-next-cacherow");
      pick(row, "id").textContent = `${v.backend || "?"} · ${v.model || "?"}`;
      const srcTag = pick(row, "src");
      srcTag.textContent = v.source || "original";
      srcTag.classList.add(v.source === "stripped" ? "is-stripped" : "is-original");
      const wordCount = (v.text || "").trim() ? (v.text || "").trim().split(/\s+/).length : 0;
      pick(row, "meta").textContent = `${wordCount} w · ${fmtMs(v.transcribe_ms)}`;
      const isPrimary = !!primary && primary.backend === v.backend && primary.model === v.model && primary.source === v.source;
      const pbtn = /** @type {HTMLButtonElement} */ (pick(row, "primary"));
      pbtn.textContent = isPrimary ? "● primary" : "set";
      if (isPrimary) pbtn.classList.add("is-primary");
      else pbtn.addEventListener("click", () => setPrimary(sel.name, v.backend, v.model, /** @type {"original"|"stripped"} */ (v.source || "original")));
      cacheBody.appendChild(row);
    }
  };

  // ---- Per-tick update ------------------------------------------------------

  /**
   * @param {import('../../types.js').AppState} _j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (_j, sess) => {
    session = sess;
    const tx = sess?.session_transcript || null;
    const sid = sess?.session || "";
    const job = sess?.progress || null;

    // ---- Job progress (one job per session — transcribe OR strip). In-place
    // text/width writes on prebuilt nodes, EVERY tick — deliberately outside
    // both signature gates. Progress ticks ~1/s during a job; when they shared
    // a signature with the merged transcript, each tick rebuilt the whole
    // O(segments) transcript DOM (the "/next freezes while transcribing" bug).
    if (job) {
      jobBar.hidden = false;
      const pct = job.total > 0 ? Math.round(100 * job.current / job.total) : 0;
      jobLabel.textContent = job.kind === "strip" ? "Stripping silence" : "Transcribing";
      jobCount.textContent = `${job.current} / ${job.total}`;
      jobFill.style.width = `${pct}%`;
      jobWav.textContent = job.current_file ? `current: ${job.current_file}` : "";
    } else {
      jobBar.hidden = true;
    }

    // ---- Merged transcript + header — gated on what THEY display: session,
    // marker stamp, loaded-ness, and the label/aliases the rendered lines
    // show. resolveMerged resets lastTxSig when the lazy body fetch lands, so
    // "loading… → loaded" re-crosses this gate without a marker change.
    const txFull = sess ? resolveMerged(tx, sid) : null;
    const meta = sess ? metaFor(sess) : null;
    const aliasSig = meta ? Object.entries(meta.aliases).map(([k, v]) => `${k}=${v}`).join(",") : "";
    const txSig = [sid, tx?.transcribed_at || "", txFull ? 1 : 0, meta?.label || "", aliasSig].join("§");
    if (txSig !== lastTxSig) {
      lastTxSig = txSig;

      header(headHost, {
        eyebrow: "Session · 3 Transcript",
        title: "Transcript",
        sub: tx && sess
          ? inline("merged result for ", strong(metaFor(sess).label || sess.session))
          : (sess ? "not transcribed yet — pick a model and transcribe below" : "no session selected — pick one from the spine"),
      });

      // Copy button: enabled only once the FULL merged body has loaded (the
      // slim marker alone can't produce alias-applied lines). Capture the body
      // + meta the pane is rendering (same `meta` the render call below uses)
      // so the click handler copies exactly what's shown. Toggled HERE, inside
      // the gate — never per tick.
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
        if (txFull) {
          mergedHost.replaceChildren(mergedTranscript.render(txFull, metaFor(sess), { showAudit: true }));
        } else {
          const loading = document.createElement("div");
          loading.className = "empty";
          loading.textContent = "loading transcript…";
          mergedHost.replaceChildren(loading);
        }
      } else {
        txHint.textContent = "not run";
        const empty = document.createElement("div");
        empty.className = "empty";
        const h = document.createElement("div");
        h.className = "empty__h";
        h.textContent = sess ? "Not transcribed yet" : "No session selected";
        const d = document.createElement("div");
        d.textContent = sess
          ? "Pick a model in the engine panel, then transcribe the session range (or a single WAV) to produce the merged transcript here."
          : "Pick a session from the spine to view its merged transcript.";
        empty.append(h, d);
        mergedHost.replaceChildren(empty);
      }
    }

    // ---- Control column (source toggle/range/note/WAV picker/cache) — own
    // signature. Skip the DOM-heavy WAV-list / cache rebuild when nothing it
    // depends on changed, or while a range box is mid-edit (so an in-progress
    // ISO edit isn't wiped). The source + stripped flag are folded in so an
    // original↔stripped switch re-renders the picker.
    const src = effectiveSource();
    const srcFiles = sourceFiles();
    const sel = selectedFor(srcFiles);
    const wavSig = srcFiles.map((f) => `${f.name}:${f.transcript?.transcribed_at || ""}:${(f.transcripts || []).length}`).join("|");
    const ctlSig = [
      sid,
      src,
      sess?.stripped ? "S" : "",
      sel?.name || "",
      [...txInflight].filter((k) => k.startsWith(`${sid}/`)).sort().join(","),
      wavSig,
      sess?.earliest_iso || "",
      sess?.latest_iso || "",
    ].join("§");
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    const editing = !!focused && (focused === rangeFrom || focused === rangeTo);
    if (ctlSig === lastCtlSig || editing) return;
    lastCtlSig = ctlSig;

    // Source toggle (original / stripped) — drives the range transcribe AND the
    // per-WAV picker below.
    srcSwHost.replaceChildren(buildSourceToggle({
      active: src,
      hasStripped: !!sess?.stripped,
      onPick: (which) => {
        if (!session) return;
        sourcePick.set(session.session, which);
        lastCtlSig = " ";
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

    // Per-WAV re-transcribe picker + selected-WAV button
    txSelLabel.textContent = sel ? `Selected: ${truncMid(sel.name, 22)} · ${src}` : "Selected WAV";
    const oneBusy = !!sel && txInflight.has(wavKey(sel.name, src));
    txOneBtn.disabled = !sel || oneBusy;
    txOneBtn.textContent = oneBusy ? "⟳ transcribing" : (sel?.transcript ? "re-transcribe" : "transcribe");
    if (!srcFiles.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = sess
        ? (src === "stripped" ? "No stripped clips — strip silence in Recordings first." : "No WAVs recorded yet.")
        : "Pick a session from the spine.";
      wavList.replaceChildren(empty);
    } else {
      const listFrag = document.createDocumentFragment();
      for (const f of srcFiles) listFrag.appendChild(wavRow(f, f.name === sel?.name));
      wavList.replaceChildren(listFrag);
    }

    // Transcript cache for the selected WAV/clip
    renderCache(sel);
  };

  return { node: frag, update, rebuildEngine: () => rebuildEngine(engineHost) };
}
