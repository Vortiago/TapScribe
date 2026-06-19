// Tests for pipeline-view.js — the pure poll → view-model mapper (module ②).
//
// No mocks: the mapper is a pure function of a raw poll body (+ optional
// local-lifecycle hints), so every test feeds a plain object and asserts the
// returned view-model. The raw shapes mirror what `GET /api/tap/sessions/
// {session}/pipeline` returns for each Recorder state (see app.py
// api_tap_pipeline_poll + batch_pipeline.py for the stage/status vocabulary).

const test = require("node:test");
const assert = require("node:assert/strict");

// pipeline-view.js is an ES module (popup-only); import it directly.
let map;
test.before(async () => {
  ({ map } = await import("../pipeline-view.js"));
});

// ---- running: each of the three stages -----------------------------------

test("running/strip maps to the stripping-silence progress label", () => {
  const view = map({ ok: true, state: "running", stage: "strip", status: "stripping", current: 0, total: 0 });
  assert.equal(view.phase, "running");
  assert.equal(view.progress, "Stripping silence…");
  assert.equal(view.stage, "strip");
  assert.equal(view.summary, null);
  assert.equal(view.failureReason, null);
});

test("running/transcribe maps to a 'Transcribing n/m…' label from current/total", () => {
  const view = map({ ok: true, state: "running", stage: "transcribe", status: "transcribing", current: 3, total: 12 });
  assert.equal(view.phase, "running");
  assert.equal(view.progress, "Transcribing 3/12…");
  assert.equal(view.stage, "transcribe");
});

test("running/transcribe with no total yet falls back to a count-less label", () => {
  const view = map({ ok: true, state: "running", stage: "transcribe", status: "transcribing", current: 0, total: 0 });
  assert.equal(view.progress, "Transcribing…");
});

test("running/transcribe surfaces the current file being transcribed", () => {
  const view = map({
    ok: true, state: "running", stage: "transcribe", status: "transcribing",
    current: 1, total: 4, current_file: "2026-06-19T10-00-01Z__Alice.wav",
  });
  assert.equal(view.currentFile, "2026-06-19T10-00-01Z__Alice.wav");
});

test("running/summarize maps to the summarizing label", () => {
  const view = map({ ok: true, state: "running", stage: "summarize", status: "summarizing", current: 0, total: 1 });
  assert.equal(view.phase, "running");
  assert.equal(view.progress, "Summarizing…");
  assert.equal(view.stage, "summarize");
});

test("running with no stage yet (job snapshot not attached) gets a generic line", () => {
  const view = map({ ok: true, state: "running" });
  assert.equal(view.phase, "running");
  assert.equal(view.progress, "Processing…");
});

// ---- done -----------------------------------------------------------------

test("done exposes the summary text and the raw summary dict for metadata", () => {
  const summary = {
    summary: "We agreed to ship Friday.",
    source: "local",
    model: "qwen3-0.6b",
    took_ms: 1234,
    created_at: "2026-06-19T10-05-00Z",
  };
  const view = map({ ok: true, state: "done", summary });
  assert.equal(view.phase, "done");
  assert.equal(view.summaryText, "We agreed to ship Friday.");
  assert.equal(view.summary.model, "qwen3-0.6b");
  assert.equal(view.summary.source, "local");
  assert.equal(view.progress, null);
});

test("done with a missing summary dict yields empty text, not a crash", () => {
  const view = map({ ok: true, state: "done", summary: null });
  assert.equal(view.phase, "done");
  assert.equal(view.summaryText, "");
  assert.equal(view.summary, null);
});

// ---- failed: one branch per error_kind ------------------------------------

const FAILURE_CASES = [
  ["NoUsableWavs", /no usable audio/i],
  ["NoMergedTranscript", /nothing was transcribed/i],
  ["SummarizerUnavailable", /summarizer isn't configured/i],
  ["SummarizerFailed", /summarizer failed/i],
  ["InvalidRange", /rejected the session's audio range/i],
];

for (const [kind, pattern] of FAILURE_CASES) {
  test("failed/" + kind + " maps to its human-readable reason and the failing stage", () => {
    const view = map({ ok: true, state: "failed", stage: "transcribe", error: "boom", error_kind: kind });
    assert.equal(view.phase, "failed");
    assert.equal(view.failureStage, "transcribe");
    assert.match(view.failureReason, pattern);
  });
}

test("failed with an unknown error_kind falls back to the raw error text", () => {
  const view = map({ ok: true, state: "failed", stage: "summarize", error: "disk full", error_kind: "WeirdNewError" });
  assert.equal(view.phase, "failed");
  assert.equal(view.failureStage, "summarize");
  assert.equal(view.failureReason, "disk full");
});

test("failed with neither a known kind nor an error string gets a generic reason", () => {
  const view = map({ ok: true, state: "failed", stage: "strip" });
  assert.match(view.failureReason, /pipeline failed/i);
});

// ---- idle + the local-lifecycle hints (recording / ending) ----------------

test("idle with no local hints maps to idle", () => {
  const view = map({ ok: true, state: "idle" });
  assert.equal(view.phase, "idle");
  assert.equal(view.progress, null);
});

test("idle while a meeting is active maps to recording", () => {
  const view = map({ ok: true, state: "idle" }, { meetingActive: true });
  assert.equal(view.phase, "recording");
});

test("idle while ending maps to ending (overrides recording)", () => {
  const view = map({ ok: true, state: "idle" }, { meetingActive: true, ending: true });
  assert.equal(view.phase, "ending");
});

test("a missing/garbage body with no hints maps to idle (never throws)", () => {
  assert.equal(map(undefined).phase, "idle");
  assert.equal(map({}).phase, "idle");
  assert.equal(map(null).phase, "idle");
});
