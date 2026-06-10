// @ts-check
// Stages · Summary (SESSION stage 4) — the post-transcription summarizer.
//
// WIRED for the Local + Command sources. Local (#86) is the bundled, offline,
// hardware-routed model and the DEFAULT source here (view-local default until
// #84 makes it operator-configurable) — it carries a model picker, populated
// once from the hardware-routed catalog (GET /api/summarize/models), so the
// operator can A/B local models. Command (#82) takes a CLI template (e.g.
// `claude -p`). The operator picks a source,
// edits the prompt, and clicks Generate; we POST to
// /api/sessions/{session}/summarize, which summarizes the session's merged
// transcript and returns the result. We render the summary + the source/model
// (or command) that produced it, surface errors, and drive the shared
// job-progress bar while the job runs. The API source (#85) is present but
// disabled until its slice lands.
//
// Persistence (#83): a generated summary is stored server-side next to the
// merged transcript; the session row carries a slim `session_summary` marker
// and this view lazily fetches the body (cached per (session, summarized_at))
// so a stored summary re-renders on revisit/reload without re-generating. No
// saved config yet (#84): the source/command/prompt are entered here and sent
// per Generate.
//
// Interaction hold: the source buttons, command <input>, and prompt <textarea>
// are built ONCE from the template and NEVER rebuilt per-tick (update() only
// mutates the button/note/job-bar/header + the output pane; the source selector
// is click-driven), so a background poll can't clobber a mid-edit prompt — the
// dashboard interaction-hold sweep covers them. The output pane re-render is
// selection-guarded (the summary is a copy target, like the merged-transcript
// pane).

import { tpl, pick, renderRegion, markRegionStale } from "../../templates.js";
import { getJson, postJson, fetchSessionSummary, peekSessionSummary } from "../../api.js";
import { header, strong, inline, renderJobBar } from "../shell.js";

