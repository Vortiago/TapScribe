// Tests for popup-presenter.js — the pure meeting presenter. No DOM, no mocks:
// feed a MeetingState, assert the MeetingView. These cover the lifecycle
// decisions that the old DOM-coupled popup tests asserted through stubs
// (button enable/disable, the headline, card phase/progress/summary/failure),
// now testable directly because the logic is DOM-free.

const test = require("node:test");
const assert = require("node:assert/strict");

// popup-presenter.js is an ES module; load it via dynamic import once.
let meetingView, shouldKeepPolling;
test.before(async () => {
  ({ meetingView, shouldKeepPolling } = await import("../popup-presenter.js"));
});

const base = { meetingSessionId: null, meetingActive: false, lastEnd: null, pollView: null };

// ---- buttons --------------------------------------------------------------

test("no meeting: Start enabled, End disabled, no card", () => {
  const v = meetingView({ ...base });
  assert.equal(v.startDisabled, false);
  assert.equal(v.endDisabled, true);
  assert.equal(v.card.visible, false);
  assert.equal(v.status, null);
});

test("active meeting: Start disabled, End enabled, headline names the Session", () => {
  const v = meetingView({ ...base, meetingSessionId: "sess-1", meetingActive: true });
  assert.equal(v.startDisabled, true);
  assert.equal(v.endDisabled, false);
  assert.match(v.status.text, /Meeting active/);
  assert.match(v.status.text, /sess-1/);
  assert.equal(v.status.tone, "ok");
});

// ---- headline from the end outcome ----------------------------------------

test("a busy end outcome surfaces a Recorder-busy headline", () => {
  const v = meetingView({ ...base, meetingSessionId: "s", lastEnd: { phase: "busy" } });
  assert.match(v.status.text, /busy/i);
  assert.equal(v.status.tone, "err");
});

test("a failed end outcome surfaces the reason", () => {
  const v = meetingView({ ...base, lastEnd: { phase: "failed", error: "no route to host" } });
  assert.match(v.status.text, /End meeting failed/);
  assert.match(v.status.text, /no route to host/);
});

// ---- card: running --------------------------------------------------------

test("running poll shows the card with the stage progress (no summary/failure)", () => {
  const v = meetingView({
    ...base, meetingSessionId: "s",
    pollView: { phase: "running", progress: "Transcribing 3/12…", currentFile: "a.wav" },
  });
  assert.equal(v.card.visible, true);
  assert.match(v.card.progress, /Transcribing 3\/12/);
  assert.match(v.card.progress, /a\.wav/);
  assert.equal(v.card.failure, null);
  assert.equal(v.card.summary, null);
});

test("the card is hidden while merely recording (idle poll, meeting active)", () => {
  const v = meetingView({ ...base, meetingSessionId: "s", meetingActive: true,
    pollView: { phase: "recording", progress: null } });
  assert.equal(v.card.visible, false);
});

// ---- card: done -----------------------------------------------------------

test("done poll exposes the summary text + light metadata and hides Dismiss while active=false", () => {
  const v = meetingView({
    ...base, meetingSessionId: "s",
    pollView: {
      phase: "done", progress: null,
      summaryText: "Ship Friday.", summary: { model: "qwen3-0.6b", source: "local" },
    },
  });
  assert.equal(v.card.visible, true);
  assert.equal(v.card.summary.text, "Ship Friday.");
  assert.match(v.card.summary.meta, /qwen3-0\.6b/);
  assert.match(v.card.summary.meta, /local/);
  assert.equal(v.card.progress, "Summary ready.");
  assert.equal(v.card.dismissHidden, false); // meeting over → Dismiss offered
});

test("Dismiss is hidden while a meeting is still actively recording", () => {
  const v = meetingView({ ...base, meetingActive: true, meetingSessionId: "s",
    pollView: { phase: "running", progress: "Stripping silence…" } });
  assert.equal(v.card.dismissHidden, true);
});

// ---- card: failed ---------------------------------------------------------

test("failed poll surfaces the failing stage + reason", () => {
  const v = meetingView({
    ...base, meetingSessionId: "s",
    pollView: { phase: "failed", progress: null, failureStage: "transcribe", failureReason: "no usable audio" },
  });
  assert.equal(v.card.visible, true);
  assert.match(v.card.failure, /transcribe/);
  assert.match(v.card.failure, /no usable audio/);
  assert.equal(v.card.summary, null);
});

// ---- card: ending ---------------------------------------------------------

test("ending shows a flushing-audio progress line", () => {
  const v = meetingView({
    ...base, meetingSessionId: "s", lastEnd: { phase: "ending" },
    pollView: { phase: "ending", progress: null },
  });
  assert.equal(v.card.visible, true);
  assert.match(v.card.progress, /flushing audio/i);
});

// ---- polling cadence ------------------------------------------------------

test("polling continues only for the in-flight phases", () => {
  assert.equal(shouldKeepPolling("running"), true);
  assert.equal(shouldKeepPolling("ending"), true);
  for (const p of ["done", "failed", "idle", "recording", null, undefined]) {
    assert.equal(shouldKeepPolling(p), false, `stops on ${p}`);
  }
});
