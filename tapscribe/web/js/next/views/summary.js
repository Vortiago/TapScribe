// @ts-check
// Stages · Summary (SESSION stage 4) — the post-transcription summarizer.
//
// WIRED for the Command source (#82): the operator picks a CLI template (e.g.
// `claude -p`), edits the prompt, and clicks Generate; we POST to
// /api/sessions/{session}/summarize, which pipes the session's merged
// transcript to the command on stdin and returns the summary from stdout. We
// render the summary + the source/command that produced it, surface errors,
// and drive the shared job-progress bar while the job runs. The Local + API
// sources are present but disabled until their slices land (#86 / #85).
//
// No persistence yet (#83): the summary lives in view-local state and is lost
// on reload / cleared when the operator switches sessions. No saved config yet
// (#84): the source/command/prompt are entered here and sent per Generate.
//
// Interaction hold: the command <input> and prompt <textarea> are built ONCE
// from the template and NEVER rebuilt per-tick (update() only mutates the
// button/note/job-bar/header + the output pane), so a background poll can't
// clobber a mid-edit prompt — the dashboard interaction-hold sweep covers them.
// The output pane re-render is selection-guarded (the summary is a copy target,
// like the merged-transcript pane).

import { tpl, pick, selectionInside } from "../../templates.js";
import { postJson } from "../../api.js";
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

  // ---- View-local state -----------------------------------------------------
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  /** The summary currently shown (null until a Generate lands). Lives only in
   * memory — no persistence in this slice. */
  /** @type {import('../../types.js').SummaryResult | null} */
  let lastSummary = null;
  /** The session id `lastSummary` belongs to — so switching sessions clears a
   * summary that isn't this session's. */
  let summarySession = "";
  /** True while a Generate POST is in flight (the button reflects it). */
  let generating = false;
  /** Sticky error message from the last failed Generate — shown in the note
   * until the next Generate clears it. */
  let errorMsg = "";

  // Three deliberately-split signatures: the header, the output pane, and the
  // controls update independently so an idle tick rebuilds nothing.
  let lastHeadSig = " ";
  let lastOutSig = " ";
  let lastCtlSig = " ";

  // ---- Helpers --------------------------------------------------------------

  const hasTranscript = () => !!session?.session_transcript;

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

  /** @param {import('../../types.js').SummaryResult} res */
  const renderSummary = (res) => {
    const out = document.createElement("div");
    out.className = "sumout";
    const title = document.createElement("div");
    title.className = "sumout__title";
    title.textContent = "Summary";
    const body = document.createElement("div");
    body.className = "sumtext";
    body.textContent = res.summary || "";
    out.append(title, body);
    sumOut.replaceChildren(out);
    sumOutHint.textContent = res.command ? `${res.source} · ${res.command}` : res.source;
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
    sumOut.replaceChildren(empty);
    sumOutHint.textContent = "";
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
      const res = /** @type {import('../../types.js').SummaryResult} */ (
        await postJson(`/api/sessions/${encodeURIComponent(sid)}/summarize`, {
          source: "command",
          command: cmdInput.value.trim(),
          prompt: promptTa.value,
        })
      );
      lastSummary = res;
      summarySession = sid;
      lastOutSig = " "; // force the output pane to re-render with the new summary
    } catch (e) {
      errorMsg = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
    } finally {
      generating = false;
      lastCtlSig = " ";
      reflectControls();
      // Re-poll for the authoritative state (the job slot is freed server-side).
      // afterMutate() re-renders synchronously, so a fresh summary lands via the
      // output-pane gate (lastOutSig was reset on success) — which also honours
      // the copy-selection guard a direct render here would bypass.
      afterMutate();
    }
  });

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
      lastOutSig = " ";
      lastCtlSig = " ";
    }

    // ---- Output pane — gated, and deferred (without advancing the gate) while
    // a text selection is active inside it, so a tick can't dissolve a
    // mid-copy selection (the merged-transcript pane's rule, for the summary).
    const outSig = [sid, lastSummary?.created_at || "", hasTranscript() ? 1 : 0].join("§");
    if (outSig !== lastOutSig && !selectionInside(sumOut)) {
      lastOutSig = outSig;
      if (lastSummary) renderSummary(lastSummary);
      else renderPlaceholder(sess);
    }

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