/**
 * @param {{
 *   metaFor: (s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { metaFor, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-summary");

  const headHost = pick(frag, "head");
  const sumOut = pick(frag, "sumOut");
  const sumOutHint = pick(frag, "sumOutHint");
  const cmdInput = /** @type {HTMLInputElement} */ (pick(frag, "sumCmd"));
  const promptTa = /** @type {HTMLTextAreaElement} */ (pick(frag, "sumPrompt"));
  const genBtn = /** @type {HTMLButtonElement} */ (pick(frag, "sumGenerate"));
  const sumNote = pick(frag, "sumNote");
  const jobBar = pick(frag, "jobBar");
  const jobLabel = pick(frag, "jobLabel");
  const jobCount = pick(frag, "jobCount");
  const jobFill = /** @type {HTMLElement} */ (pick(frag, "jobFill"));
  const jobWav = pick(frag, "jobWav");

  // Source selector (segmented) + the per-source detail panes. The buttons and
  // both panes are built ONCE from the template; a click switches `source` and
  // toggles which pane shows — never a per-tick rebuild, so the interaction
  // hold holds (the command <input> / prompt <textarea> live inside, untouched).
  const srcButtons = /** @type {NodeListOf<HTMLButtonElement>} */ (
    frag.querySelectorAll(".segctl--wide [data-src]")
  );
  const srcLocal = /** @type {HTMLElement} */ (pick(frag, "srcLocal"));
  const srcCommand = /** @type {HTMLElement} */ (pick(frag, "srcCommand"));

  // Local model picker. The <select> is populated ONCE from the hardware-routed
  // catalog (GET /api/summarize/models) and never rebuilt per-tick, so the
  // interaction hold holds (same discipline as the source buttons / inputs).
  const modelSel = /** @type {HTMLSelectElement} */ (pick(frag, "sumModel"));
  const modelNote = pick(frag, "sumModelNote");
  const maxTokInput = /** @type {HTMLInputElement} */ (pick(frag, "sumMaxTokens"));

  // ---- View-local state -----------------------------------------------------
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  /** The summary currently shown (null until a Generate lands). Either the
   * just-generated POST result or the lazily-fetched stored body. */
  /** @type {import('../../types.js').PersistedSummary | null} */
  let lastSummary = null;
  /** The session id `lastSummary` belongs to — so switching sessions clears a
   * summary that isn't this session's. */
  let summarySession = "";
  /** True while a Generate POST is in flight (the button reflects it). */
  let generating = false;
  /** Sticky error message from the last failed Generate — shown in the note
   * until the next Generate clears it. */
  let errorMsg = "";
  /** The selected summarizer source. Local (bundled, offline) is the default
   * for this view — view-local until #84 makes the default operator-configurable.
   * Command is also wired (#82); API (#85) is present but disabled. */
  let source = "local";
  /** The selected Local model repo id. Empty until the catalog loads (the
   * server then falls back to its default); once loaded, mirrors the <select>.
   * Sent in the Generate body ONLY for the local source. */
  let model = "";
  /** The loaded catalog rows, kept so the note can show the picked model's
   * footprint/context. */
  /** @type {import('../../types.js').SummaryModel[]} */
  let models = [];

  // Split render gates: the header and the controls each update independently so
  // an idle tick rebuilds nothing. The output pane has no closure sig — it
  // renders through `renderRegion(sumOut, …, {sig})`, which owns its own gate.
  let lastHeadSig = " ";
  let lastCtlSig = " ";

  // ---- Helpers --------------------------------------------------------------

  const hasTranscript = () => !!session?.session_transcript;

  /** In-flight lazy summary fetches, keyed `${sid}@${stamp}` (dedup). */
  const sumPending = new Set();

  /**
   * Resolve the persisted summary behind the session's slim marker: the cached
   * body when already in hand, else fire ONE lazy fetch and re-cross the
   * output gate when it lands (the resolveMerged pattern from transcript.js).
   * Returns null until loaded — the placeholder shows meanwhile.
   * @param {import('../../types.js').SummaryMarker | null | undefined} marker
   * @param {string} sid
   * @returns {import('../../types.js').PersistedSummary | null}
   */
  const resolveStored = (marker, sid) => {
    if (!marker || !marker.summarized_at || !sid) return null;
    const stamp = marker.summarized_at;
    const cached = peekSessionSummary(sid, stamp);
    if (cached !== undefined) return cached;
    const key = `${sid}@${stamp}`;
    if (!sumPending.has(key)) {
      sumPending.add(key);
      fetchSessionSummary(sid, stamp)
        .catch(() => {})
        .finally(() => {
          sumPending.delete(key);
          markRegionStale(sumOut);
          afterMutate();
        });
    }
    return null;
  };

  /** Sync the Generate button + the note line from current state. Never touches
   * the command/prompt inputs (the operator owns those). */
  const reflectControls = () => {
    genBtn.disabled = !session || generating;
    genBtn.textContent = generating ? "⟳ Generating…" : "🪄 Generate summary";
    if (errorMsg) {
      sumNote.textContent = errorMsg;
      sumNote.classList.add("is-err");
      return;
    }
    sumNote.classList.remove("is-err");
    sumNote.textContent = generating
      ? ""
      : !session
        ? "pick a session from the spine first"
        : !hasTranscript()
          ? "transcribe this session first — Generate needs a merged transcript"
          : "";
  };

  /**
   * @param {import('../../types.js').PersistedSummary} res
   * @param {boolean} [stale] true when the summary predates the current transcript (#94)
   */
  const renderSummary = (res, stale = false) => {
    const out = document.createElement("div");
    out.className = "sumout";
    if (stale) {
      const warn = document.createElement("div");
      warn.className = "sumout__stale";
      warn.textContent =
        "This summary predates the current transcript — regenerate to refresh it.";
      out.append(warn);
    }
    const title = document.createElement("div");
    title.className = "sumout__title";
    title.textContent = "Summary";
    const body = document.createElement("div");
    body.className = "sumtext";
    body.textContent = res.summary || "";
    out.append(title, body);
    // Show what produced it: the model (local/api) if present, else the command
    // template (command source), else just the source name. Side-effect inside
    // the build closure so it only fires on a real render (renderRegion gate).
    sumOutHint.textContent = res.model
      ? `${res.source} · ${res.model}`
      : res.command
        ? `${res.source} · ${res.command}`
        : res.source;
    return out;
  };

  /** @param {import('../../types.js').Session | null} sess */
  const renderPlaceholder = (sess) => {
    const empty = document.createElement("div");
    empty.className = "empty";
    const h = document.createElement("div");
    h.className = "empty__h";
    h.textContent = sess ? "No summary yet" : "No session selected";
    const d = document.createElement("div");
    d.textContent = sess
      ? hasTranscript()
        ? "Edit the prompt and click Generate to summarize this session's merged transcript."
        : "Transcribe this session first, then Generate a summary from its merged transcript."
      : "Pick a session from the spine to summarize it.";
    empty.append(h, d);
    sumOutHint.textContent = "";
    return empty;
  };

  // ---- Generate (REAL) ------------------------------------------------------
  // Bound ONCE at build time; reads the live input values at click time.
  genBtn.addEventListener("click", async () => {
    if (!session || generating) return;
    const sid = session.session;
    errorMsg = "";
    generating = true;
    lastCtlSig = " "; // re-sync the button immediately, don't wait for a poll
    reflectControls();
    try {
      // The Local source carries the picked model + output cap (empty/NaN →
      // server defaults); the Command source carries a CLI template instead.
      /** @type {{ source: string, prompt: string, command?: string, model?: string, max_tokens?: number }} */
      const body = { source, prompt: promptTa.value };
      if (source === "command") body.command = cmdInput.value.trim();
      if (source === "local") {
        if (model) body.model = model;
        const mt = parseInt(maxTokInput.value, 10);
        if (Number.isFinite(mt)) body.max_tokens = mt;
      }
      const res = /** @type {import('../../types.js').SummaryResult} */ (
        await postJson(`/api/sessions/${encodeURIComponent(sid)}/summarize`, body)
      );
      lastSummary = res;
      summarySession = sid;
      markRegionStale(sumOut); // force the output pane to re-render with the new summary
    } catch (e) {
      errorMsg = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
    } finally {
      generating = false;
      lastCtlSig = " ";
      reflectControls();
      // Re-poll for the authoritative state (the job slot is freed server-side).
      // afterMutate() re-renders synchronously, so a fresh summary lands via the
      // output-pane renderRegion (marked stale on success) — which also honours
      // the copy-selection guard a direct render here would bypass.
      afterMutate();
    }
  });

  // ---- Source selector (REAL) -----------------------------------------------
  // Bound ONCE at build time. Switching source toggles which detail pane shows;
  // the command/prompt inputs inside are never rebuilt (interaction hold).

  /** Reflect the selected `source` onto the segmented buttons + detail panes.
   * Pure view sync, no fetch — called on a click and once at build. */
  const applySource = () => {
    for (const b of srcButtons) {
      const on = b.dataset.src === source;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
    srcLocal.hidden = source !== "local";
    srcCommand.hidden = source !== "command";
  };

  for (const b of srcButtons) {
    b.addEventListener("click", () => {
      const next = b.dataset.src;
      if (b.disabled || generating || !next || next === source) return;
      source = next;
      applySource();
    });
  }
  applySource(); // seed the default (Local) selection + pane visibility

  // ---- Local model picker (REAL) --------------------------------------------
  // Populate the <select> ONCE from the hardware-routed catalog, then leave it
  // alone — a click on an option mutates `model`, never a per-tick rebuild, so
  // the interaction hold holds (the dashboard sweep covers it).

  /** @param {number} t */
  const ctxLabel = (t) => (t >= 1000 ? `${Math.round(t / 1000)}K` : `${t}`);

  /** Show the picked model's footprint + context + note under the dropdown. */
  const reflectModelNote = () => {
    const m = models.find((x) => x.repo_id === model);
    modelNote.textContent = m
      ? `≈${m.approx_gb} GB · ${ctxLabel(m.context_tokens)} ctx${m.note ? ` · ${m.note}` : ""}`
      : "first Generate downloads the model, then it runs fully offline";
  };

  modelSel.addEventListener("change", () => {
    model = modelSel.value;
    reflectModelNote();
  });

  (async () => {
    try {
      const cat = /** @type {import('../../types.js').SummaryModelCatalog} */ (
        await getJson("/api/summarize/models")
      );
      models = cat.models || [];
      modelSel.replaceChildren();
      for (const m of models) modelSel.add(new Option(m.label || m.repo_id, m.repo_id, m.is_default, m.is_default));
      if (!models.length) {
        modelSel.add(new Option("no local models", "", true, true));
        modelSel.disabled = true;
      }
      model = modelSel.value;
      reflectModelNote();
      // Seed the output-cap input's bounds + default from the server (one source
      // of truth — the HTML value is only a placeholder until this lands, which
      // is once at mount, before the operator can edit).
      if (typeof cat.max_tokens_min === "number") maxTokInput.min = String(cat.max_tokens_min);
      if (typeof cat.max_tokens_max === "number") maxTokInput.max = String(cat.max_tokens_max);
      if (typeof cat.max_tokens_default === "number") maxTokInput.value = String(cat.max_tokens_default);
    } catch {
      // Best-effort: if the catalog fetch fails, leave the dropdown disabled and
      // let the server fall back to its default model (an empty `model` in the
      // Generate body), rather than blocking the operator.
      modelSel.add(new Option("model list unavailable — using default", "", true, true));
      modelSel.disabled = true;
    }
  })();

  // ---- Per-tick update ------------------------------------------------------

  /**
   * @param {import('../../types.js').AppState} _j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (_j, sess) => {
    session = sess;
    const sid = sess?.session || "";
    const job = sess?.progress || null;

    // ---- Job progress — in-place writes on prebuilt nodes, EVERY tick
    // (deliberately outside the signature gates, same as transcript.js). Scoped
    // to a summarize job so a transcribe/strip on the same session doesn't show
    // in the Summarizer panel.
    renderJobBar({ jobBar, jobLabel, jobCount, jobFill, jobWav }, job, { only: "summarize" });

    // ---- Header — gated on session + has-transcript.
    const headSig = [sid, hasTranscript() ? 1 : 0].join("§");
    if (headSig !== lastHeadSig) {
      lastHeadSig = headSig;
      header(headHost, {
        eyebrow: "Session · 4 Summary",
        title: "Summary",
        sub: sess
          ? inline("summarize ", strong(metaFor(sess).label || sess.session))
          : "no session selected — pick one from the spine",
      });
    }

    // ---- Session switch — drop a summary/error that belonged to another
    // session, and force the output + controls to re-sync.
    if (sid !== summarySession) {
      lastSummary = null;
      summarySession = sid;
      errorMsg = "";
      markRegionStale(sumOut);
      lastCtlSig = " ";
    }

    // ---- Output pane — rendered through renderRegion: it gates on outSig AND
    // defers (without advancing) while a text selection is active inside it, so
    // a tick can't dissolve a mid-copy selection. The hint side-effect lives in
    // the build closures, so it only fires on a real render.
    // A persisted summary (slim marker on the session) is resolved lazily — the
    // cached body when in hand, else null + a one-shot fetch that marks the pane
    // stale when it lands (resolveStored).
    const marker = sess?.session_summary || null;
    const shown = lastSummary || resolveStored(marker, sid);
    // Staleness (#94): the summary carries the `transcribed_at` of the transcript
    // it was built from; if the session was re-transcribed since, the live
    // transcript marker's stamp is newer. Prefer the resolved body's stamp, fall
    // back to the slim marker's. ISO 8601 from one source → lexical compare is
    // chronological. `stale` joins outSig so a re-transcribe re-renders the pane.
    const curStamp = sess?.session_transcript?.transcribed_at || "";
    const sumStamp = shown?.transcribed_at || marker?.transcribed_at || "";
    const stale = !!(curStamp && sumStamp && sumStamp < curStamp);
    const outSig = [
      sid,
      lastSummary?.created_at || "",
      marker?.summarized_at || "",
      shown ? 1 : 0,
      hasTranscript() ? 1 : 0,
      stale ? 1 : 0,
    ].join("§");
    renderRegion(sumOut, () => (shown ? renderSummary(shown, stale) : renderPlaceholder(sess)), {
      sig: outSig,
    });

    // ---- Controls (button + note) — gated. The command/prompt inputs are NOT
    // rebuilt here (interaction hold).
    const ctlSig = [sid, hasTranscript() ? 1 : 0, generating ? 1 : 0, errorMsg ? 1 : 0].join("§");
    if (ctlSig !== lastCtlSig) {
      lastCtlSig = ctlSig;
      reflectControls();
    }
  };

  return { node: frag, update };
}
