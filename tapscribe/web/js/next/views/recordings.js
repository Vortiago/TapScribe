// @ts-check
// Stages · Recordings (SESSION stage 2). The per-session AUDIO-FILES stage: a
// waveform-cut PLACEHOLDER (net-new, no backend) sitting above the REAL
// strip-silence knobs + the per-WAV list (originals + indented stripped
// region clips) with an original/stripped source toggle. Transcription (the
// engine selector, transcribe controls, and per-WAV cache) moved to the
// Transcript stage — Recordings is files + silence-stripping only.
//
// Mirrors session-detail.js's data flow (the classic dashboard) for the strip
// pieces but is FRESH /next code — it builds the WAV list / strip controls
// from /api/state and the same endpoints (POST
// /api/sessions/{s}/strip-silence, DELETE /api/sessions/{s}/stripped). No mock
// data — the only stub is the waveform canvas, tagged inline.
//
// Built once for the page; `update(j, session)` re-renders the WAV list /
// stats / strip-job progress each tick (signature-gated so an in-progress
// strip slider isn't clobbered).

import { tpl, pick } from "../../templates.js";
import { postJson, del } from "../../api.js";
import { fmtBytes, fmtDur, fmtClock, truncMid } from "../../formatters.js";
import { header, strong, inline } from "../shell.js";

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
  /** Last strip-silence response stats, per session id (overlay on s.stripped). */
  /** @type {Map<string, import('../../types.js').StripSilenceResult>} */
  const lastStrip = new Map();
  /** Sessions with a strip POST in flight (the job snapshot also flags this). */
  /** @type {Set<string>} */
  const stripInflight = new Set();
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
    if (f.transcript) { tag.textContent = "✓ tx"; tag.className = "wavrow__tx is-done"; }
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
        const ctag = pick(clip, "txTag");
        if (r.transcript) { ctag.textContent = "✓ tx"; ctag.className = "wavrow__tx is-done"; }
        else { ctag.textContent = "no tx"; ctag.className = "wavrow__tx is-none"; }
        out.appendChild(clip);
      }
    }
    return out;
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
    // depends on actually changed. Skips while a strip slider is focused so an
    // edit-in-progress isn't wiped. (The knob value labels are updated by their
    // own input listeners, not here.)
    const job = sess?.progress || null;
    const txSig = files.map((f) => `${f.name}:${!!f.transcript}:${(f.regions || []).length}`).join("|");
    const sig = [
      sid, src, sel?.name || "",
      stripped ? `${stripped.count}:${stripped.stripped_at}` : "",
      job ? `${job.kind}:${job.current}/${job.total}:${job.current_file || ""}` : "",
      stripInflight.has(sid) ? "S" : "",
      lastStrip.has(sid) ? JSON.stringify(lastStrip.get(sid)) : "",
      txSig,
    ].join("§");
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    const editing = !!focused && focused.dataset?.stripKnob != null;
    // Skip the DOM-heavy rebuild when nothing the body depends on changed, or
    // while a knob is mid-edit. Everything Recordings shows is captured in the
    // signature (strip-job progress included), so there's no live-only chrome
    // to repaint on the skip path.
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

    // Job progress bar (one job per session — surfaced here for strip; the
    // transcribe job is driven from the Transcript stage but shows here too).
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
  };

  return { node: frag, update };
}
