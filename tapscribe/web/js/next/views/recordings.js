// @ts-check
// Stages · Recordings (SESSION stage 2). The WIDE per-session stage: a
// waveform-cut PLACEHOLDER (net-new, no backend) sitting above the REAL
// strip-silence knobs + the per-WAV list (originals + indented stripped
// region clips), a source (original/stripped) toggle, transcribe (one WAV +
// session range with from/to + force) driven by the REUSED Stages engine
// selector, and the per-WAV transcript cache with set-primary.
//
// Mirrors session-detail.js's data flow (the classic dashboard) but is
// FRESH /next code — it builds the WAV list / strip controls / transcribe /
// cache from /api/state and the same endpoints (POST /api/transcribe,
// POST /api/transcribe-session, POST /api/sessions/{s}/strip-silence,
// DELETE /api/sessions/{s}/stripped, PUT /api/wav/{s}/{name}/primary). No
// mock data — the only stub is the waveform canvas, tagged inline.
//
// Built once for the page; `update(j, session)` re-renders the WAV list /
// stats / job progress each tick (signature-gated so an in-progress strip
// slider or range edit isn't clobbered), and the engine panel is rebuilt by
// main on engine state changes (rebuildEngine).

import { tpl, pick } from "../../templates.js";
import { postJson, putJson, del } from "../../api.js";
import { fmtBytes, fmtDur, fmtMs, fmtClock, truncMid } from "../../formatters.js";
import { header, strong, inline } from "../shell.js";

/** Strip-silence knob defaults — mirror STRIP_OPT_DEFAULTS / the server-side
 * fallbacks in api_session_strip_silence (tapscribe/app.py). */
const STRIP_DEFAULTS = Object.freeze({ min_silence_ms: 500, pad_ms: 200, speech_floor_db: -45 });

