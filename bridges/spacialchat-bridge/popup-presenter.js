// @ts-check
// SpatialChat Bridge — popup-presenter.js (pure meeting presenter)
//
// The popup's bracketed-meeting decision logic, extracted DOM-free so it can
// be unit-tested with plain inputs (no jsdom, no stubs) — the deep-module half
// of the vanilla-web split. The popup's thin DOM shell (popup.js) feeds this
// the current meeting state + the latest pipeline view-model (from
// pipeline-view.js) and applies the returned MeetingView to its components;
// it owns no "what should show" decisions of its own.

/**
 * The card view-model from pipeline-view.js's `map()`. Re-declared as the
 * fields this presenter reads so the presenter stays decoupled from how the
 * poll was mapped.
 * @typedef {{
 *   phase: string,
 *   progress: string | null,
 *   currentFile?: string | null,
 *   summary?: Record<string, unknown> | null,
 *   summaryText?: string | null,
 *   failureStage?: string | null,
 *   failureReason?: string | null,
 * }} PollView
 */

/**
 * The popup's durable meeting state + the latest poll, all the presenter needs.
 * @typedef {{
 *   meetingSessionId: string | null,
 *   meetingActive: boolean,
 *   lastEnd: { phase: string, error?: string | null } | null,
 *   pollView: PollView | null,
 * }} MeetingState
 */

/**
 * @typedef {{
 *   startDisabled: boolean,
 *   endDisabled: boolean,
 *   status: { text: string, tone: "ok" | "err" | "" } | null,
 *   card: {
 *     visible: boolean,
 *     progress: string | null,
 *     failure: string | null,
 *     summary: { text: string, meta: string } | null,
 *     dismissHidden: boolean,
 *   },
 * }} MeetingView
 */

/** Light `model · source` line under a finished summary. @param {Record<string, unknown> | null | undefined} s */
function summaryMeta(s) {
  if (!s) return "";
  const bits = [];
  if (typeof s.model === "string" && s.model) bits.push("model: " + s.model);
  if (typeof s.source === "string" && s.source) bits.push("source: " + s.source);
  return bits.join(" · ");
}

/**
 * The headline status line (the steady-state text above the card). Transient
 * action feedback ("Starting meeting…") is the shell's concern; this is only
 * what's derivable from durable state.
 * @param {MeetingState} st
 * @returns {{ text: string, tone: "ok" | "err" | "" } | null}
 */
function headline(st) {
  if (st.meetingActive && st.meetingSessionId) {
    return { text: "Meeting active — capturing into " + st.meetingSessionId + ".", tone: "ok" };
  }
  const end = st.lastEnd;
  if (!end) return null;
  if (end.phase === "busy") {
    return { text: "Recorder busy — another job is already running on this session.", tone: "err" };
  }
  if (end.phase === "failed") {
    return { text: "End meeting failed: " + (end.error || "unknown error") + ".", tone: "err" };
  }
  if (end.phase === "started") {
    return { text: "Meeting ended — processing started on the recorder.", tone: "ok" };
  }
  if (end.phase === "ending") return { text: "Ending meeting…", tone: "" };
  return null;
}

/**
 * The card region: visible only once the pipeline is in flight or finished
 * (recording / idle is covered by the headline). Progress is a single line
 * the shell updates in place; the summary is rendered once on the transition
 * to done (the shell enforces the render-once guard so a poll tick can't
 * clobber a mid-copy selection).
 * @param {MeetingState} st
 * @returns {MeetingView["card"]}
 */
function cardView(st) {
  const v = st.pollView;
  const phase = v ? v.phase : "idle";
  const visible = phase === "running" || phase === "done" || phase === "failed" || phase === "ending";
  const dismissHidden = st.meetingActive;
  if (!visible || !v) {
    return { visible: false, progress: null, failure: null, summary: null, dismissHidden };
  }
  let progress = null;
  if (phase === "running") {
    progress = (v.progress || "") + (v.currentFile ? " — " + v.currentFile : "");
  } else if (phase === "ending") {
    progress = "Ending meeting — flushing audio, then processing…";
  } else if (phase === "done") {
    progress = "Summary ready.";
  }
  const failure = phase === "failed"
    ? "Failed" + (v.failureStage ? " during " + v.failureStage : "") + ": " + (v.failureReason || "")
    : null;
  const summary = phase === "done"
    ? { text: v.summaryText || "", meta: summaryMeta(v.summary) }
    : null;
  return { visible: true, progress, failure, summary, dismissHidden };
}

/**
 * Derive the whole meeting region's view-model from durable state + the latest
 * poll. Pure — same inputs, same output, no DOM / storage / clock.
 * @param {MeetingState} st
 * @returns {MeetingView}
 */
export function meetingView(st) {
  return {
    startDisabled: st.meetingActive,
    endDisabled: !st.meetingActive,
    status: headline(st),
    card: cardView(st),
  };
}
