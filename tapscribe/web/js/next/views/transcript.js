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
import { fmtBytes, fmtDur, fmtMs, truncMid } from "../../formatters.js";
import { header, strong, inline } from "../shell.js";
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
  /** Selected original WAV name, per session id (drives re-transcribe + cache). */
  /** @type {Map<string, string>} */
  const selectedWav = new Map();
  /** wavKey ("session/name[@stripped]") currently transcribing optimistically. */
  /** @type {Set<string>} */
  const txInflight = new Set();
  let lastSig = " "; // sentinel so the first update always renders
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
        .finally(() => { txRerenderPending.delete(key); lastSig = " "; afterMutate(); });
    }
    return null;
  };

  /** Resolve the selected original WAV for the focused session (first if unset). */
  const selectedFor = () => {
    if (!session) return null;
    const files = session.files || [];
    if (!files.length) return null;
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
    const key = `${sid}/${name}${src === "stripped" ? "@stripped" : ""}`;
    txInflight.add(key);
    lastSig = " ";
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
    if (sel) transcribeWav(sel.name, "original");
  });

  txRangeBtn.addEventListener("click", async () => {
    if (!session) return;
    const sid = session.session;
    const eng = engineState();
    txRangeBtn.disabled = true;
    try {
      await postJson("/api/transcribe-session", {
        session: sid,
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
      lastSig = " ";
      afterMutate();
    }
  };

  // ---- Per-WAV picker (drives re-transcribe + the cache panel) --------------

  /** @param {import('../../types.js').WavFile} f @param {boolean} selected */
  const wavRow = (f, selected) => {
    const node = tpl("tpl-next-txwavrow");
    const btn = /** @type {HTMLButtonElement} */ (node.firstElementChild);
    if (selected) btn.classList.add("is-sel");
    pick(node, "name").textContent = truncMid(f.name, 30);
    const who = f.speaker_name ? `${f.speaker_name} · ` : "";
    pick(node, "sub").textContent = `${who}${fmtBytes(f.size)}`;
    pick(node, "dur").textContent = fmtDur(f.duration_s);
    const tag = pick(node, "txTag");
    const inflight = txInflight.has(`${session?.session || ""}/${f.name}`);
    if (inflight) { tag.textContent = "⟳ tx"; tag.className = "wavrow__tx is-busy"; }
    else if (f.transcript) { tag.textContent = "✓ tx"; tag.className = "wavrow__tx is-done"; }
    else { tag.textContent = "no tx"; tag.className = "wavrow__tx is-none"; }
    btn.addEventListener("click", () => {
      if (session) { selectedWav.set(session.session, f.name); lastSig = " "; afterMutate(); }
    });
    return node;
  };

  // ---- Transcript cache (REAL — moved from recordings.js) -------------------

  /** @param {import('../../types.js').WavFile | null} sel */
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
    const files = sess?.files || [];
    const sel = selectedFor();
    const job = sess?.progress || null;

    // ---- Merged transcript (own signature — cheap to gate separately). ----
    const txSig = `${sid}::${tx?.transcribed_at || ""}`;

    // ---- Control-column signature gate. Skip the DOM-heavy WAV-list / cache
    // rebuild when nothing it depends on changed, or while a range box is
    // mid-edit (so an in-progress ISO edit isn't wiped).
    const wavSig = files.map((f) => `${f.name}:${f.transcript?.transcribed_at || ""}:${(f.transcripts || []).length}`).join("|");
    const sig = [
      txSig,
      sel?.name || "",
      job ? `${job.kind}:${job.current}/${job.total}:${job.current_file || ""}` : "",
      [...txInflight].filter((k) => k.startsWith(`${sid}/`)).sort().join(","),
      wavSig,
    ].join("§");
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    const editing = !!focused && (focused === rangeFrom || focused === rangeTo);
    if (sig === lastSig || editing) return;
    lastSig = sig;

    // Header
    header(headHost, {
      eyebrow: "Session · 3 Transcript",
      title: "Transcript",
      sub: tx && sess
        ? inline("merged result for ", strong(metaFor(sess).label || sess.session))
        : (sess ? "not transcribed yet — pick a model and transcribe below" : "no session selected — pick one from the spine"),
    });

    // Merged transcript (main/left). `tx` is the slim marker; the body comes
    // from the lazy cache. While it loads, the marker still drives the "has a
    // transcript" branch so the hint shows the marker's segment count.
    const txFull = sess ? resolveMerged(tx, sid) : null;
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

    // Job progress (one job per session — transcribe OR strip).
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

    // Range placeholders + note
    if (!rangeFrom.value) rangeFrom.placeholder = sess?.earliest_iso || "ISO";
    if (!rangeTo.value) rangeTo.placeholder = sess?.latest_iso || "ISO";
    txRangeBtn.disabled = !sess || !files.length;
    txNote.textContent = sess
      ? (files.length ? "transcribes every WAV in the range" : "no WAVs to transcribe yet")
      : "pick a session first";

    // Per-WAV re-transcribe picker + selected-WAV button
    txSelLabel.textContent = sel ? `Selected: ${truncMid(sel.name, 24)}` : "Selected WAV";
    const oneBusy = !!sel && txInflight.has(`${sid}/${sel.name}`);
    txOneBtn.disabled = !sel || oneBusy;
    txOneBtn.textContent = oneBusy ? "⟳ transcribing" : (sel?.transcript ? "re-transcribe" : "transcribe");
    if (!files.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = sess
        ? "No WAVs recorded yet."
        : "Pick a session from the spine.";
      wavList.replaceChildren(empty);
    } else {
      const listFrag = document.createDocumentFragment();
      for (const f of files) listFrag.appendChild(wavRow(f, f.name === sel?.name));
      wavList.replaceChildren(listFrag);
    }

    // Transcript cache for the selected WAV
    renderCache(sel);
  };

  return { node: frag, update, rebuildEngine: () => rebuildEngine(engineHost) };
}
