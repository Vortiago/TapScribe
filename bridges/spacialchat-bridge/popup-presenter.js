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
 * `endRequestedAt` mirrors storage's `meetingEndRequestedAt` (the live-tab End
 * nonce); `now` is the shell-supplied clock so this module stays pure.
 * @typedef {{
 *   meetingSessionId: string | null,
 *   meetingActive: boolean,
 *   lastEnd: { phase: string, error?: string | null } | null,
 *   pollView: PollView | null,
 *   endRequestedAt?: number | null,
 *   now?: number,
 * }} MeetingState
 */

/**
 * How long a live-tab End request may sit unacknowledged (`meetingEnd` never
 * written; `startMeeting` nulls it, so null == untouched) before the popup
 * stops trusting the tab (#219). Generous vs the content script's own
 * budgets: an alive one flips `meetingEnd` to "ending" as soon as its
 * storage.onChanged fires — seconds, not tens of seconds — so crossing this
 * means hung/reloading, not slow.
 */
export const END_UNRESPONSIVE_MS = 10_000;

/**
 * The live-tab End-request lifecycle: `pending` while the nonce is out and
 * the content script hasn't acknowledged (no `meetingEnd` yet);
 * `unresponsive` once that pending state has outlived END_UNRESPONSIVE_MS.
 * @param {MeetingState} st
 */
function endRequestState(st) {
  const pending = !!(st.meetingActive && st.endRequestedAt != null && !st.lastEnd);
  const unresponsive =
    pending && (st.now ?? 0) - /** @type {number} */ (st.endRequestedAt) >= END_UNRESPONSIVE_MS;
  return { pending, unresponsive };
}

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
  const end = st.lastEnd;
  // A live-tab End request the content script hasn't acknowledged yet also
  // takes precedence over "active" — and once it has sat unacknowledged past
  // END_UNRESPONSIVE_MS, the operator gets an actionable line instead of a
  // forever-"Ending meeting…" wedge (#219: a live-but-HUNG tab; the
  // stale/absent-tab case never gets here — endMeeting completes it directly).
  const req = endRequestState(st);
  if (req.unresponsive) {
    return {
      text:
        "The SpatialChat tab isn't responding to the End request — reload the tab "
        + "and press End again, or close it and press End to finish without it.",
      tone: "err",
    };
  }
  if (req.pending) return { text: "Ending meeting…", tone: "" };
  // An in-progress End takes precedence over "active": the taps are draining,
  // so showing "Meeting active" would contradict the card's ending line.
  if (end && end.phase === "ending") return { text: "Ending meeting…", tone: "" };
  if (st.meetingActive && st.meetingSessionId) {
    return { text: "Meeting active — capturing into " + st.meetingSessionId + ".", tone: "ok" };
  }
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
 * Whether the card should schedule another poll. Only the in-flight phases
 * (running, ending) warrant a timer; done / failed / idle / recording are
 * steady states the next popup-open re-derives, so polling stops there.
 * @param {string | null | undefined} phase
 * @returns {boolean}
 */
export function shouldKeepPolling(phase) {
  return phase === "running" || phase === "ending";
}

/**
 * Derive the whole meeting region's view-model from durable state + the latest
 * poll. Pure — same inputs, same output, no DOM / storage / clock.
 * @param {MeetingState} st
 * @returns {MeetingView}
 */
export function meetingView(st) {
  const req = endRequestState(st);
  return {
    startDisabled: st.meetingActive,
    // End stays clickable on an active meeting EXCEPT while a request is
    // pending-and-fresh (a re-click would only re-bump the nonce). Once
    // unresponsive it re-enables so the operator can retry: with the tab
    // reloaded the fresh content script picks the new nonce up; with the tab
    // closed the snapshot goes stale and End completes directly (#219).
    endDisabled: !st.meetingActive || (req.pending && !req.unresponsive),
    status: headline(st),
    card: cardView(st),
  };
}