/** @typedef {{ min_silence_ms: number, pad_ms: number, speech_floor_db: number }} StripKnobs */

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
  const frag = tpl("tpl-next-view-recordings");

  const headHost = pick(frag, "head");
  const engineHost = pick(frag, "engineHost");
  const waveName = pick(frag, "waveName");
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
  const txSelLabel = pick(frag, "txSelLabel");
  const txOneBtn = /** @type {HTMLButtonElement} */ (pick(frag, "txOneBtn"));
  const rangeFrom = /** @type {HTMLInputElement} */ (pick(frag, "rangeFrom"));
  const rangeTo = /** @type {HTMLInputElement} */ (pick(frag, "rangeTo"));
  const forceBox = /** @type {HTMLInputElement} */ (pick(frag, "forceBox"));
  const txRangeBtn = /** @type {HTMLButtonElement} */ (pick(frag, "txRangeBtn"));
  const txNote = pick(frag, "txNote");
  const cacheHint = pick(frag, "cacheHint");
  const cacheBody = pick(frag, "cacheBody");

  rebuildEngine(engineHost);

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
  /** Selected original WAV name, per session id (drives the cache panel). */
  /** @type {Map<string, string>} */
  const selectedWav = new Map();
  /** Last strip-silence response stats, per session id (overlay on s.stripped). */
  /** @type {Map<string, import('../../types.js').StripSilenceResult>} */
  const lastStrip = new Map();
  /** Sessions with a strip POST in flight (the job snapshot also flags this). */
  /** @type {Set<string>} */
  const stripInflight = new Set();
  /** wavKey ("session/name[@stripped]") currently transcribing optimistically. */
  /** @type {Set<string>} */
  const txInflight = new Set();
  let lastSig = " "; // sentinel so the first update always renders the body

  // ---- Helpers --------------------------------------------------------------

  /** @param {string} sid @returns {"original" | "stripped"} */
  const effectiveSource = (sid) => {
    const s = latest?.sessions?.find((x) => x.session === sid) || null;
    const want = sourcePick.get(sid) || "original";
    return (want === "stripped" && !s?.stripped) ? "original" : want;
  };

  /** Resolve the selected original WAV for the focused session (first if unset). */
  const selectedFor = () => {
    if (!session) return null;
    const files = session.files || [];
    if (!files.length) return null;
    const want = selectedWav.get(session.session);
    return files.find((f) => f.name === want) ?? files[0] ?? null;
  };

  // ---- Knobs ----------------------------------------------------------------

  /** @param {keyof StripKnobs} key */
  const knobUnit = (key) => (key === "speech_floor_db" ? "dB" : "ms");
  /** @param {keyof StripKnobs} key */
  const paintKnob = (key) => { knobVals[key].textContent = `${knobs[key]} ${knobUnit(key)}`; };

  for (const inp of /** @type {NodeListOf<HTMLInputElement>} */ (frag.querySelectorAll("[data-strip-knob]"))) {
    const key = /** @type {keyof StripKnobs} */ (inp.dataset.stripKnob);
    inp.value = String(knobs[key]);
    paintKnob(key);
    inp.addEventListener("input", () => {
      const n = Number(inp.value);
      if (Number.isFinite(n)) { knobs[key] = n; paintKnob(key); }
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
      // Flip to the cleaned audio on success so the operator can act on it.
      if ((res.files_written || 0) > 0) sourcePick.set(sid, "stripped");
    } catch (e) {
      alert(`Strip silence failed: ${String(e).replace(/^Error:\s*/, "")}`);
    } finally {
      stripInflight.delete(sid);
      stripBtn.disabled = false;
      lastSig = " "; // force a body re-render with the new stripped clips
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
    if (sourcePick.get(sid) === "stripped") sourcePick.delete(sid);
    lastSig = " ";
    afterMutate();
  });

  // ---- Transcribe (REAL) ----------------------------------------------------

  /** Read the Canary source/target lang from the engine panel's selects. */
  const langValues = () => {
    /** @param {string} name */
    const valOf = (name) => /** @type {HTMLSelectElement | null} */ (
      engineHost.querySelector(`select[data-input-name="${name}"]`))?.value || "";
    return { source_lang: valOf("source_lang"), target_lang: valOf("target_lang") };
  };

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
    if (sel) transcribeWav(sel.name, effectiveSource(session?.session || ""));
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
        source: effectiveSource(sid),
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

  // ---- Set primary (REAL) ---------------------------------------------------

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

  // ---- WAV list -------------------------------------------------------------

  /** @param {import('../../types.js').WavFile} f @param {"original"|"stripped"} src @param {boolean} selected */
  const wavRow = (f, src, selected) => {
    const out = document.createDocumentFragment();
    const node = tpl("tpl-next-wavrow");
    const btn = /** @type {HTMLButtonElement} */ (node.firstElementChild);
    if (selected) btn.classList.add("is-sel");
    pick(node, "name").textContent = truncMid(f.name, 40);
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
    out.appendChild(node);

    // Indented stripped region clips (only when the stripped source is active).
    if (src === "stripped") {
      for (const r of (f.regions || [])) {
        const clip = tpl("tpl-next-wavclip");
        pick(clip, "name").textContent = `↳ ${truncMid(r.name, 36)}`;
        const span = r.wav_start && r.wav_end
          ? `${fmtClock(r.wav_start)}–${fmtClock(r.wav_end)}`
          : "stripped region";
        pick(clip, "sub").textContent = `${span} · ${fmtBytes(r.size)}`;
        pick(clip, "dur").textContent = fmtDur(r.duration_s);
        const cbtn = /** @type {HTMLButtonElement} */ (pick(clip, "clipTx"));
        const cInflight = txInflight.has(`${session?.session || ""}/${r.name}@stripped`);
        if (cInflight) { cbtn.textContent = "⟳"; cbtn.disabled = true; }
        else cbtn.textContent = r.transcript ? "re-tx" : "transcribe";
        cbtn.addEventListener("click", () => transcribeWav(r.name, "stripped"));
        out.appendChild(clip);
      }
    }
    return out;
  };

  // ---- Transcript cache -----------------------------------------------------

  /** @param {import('../../types.js').WavFile | null} sel @param {"original"|"stripped"} src */
  const renderCache = (sel, src) => {
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
      else pbtn.addEventListener("click", () => setPrimary(sel.name, v.backend, v.model, /** @type {"original"|"stripped"} */ (v.source || src)));
      cacheBody.appendChild(row);
    }
  };

  // ---- Source toggle (header actions) ---------------------------------------

  /** @param {"original"|"stripped"} active @param {boolean} hasStripped */
  const buildSrcSw = (active, hasStripped) => {
    const sw = tpl("tpl-next-srcsw");
    for (const b of /** @type {NodeListOf<HTMLButtonElement>} */ (sw.querySelectorAll("[data-src]"))) {
      const which = /** @type {"original"|"stripped"} */ (b.dataset.src);
      if (which === active) b.classList.add("is-on");
      if (which === "stripped" && !hasStripped) {
        b.disabled = true;
        b.title = "no stripped/ folder — run strip silence first";
      }
      b.addEventListener("click", () => {
        if (b.disabled || !session) return;
        sourcePick.set(session.session, which);
        lastSig = " ";
        afterMutate();
      });
    }
    return sw;
  };

  // ---- Per-tick update ------------------------------------------------------

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    latest = j;
    session = sess;
    const sid = sess?.session || "";
    const src = effectiveSource(sid);
    const files = sess?.files || [];
    const stripped = sess?.stripped || null;
    const sel = selectedFor();

    // Signature gate — only rebuild the DOM-heavy body when something the body
    // depends on actually changed. Skips while a strip slider / range box is
    // focused so an edit-in-progress isn't wiped. (The knob value labels are
    // updated by their own input listeners, not here.)
    const job = sess?.progress || null;
    const txSig = files.map((f) => `${f.name}:${f.transcript?.transcribed_at || ""}:${(f.transcripts || []).length}:${(f.regions || []).length}`).join("|");
    const sig = [
      sid, src, sel?.name || "",
      stripped ? `${stripped.count}:${stripped.stripped_at}` : "",
      job ? `${job.kind}:${job.current}/${job.total}:${job.current_file || ""}` : "",
      stripInflight.has(sid) ? "S" : "",
      [...txInflight].filter((k) => k.startsWith(`${sid}/`)).sort().join(","),
      lastStrip.has(sid) ? JSON.stringify(lastStrip.get(sid)) : "",
      txSig,
    ].join("§");
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    const editing = !!focused && (focused.dataset?.stripKnob != null || focused === rangeFrom || focused === rangeTo);
    // Skip the DOM-heavy rebuild when nothing the body depends on changed, or
    // while a knob / range box is mid-edit. Everything Recordings shows is
    // captured in the signature (job progress included), so there's no
    // live-only chrome to repaint on the skip path.
    if ((sig === lastSig || editing) && sess) return;
    lastSig = sig;

    // Header
    header(headHost, {
      eyebrow: "Session · 2 Recordings",
      title: "Recordings",
      sub: sess
        ? inline(`${files.length} WAV${files.length === 1 ? "" : "s"} in `, strong(metaFor(sess).label || sess.session), " · strip silence, then transcribe")
        : "no session selected — pick one from the spine",
      actions: sess && files.length ? buildSrcSw(src, !!stripped) : undefined,
    });

    if (!sess || !files.length) {
      waveName.textContent = sess ? "no WAVs recorded yet" : "no session selected";
      for (const v of Object.values(stats)) v.textContent = "—";
      wavHint.textContent = "0 files";
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = sess
        ? "No recordings yet. Once taps record into this session, each WAV appears here."
        : "Pick a session from the spine to manage its recordings.";
      wavList.replaceChildren(empty);
      txSelLabel.textContent = "Selected WAV";
      txOneBtn.disabled = true;
      txOneBtn.textContent = "transcribe";
      txNote.textContent = sess ? "record into this session to enable transcription" : "";
      renderCache(null, src);
      stripBtn.disabled = !sess;
      clearBtn.disabled = !stripped;
      jobBar.hidden = true;
      return;
    }

    // Waveform header (stub) name + stats. Prefer the last strip-silence
    // response; fall back to the on-disk stripped summary; else placeholders.
    waveName.textContent = sel
      ? `🌊 ${truncMid(sel.name, 40)} · ${fmtDur(sel.duration_s)} · ${src}`
      : "no WAV selected";
    const ls = lastStrip.get(sid);
    if (ls) {
      const kept = ls.in_seconds > 0 ? Math.round(100 * ls.speech_seconds / ls.in_seconds) : 0;
      stats.clips.textContent = String(ls.files_written ?? 0);
      stats.speech.textContent = `${Math.round(ls.speech_seconds)}s`;
      stats.in.textContent = `${Math.round(ls.in_seconds)}s`;
      stats.kept.textContent = `${kept}%`;
    } else if (stripped) {
      stats.clips.textContent = String(stripped.count);
      stats.speech.textContent = `${Math.round(stripped.speech_seconds)}s`;
      stats.in.textContent = "—";
      stats.kept.textContent = "—";
    } else {
      stats.clips.textContent = "—";
      stats.speech.textContent = "—";
      stats.in.textContent = "—";
      stats.kept.textContent = "—";
    }

    // Strip + clear button states (busy reflects the job snapshot too).
    const stripBusy = stripInflight.has(sid) || job?.kind === "strip";
    stripBtn.disabled = stripBusy;
    stripBtn.textContent = stripBusy ? "⟳ stripping…" : "✂ strip";
    clearBtn.disabled = !stripped || stripBusy;

    // Job progress bar (one job per session — transcribe OR strip).
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

    // WAV list
    wavHint.textContent = `${files.length} original${files.length === 1 ? "" : "s"}`;
    const listFrag = document.createDocumentFragment();
    for (const f of files) listFrag.appendChild(wavRow(f, src, f.name === sel?.name));
    wavList.replaceChildren(listFrag);

    // Transcribe panel
    txSelLabel.textContent = sel ? `Selected: ${truncMid(sel.name, 24)}` : "Selected WAV";
    const oneBusy = !!sel && txInflight.has(`${sid}/${sel.name}${src === "stripped" ? "@stripped" : ""}`);
    txOneBtn.disabled = !sel || oneBusy;
    txOneBtn.textContent = oneBusy ? "⟳ transcribing" : (sel?.transcript ? "re-transcribe" : "transcribe");
    if (!rangeFrom.value) rangeFrom.placeholder = sess.earliest_iso || "ISO";
    if (!rangeTo.value) rangeTo.placeholder = sess.latest_iso || "ISO";
    txNote.textContent = `source: ${src}${stripped ? "" : " · (run strip to enable stripped)"}`;

    // Transcript cache for the selected WAV
    renderCache(sel, src);
  };

  return { node: frag, update, rebuildEngine: () => rebuildEngine(engineHost) };
}
