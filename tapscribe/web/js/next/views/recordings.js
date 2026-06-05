// @ts-check
// Stages · Recordings (SESSION stage 2). The per-session AUDIO-FILES stage: a
// real waveform (an isolated <canvas> drawn from server-computed peaks)
// sitting above the REAL strip-silence knobs + the per-WAV list (originals +
// indented stripped region clips) with an original/stripped source toggle.
// Transcription (the engine selector, transcribe controls, and per-WAV cache)
// moved to the Transcript stage — Recordings is files + silence-stripping only.
//
// Mirrors session-detail.js's data flow (the classic dashboard) for the strip
// pieces but is FRESH /next code — it builds the WAV list / strip controls
// from /api/state and the same endpoints (POST
// /api/sessions/{s}/strip-silence, DELETE /api/sessions/{s}/stripped). The
// waveform fetches peaks lazily from /api/wav/{s}/{name}/peaks; the cut
// overlay on top of it lands in a later slice.
//
// Built once for the page; `update(j, session)` re-renders the WAV list /
// stats / strip-job progress each tick (signature-gated so an in-progress
// strip slider isn't clobbered).

import { tpl, pick, selectionInside } from "../../templates.js";
import { postJson, del, fetchWavTranscript, peekWavTranscript, fetchWavePeaks, peekWavePeaks } from "../../api.js";
import { fmtBytes, fmtDur, fmtClock, fmtMs, truncMid } from "../../formatters.js";
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
  /** WAV rows whose inline transcript is EXPANDED. Keyed by the same
   * "session/name@source" shape the transcribe/delete dispatch uses so a clip
   * and its parent original never collide. Lives in view-local state + is
   * folded into the render signature so the expanded set survives poll ticks. */
  /** @type {Set<string>} */
  const expandedKeys = new Set();
  /** Expand keys we've already scheduled a re-render for after the lazy
   * fetchWavTranscript lands — dedupes repeated misses across ticks. */
  /** @type {Set<string>} */
  const txRerenderPending = new Set();
  /** Last strip-silence response stats, per session id (overlay on s.stripped). */
  /** @type {Map<string, import('../../types.js').StripSilenceResult>} */
  const lastStrip = new Map();
  /** Sessions with a strip POST in flight (the job snapshot also flags this). */
  /** @type {Set<string>} */
  const stripInflight = new Set();
  let lastSig = " "; // sentinel so the first update always renders the body
  // Waveform render state. `lastWaveSig` is the canvas's OWN small signature
  // (selected WAV · source · size · load-state) so a per-second strip/transcribe
  // job tick — which churns the body's signature — never rebuilds the O(bins)
  // canvas (render-signature hygiene). `pendingWave` dedupes the lazy peaks
  // fetch; `failedWave` remembers an unreadable WAV so it shows a message
  // instead of refetching every tick.
  let lastWaveSig = " ";
  /** @type {Set<string>} */
  const pendingWave = new Set();
  /** @type {Map<string, string>} */
  const failedWave = new Map();

  // ---- Helpers --------------------------------------------------------------

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

  /** Resolve the selected original WAV for the focused session (first if unset). */
  const selectedFor = () => {
    if (!session) return null;
    const files = session.files || [];
    if (!files.length) return null;
    const want = selectedWav.get(session.session);
    return files.find((f) => f.name === want) ?? files[0] ?? null;
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
      const wsig = `none:${session ? "nofiles" : "nosession"}`;
      if (wsig === lastWaveSig) return;
      lastWaveSig = wsig;
      waveform.showMessage(session ? "no WAVs recorded yet" : "no session selected");
      return;
    }
    const fileSig = String(sel.size);
    const key = `${sid}/${sel.name}@${src}@${fileSig}`;
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
              // Force one more render so the now-cached peaks (or the error)
              // get drawn; reset the wave sig too so the redraw isn't skipped.
              lastSig = " ";
              lastWaveSig = " ";
              afterMutate();
            });
        }
      }
    }
    const wsig = `${key}@${state}`;
    if (wsig === lastWaveSig) return;
    lastWaveSig = wsig;
    if (state === "ok" && data) waveform.showWaveform(data.peaks, data.duration_s);
    else if (state === "loading") waveform.showMessage("loading waveform…");
    else waveform.showMessage(message);
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

  // ---- Per-WAV transcript expand (inline, lazy) -----------------------------

  /** The expand key for a WAV row — same "session/name@source" shape as the
   * transcribe/delete dispatch so a clip and its parent never collide. */
  /** @param {string} sid @param {string} name @param {"original"|"stripped"} src */
  const expandKey = (sid, name, src) => `${sid}/${name}${src === "stripped" ? "@stripped" : ""}`;

  /** Resolve the OPEN row's FULL cached transcript from the lazy client cache.
   * /api/state ships only a slim marker; on a cache miss this fires the fetch
   * once (keyed on the marker's transcribed_at, so a re-transcribe re-fetches)
   * and re-renders via afterMutate when it lands. Returns undefined while the
   * fetch is in flight (→ show the loading placeholder), or the resolved body
   * (possibly null) once settled. */
  /**
   * @param {string} sid
   * @param {string} name
   * @param {"original"|"stripped"} src
   * @param {import('../../types.js').WavTranscriptMarker} marker
   * @returns {import('../../types.js').WavTranscript | null | undefined}
   */
  const resolveWavTx = (sid, name, src, marker) => {
    const stamp = marker.transcribed_at || "";
    const cached = peekWavTranscript(sid, name, src, stamp);
    if (cached !== undefined) return cached;
    const key = `${sid}/${name}@${src}@${stamp}`;
    if (!txRerenderPending.has(key)) {
      txRerenderPending.add(key);
      fetchWavTranscript(sid, name, src, stamp)
        .catch(() => { /* transient failure — the next poll re-attempts */ })
        .finally(() => { txRerenderPending.delete(key); lastSig = " "; afterMutate(); });
    }
    return undefined;
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
        const mins = Math.floor(ln.start / 60);
        const secs = Math.floor(ln.start % 60);
        pick(line, "ts").textContent = `[${mins}:${String(secs).padStart(2, "0")}]`;
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
    expandedKeys.delete(expandKey(sid, name, src));
    lastSig = " ";
    afterMutate();
  };

  // ---- WAV list -------------------------------------------------------------

  /** Wire one row's tx-tag, expand toggle, download link, and delete button —
   * shared by originals and clips. Appends the expand body after the row when
   * the row is in `expandedKeys`. */
  /**
   * @param {DocumentFragment} out
   * @param {DocumentFragment} node
   * @param {import('../../types.js').WavFile | import('../../types.js').WavRegion} f
   * @param {"original"|"stripped"} src
   * @param {boolean} isCurrent
   */
  const decorateRow = (out, node, f, src, isCurrent) => {
    const sid = session?.session || "";
    const key = expandKey(sid, f.name, src);
    const open = expandedKeys.has(key);

    // Stable per-row hooks (e2e + debugging): the WAV/clip name + source.
    const rowEl = /** @type {HTMLElement} */ (pick(node, "row"));
    rowEl.dataset.wav = f.name;
    rowEl.dataset.src = src;

    const tag = pick(node, "txTag");
    if (f.transcript) { tag.textContent = "✓ tx"; tag.className = "wavrow__tx is-done"; }
    else { tag.textContent = "no tx"; tag.className = "wavrow__tx is-none"; }

    // Expand toggle — only meaningful when a cached transcript exists.
    const expandBtn = /** @type {HTMLButtonElement} */ (node.querySelector("[data-wav-expand]"));
    expandBtn.textContent = open ? "▾ tx" : "tx";
    if (!f.transcript) {
      expandBtn.disabled = true;
      expandBtn.title = "no cached transcript — transcribe this WAV first";
    } else {
      expandBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (open) expandedKeys.delete(key); else expandedKeys.add(key);
        lastSig = " ";
        afterMutate();
      });
    }

    // Download — a plain anchor straight to the API (matches classic).
    const dl = /** @type {HTMLAnchorElement} */ (pick(node, "download"));
    const dlQs = src === "stripped" ? "?source=stripped" : "";
    dl.href = `/api/wav/${encodeURIComponent(sid)}/${encodeURIComponent(f.name)}${dlQs}`;

    // Delete — the backend refuses the current session (409), so hide it there.
    const delBtn = /** @type {HTMLButtonElement} */ (node.querySelector("[data-wav-delete]"));
    if (isCurrent) {
      delBtn.remove();
    } else {
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteWav(f.name, src);
      });
    }

    out.appendChild(node);

    if (open && f.transcript) {
      const full = resolveWavTx(sid, f.name, src, f.transcript);
      if (full) out.appendChild(buildExpand(full, f.speaker_name || ""));
      else if (full === undefined) out.appendChild(tpl("tpl-next-txloading"));
      // `full === null` → no transcript body on disk; nothing to show.
    }
  };

  /** @param {import('../../types.js').WavFile} f @param {"original"|"stripped"} src @param {boolean} selected @param {boolean} isCurrent */
  const wavRow = (f, src, selected, isCurrent) => {
    const out = document.createDocumentFragment();
    const node = tpl("tpl-next-wavrow");
    const row = /** @type {HTMLElement} */ (pick(node, "row"));
    if (selected) row.classList.add("is-sel");
    pick(node, "name").textContent = truncMid(f.name, 40);
    const who = f.speaker_name ? `${f.speaker_name} · ` : "";
    pick(node, "sub").textContent = `${who}${fmtBytes(f.size)}`;
    pick(node, "dur").textContent = fmtDur(f.duration_s);

    // Select the WAV (drives the waveform header) from the name/sub block.
    const selectEl = /** @type {HTMLElement} */ (node.querySelector("[data-wav-select]"));
    const select = () => {
      if (session) { selectedWav.set(session.session, f.name); lastSig = " "; afterMutate(); }
    };
    selectEl.addEventListener("click", select);
    selectEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); }
    });

    decorateRow(out, node, f, src, isCurrent);

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
        decorateRow(out, clip, r, "stripped", isCurrent);
      }
    }
    return out;
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
    // Include each WAV's transcribed_at so a re-transcribe (new stamp) re-renders
    // the row's tx-tag and busts an open expand. Regions feed the strip toggle.
    const txSig = files
      .map((f) => `${f.name}:${f.transcript?.transcribed_at || ""}:${(f.regions || []).length}`)
      .join("|");
    // Fold the open session's expanded set into the signature so toggling a row
    // open/closed re-renders AND the expanded set survives idle poll ticks (it's
    // part of what the body is gated on). The "loading… → loaded" transition is
    // handled separately: resolveWavTx resets lastSig in its .finally when the
    // lazy fetch lands, forcing one more render that swaps the placeholder for
    // the real lines.
    const expandedSig = [...expandedKeys].filter((k) => k.startsWith(`${sid}/`)).sort().join(",");
    const sig = [
      sid, src, sel?.name || "",
      stripped ? `${stripped.count}:${stripped.stripped_at}` : "",
      job ? `${job.kind}:${job.current}/${job.total}:${job.current_file || ""}` : "",
      stripInflight.has(sid) ? "S" : "",
      // lastStrip is NOT in the sig: both its mutations (set on a successful
      // strip, delete on clear) already reset lastSig=" " to force one render,
      // so stringifying the whole strip response every poll tick was pure waste.
      (j.current_session || "") === sid ? "CUR" : "",
      txSig,
      expandedSig,
    ].join("§");
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    // "editing" = any interaction state a rebuild would destroy: a strip knob
    // mid-drag, or a text selection in the WAV list (an expanded row's inline
    // transcript is a natural copy target, and a strip/transcribe job ticking
    // in the background changes the sig under it). Deferring without updating
    // lastSig means the rebuild lands on the first tick after release.
    const editing =
      (!!focused && focused.dataset?.stripKnob != null) || selectionInside(wavList);
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
      actions: sess && files.length ? buildSourceToggle({
        active: src,
        hasStripped: !!stripped,
        onPick: (which) => {
          if (!session) return;
          sourcePick.set(session.session, which);
          lastSig = " ";
          afterMutate();
        },
      }) : undefined,
    });

    if (!sess || !files.length) {
      waveName.textContent = sess ? "no WAVs recorded yet" : "no session selected";
      for (const v of Object.values(stats)) setStat(v, "—");
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
      drawWaveform(null, src);
      return;
    }

    // Waveform header (stub) name + stats. Prefer the last strip-silence
    // response; fall back to the on-disk stripped summary; else placeholders.
    waveName.textContent = sel
      ? `🌊 ${truncMid(sel.name, 40)} · ${fmtDur(sel.duration_s)} · ${src}`
      : "no WAV selected";
    drawWaveform(sel, src);
    const ls = lastStrip.get(sid);
    if (ls) {
      const kept = ls.in_seconds > 0 ? Math.round(100 * ls.speech_seconds / ls.in_seconds) : 0;
      setStat(stats.clips, String(ls.files_written ?? 0));
      setStat(stats.speech, `${Math.round(ls.speech_seconds)}s`);
      setStat(stats.in, `${Math.round(ls.in_seconds)}s`);
      setStat(stats.kept, `${kept}%`);
    } else if (stripped) {
      setStat(stats.clips, String(stripped.count));
      setStat(stats.speech, `${Math.round(stripped.speech_seconds)}s`);
      setStat(stats.in, "—");
      setStat(stats.kept, "—");
    } else {
      setStat(stats.clips, "—");
      setStat(stats.speech, "—");
      setStat(stats.in, "—");
      setStat(stats.kept, "—");
    }

    // Strip + clear button states (busy reflects the job snapshot too).
    const stripBusy = stripInflight.has(sid) || job?.kind === "strip";
    stripBtn.disabled = stripBusy;
    stripBtn.textContent = stripBusy ? "⟳ stripping…" : "✂ strip";
    clearBtn.disabled = !stripped || stripBusy;

    // Job progress bar (one job per session — surfaced here for strip; the
    // transcribe job is driven from the Transcript stage but shows here too).
    renderJobBar({ jobBar, jobLabel, jobCount, jobFill, jobWav }, job);

    // WAV list. Delete is refused on the current (recording) session by the
    // backend (409), so the row hides its delete button there — matching how
    // the classic per-WAV row dropped delete on the live session.
    const isCurrent = (j.current_session || "") === sid;
    wavHint.textContent = `${files.length} original${files.length === 1 ? "" : "s"}`;
    const listFrag = document.createDocumentFragment();
    for (const f of files) listFrag.appendChild(wavRow(f, src, f.name === sel?.name, isCurrent));
    wavList.replaceChildren(listFrag);
  };

  return { node: frag, update };
}
