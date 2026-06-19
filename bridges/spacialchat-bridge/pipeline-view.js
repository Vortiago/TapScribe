// @ts-check
// SpatialChat Bridge — pipeline-view.js (pure view-model mapper)
//
// Module ② of the bracketed-meeting flow: a single pure function that turns
// a raw `GET /api/tap/sessions/{session}/pipeline` poll response into the
// view-model the popup's meeting card renders directly. The analogue of the
// Windows tray Bridge's pure `StatusView` — no fetch, no DOM, no storage, no
// clock, so it is exhaustively unit-tested with plain inputs and no mocks.
//
// Loaded as a plain global into the popup page only (via a <script> tag in
// popup.html, after control-client.js). The content script doesn't render a
// card, so it doesn't need this module.
//
// The Recorder fixes the stage/status vocabulary and the mapper consumes it
// verbatim (see CONTEXT.md "End-of-meeting pipeline" + batch_pipeline.py):
//   - running: { state:"running", stage, status, current, total, current_file }
//       stage/status pairs strip/stripping, transcribe/transcribing,
//       summarize/summarizing. `current`/`total` count WAVs in transcribe.
//   - done:    { state:"done", summary:{ summary, source, model, … } }
//   - failed:  { state:"failed", stage, error, error_kind }
//       error_kind ∈ NoUsableWavs · NoMergedTranscript · SummarizerUnavailable
//                    · SummarizerFailed · InvalidRange.
//   - idle:    { state:"idle" }  (no pipeline record AND no persisted summary)

/**
 * A raw poll body from `GET /api/tap/sessions/{session}/pipeline`. Every field
 * is optional: which ones are present depends on `state` (see app.py
 * api_tap_pipeline_poll). The mapper reads them defensively.
 * @typedef {{
 *   ok?: boolean,
 *   state?: string,
 *   stage?: string,
 *   status?: string,
 *   current?: number,
 *   total?: number,
 *   current_file?: string,
 *   summary?: Record<string, unknown> | null,
 *   error?: string,
 *   error_kind?: string,
 *   session?: string,
 * }} PipelinePoll
 */

/**
 * The view-model the card renders. Every field is always present so the
 * renderer never has to feature-detect; irrelevant fields are null.
 * @typedef {{
 *   phase: string,
 *   progress: string | null,
 *   stage: string | null,
 *   currentFile: string | null,
 *   summary: Record<string, unknown> | null,
 *   summaryText: string | null,
 *   failureStage: string | null,
 *   failureReason: string | null,
 * }} PipelineView
 */

(function (/** @type {any} */ root) {
  "use strict";

  // stage → the live progress line shown while the pipeline runs. `current`/
  // `total` only carry useful counts during transcribe (one per WAV); strip
  // and summarize are single-shot. A running poll that has no `stage` yet
  // (the job snapshot hasn't attached in the instant after the trigger) gets
  // a generic line rather than a blank.
  /** @param {PipelinePoll | null | undefined} raw @returns {string} */
  function progressLabel(raw) {
    const stage = raw && raw.stage;
    const total = Number(raw && raw.total) || 0;
    const current = Number(raw && raw.current) || 0;
    if (stage === "strip") return "Stripping silence…";
    if (stage === "transcribe") {
      return total > 0 ? "Transcribing " + current + "/" + total + "…" : "Transcribing…";
    }
    if (stage === "summarize") return "Summarizing…";
    return "Processing…";
  }

  // error_kind → a human-readable, operator-free explanation. Each maps to a
  // domain error the pipeline can raise (session_merge / batch_summarize). A
  // Map (not a plain object) so an attacker-ish error_kind like "constructor"
  // or "toString" can't resolve to an inherited prototype member.
  /** @type {Map<string, string>} */
  const FAILURE_REASONS = new Map([
    ["NoUsableWavs", "No usable audio was captured — there was nothing to transcribe."],
    ["NoMergedTranscript", "Nothing was transcribed, so there was nothing to summarize."],
    ["SummarizerUnavailable", "The summarizer isn't configured on the recorder."],
    ["SummarizerFailed", "The summarizer failed while writing the notes."],
    ["InvalidRange", "The recorder rejected the session's audio range."],
  ]);

  // An unrecognised kind falls back to the raw `error` text, then a generic
  // line, so a future Recorder error never renders as a blank failure.
  /** @param {PipelinePoll | null | undefined} raw @returns {string} */
  function failureReason(raw) {
    const kind = raw && raw.error_kind;
    const known = typeof kind === "string" ? FAILURE_REASONS.get(kind) : undefined;
    if (known) return known;
    const err = raw && raw.error;
    if (typeof err === "string" && err) return err;
    return "The end-of-meeting pipeline failed.";
  }

  // The view-model shape the card renders. Every field is always present so
  // the renderer never has to feature-detect: irrelevant fields are null.
  /** @param {string} phase @param {Partial<PipelineView>} [extra] @returns {PipelineView} */
  function vm(phase, extra) {
    return Object.assign(
      {
        phase: phase,
        progress: null,       // running: the stage progress line
        stage: null,          // running/failed: the current/failing stage key
        currentFile: null,    // running: the WAV being transcribed, if any
        summary: null,        // done: the raw summary dict (model/source metadata)
        summaryText: null,    // done: the summary text the Copy button copies
        failureStage: null,   // failed: the failing stage
        failureReason: null,  // failed: the human-readable reason
      },
      extra || {},
    );
  }

  // Map a raw poll body to the card view-model.
  //
  // `opts.meetingActive` / `opts.ending` are the popup's LOCAL meeting
  // lifecycle, consulted ONLY when the poll itself is non-informative (state
  // "idle" or absent — the Recorder holds no pipeline record yet). That lets
  // the same card surface the two pre-pipeline phases the poll can't express:
  //   - ending:    End was clicked; taps are draining toward the trigger.
  //   - recording: a meeting is active but hasn't been ended.
  // Every informative poll state (running / done / failed) is resolved from
  // the body alone, so those branches are pure functions of the response.
  /**
   * @param {PipelinePoll | null | undefined} [raw]
   * @param {{ meetingActive?: boolean, ending?: boolean }} [opts]
   * @returns {PipelineView}
   */
  function map(raw, opts) {
    const o = opts || {};
    const state = raw && raw.state;

    if (state === "running") {
      return vm("running", {
        progress: progressLabel(raw),
        stage: (raw && raw.stage) || null,
        currentFile: (raw && raw.current_file) || null,
      });
    }
    if (state === "done") {
      const summary = (raw && raw.summary) || null;
      const text = summary && typeof summary.summary === "string" ? summary.summary : "";
      return vm("done", { summary: summary, summaryText: text });
    }
    if (state === "failed") {
      return vm("failed", {
        stage: (raw && raw.stage) || null,
        failureStage: (raw && raw.stage) || null,
        failureReason: failureReason(raw),
      });
    }
    // "idle" / missing / unrecognised: fold in the local lifecycle.
    if (o.ending) return vm("ending");
    if (o.meetingActive) return vm("recording");
    return vm("idle");
  }

  root.TapscribePipelineView = { map, progressLabel, failureReason };
})(typeof globalThis !== "undefined" ? globalThis : typeof self !== "undefined" ? self : this);
